"""Host tmux adapter: registered targets only; no caller-provided commands."""
import hashlib
import json
import os
import threading
import time
from collections import namedtuple
from pathlib import Path
from uuid import uuid4
from bazarlyk.agents import roles
from qorqut_operations.contracts import DomainError
from bazarlyk.transport import terminal_composer

SHELLS = frozenset({'bash','zsh','sh','fish'})
# The fixed launcher of Dev and of a global worker per trusted profile runtime: the normal configured client, no arguments.
LAUNCH_CLIENTS = {'claude-code':'claude', 'codex':'codex'}
CLIENT_PROCESSES = {'codex':frozenset({'codex'}), 'claude':frozenset({'claude','claude.exe'})}
# A profile's runtime names the client its terminal must be running for owner input.
RUNTIME_CLIENTS = {'claude-code':'claude', 'codex':'codex'}
CLIENT_NAMES = {'claude':'Claude Code', 'codex':'Codex'}
NATIVE_ONLY = 'Terminal control currently requires native host execution'
MAX_OWNER_INPUT_CHARS = 17000
# ``receipt`` names what was observed once input was attempted (client, a hash of the terminal identity, the composer
# fingerprint); it never carries screen text or the typed text.
OwnerInputOutcome = namedtuple('OwnerInputOutcome', 'status reason receipt', defaults=(None,))


def _launch_client(row, role_id):
    """The launcher from a trusted profile runtime when the profile holds ``role_id``; None for every other role."""
    value = row.get('profile_json') if isinstance(row, dict) else None
    data = json.loads(value) if isinstance(value, str) else value
    if not isinstance(data, dict) or (data.get('role') or {}).get('id') != role_id:
        return None
    client = LAUNCH_CLIENTS.get(data.get('runtime'))
    if client is None:
        raise DomainError('unsupported_runtime', 'Terminal control supports Claude Code and Codex only')
    return client


def dev_client(row):
    """Dev's launcher from its trusted profile runtime; None for every other role."""
    return _launch_client(row, roles.DEV_ROLE)


def worker_client(row):
    """A global worker's launcher from its trusted profile runtime; None for every other role."""
    return _launch_client(row, roles.GLOBAL_WORKER_ROLE)


def runtime_client(row):
    """The client (``codex`` or ``claude``) a registered profile's runtime runs; None when it is not one of those."""
    value = row.get('profile_json') if isinstance(row, dict) else None
    data = json.loads(value) if isinstance(value, str) else value
    return RUNTIME_CLIENTS.get(data.get('runtime')) if isinstance(data, dict) else None


class OwnerInputDeferred(Exception):
    """The terminal is not verifiably ready; nothing was typed. ``reason`` is a stable code for status."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class AgentTerminalBackend:
    def __init__(self, terminals, *, timeout=30, sleep=time.sleep, screen_settle=.15, paste_settle=.3, submit_settle=.4):
        self.terminals, self.runtime = terminals, terminals.runtime
        self.timeout, self.sleep = timeout, sleep
        self.screen_settle, self.paste_settle, self.submit_settle = screen_settle, paste_settle, submit_settle
        self._owner_locks = {}

    def selected(self, row):
        with self.runtime.database.read() as uow:
            business = uow.repository('businesses').get(row['home_business_id']) if row['home_business_id'] else None
        execution = self.terminals.execution(business)
        if execution.target_identity is not None or execution.broker is not self.terminals.broker:
            raise DomainError('operation_unavailable', NATIVE_ONLY)
        broker = execution.broker
        if broker is None or not broker.tmux_available():
            raise DomainError('operation_unavailable', 'Host tmux is unavailable')
        key, session, cwd = self.terminals.binding(business, row, broker)
        return broker, key, session, cwd, business

    def snapshot(self, row, *, client=None):
        """The registered terminal's observation. ``client`` (owner input) names the client to verify for any role."""
        selected = client or dev_client(row) or worker_client(row)
        client = selected or 'codex'
        broker, _, session, _, _ = self.selected(row)
        if not broker.session_alive(session): return {'state':'absent', **({'client':selected} if selected else {})}
        code, output = broker.tmux('list-panes', '-t', '='+session, '-F', '#{pid}|#{session_id}|#{pane_id}|#{pane_pid}|#{pane_current_command}|#{pane_tty}')
        lines = output.strip().splitlines()
        if code or len(lines)!=1:
            raise DomainError('operation_unavailable', 'Expected exactly one registered terminal pane')
        fields = lines[0].split('|')
        if len(fields)!=6: raise DomainError('operation_unavailable', 'Invalid terminal observation')
        try:
            # tcgetpgrp on another session's tty can fail with ENOTTY. Linux
            # exposes the pane shell's foreground process group in /proc instead.
            stat = Path(f'/proc/{fields[3]}/stat').read_text()
            foreground = int(stat[stat.rfind(')')+2:].split()[5])
            if foreground <= 0:
                raise ValueError('No foreground process')
            stat = Path(f'/proc/{foreground}/stat').read_text()
            started = stat[stat.rfind(')')+2:].split()[19]
            command, verified = fields[4], False
            # npm's launcher is node; verify a real client process in its group.
            # Native Claude (claude/claude.exe) is recognized only for Dev.
            for process in Path('/proc').iterdir():
                if not process.name.isdigit():
                    continue
                try:
                    stat = (process / 'stat').read_text()
                    group = int(stat[stat.rfind(')')+2:].split()[2])
                    if group != foreground or (process / 'comm').read_text().strip() not in CLIENT_PROCESSES[client]:
                        continue
                    args = (process / 'cmdline').read_bytes().split(b'\0')
                    if b'--remote' in args or b'app-server' in args:
                        raise DomainError('operation_unavailable', 'Remote app-server clients require a separate lifecycle adapter')
                    command, verified = client, True
                    break
                except (OSError, ValueError, IndexError):
                    continue
        except (OSError, IndexError, ValueError) as exc:
            raise DomainError('operation_unavailable', 'Cannot identify the foreground terminal process') from exc
        if selected and command not in SHELLS and not verified:
            # An unverified foreground program is never reported as the selected client.
            command = 'unrecognized'
        return {'foreground_pid':foreground, 'foreground_started':started, 'state':'present', 'server_pid':fields[0], 'session_id':fields[1], 'pane_id':fields[2], 'pane_pid':fields[3], 'command':command,
                **({'client':selected} if selected else {})}

    @staticmethod
    def validate(action, snapshot):
        if action == 'start':
            if snapshot['state']!='absent' and snapshot.get('command') not in SHELLS:
                raise DomainError('invalid_transition', 'Start requires an absent terminal or idle shell')
        elif snapshot['state']!='present':
            raise DomainError('invalid_transition', 'Action requires an existing terminal')
        # A Dev binding names its runtime's client; every other target keeps Codex.
        if action=='ping' and snapshot.get('command')!=snapshot.get('client', 'codex'):
            raise DomainError('invalid_transition', 'Direct ping requires the agent\'s verified foreground client')

    def wait_ready(self, row, client='codex'):
        broker, _, session, _, _ = self.selected(row)
        until = time.monotonic()+self.timeout
        while time.monotonic()<until:
            snap = self.snapshot(row)
            code, screen = broker.tmux('capture-pane','-p','-t',snap['pane_id'],'-S','-30')
            if not code and ('Blocked by hook' in screen or 'reprepare_required' in screen):
                raise DomainError('reprepare_required', 'Agent preparation failed')
            if not code and snap.get('command')==client:
                # Only a verified idle composer: never type into trust/login/error prompts.
                if client=='claude':
                    try:
                        self._ready(row, client)
                        return
                    except OwnerInputDeferred:
                        pass
                elif any(line.lstrip().startswith('›') for line in screen.splitlines()):
                    return
            self.sleep(.2)
        raise DomainError('operation_unavailable', f"{CLIENT_NAMES[client]} did not reach its input prompt; inspect terminal")

    def execute(self, row, action, message, expected, input_key=None):
        if self.snapshot(row)!=expected:
            raise DomainError('revision_conflict', 'Terminal changed before dispatch')
        self.validate(action, expected)
        dev = dev_client(row)
        launcher = dev or worker_client(row)
        if launcher and expected.get('client') != launcher:
            raise DomainError('revision_conflict', 'Agent runtime changed before dispatch')
        broker, key, session, _, business = self.selected(row)
        if action=='ping':
            return self._ping(row, message, input_key or f"terminal-action:{uuid4()}", launcher)
        if action in {'kill','restart'}:
            # Target the observed tmux ID, never a name that may now refer to a new session.
            code, _ = broker.tmux('kill-session', '-t', expected['session_id'])
            if code: raise DomainError('operation_failed', 'Terminal termination could not be confirmed')
            broker.stop(key)
            if action=='kill': return 'Bound terminal terminated'
        # Authorization has already been persisted and consumed by the controller.
        # Reconstitute owner context solely for the existing trusted preparation path.
        with self.runtime.database.read() as uow:
            owner = uow.repository('installation').get('service')['owner_principal_id']
            context = self.runtime.policy.context_for(owner, business_id=row['home_business_id'], review_business_id=None, uow=uow)
        self.terminals.open(context, business, row['agent_id'])
        snap = self.snapshot(row)
        if snap.get('command') not in SHELLS:
            raise DomainError('invalid_transition', 'Terminal is not an idle shell')
        # Clear any partially typed shell command before entering the fixed launcher.
        code, _ = broker.tmux('send-keys','-t',snap['pane_id'],'C-u')
        if code: raise DomainError('operation_failed', 'Could not prepare shell input')
        if dev:
            # Dev is owner-project-led: no role, work prompt or readiness scraping follows.
            broker.submit_input(session, dev)
            return 'Startup command submitted; readiness/work is not confirmed'
        # A worker runs its profile's client; every other target keeps Codex.
        client = launcher or 'codex'
        name, role_command = CLIENT_NAMES[client], '/role' if client=='claude' else '$role'
        broker.submit_input(session, client)
        self.wait_ready(row, client)
        broker.submit_input(session, role_command)
        self.sleep(.2)
        snap = self.snapshot(row)
        if snap.get('command') != client:
            raise DomainError('operation_failed', f'{name} exited during role submission')
        if client=='codex':
            # Codex shows a skill picker for $role; one more Enter selects it.
            code, _ = broker.tmux('send-keys', '-t', snap['pane_id'], 'Enter')
            if code: raise DomainError('operation_failed', 'Role submission could not be confirmed')
        return f'{name} started and {role_command} submitted; role work completion is not implied'

    # ----- owner input --------------------------------------------------------------------------------------
    # Typed into the exact registered client only when its screen positively shows an idle, empty composer. This is a
    # separate, narrower path than ``execute``: it never starts, restarts or kills a terminal, never invokes a launcher,
    # never types into a shell, never clears text, and never presses anything but the submit key after its own paste.
    # A human typing into the same terminal between the last check and the paste cannot be excluded; the paste is
    # guarded by three observations of an empty composer on the same client and re-verified before submit, and any doubt
    # after the first input is reported as uncertain and never replayed. By default a running turn means "not ready"
    # (Telegram and every other caller). ``allow_busy`` is the explicit opt-in used by Terminal Chat alone: a turn that is
    # still running is then fine as long as every observation, the paste and the submit show the ordinary editable empty
    # composer of the same client, so the native client queues or steers the message exactly as if the owner had typed it.

    @staticmethod
    def validate_owner_text(text):
        if type(text) is not str or not text.strip() or len(text) > MAX_OWNER_INPUT_CHARS:
            raise DomainError('validation_error', 'Owner input needs 1-17000 characters')
        for char in text:
            code = ord(char)
            if char not in '\n\t' and (code < 0x20 or code == 0x7f or 0x80 <= code <= 0x9f):
                raise DomainError('validation_error', 'Owner input cannot contain terminal control characters')

    def _observe(self, row, client, expect=None, allow_busy=False):
        """One verified observation ``(broker, snapshot, reading)`` or ``OwnerInputDeferred``. Nothing here has an effect.

        ``expect`` is the text just pasted: the reading is then ``pasted`` only if that exact text occupies the composer.
        ``allow_busy`` lets a running turn coexist with an otherwise valid composer (see ``terminal_composer.read``).
        """
        try:
            broker, _, session, _, _ = self.selected(row)
        except DomainError as exc:
            raise OwnerInputDeferred('terminal_unsupported' if exc.message == NATIVE_ONLY else 'terminal_unavailable') from None
        try:
            snap = self.snapshot(row, client=client)
        except DomainError:
            raise OwnerInputDeferred('terminal_unverified') from None
        if snap['state'] == 'absent':
            raise OwnerInputDeferred('terminal_absent')
        if snap.get('command') != client:
            raise OwnerInputDeferred('terminal_unverified')
        pane = snap['pane_id']
        code, meta = broker.tmux('display-message', '-p', '-t', pane, '#{pane_in_mode}|#{pane_dead}|#{cursor_flag}|#{cursor_x}|#{cursor_y}')
        fields = meta.strip().split('|')
        if code or len(fields) != 5 or not all(item.isdigit() for item in fields):
            raise OwnerInputDeferred('terminal_unreadable')
        if fields[0] != '0' or fields[1] != '0':
            raise OwnerInputDeferred('terminal_in_mode')
        code, screen = broker.tmux('capture-pane', '-p', '-e', '-t', pane)
        if code:
            raise OwnerInputDeferred('terminal_unreadable')
        reading = terminal_composer.read(client, screen, (int(fields[3]), int(fields[4]), fields[2] == '1'), expect=expect,
                                         allow_busy=allow_busy)
        return broker, snap, reading

    def _ready(self, row, client, allow_busy=False):
        broker, snap, reading = self._observe(row, client, allow_busy=allow_busy)
        if not reading.ready:
            if reading.reason == 'enter_interrupts':
                raise OwnerInputDeferred('submit_interrupts')
            raise OwnerInputDeferred({'text': 'composer_not_empty', 'busy': 'composer_busy', 'modal': 'composer_modal'}
                                     .get(reading.state, 'composer_unrecognized'))
        return broker, snap, reading

    def owner_input(self, row, text, *, workdir, checkpoint, require_paste_echo=True, allow_busy=False):
        """Type ``text`` into the target's empty composer and submit it. Returns an ``OwnerInputOutcome``.

        ``deferred``: nothing was typed (the terminal is not ready); ``submitted``: the submit key was sent, processing
        is not confirmed; ``uncertain``: input may have reached the terminal and must never be replayed. ``checkpoint``
        is called once after the pre-input checks and before the first input, and one more observation follows it; if
        either fails nothing is typed. ``allow_busy`` (default off) accepts a verified empty composer while the native
        turn is still running; the same checks, paste and single normal submit apply and nothing else is ever pressed.
        ``workdir`` is a private (0700) directory for the short-lived paste file, which never carries the text in argv.
        """
        self.validate_owner_text(text)
        client = runtime_client(row)
        if client is None:
            return OwnerInputOutcome('failed', 'unsupported_runtime')
        lock = self._owner_locks.setdefault(row['agent_id'], threading.Lock())
        if not lock.acquire(blocking=False):
            return OwnerInputOutcome('deferred', 'target_busy')
        try:
            return self._deliver(row, client, text, workdir, checkpoint, require_paste_echo, allow_busy)
        finally:
            lock.release()

    def _ping(self, row, message, key, client):
        """A direct message into the exact registered native session, through the shared input arbiter. Nothing is typed unless
        that session is verifiably there; an input that was definitely not typed is reported as such, never as uncertain."""
        from bazarlyk.native_chat.contracts import InputRequest, NativeChatError, STATE_SUBMITTED, STATE_UNCERTAIN, TYPE_DIRECT
        try:
            result = self.runtime.native_chat.arbiter.submit(InputRequest(row['agent_id'], TYPE_DIRECT, key, message))
        except NativeChatError as error:
            raise DomainError('not_delivered', error.message) from None
        if result.state == STATE_SUBMITTED:
            return f"Direct message submitted to {CLIENT_NAMES[client or 'codex']}; processing is not confirmed"
        if result.state == STATE_UNCERTAIN:
            raise DomainError('operation_failed', 'Direct message outcome could not be confirmed')
        raise DomainError('not_delivered', f"Direct message was not typed: {result.reason or 'the native session is not ready'}")

    def _deliver(self, row, client, text, workdir, checkpoint, require_paste_echo=True, allow_busy=False):
        # Each of the three observations must independently show the ordinary empty composer on the exact same client
        # snapshot. The rest of the screen (a transcript or status timer, the footer, colours) is free to change, and with
        # ``allow_busy`` so is whether a turn is running: busy to idle between the checks is fine.
        try:
            broker, snap, first = self._ready(row, client, allow_busy)
            self.sleep(self.screen_settle)
            _, again, _ = self._ready(row, client, allow_busy)
        except OwnerInputDeferred as exc:
            return OwnerInputOutcome('deferred', exc.reason)
        if again != snap:
            return OwnerInputOutcome('deferred', 'screen_changing')
        try:
            checkpoint()
        except Exception:
            return OwnerInputOutcome('deferred', 'checkpoint_failed')
        # The checkpoint is a database write that can wait: look at the composer once more right before the first input.
        try:
            _, last, _ = self._ready(row, client, allow_busy)
        except OwnerInputDeferred as exc:
            return OwnerInputOutcome('deferred', exc.reason)
        if last != snap:
            return OwnerInputOutcome('deferred', 'screen_changing')
        receipt = {'client': client, 'composer': first.fingerprint,
                   'target': hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()}
        return self._type(row, client, broker, snap, first, text, workdir, require_paste_echo, allow_busy)._replace(receipt=receipt)

    def _type(self, row, client, broker, snap, first, text, workdir, require_paste_echo=True, allow_busy=False):
        """Everything from the first input on. Any outcome here may follow a write, so it is never replayed."""
        pane = snap['pane_id']
        paste = self._paste(broker, pane, text, workdir)
        if paste is not None:
            return paste
        self.sleep(self.paste_settle)
        # Chat owns this successful paste into a verified empty composer. Long
        # text may be clipped or collapsed by the CLI; visible echo is optional.
        # Identity/mode checks still run before sending exactly one Enter.
        try:
            _, seen, pasted = self._observe(row, client, expect=text, allow_busy=allow_busy)
        except OwnerInputDeferred:
            return OwnerInputOutcome('uncertain', 'paste_unverified')
        # The exact pasted text (or the client's placeholder for it) occupies the composer, on the same unchanged client, with
        # no running turn unless ``allow_busy`` (a running turn is then expected, and is not by itself a doubt). Wording on
        # screen is never read as a dialog here: a dialog would not show this text as the composer.
        if seen != snap or pasted.reason == 'enter_interrupts' \
                or (require_paste_echo and (pasted.state != 'pasted' or pasted.fingerprint == first.fingerprint)):
            return OwnerInputOutcome('uncertain', 'paste_unverified')
        code, _ = broker.tmux('send-keys', '-t', pane, 'Enter')
        if code:
            return OwnerInputOutcome('uncertain', 'submit_failed')
        self.sleep(self.submit_settle)
        try:
            _, _, after = self._observe(row, client, expect=text, allow_busy=allow_busy)
        except OwnerInputDeferred:
            return OwnerInputOutcome('submitted', 'processing_not_confirmed')
        # The text still in the composer means the submit did not take.
        if after.state == 'pasted':
            return OwnerInputOutcome('uncertain', 'submit_unconfirmed')
        if after.state == 'text' and after.reason == 'composer_not_empty':
            return OwnerInputOutcome('uncertain', 'submit_unconfirmed')
        return OwnerInputOutcome('submitted', 'processing_not_confirmed')

    def _paste(self, broker, pane, text, workdir):
        """Bracketed paste through a private file. Returns None on success, else the outcome to report."""
        path = Path(workdir) / ('paste-' + uuid4().hex + '.paste')
        buffer = 'qorqut-owner-' + uuid4().hex
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.write(descriptor, text.encode('utf-8'))
            finally:
                os.close(descriptor)
            code, _ = broker.tmux('load-buffer', '-b', buffer, str(path))
            if code:
                return OwnerInputOutcome('deferred', 'paste_failed')
            code, _ = broker.tmux('paste-buffer', '-d', '-r', '-p', '-b', buffer, '-t', pane)
            if code:
                return OwnerInputOutcome('uncertain', 'paste_failed')
            return None
        except OSError:
            return OwnerInputOutcome('deferred', 'paste_failed')
        finally:
            try:
                size = path.stat().st_size
                with open(path, 'r+b') as handle:
                    handle.write(b'\0' * size)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError:
                pass
            path.unlink(missing_ok=True)
            broker.tmux('delete-buffer', '-b', buffer)
