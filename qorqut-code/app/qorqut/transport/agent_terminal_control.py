"""Host tmux adapter: registered targets only; no caller-provided commands."""
import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path
from uuid import uuid4
from qorqut.agents import roles
from qorqut_operations.contracts import DomainError
from qorqut.transport import terminal_composer

SHELLS = frozenset({'bash','zsh','sh','fish'})
# The fixed launcher of Dev and of a global worker per trusted profile runtime: the normal configured client, no arguments.
LAUNCH_CLIENTS = {'claude-code':'claude', 'codex':'codex'}
CLIENT_PROCESSES = {'codex':frozenset({'codex'}), 'claude':frozenset({'claude','claude.exe'})}
# A profile's runtime names the client its terminal must be running for owner input.
RUNTIME_CLIENTS = {'claude-code':'claude', 'codex':'codex'}
CLIENT_NAMES = {'claude':'Claude Code', 'codex':'Codex'}
NATIVE_ONLY = 'Terminal control currently requires native host execution'
MAX_OWNER_INPUT_CHARS = 17000
# A busy CLI can take several frames to display a bracketed paste. Re-read only:
# never paste twice or submit without the existing checks.
PASTE_VERIFY_ATTEMPTS = 10
PASTE_VERIFY_INTERVAL = .2
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


def agent_client(row):
    """The client a registered agent's trusted profile runtime runs, for every role: Dev and a global worker per their own
    launcher rules, every other agent (Ordubashy and the rest) by its profile runtime. None when the profile names no supported
    runtime; the caller refuses then, and never assumes Codex."""
    return dev_client(row) or worker_client(row) or runtime_client(row)


class OwnerInputDeferred(Exception):
    """The terminal is not verifiably ready; nothing was typed. ``reason`` is a stable code for status."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class AgentTerminalBackend:
    def __init__(self, terminals, *, timeout=30, sleep=time.sleep, screen_settle=.15, paste_settle=.3, submit_settle=1.0):
        self.terminals, self.runtime = terminals, terminals.runtime
        self.timeout, self.sleep = timeout, sleep
        self.screen_settle, self.paste_settle, self.submit_settle = screen_settle, paste_settle, submit_settle
        self._owner_locks = {}

    def selected_client(self, row):
        """The client of the agent's selected conversation, which may differ from the profile runtime after a terminal
        switch between Claude and Codex; None when nothing is selected."""
        from qorqut.native_chat import selection
        with self.runtime.database.read() as uow:
            chosen = selection.selected(uow.connection, row['agent_id'])
        return chosen.provider if chosen is not None and chosen.provider in ('claude', 'codex') else None

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
        selected = client or agent_client(row)
        client = selected or 'codex'
        native = getattr(self.runtime, 'native_chat', None)
        revision = native.attachment.revision(row['agent_id']) if native is not None else None
        broker, _, session, _, _ = self.selected(row)
        if not broker.session_alive(session):
            return {'state':'absent', 'selection_revision':revision, **({'client':selected} if selected else {})}
        code, output = broker.tmux('list-panes', '-t', '='+session, '-F', '#{pid}|#{session_id}|#{pane_id}|#{pane_pid}|#{pane_current_command}|#{pane_tty}')
        lines = output.strip().splitlines()
        if code or len(lines)!=1:
            raise DomainError('operation_unavailable', 'Expected exactly one registered terminal pane')
        fields = lines[0].split('|')
        if len(fields)!=6: raise DomainError('operation_unavailable', 'Invalid terminal observation')
        try:
            # tcgetpgrp on another session's tty can fail with ENOTTY. Linux
            # exposes the pane shell's foreground process group in /proc instead.
            if sys.platform == 'darwin':
                from qorqut import process_info
                foreground = int(process_info.fields(int(fields[3]))[5])
            else:
                stat = Path(f'/proc/{fields[3]}/stat').read_text()
                foreground = int(stat[stat.rfind(')')+2:].split()[5])
            if foreground <= 0:
                raise ValueError('No foreground process')
            if sys.platform == 'darwin':
                started = process_info.start_token(foreground)
            else:
                stat = Path(f'/proc/{foreground}/stat').read_text()
                started = stat[stat.rfind(')')+2:].split()[19]
            command, verified = fields[4], False
            # npm's launcher is node; verify a real client process in its group.
            # Native Claude (claude/claude.exe) is recognized only for Dev.
            processes = (Path(str(pid)) for pid in process_info.pids()) if sys.platform == 'darwin' else Path('/proc').iterdir()
            for process in processes:
                if not process.name.isdigit():
                    continue
                try:
                    if sys.platform == 'darwin':
                        group = os.getpgid(int(process.name))
                        if group != foreground:
                            continue
                        name = process_info.name(int(process.name))
                        # Only the client's own arguments are read: a CLI's short-lived helpers (MCP servers started
                        # through npx, for example) share its group, and macOS refuses their arguments while they exec.
                        if name not in CLIENT_PROCESSES[client]:
                            continue
                        args = [arg.encode() for arg in process_info.cmdline(int(process.name))]
                    else:
                        stat = (process / 'stat').read_text()
                        group = int(stat[stat.rfind(')')+2:].split()[2])
                        name = (process / 'comm').read_text().strip()
                        args = (process / 'cmdline').read_bytes().split(b'\0')
                    if group != foreground or name not in CLIENT_PROCESSES[client]:
                        continue
                    # Lifecycle control owns the viewer pane, never the shared server.
                    # Current-conversation proof for raw input lives in AttachmentService.
                    if b'app-server' in args:
                        raise DomainError('operation_unavailable', 'A shared app-server is not a terminal viewer')
                    command, verified = client, True
                    break
                except PermissionError:
                    if sys.platform == 'darwin':
                        raise
                except (OSError, ValueError, IndexError):
                    continue
        except (OSError, IndexError, ValueError) as exc:
            raise DomainError('operation_unavailable', 'Cannot identify the foreground terminal process') from exc
        if selected and command not in SHELLS and not verified:
            # An unverified foreground program is never reported as the selected client.
            command = 'unrecognized'
        return {'foreground_pid':foreground, 'foreground_started':started, 'state':'present', 'server_pid':fields[0], 'session_id':fields[1], 'pane_id':fields[2], 'pane_pid':fields[3], 'command':command,
                'selection_revision':revision, **({'client':selected} if selected else {})}

    def pane_observation(self, row, client):
        """A read-only account of the registered pane for the selected-conversation attachment, by process identity and never by
        a label: ``absent``, or the one pane with ``kind`` =

        * ``shell``  the pane's registered shell (tmux's pane process, a known interactive shell) is itself the terminal's
                     foreground process group and the only process in it: an idle prompt. A nested shell, a suspended or
                     background job's group, or any other foreground is not this.
        * ``client`` exactly one process of ``client`` is in the foreground process group, descends from the pane shell and
                     shares its session and controlling terminal. ``client`` carries its pid, start ticks and argv.
        * ``other``  anything else, including an ambiguous foreground.

        Unlike ``snapshot`` it also describes a Codex client attached to a shared server (``--remote``), which control and input
        never act on. Nothing is typed or changed; an observation that cannot be made raises, and a caller fails closed."""
        broker, _, session, _, _ = self.selected(row)
        if not broker.session_alive(session):
            return {'state': 'absent'}
        code, output = broker.tmux('list-panes', '-t', '='+session, '-F', '#{session_id}|#{pane_id}|#{pane_pid}|#{pane_current_command}|#{pane_title}')
        lines = output.strip().splitlines()
        if code or len(lines) != 1:
            raise DomainError('operation_unavailable', 'Expected exactly one registered terminal pane')
        fields = lines[0].split('|', 4)
        if len(fields) != 5 or not fields[2].isdigit():
            raise DomainError('operation_unavailable', 'Invalid terminal observation')
        session_id, pane_id, command = fields[0], fields[1], fields[3]
        shell = int(fields[2])
        try:
            shell_fields = self._stat(shell)
            if sys.platform == 'darwin':
                from qorqut import process_info
                shell_comm = process_info.name(shell)
            else:
                shell_comm = (Path(f'/proc/{shell}/comm')).read_text().strip()
            foreground = int(shell_fields[5])
            if foreground <= 0:
                raise ValueError('No foreground process')
            members = {}
            processes = (Path(str(pid)) for pid in process_info.pids()) if sys.platform == 'darwin' else Path('/proc').iterdir()
            for process in processes:
                if not process.name.isdigit():
                    continue
                try:
                    if sys.platform == 'darwin' and os.getpgid(int(process.name)) != foreground:
                        continue
                    parts = self._stat(int(process.name))
                    if int(parts[2]) == foreground:
                        members[int(process.name)] = parts
                except PermissionError:
                    if sys.platform == 'darwin':
                        raise
                except (OSError, ValueError, IndexError):
                    continue
            kind, found = 'other', None
            if foreground == shell:
                if shell_comm in SHELLS and int(shell_fields[2]) == shell and set(members) == {shell}:
                    kind = 'shell'
            else:
                matches = []
                for pid, parts in sorted(members.items()):
                    if parts[3] != shell_fields[3] or parts[4] != shell_fields[4]:
                        continue
                    name = process_info.name(pid) if sys.platform == 'darwin' else (Path(f'/proc/{pid}/comm')).read_text().strip()
                    if name not in CLIENT_PROCESSES[client]:
                        continue
                    if not self._descends(pid, shell):
                        continue
                    argv = (process_info.cmdline(pid) if sys.platform == 'darwin' else
                            [item.decode('utf-8', 'replace') for item in (Path(f'/proc/{pid}/cmdline')).read_bytes().split(b'\0') if item])
                    matches.append({'pid': pid, 'started': parts[19], 'argv': argv})
                if len(matches) == 1:
                    kind, found = 'client', matches[0]
        except (OSError, IndexError, ValueError) as exc:
            raise DomainError('operation_unavailable', 'Cannot identify the foreground terminal process') from exc
        return {'state': 'present', 'session_id': session_id, 'pane_id': pane_id, 'pane_pid': shell, 'shell_started': shell_fields[19],
                'command': command, 'kind': kind, 'client': found, 'title': fields[4]}

    @staticmethod
    def _stat(pid):
        """The fields of ``/proc/<pid>/stat`` after the command name: [0] state, [1] ppid, [2] pgrp, [3] session, [4] tty,
        [5] foreground group of that tty, [19] start ticks."""
        if sys.platform == 'darwin':
            from qorqut.process_info import fields
            return fields(pid)
        text = Path(f'/proc/{pid}/stat').read_text()
        return text[text.rfind(')')+2:].split()

    def _descends(self, pid, ancestor):
        """Whether ``ancestor`` is on ``pid``'s parent chain (the exact pane ancestry), within a bounded depth."""
        for _ in range(24):
            if pid == ancestor:
                return True
            if sys.platform == 'darwin':
                from qorqut.process_info import parent_pid
                pid = parent_pid(pid)
            else:
                pid = int(self._stat(pid)[1])
            if pid <= 1:
                return False
        return False

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

    # The terminal's foreground shell is what receives the launcher and what carries Qorqbot's claude and codex wrappers, which
    # apply only in the agent's own workspace root (bootstrap/preparation.py). Start never moves it there or types a cd.
    @staticmethod
    def _physical(path):
        return os.path.realpath(os.fspath(path), strict=True)

    def _require_agent_folder(self, snap, workspace):
        """Refuse unless the observed foreground shell is, physically, in the prepared agent workspace root. Both folders are
        resolved through symlinks, and anything that cannot be established (the process, its folder, the workspace) refuses
        too. Nothing is changed or typed."""
        from qorqut import process_info
        pid = snap.get('foreground_pid')
        try:
            if type(pid) is not int or pid <= 0 or workspace is None:
                raise ValueError('No foreground shell to inspect')
            here, root = self._physical(process_info.cwd(pid)), self._physical(workspace)
        except (OSError, ValueError) as exc:
            raise DomainError('operation_unavailable', "Cannot verify which folder the agent's terminal is in, so nothing was "
                              "started. Return the agent terminal to its own folder, then press Start again.") from exc
        if here != root:
            raise DomainError('invalid_transition', f"The agent's terminal is not in the agent's own folder ({root}). Return it "
                              "to that folder before pressing Start; Qorqbot does not change folders or type commands for you.")

    def _require_same_shell_in_folder(self, row, observed, workspace):
        """Observe the pane again, just before anything is typed: the same pane and the same idle foreground shell process as
        ``observed``, still in the agent's own folder. A different process, or one that cannot be read, refuses."""
        now = self.snapshot(row)
        identity = ('state', 'server_pid', 'session_id', 'pane_id', 'pane_pid', 'foreground_pid', 'foreground_started')
        if now.get('command') not in SHELLS or any(now.get(key) != observed.get(key) for key in identity):
            raise DomainError('revision_conflict', 'Terminal changed before dispatch')
        self._require_agent_folder(now, workspace)

    def opened_snapshot(self, row, attempts=50):
        """The pane just opened for a start. A new pane's shell takes a moment (the owner's zsh setup, a prompt theme) before
        it has a foreground process; until then the pane cannot be read, so it is read again for up to about ten seconds.
        Anything else that stops the observation still stops the start."""
        for attempt in range(attempts):
            try:
                return self.snapshot(row)
            except DomainError as exc:
                if (exc.code != 'operation_unavailable' or not isinstance(exc.__cause__, (OSError, ValueError))
                        or attempt == attempts - 1):
                    raise
            self.sleep(.2)
        raise DomainError('operation_unavailable', 'Cannot identify the foreground terminal process')

    def wait_ready(self, row, client='codex'):
        broker, _, session, _, _ = self.selected(row)
        until = time.monotonic()+self.timeout
        while time.monotonic()<until:
            try:
                snap = self.snapshot(row)
            except DomainError as exc:
                # A process changing while it is observed is retried; the observation itself never acts.
                if exc.code != 'operation_unavailable' or not isinstance(exc.__cause__, OSError):
                    raise
                self.sleep(.2)
                continue
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
        """One owner-authorized terminal action. It runs under the agent's one lock (shared with Chat sends, selection,
        reconnect and raw terminal input), so none of them can land between the checks below and the change to the terminal."""
        from contextlib import nullcontext
        native = getattr(self.runtime, 'native_chat', None)
        with native.arbiter.locks(row['agent_id']) if native is not None else nullcontext():
            return self._execute(row, action, message, expected, input_key, native)

    def _execute(self, row, action, message, expected, input_key, native):
        if self.snapshot(row)!=expected:
            raise DomainError('revision_conflict', 'Terminal changed before dispatch')
        self.validate(action, expected)
        dev = dev_client(row)
        launcher = agent_client(row)
        if launcher and expected.get('client') != launcher:
            raise DomainError('revision_conflict', 'Agent runtime changed before dispatch')
        # The selection this action was authorized against, read before the old pane is touched; start/restart may resume only
        # exactly this one, never whichever selection exists after the old terminal is gone.
        authorized = expected.get('selection_revision')
        broker, key, session, workspace, business = self.selected(row)
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
        snap = self.opened_snapshot(row)
        if snap.get('command') not in SHELLS:
            raise DomainError('invalid_transition', 'Terminal is not an idle shell')
        # Every agent runs the client its trusted profile runtime names; there is no Codex default, and nothing is typed for one
        # whose runtime is not a supported client.
        if launcher is None:
            raise DomainError('unsupported_runtime', 'Terminal control supports Claude Code and Codex agents only')
        # An agent's launcher is typed into its shell, whose wrappers belong to the agent's own folder: a terminal the owner
        # has moved elsewhere is refused before any trust is recorded or anything is typed. Dev's owner-project launch is
        # exempt.
        if not dev:
            self._require_agent_folder(snap, workspace)
        # With the owner's consent, the CLI's folder-trust prompt for the workspace is answered in its own file before launch,
        # so the readiness wait below reaches the composer instead of stopping at a prompt it must never type into. A prompt
        # about an enclosing repository is the owner's to answer in the terminal. No consent: unchanged.
        try:
            from qorqut import agent_trust
            agent_trust.ensure(self.runtime.paths.runtime, workspace)
        except (OSError, DomainError):
            logging.getLogger(__name__).warning('Agent folder trust could not be recorded', exc_info=True)
        # The observed shell and its folder are checked once more at the last moment before the resume or the Ctrl-U. (A person
        # typing in the same tmux pane outside Qorqbot's lock cannot be excluded.)
        if not dev:
            self._require_same_shell_in_folder(row, snap, workspace)
        # An agent with a selected native conversation is resumed as exactly that conversation, with no role bootstrap; a
        # launch that cannot be proved safe is refused, never replaced by a fresh conversation. That path verifies the pane's
        # shell by process identity and does its own line-clear, so nothing is typed before it has.
        resumed = native.attachment.resume_selected(row['agent_id'], authorized) if native is not None else None
        if resumed is not None:
            return resumed
        # Clear any partially typed shell command before entering the fixed launcher.
        code, _ = broker.tmux('send-keys','-t',snap['pane_id'],'C-u')
        if code: raise DomainError('operation_failed', 'Could not prepare shell input')
        if dev:
            # Dev is owner-project-led: no role, work prompt or readiness scraping follows.
            broker.submit_input(session, dev)
            return 'Startup command submitted; readiness/work is not confirmed'
        client = launcher
        name, role_command = CLIENT_NAMES[client], '/role' if client=='claude' else '$role'
        # The agent shell's wrapper would otherwise open a bare launch with the role prompt typed again just below.
        broker.submit_input(session, 'QORQBOT_ROLE_SENT=1 ' + client)
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

    def _observe(self, row, client, expect=None, allow_busy=False, allow_text=False):
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
                                         allow_busy=allow_busy, allow_text=allow_text)
        return broker, snap, reading

    def _ready(self, row, client, allow_busy=False, replace_composer=False):
        options = {"allow_text": True} if replace_composer else {}
        broker, snap, reading = self._observe(row, client, allow_busy=allow_busy, **options)
        if not (reading.ready or (replace_composer and reading.state == "editable")):
            if reading.reason == 'enter_interrupts':
                raise OwnerInputDeferred('submit_interrupts')
            raise OwnerInputDeferred({'text': 'composer_not_empty', 'busy': 'composer_busy', 'modal': 'composer_modal'}
                                     .get(reading.state, 'composer_unrecognized'))
        return broker, snap, reading

    def owner_input(self, row, text, *, workdir, checkpoint, require_paste_echo=True, allow_busy=False, replace_composer=False):
        """Type ``text`` into the target composer and submit it. Returns an ``OwnerInputOutcome``.

        Owner Chat may explicitly replace the draft; other producers require an empty composer.

        ``deferred``: nothing was typed (the terminal is not ready); ``submitted``: the submit key was sent, processing
        is not confirmed; ``uncertain``: input may have reached the terminal and must never be replayed. ``checkpoint``
        is called once after the pre-input checks and before the first input, and one more observation follows it; if
        either fails nothing is typed. ``allow_busy`` (default off) accepts a verified empty composer while the native
        turn is still running. Replacement uses editing keys only, after the durable claim,
        then requires exact paste verification before submit. If the same paste remains in the same composer after
        ``submit_settle`` (1 second), retry Enter once without repasting or clearing anything.
        ``workdir`` is a private (0700) directory for the short-lived paste file, which never carries the text in argv.
        """
        self.validate_owner_text(text)
        client = self.selected_client(row) or runtime_client(row)
        if client is None:
            return OwnerInputOutcome('failed', 'unsupported_runtime')
        lock = self._owner_locks.setdefault(row['agent_id'], threading.Lock())
        if not lock.acquire(blocking=False):
            return OwnerInputOutcome('deferred', 'target_busy')
        try:
            return self._deliver(row, client, text, workdir, checkpoint, require_paste_echo, allow_busy, replace_composer)
        finally:
            lock.release()

    def _ping(self, row, message, key, client):
        """A direct message into the exact registered native session, through the shared input arbiter. Nothing is typed unless
        that session is verifiably there; an input that was definitely not typed is reported as such, never as uncertain."""
        from qorqut.native_chat.contracts import InputRequest, NativeChatError, STATE_SUBMITTED, STATE_UNCERTAIN, TYPE_DIRECT
        try:
            result = self.runtime.native_chat.arbiter.submit(InputRequest(row['agent_id'], TYPE_DIRECT, key, message))
        except NativeChatError as error:
            raise DomainError('not_delivered', error.message) from None
        if result.state == STATE_SUBMITTED:
            return f"Direct message submitted to {CLIENT_NAMES.get(client, 'the native client')}; processing is not confirmed"
        if result.state == STATE_UNCERTAIN:
            raise DomainError('operation_failed', 'Direct message outcome could not be confirmed')
        raise DomainError('not_delivered', f"Direct message was not typed: {result.reason or 'the native session is not ready'}")

    def _deliver(self, row, client, text, workdir, checkpoint, require_paste_echo=True, allow_busy=False, replace_composer=False):
        # Each observation must show the ordinary composer (empty unless Chat replaces it) on the same client
        # snapshot. The rest of the screen (a transcript or status timer, the footer, colours) is free to change, and with
        # ``allow_busy`` so is whether a turn is running: busy to idle between the checks is fine.
        try:
            broker, snap, first = self._ready(row, client, allow_busy, replace_composer)
            self.sleep(self.screen_settle)
            _, again, _ = self._ready(row, client, allow_busy, replace_composer)
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
            _, last, _ = self._ready(row, client, allow_busy, replace_composer)
        except OwnerInputDeferred as exc:
            return OwnerInputOutcome('deferred', exc.reason)
        if last != snap:
            return OwnerInputOutcome('deferred', 'screen_changing')
        receipt = {'client': client, 'composer': first.fingerprint,
                   'target': hashlib.sha256(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()}
        outcome = self._type(row, client, broker, snap, first, text, workdir, require_paste_echo, allow_busy, replace_composer)
        return outcome._replace(receipt={**receipt, **(outcome.receipt or {})})

    def _type(self, row, client, broker, snap, first, text, workdir, require_paste_echo=True, allow_busy=False, replace_composer=False):
        """Everything from the first input on. Any outcome here may follow a write, so it is never replayed."""
        pane = snap['pane_id']
        if replace_composer:
            # The durable claim has already been made. Clear only the editable
            # draft, never send Escape/Ctrl-C, and never replay after a key write.
            for _ in range(first.clear_passes):
                code, _ = broker.tmux('send-keys', '-t', pane, 'C-e', 'C-u', 'C-k')
                if code:
                    return OwnerInputOutcome('uncertain', 'composer_clear_failed')
                self.sleep(self.screen_settle)
                try:
                    _, seen, _ = self._ready(row, client, allow_busy, True)
                    if seen != snap:
                        return OwnerInputOutcome('uncertain', 'session_changed')
                except OwnerInputDeferred as exc:
                    return OwnerInputOutcome('uncertain', 'composer_clear_unverified', {'clear_check': exc.reason})
            # A suggestion can remain visible after clearing. The exact-paste
            # check below proves replacement; never submit leftover draft text.
        paste = self._paste(broker, pane, text, workdir)
        if paste is not None:
            return paste
        self.sleep(self.paste_settle)
        # Chat owns this successful paste into a verified empty composer. Long
        # text may be clipped or collapsed by the CLI; visible echo is optional.
        # Identity/mode checks still run before the initial Enter.
        for attempt in range(1, PASTE_VERIFY_ATTEMPTS + 1):
            try:
                _, seen, pasted = self._observe(row, client, expect=text, allow_busy=allow_busy)
            except OwnerInputDeferred as exc:
                return OwnerInputOutcome('uncertain', 'paste_unverified',
                                         {'paste_check': exc.reason, 'paste_checks': attempt})
            # Loss of identity or an interrupting submit key is final, even if a later frame looks valid.
            if seen != snap or pasted.reason == 'enter_interrupts':
                return OwnerInputOutcome('uncertain', 'paste_unverified',
                                         {'paste_check': 'session_changed' if seen != snap else pasted.reason,
                                          'paste_checks': attempt})
            if not require_paste_echo or (pasted.state == 'pasted' and pasted.fingerprint != first.fingerprint):
                break
            # Only the echo may still be rendering. Each observation re-verifies the client, pane and mode;
            # a partial or different message never authorizes Enter. Waiting cannot resend any input.
            if attempt == PASTE_VERIFY_ATTEMPTS:
                return OwnerInputOutcome('uncertain', 'paste_unverified',
                                         {'paste_check': pasted.reason, 'paste_checks': attempt})
            self.sleep(PASTE_VERIFY_INTERVAL)
        for submit_attempt in (1, 2):
            submit_receipt = {'submit_attempts': submit_attempt}
            code, _ = broker.tmux('send-keys', '-t', pane, 'Enter')
            if code:
                return OwnerInputOutcome('uncertain', 'submit_failed', submit_receipt)
            self.sleep(self.submit_settle)
            try:
                _, seen, after = self._observe(row, client, expect=text, allow_busy=allow_busy)
            except OwnerInputDeferred:
                return OwnerInputOutcome('submitted', 'processing_not_confirmed', submit_receipt)
            if seen != snap:
                return OwnerInputOutcome('uncertain', 'session_changed', submit_receipt)
            # Retry only the submit key, only when the exact pending paste is still in the ordinary composer.
            # A cleared/different draft, modal, interrupting mode or unreadable screen never authorizes another Enter.
            if after.state == 'pasted':
                continue
            if after.state == 'text' and after.reason == 'composer_not_empty':
                return OwnerInputOutcome('uncertain', 'submit_unconfirmed', submit_receipt)
            return OwnerInputOutcome('submitted', 'processing_not_confirmed', submit_receipt)
        return OwnerInputOutcome('uncertain', 'submit_unconfirmed', submit_receipt)

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
