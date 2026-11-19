"""One-time move of an installation's agents from tmux terminals to managed sessions.

Until this release every agent ran as an interactive Claude Code or Codex client inside a tmux session of the installation's
own tmux server. From now on every agent is a managed session (a detached host process drives a headless client) and there is
no tmux. This tool carries an installation over, with the service stopped::

    python -m qorqut.migration.managed_cutover inspect --instance-root ROOT [--json]
    python -m qorqut.migration.managed_cutover apply   --instance-root ROOT [--only AGENT_ID] [--force] [--json]

``inspect`` changes nothing. It reports, per agent, whether a tmux session exists, which client really runs in it (decided from the
process, never from the profile), its pid, the conversation it holds (from the database and from the process), model, effort,
whether a turn is running, and what ``apply`` would do. The owner's own shell sessions in the same tmux server are listed and
never touched.

``apply`` takes the instance lifetime lock (so it refuses while the service runs) and then, agent by agent, stopping at the
first failure: records the client, model and effort the client was started with in the agent's ``options.json``; ends the
client with SIGTERM (waits for a turn in progress up to ``--wait-seconds`` unless ``--force``), verifies by pid and start time
that it is gone; ends the agent's tmux session; removes the per-agent leftovers that only served terminals. Database rows are
never edited: they stay as history and the service settles them on its next start; the next input resumes the recorded
conversation. A second run only finishes what is left.

Why SIGTERM (measured with the real clients, see the package report): Claude Code and Codex both end promptly on it, Claude Code
runs its SessionEnd hook, the conversation file is complete and parseable afterwards and ``claude -p --resume`` /
``codex app-server thread/resume`` continue the same conversation, also when a turn was cut off (the resumed client sees the
unfinished tool call as interrupted). Typing ``/exit`` gives the same result but needs a verified empty composer, which cannot be
proved from outside, so nothing is ever typed into a pane. SIGKILL gives the same conversation but leaves the client's tool
children running and a stale session file, so it is used only after SIGTERM failed and only with ``--force``.

Only the installation's own tmux server is ever addressed (socket name from ``BIZ_TMUX_SOCKET``, default ``qorqut``, plus the
pre-rename ``bazarlyk`` socket when the variable is unset; folder from ``TMUX_TMPDIR`` or, for a folder installation, ``<root>/.run``),
through ``tmux -L``, and only exact sessions named for a registered agent are ended.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time

from qorqut import process_info
from qorqut.composition.locations import agent_workspace_of
from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments
from qorqut.service.ownership import InstanceOwnership
from qorqut_operations.contracts import DomainError

DEFAULT_SOCKET = 'qorqut'
LEGACY_SOCKET = 'bazarlyk'
SHELLS = frozenset({'bash', 'zsh', 'sh', 'dash', 'fish'})
CLAUDE_NAMES = frozenset({'claude', 'claude.exe'})
WRAPPER_NAMES = frozenset({'node', 'nodejs', 'bun'})
SOCKET_NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')
UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
NAME_BAD = re.compile(r'[^A-Za-z0-9-]')
SHELL_FILES = ('.qorqut/agent-shell.bash', '.qorqut/zsh/.zshenv', '.qorqut/zsh/.zshrc')
GENERATED_HEADER = '# Generated Qorqut agent shell'
MODE_FILE = 'mode.json'
RECORD_FOLDER = 'managed-cutover'
# claude / codex options that take a value in the next argument; everything else starting with a dash is a plain switch.
CLAUDE_VALUE_FLAGS = frozenset({'--model', '--effort', '--settings', '--permission-mode', '--session-id', '--append-system-prompt',
                                '--system-prompt', '--mcp-config', '--add-dir', '--allowedTools', '--allowed-tools', '--disallowedTools',
                                '--disallowed-tools', '--agents', '--plugin-dir', '--fallback-model', '--betas', '--name', '-n',
                                '--output-format', '--input-format'})
CODEX_VALUE_FLAGS = frozenset({'-m', '--model', '-c', '--config', '-C', '--cd', '-s', '--sandbox', '-a', '--ask-for-approval', '-p',
                               '--profile', '-i', '--image', '--enable', '--disable', '--add-dir', '--local-provider', '--oss-provider'})
CODEX_INTERACTIVE_SUBCOMMANDS = frozenset({'resume', 'fork'})
TERM_WAIT = 30.0          # seconds a client gets to end after SIGTERM before ``--force`` may kill it
KILL_WAIT = 10.0


class CutoverError(Exception):
    """A refusal or failure with a stable ``code`` and a message that is safe to show the owner."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# ----------------------------------------------------------------------------------------------------------------------
# Processes: every observation and signal goes through one object so tests can stand in for the machine.
# ----------------------------------------------------------------------------------------------------------------------
class Machine:
    """Process facts and signals of this machine (Linux and macOS through ``process_info``)."""

    def pids(self):
        return process_info.pids()

    def parent(self, pid):
        return process_info.parent_pid(pid)

    def argv(self, pid):
        return process_info.cmdline(pid)

    def comm(self, pid):
        return process_info.name(pid)

    def exe(self, pid):
        return process_info.exe(pid).name

    def cwd(self, pid):
        return str(process_info.cwd(pid))

    def environ(self, pid):
        return process_info.environ(pid)

    def start(self, pid):
        return process_info.start_token(pid)

    def zombie(self, pid):
        return process_info.is_zombie(pid)

    def open_files(self, pid):
        """Paths this process has open; a platform that cannot say returns an empty list."""
        if sys.platform != 'darwin':
            found = []
            for entry in Path(f'/proc/{int(pid)}/fd').iterdir():
                try:
                    found.append(os.readlink(entry))
                except OSError:
                    continue
            return found
        import psutil
        return [item.path for item in psutil.Process(int(pid)).open_files()]

    def signal(self, pid, number):
        os.kill(int(pid), number)

    def alive(self, pid, start):
        """The process with this pid AND this start token still runs (a reused pid is not the same process)."""
        try:
            return not self.zombie(pid) and self.start(pid) == start
        except (OSError, ProcessLookupError):
            return False

    def sleep(self, seconds):
        time.sleep(seconds)

    def now(self):
        return time.monotonic()


def _try(function, *args, default=None):
    try:
        return function(*args)
    except (OSError, ProcessLookupError, PermissionError, ValueError, KeyError, IndexError):
        return default


# ----------------------------------------------------------------------------------------------------------------------
# tmux: only the installation's own server, only through -L.
# ----------------------------------------------------------------------------------------------------------------------
def own_sockets(env):
    """The socket names that belong to this installation: the configured one, and the pre-rename one when nothing was configured."""
    configured = (env.get('BIZ_TMUX_SOCKET') or '').strip()
    if configured:
        if not SOCKET_NAME.fullmatch(configured):
            raise CutoverError('invalid_configuration', 'BIZ_TMUX_SOCKET is not a plain socket name')
        return [configured]
    return [DEFAULT_SOCKET, LEGACY_SOCKET]


def tmux_folder(instance_root, env, explicit=None):
    """Where the installation's tmux sockets live: ``--tmux-tmpdir``, else ``TMUX_TMPDIR``, else ``<root>/.run`` for a folder
    installation (instance folder named ``instance`` next to a ``.run`` folder holding ``tmux-<uid>``), else tmux's own default."""
    if explicit:
        return str(explicit)
    if env.get('TMUX_TMPDIR'):
        return env['TMUX_TMPDIR']
    root = Path(instance_root)
    if root.name == 'instance' and (root.parent / '.run' / f'tmux-{os.getuid()}').is_dir():
        return str(root.parent / '.run')
    return None


class Tmux:
    """The installation's tmux server on one socket. ``-L <socket>`` is always first; nothing else can address a server."""

    def __init__(self, socket, folder=None, *, runner=None, executable=None, env=None):
        if not SOCKET_NAME.fullmatch(str(socket)):
            raise CutoverError('foreign_tmux_server', 'Not a plain tmux socket name')
        self.socket, self.missing = socket, False
        self.env = {key: value for key, value in (os.environ if env is None else env).items() if key not in ('TMUX', 'TMUX_PANE')}
        if folder:
            self.env['TMUX_TMPDIR'] = str(folder)
        self.executable = executable or shutil.which('tmux', path=self.env.get('PATH')) or 'tmux'
        self._runner = runner or self._run_real

    @staticmethod
    def _run_real(argv, env):
        try:
            done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return 127, '', 'tmux is not installed'
        except subprocess.TimeoutExpired:
            return 124, '', 'tmux did not answer'
        return done.returncode, done.stdout, done.stderr

    def run(self, *args):
        if args and args[0] in ('-S', '-L'):
            raise CutoverError('foreign_tmux_server', 'A tmux command may not choose its own server')
        return self._runner([self.executable, '-L', self.socket, *args], self.env)

    def sessions(self):
        """[{id, name}] of the server, or None when no server runs on this socket. Never starts a server."""
        code, out, err = self.run('list-sessions', '-F', '#{session_id}\t#{session_name}')
        if code != 0:
            text = (err or '').lower()
            if code == 127:
                self.missing = True
                return None
            if 'no server running' in text or 'no such file or directory' in text or 'error connecting' in text:
                return None
            raise CutoverError('tmux_unreadable', 'tmux did not list its sessions: ' + (err or '').strip()[:200])
        found = []
        for line in out.splitlines():
            ident, _, name = line.partition('\t')
            if ident.startswith('$') and name:
                found.append({'id': ident, 'name': name})
        return found

    def pane_pids(self, session_id):
        code, out, err = self.run('list-panes', '-s', '-t', session_id, '-F', '#{pane_pid}')
        if code != 0:
            raise CutoverError('tmux_unreadable', 'tmux did not list the panes of a session: ' + (err or '').strip()[:200])
        return [int(item) for item in out.split() if item.isdigit()]

    def kill_session(self, session_id):
        code, _, err = self.run('kill-session', '-t', session_id)
        if code != 0 and 'no server running' not in (err or '').lower() and "can't find session" not in (err or '').lower():
            raise CutoverError('tmux_kill_failed', 'tmux did not end the session: ' + (err or '').strip()[:200])


# ----------------------------------------------------------------------------------------------------------------------
# What is in the database.
# ----------------------------------------------------------------------------------------------------------------------
def tmux_name(value):
    """tmux turns ``.`` and ``:`` in a session name into ``_``."""
    return value.replace('.', '_').replace(':', '_')


def session_name(slug, agent):
    return f'{NAME_BAD.sub("-", slug)}__{agent}'


def read_agents(paths):
    """Registered agents and their selected conversation, read-only. ``[]`` pieces for tables an old store lacks."""
    database = Path(paths.database).resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=30)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        rows = connection.execute(
            'SELECT p.agent_id, p.name, p.home_business_id, p.status, p.profile_json, b.slug AS slug '
            'FROM agent_profiles p LEFT JOIN businesses b ON b.id=p.home_business_id ORDER BY p.name, p.agent_id').fetchall()
        try:
            owners = {row['agent_id']: row for row in connection.execute(
                'SELECT agent_id, consumer_id, binding_json, generation, desired_state FROM native_session_owners')}
        except sqlite3.Error:
            owners = {}
    agents = []
    for row in rows:
        try:
            profile = json.loads(row['profile_json'])
        except (TypeError, ValueError):
            profile = {}
        owner = owners.get(row['agent_id'])
        binding = {}
        if owner is not None:
            try:
                binding = json.loads(owner['binding_json'])
            except (TypeError, ValueError):
                binding = {}
        runtime = profile.get('runtime') if isinstance(profile, dict) else None
        is_global = row['home_business_id'] is None
        slug = 'global' if is_global else (row['slug'] or '')
        agents.append({
            'agent_id': row['agent_id'], 'name': row['name'], 'status': row['status'], 'scope': slug,
            'profile_runtime': runtime if isinstance(runtime, str) else None,
            'session': tmux_name(session_name('global', '_' + row['agent_id']) if is_global else session_name(slug, row['name'])),
            'workspace': str(agent_workspace_of(paths, row['agent_id'], row['home_business_id'], row['slug'])),
            'db_conversation': owner['consumer_id'] if owner is not None else None,
            'db_client': {'claude-code': 'claude', 'codex': 'codex'}.get(binding.get('runtime') if isinstance(binding, dict) else None),
            'desired_state': owner['desired_state'] if owner is not None else None})
    return agents


# ----------------------------------------------------------------------------------------------------------------------
# Reading a live client from outside.
# ----------------------------------------------------------------------------------------------------------------------
def option_values(argv, value_flags):
    """(options, positionals) of an argument vector (without the program): ``--flag value`` and ``--flag=value`` both read."""
    options, positionals, index = {}, [], 0
    while index < len(argv):
        item = argv[index]
        if item == '--':
            positionals += argv[index + 1:]
            break
        if item.startswith('--') and '=' in item:
            key, _, value = item.partition('=')
            options.setdefault(key, []).append(value)
        elif item in value_flags and index + 1 < len(argv):
            options.setdefault(item, []).append(argv[index + 1])
            index += 1
        elif item.startswith('-'):
            options.setdefault(item, [])
        else:
            positionals.append(item)
        index += 1
    return options, positionals


def claude_options(argv):
    """What a Claude Code command line (without the program) says: model, effort, resume id, headless or not."""
    options, positionals = option_values(argv, CLAUDE_VALUE_FLAGS)
    resume = None
    for flag in ('--resume', '-r'):
        if flag in options:
            value = options[flag][0] if options[flag] else None
            if value is None and positionals:
                value = positionals[0]
            resume = value if value and UUID.fullmatch(value) else resume
    for item in argv:
        if item.startswith('--resume=') and UUID.fullmatch(item.partition('=')[2]):
            resume = item.partition('=')[2]
    return {'model': (options.get('--model') or [None])[-1], 'effort': (options.get('--effort') or [None])[-1], 'resume': resume,
            'headless': '-p' in options or '--print' in options, 'continue': '-c' in options or '--continue' in options}


def codex_options(argv):
    """The same for Codex: the interactive client takes ``-m``/``--model`` and ``-c model=...``/``-c model_reasoning_effort=...``."""
    options, positionals = option_values(argv, CODEX_VALUE_FLAGS)
    model, effort = (options.get('--model') or options.get('-m') or [None])[-1], None
    for setting in options.get('-c', []) + options.get('--config', []):
        key, _, value = setting.partition('=')
        value = value.strip().strip('"\'')
        if key.strip() == 'model' and model is None:
            model = value or None
        elif key.strip() == 'model_reasoning_effort':
            effort = value or None
    subcommand = positionals[0] if positionals else None
    resume = next((item for item in positionals[1:] if UUID.fullmatch(item)), None) if subcommand in CODEX_INTERACTIVE_SUBCOMMANDS else None
    return {'model': model, 'effort': effort, 'resume': resume, 'subcommand': subcommand,
            'headless': subcommand is not None and subcommand not in CODEX_INTERACTIVE_SUBCOMMANDS}


def classify(machine, pid):
    """What a process is, from its own name and arguments: shell, claude, codex, headless (a non-interactive claude/codex run) or
    other. ``program`` is the argument vector without the executable (and without ``node script`` for a wrapper)."""
    argv = _try(machine.argv, pid, default=None)
    if not argv:
        return {'kind': 'gone', 'pid': pid}
    comm = _try(machine.comm, pid, default='') or ''
    exe = _try(machine.exe, pid, default='') or ''
    base = Path(argv[0]).name
    names = {comm, exe, base}
    program = argv[1:]
    if names & WRAPPER_NAMES and len(argv) > 1:
        script = Path(argv[1]).name if not argv[1].startswith('-') else ''
        script_path = argv[1] if script else ''
        if script in ('codex', 'codex.js') or '/@openai/codex/' in script_path:
            names, program = {'codex'}, argv[2:]
        elif script == 'claude' or '/@anthropic-ai/claude-code/' in script_path:
            names, program = {'claude'}, argv[2:]
    kind = 'other'
    if names & SHELLS:
        kind = 'shell'
    elif names & CLAUDE_NAMES:
        kind = 'headless' if claude_options(program)['headless'] else 'claude'
    elif 'codex' in names:
        kind = 'headless' if codex_options(program)['headless'] else 'codex'
    return {'kind': kind, 'pid': pid, 'argv': argv, 'program': program, 'comm': comm}


def children_map(machine):
    children = {}
    for pid in _try(machine.pids, default=[]):
        parent = _try(machine.parent, pid, default=None)
        if parent is not None:
            children.setdefault(parent, []).append(pid)
    return children


class Pane:
    """The processes of one agent's tmux session: its shell(s), the interactive client if any, everything else."""

    def __init__(self, machine, pane_pids, children=None):
        children = children_map(machine) if children is None else children
        self.machine, self.pane_pids, self.shells, self.clients, self.others, self.headless = machine, list(pane_pids), [], [], [], []
        seen = set()

        def walk(pid, inside_client):
            if pid in seen:
                return
            seen.add(pid)
            info = classify(machine, pid)
            if info['kind'] == 'gone':
                return
            info['start'] = _try(machine.start, pid)
            nested = inside_client
            if info['kind'] in ('claude', 'codex') and not inside_client:
                self.clients.append(info)
                nested = True
            elif inside_client:
                pass                      # a tool or helper of the client: ends with it
            elif info['kind'] == 'shell':
                self.shells.append(info)
            elif info['kind'] == 'headless':
                self.headless.append(info)
            else:
                self.others.append(info)
            for child in sorted(children.get(pid, [])):
                walk(child, nested)
        for pid in self.pane_pids:
            walk(pid, False)
        for client in self.clients:
            client['members'] = self._subtree(client['pid'], children)

    @staticmethod
    def _subtree(pid, children):
        out, stack = [], [pid]
        while stack:
            item = stack.pop()
            out.append(item)
            stack += children.get(item, [])
        return out

    def kind(self):
        if len(self.clients) == 1:
            return self.clients[0]['kind']
        if len(self.clients) > 1:
            return 'unknown'
        return 'unknown' if self.others or self.headless else 'none'


def claude_home(machine, pid):
    env = _try(machine.environ, pid, default={}) or {}
    if env.get('CLAUDE_CONFIG_DIR'):
        return Path(env['CLAUDE_CONFIG_DIR'])
    return Path(env.get('HOME') or Path.home()) / '.claude'


def codex_home(machine, pid):
    env = _try(machine.environ, pid, default={}) or {}
    if env.get('CODEX_HOME'):
        return Path(env['CODEX_HOME'])
    return Path(env.get('HOME') or Path.home()) / '.codex'


def read_json(path, limit=65536):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def tail_lines(path, size=262144):
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            handle.seek(max(0, end - size))
            data = handle.read()
    except OSError:
        return []
    return [line for line in data.decode('utf-8', 'replace').splitlines() if line.strip()]


def observe_claude(machine, client, workspace, now_wall):
    """Conversation, activity and transcript of a live Claude Code from its own session file (``<config>/sessions/<pid>.json``,
    valid only while its pid and start token match the process), the hook marker, and the command line."""
    pid, start = client['pid'], client['start']
    home = claude_home(machine, pid)
    session = read_json(home / 'sessions' / f'{pid}.json')
    if session is not None and (session.get('pid') != pid or str(session.get('procStart', start)) != str(start)):
        session = None
    found = {'conversation': None, 'source': None, 'turn': 'unknown', 'transcript': None, 'observed_model': None}
    marker = read_json(Path(workspace) / '.qorqut/terminal-chat.json')
    if marker is not None and not (marker.get('pid') == pid and str(marker.get('started')) == str(start)):
        marker = None
    options = claude_options(client['program'])
    if session and UUID.fullmatch(str(session.get('sessionId', ''))):
        found['conversation'], found['source'] = session['sessionId'], 'client session file'
    elif marker and UUID.fullmatch(str(marker.get('session_id', ''))):
        found['conversation'], found['source'] = marker['session_id'], 'hook marker'
    elif options['resume']:
        found['conversation'], found['source'] = options['resume'], 'command line'
    if session and session.get('status') in ('idle', 'busy'):
        found['turn'] = 'running' if session['status'] == 'busy' else 'idle'
    conversation = found['conversation']
    if conversation:
        for path in sorted((home / 'projects').glob(f'*/{conversation}.jsonl'))[:1]:
            found['transcript'] = str(path)
            for line in reversed(tail_lines(path, 65536)):
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                model = record.get('message', {}).get('model') if isinstance(record.get('message'), dict) else None
                if record.get('type') == 'assistant' and isinstance(model, str):
                    found['observed_model'] = model
                    break
            if found['turn'] == 'unknown':
                age = now_wall - path.stat().st_mtime
                found['turn'] = 'running' if age < 10 else 'idle'
    return found, options


def observe_codex(machine, client, workspace, now_wall):
    """The same for Codex: the thread-writer lock and the rollout file the process holds open name its thread; the rollout's last
    events say whether a turn runs."""
    pid, start = client['pid'], client['start']
    home = codex_home(machine, pid)
    found = {'conversation': None, 'source': None, 'turn': 'unknown', 'transcript': None, 'observed_model': None}
    options = codex_options(client['program'])
    files = _try(machine.open_files, pid, default=[]) or []
    rollout = None
    for name in files:
        item = Path(name)
        if item.name.startswith('rollout-') and item.suffix == '.jsonl':
            rollout = item
        match = UUID.search(item.stem) if item.parent.name == 'thread-writer-locks' else None
        if match:
            found['conversation'], found['source'] = match.group(0), 'client lock file'
    if rollout is None and found['conversation']:
        rollout = next(iter(sorted((home / 'sessions').glob(f'*/*/*/rollout-*-{found["conversation"]}*.jsonl'))), None)
    marker = read_json(Path(workspace) / '.qorqut/terminal-chat.json')
    if found['conversation'] is None and marker and marker.get('pid') == pid and str(marker.get('started')) == str(start) \
            and UUID.fullmatch(str(marker.get('session_id', ''))):
        found['conversation'], found['source'] = marker['session_id'], 'hook marker'
    if found['conversation'] is None and options['resume']:
        found['conversation'], found['source'] = options['resume'], 'command line'
    if rollout is not None:
        found['transcript'] = str(rollout)
        state = None
        for line in tail_lines(rollout):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            payload = record.get('payload') if isinstance(record.get('payload'), dict) else {}
            if record.get('type') == 'event_msg' and payload.get('type') in ('task_started', 'turn_started'):
                state = 'running'
            elif record.get('type') == 'event_msg' and payload.get('type') in ('task_complete', 'turn_complete', 'turn_aborted'):
                state = 'idle'
            elif record.get('type') == 'turn_context' and isinstance(payload.get('model'), str):
                found['observed_model'] = payload['model']
        if state:
            found['turn'] = state
    return found, options


# ----------------------------------------------------------------------------------------------------------------------
# Inspect and apply.
# ----------------------------------------------------------------------------------------------------------------------
def leftovers(paths, agent):
    """Per-agent files that only served terminals and are safe to remove: the generated shell wrappers of the workspace (only when
    they still carry the generator's header) and the agent's mode file."""
    found = []
    workspace = Path(agent['workspace'])
    for relative in SHELL_FILES:
        path = workspace / relative
        if path.is_symlink() or not path.is_file():
            continue
        try:
            first = path.read_text(errors='replace').splitlines()[:1]
        except OSError:
            continue
        if first and first[0].startswith(GENERATED_HEADER):
            found.append(str(path))
    mode = _mode_file(paths, agent['agent_id'])
    if mode is not None and mode.is_file() and not mode.is_symlink():
        found.append(str(mode))
    return found


def _state_folder(paths, agent_id):
    from qorqut.managed_runtime import state
    return state.agent_state(paths, agent_id)


def _mode_file(paths, agent_id):
    try:
        return _state_folder(paths, agent_id) / MODE_FILE
    except (ValueError, ImportError):
        return None


def stored_options(paths, agent_id):
    try:
        from qorqut.managed_runtime import options
        return options.read(paths, agent_id).public()
    except ImportError:
        path = _options_path(paths, agent_id)
        value = read_json(path, 4096) if path is not None else None
        return {key: (value or {}).get(key) for key in ('client', 'model', 'effort')}


def _options_path(paths, agent_id):
    try:
        return _state_folder(paths, agent_id) / 'options.json'
    except (ValueError, ImportError):
        return None


def write_options(paths, agent_id, client, model, effort):
    """Store what the client was started with. Goes through ``qorqut.managed_runtime.options`` (the one owner of the file); a value
    the client was not given is not written, so it never overwrites a stored one with nothing."""
    changes = {'client': client}
    if model:
        changes['model'] = model
    if effort:
        changes['effort'] = effort
    try:
        from qorqut.managed_runtime import options
    except ImportError:
        return _write_options_fallback(paths, agent_id, changes)
    try:
        return options.write(paths, agent_id, **changes).public()
    except ValueError as error:                     # OptionsError: a value the options file would not accept
        raise CutoverError('options_refused', f'The options file refused {sorted(changes)}: {error}') from error
    except OSError as error:
        raise CutoverError('options_not_written', f'The options file could not be written: {error}') from error


def _write_options_fallback(paths, agent_id, changes):
    """The same JSON shape at the same path, for a build without ``managed_runtime/options.py``. Easy to delete."""
    path = _options_path(paths, agent_id)
    if path is None:
        raise CutoverError('options_not_written', 'The agent state folder could not be derived')
    current = {key: (read_json(path, 4096) or {}).get(key) for key in ('client', 'model', 'effort')}
    merged = {**current, **changes}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump({'schema_version': 1, **merged}, handle)
    os.replace(temporary, path)
    return merged


def survey(paths, env, *, machine=None, tmux_factory=None, socket=None, folder=None, now_wall=None):
    """Everything inspect reports: for each agent its tmux session and client, the owner's own sessions, and per-socket status."""
    machine = machine or Machine()
    now_wall = time.time() if now_wall is None else now_wall
    allowed = own_sockets(env)
    if socket is not None and socket not in allowed:
        raise CutoverError('foreign_tmux_server', f'tmux socket {socket!r} is not this installation\'s own ({", ".join(allowed)}); '
                           'nothing was touched')
    sockets = [socket] if socket is not None else allowed
    folder = tmux_folder(paths.instance_root, env, folder)
    make = tmux_factory or (lambda name: Tmux(name, folder, env=env))
    agents = read_agents(paths)
    by_session = {agent['session']: agent for agent in agents}
    servers, owner_sessions, other_sessions, entries = [], [], [], {}
    for name in sockets:
        tmux = make(name)
        sessions = tmux.sessions()
        servers.append({'socket': name, 'folder': folder, 'running': sessions is not None, 'tmux_installed': not tmux.missing})
        for item in sessions or []:
            agent = by_session.get(item['name'])
            if agent is not None:
                entries[agent['agent_id']] = (tmux, item)
            elif re.fullmatch(r'global__workspace(-\d+)?', item['name']):
                owner_sessions.append({'socket': name, 'session': item['name']})
            else:
                other_sessions.append({'socket': name, 'session': item['name']})
    report, children = [], children_map(machine)
    for agent in agents:
        entry = entries.get(agent['agent_id'])
        report.append(describe_agent(paths, agent, entry, machine, now_wall, children))
    return {'servers': servers, 'agents': report, 'owner_shell_sessions': owner_sessions, 'other_sessions': other_sessions}


def describe_agent(paths, agent, entry, machine, now_wall, children=None):
    view = {key: agent[key] for key in ('agent_id', 'name', 'status', 'scope', 'profile_runtime', 'workspace', 'desired_state')}
    view.update({'session': agent['session'], 'session_present': entry is not None, 'socket': entry[0].socket if entry else None,
                 'client': 'none', 'pid': None, 'started': None, 'command': None, 'cwd': None,
                 'conversation_db': agent['db_conversation'], 'conversation_process': None, 'conversation_source': None,
                 'db_client': agent['db_client'], 'model': None, 'effort': None, 'observed_model': None, 'transcript': None,
                 'turn': None, 'other_processes': [], 'problems': [], 'warnings': [], 'stored_options': stored_options(paths, agent['agent_id']),
                 'leftovers': leftovers(paths, agent), '_client': None, '_session_id': entry[1]['id'] if entry else None})
    if entry is None:
        view['plan'] = _plan(view)
        return view
    tmux, item = entry
    panes = tmux.pane_pids(item['id'])
    pane = Pane(machine, panes, children)
    view['client'] = pane.kind()
    view['other_processes'] = [' '.join(info['argv'])[:120] for info in pane.others + pane.headless]
    if view['client'] == 'unknown':
        if len(pane.clients) > 1:
            view['problems'].append('more_than_one_client: ' + ', '.join(str(c['pid']) for c in pane.clients))
        else:
            view['problems'].append('unrecognised_process_in_pane')
    elif view['client'] in ('claude', 'codex'):
        client = pane.clients[0]
        view['_client'] = client
        view['pid'], view['started'], view['command'] = client['pid'], client['start'], ' '.join(client['argv'])[:300]
        view['cwd'] = _try(machine.cwd, client['pid'])
        observe = observe_claude if view['client'] == 'claude' else observe_codex
        found, options = observe(machine, client, agent['workspace'], now_wall)
        view.update(conversation_process=found['conversation'], conversation_source=found['source'], turn=found['turn'],
                    observed_model=found['observed_model'], transcript=found['transcript'], model=options['model'], effort=options['effort'])
        _check(view, agent, pane, client)
        if view['client'] == 'claude' and found['conversation'] and not found['transcript']:
            view['warnings'].append('no transcript file is found yet for this conversation (no turn so far?): the conversation cannot be resumed')
    view['plan'] = _plan(view)
    return view


def _check(view, agent, pane, client):
    if view['cwd'] is None:
        view['problems'].append('client_working_folder_unreadable')
    else:
        folder, workspace = os.path.realpath(view['cwd']), os.path.realpath(agent['workspace'])
        if folder != workspace and not folder.startswith(workspace + os.sep):
            view['problems'].append(f'client_not_in_agent_workspace: {view["cwd"]}')
    mine, theirs = view['conversation_db'], view['conversation_process']
    if mine and theirs and mine != theirs:
        view['problems'].append(f'conversation_mismatch: database {mine}, process {theirs}')
    if view['db_client'] and view['db_client'] != view['client']:
        view['warnings'].append(f'database selection is a {view["db_client"]} conversation but a {view["client"]} client runs; '
                                f'the recorded client is {view["client"]}')
    if not mine:
        view['warnings'].append('the database has no selected conversation for this agent; the managed session will start a new one')
    elif not theirs:
        view['warnings'].append('the conversation of the running client could not be read from the process')
    profile = {'claude-code': 'claude', 'codex': 'codex'}.get(view['profile_runtime'])
    if profile and profile != view['client']:
        view['warnings'].append(f'the profile says {view["profile_runtime"]} but {view["client"]} runs; the options file records {view["client"]}')
    if view['turn'] == 'running':
        view['warnings'].append('a turn is running')


def _plan(view):
    steps = []
    if view['client'] in ('claude', 'codex'):
        recorded = {key: value for key, value in (('client', view['client']), ('model', view['model']), ('effort', view['effort'])) if value}
        steps.append('write options ' + ', '.join(f'{k}={v}' for k, v in recorded.items()))
        steps.append(f'end {view["client"]} pid {view["pid"]} with SIGTERM' + (' after the running turn finishes (or --force)' if view['turn'] == 'running' else ''))
    elif view['session_present'] and view['client'] == 'none':
        steps.append('no client runs in the session: nothing to record')
    elif view['session_present']:
        steps.append('refuse: the pane holds something that is not this agent\'s client')
    if view['session_present']:
        steps.append('end the tmux session')
    if view['leftovers']:
        steps.append(f'remove {len(view["leftovers"])} terminal-only file(s)')
    if not steps:
        steps.append('nothing to do')
    return steps


def refusals(view, *, force, allow_mismatch):
    """Why apply must stop at this agent (empty: it may proceed)."""
    problems = []
    for problem in view['problems']:
        code = problem.split(':')[0]
        if code == 'conversation_mismatch' and allow_mismatch:
            continue
        problems.append(problem)
    if view['session_present'] and view['client'] == 'none' and view['other_processes'] and not force:
        problems.append('other_processes_in_pane: ' + '; '.join(view['other_processes'])[:200])
    return problems


def process_gone(machine, members, start_by_pid):
    return all(not machine.alive(pid, start_by_pid[pid]) for pid in members)


def end_client(machine, view, done, *, force, wait_seconds, poll, term_wait=TERM_WAIT, kill_wait=KILL_WAIT):
    """Wait out a turn in progress, SIGTERM the verified client, verify by pid and start token that it and its processes are gone.
    Appends what was done to ``done`` as it goes, so a failure still reports the steps before it."""
    client = view['_client']
    pid, start = client['pid'], client['start']
    starts = {item: _try(machine.start, item) for item in client['members']}
    if view['turn'] == 'running' and not force:
        deadline = machine.now() + wait_seconds
        while view['turn'] == 'running':
            if machine.now() >= deadline:
                raise CutoverError('turn_in_progress', f'a turn is still running after {wait_seconds:g} s; nothing was ended '
                                   '(wait longer with --wait-seconds, or end it anyway with --force)')
            machine.sleep(poll)
            view['turn'] = _turn_now(machine, view)
        done.append('turn finished')
    elif view['turn'] == 'running':
        done.append('turn in progress cut off (--force)')
    _reverify(machine, pid, start, client)
    machine.signal(pid, signal.SIGTERM)
    done.append(f'SIGTERM to pid {pid}')
    deadline = machine.now() + term_wait
    while not process_gone(machine, client['members'], starts):
        if machine.now() >= deadline:
            break
        machine.sleep(poll)
    if not process_gone(machine, client['members'], starts):
        stuck = [item for item in client['members'] if machine.alive(item, starts[item])]
        if not force:
            raise CutoverError('client_did_not_end', f'{view["client"]} pid {pid} did not end within {term_wait:g} s of SIGTERM '
                               f'(still running: {stuck}); re-run with --force to kill it')
        for item in stuck:
            _try(machine.signal, item, signal.SIGKILL)
        done.append(f'SIGKILL to {stuck} (--force)')
        deadline = machine.now() + kill_wait
        while not process_gone(machine, client['members'], starts) and machine.now() < deadline:
            machine.sleep(poll)
        if not process_gone(machine, client['members'], starts):
            raise CutoverError('client_did_not_end', f'{view["client"]} pid {pid} is still running after SIGKILL')
    done.append('client gone (pid and start time verified)')


def _turn_now(machine, view):
    client = view['_client']
    observe = observe_claude if view['client'] == 'claude' else observe_codex
    found, _ = observe(machine, {**client, 'start': client['start']}, view['workspace'], time.time())
    return found['turn']


def _reverify(machine, pid, start, client):
    """Immediately before signalling: still the same process, still the same program, still inside this agent's pane."""
    if not machine.alive(pid, start):
        raise CutoverError('client_changed', f'process {pid} is no longer the client that was inspected; nothing was signalled')
    again = classify(machine, pid)
    if again.get('kind') not in ('claude', 'codex') or again.get('argv') != client['argv']:
        raise CutoverError('client_changed', f'process {pid} no longer runs the inspected command; nothing was signalled')


def apply_agent(paths, view, machine, tmux, outcome, *, force, wait_seconds, poll, keep_mode_files, allow_mismatch, term_wait=TERM_WAIT):
    """One agent. Steps are appended to ``outcome['done']`` as they happen; raises CutoverError at the first thing that fails."""
    done, skipped = outcome['done'], outcome['skipped']
    problems = refusals(view, force=force, allow_mismatch=allow_mismatch)
    if problems:
        raise CutoverError('refused', '; '.join(problems))
    if view['client'] in ('claude', 'codex'):
        write_options(paths, view['agent_id'], view['client'], view['model'], view['effort'])
        done.append('options written')
        end_client(machine, view, done, force=force, wait_seconds=wait_seconds, poll=poll, term_wait=term_wait)
    elif view['session_present']:
        skipped.append('no client in the session')
    if view['session_present']:
        done += end_session(machine, tmux, view, force=force)
    done += remove_leftovers(paths, view, keep_mode_files=keep_mode_files)


def end_session(machine, tmux, view, *, force):
    sessions = tmux.sessions() or []
    current = next((item for item in sessions if item['id'] == view['_session_id'] and item['name'] == view['session']), None)
    if current is None:
        return ['tmux session already gone']
    pane = Pane(machine, tmux.pane_pids(current['id']))
    if pane.clients:
        raise CutoverError('client_still_running', 'a client is still running in the session; the session was not ended')
    rest = [' '.join(info['argv'])[:100] for info in pane.others + pane.headless]
    if rest and not force:
        raise CutoverError('other_processes_in_pane', 'the session still holds: ' + '; '.join(rest) + ' (end them, or use --force)')
    tmux.kill_session(current['id'])
    return ['tmux session ended']


def remove_leftovers(paths, view, *, keep_mode_files):
    done = []
    for name in leftovers(paths, {'agent_id': view['agent_id'], 'workspace': view['workspace']}):
        if keep_mode_files and Path(name).name == MODE_FILE:
            continue
        try:
            Path(name).unlink()
            done.append(f'removed {name}')
        except FileNotFoundError:
            pass
        except OSError as error:
            raise CutoverError('leftover_not_removed', f'{name} could not be removed: {error}') from error
    folder = Path(view['workspace']) / '.qorqut/zsh'
    try:
        folder.rmdir()
        done.append(f'removed {folder}')
    except OSError:
        pass
    return done


def public(view):
    return {key: value for key, value in view.items() if not key.startswith('_')}


def record_path(paths, stamp):
    return Path(paths.runtime) / RECORD_FOLDER / f'{stamp}-apply.json'


def write_record(path, document):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(document, handle, indent=1, sort_keys=True)
    os.replace(temporary, path)


def run_apply(paths, env, *, only=None, force=False, wait_seconds=300.0, poll=2.0, keep_mode_files=False, allow_mismatch=False,
              machine=None, tmux_factory=None, socket=None, folder=None, term_wait=TERM_WAIT, lock=True, record=True):
    machine = machine or Machine()
    owner = InstanceOwnership(paths).acquire() if lock else None
    try:
        return _apply_locked(paths, env, only, force, wait_seconds, poll, keep_mode_files, allow_mismatch, machine, tmux_factory,
                             socket, folder, term_wait, record)
    finally:
        if owner is not None:
            owner.release()


def _apply_locked(paths, env, only, force, wait_seconds, poll, keep_mode_files, allow_mismatch, machine, tmux_factory, socket, folder,
                  term_wait, record):
    state = survey(paths, env, machine=machine, tmux_factory=tmux_factory, socket=socket, folder=folder)
    chosen = [view for view in state['agents'] if only is None or view['agent_id'] == only]
    if only is not None and not chosen:
        raise CutoverError('unknown_agent', f'No registered agent has the id {only}')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    document = {'started': stamp, 'only': only, 'force': force, 'results': [], 'stopped_at': None}
    path = record_path(paths, stamp) if record else None
    folder_used = tmux_folder(paths.instance_root, env, folder)
    tmuxes = {}

    def tmux_for(name):
        if name not in tmuxes:
            tmuxes[name] = (tmux_factory or (lambda n: Tmux(n, folder_used, env=env)))(name)
        return tmuxes[name]

    failed = None
    for view in chosen:
        result = {'agent_id': view['agent_id'], 'name': view['name'], 'client': view['client'], 'pid': view['pid'],
                  'conversation_db': view['conversation_db'], 'conversation_process': view['conversation_process'],
                  'model': view['model'], 'effort': view['effort'], 'transcript': view['transcript']}
        if failed is not None:
            result.update(status='not_attempted', done=[], skipped=[])
        else:
            outcome = {'done': [], 'skipped': []}
            try:
                tmux = tmux_for(view['socket']) if view['socket'] else None
                apply_agent(paths, view, machine, tmux, outcome, force=force, wait_seconds=wait_seconds, poll=poll,
                            keep_mode_files=keep_mode_files, allow_mismatch=allow_mismatch, term_wait=term_wait)
                result.update(status='done' if outcome['done'] or outcome['skipped'] else 'nothing_to_do', **outcome)
            except CutoverError as error:
                failed = {'agent_id': view['agent_id'], 'name': view['name'], 'code': error.code, 'message': error.message}
                result.update(status='failed', error={'code': error.code, 'message': error.message}, **outcome)
        document['results'].append(result)
        if path is not None:
            write_record(path, document)
    document['stopped_at'] = failed
    document['record'] = str(path) if path else None
    if path is not None:
        write_record(path, document)
    return document


# ----------------------------------------------------------------------------------------------------------------------
# Output and command line.
# ----------------------------------------------------------------------------------------------------------------------
def render_inspect(state):
    lines = []
    for server in state['servers']:
        lines.append(f'tmux socket {server["socket"]}: ' + ('server running' if server['running'] else 'no server running'))
    for view in state['agents']:
        if not view['session_present'] and not view['leftovers'] and view['client'] == 'none':
            continue
        lines.append('')
        lines.append(f'{view["name"]} ({view["agent_id"]}), {view["scope"]}, profile runtime {view["profile_runtime"] or "-"}')
        lines.append(f'  tmux session {view["session"]}: ' + ('present' if view['session_present'] else 'not present'))
        if view['session_present']:
            lines.append(f'  client: {view["client"]}' + (f', pid {view["pid"]}' if view['pid'] else ''))
            if view['command']:
                lines.append(f'  command: {view["command"]}')
            lines.append(f'  conversation: database {view["conversation_db"] or "-"}; process {view["conversation_process"] or "-"}'
                         + (f' ({view["conversation_source"]})' if view['conversation_source'] else ''))
            lines.append(f'  model: {view["model"] or "not given"}' + (f' (last used {view["observed_model"]})' if view['observed_model'] else '')
                         + f'; effort: {view["effort"] or "not given"}; turn: {view["turn"] or "-"}')
        for kind in ('problems', 'warnings'):
            for item in view[kind]:
                lines.append(f'  {kind[:-1]}: {item}')
        for step in view['plan']:
            lines.append(f'  apply would: {step}')
    if state['owner_shell_sessions']:
        lines += ['', 'owner shell sessions (not agents, left alone): ' + ', '.join(item['session'] for item in state['owner_shell_sessions'])]
    if state['other_sessions']:
        lines += ['', 'other sessions in this tmux server (left alone): ' + ', '.join(item['session'] for item in state['other_sessions'])]
    return '\n'.join(lines).strip()


def render_apply(document):
    lines = []
    for result in document['results']:
        lines.append(f'{result["name"]}: {result["status"]}')
        for item in result.get('done', []):
            lines.append(f'  done: {item}')
        for item in result.get('skipped', []):
            lines.append(f'  skipped: {item}')
        if result.get('error'):
            lines.append(f'  FAILED: {result["error"]["code"]}: {result["error"]["message"]}')
    if document['stopped_at']:
        lines.append('')
        lines.append(f'Stopped at {document["stopped_at"]["name"]}; later agents were not touched. Fix the cause and run again: '
                     'agents already done are skipped.')
    elif document['results']:
        lines.append('')
        lines.append('All agents are cut over. The conversations stay in the database; the first input to each agent resumes its conversation.')
    if document.get('record'):
        lines.append(f'Record: {document["record"]}')
    return '\n'.join(lines)


def main(argv=None, *, env=None, machine=None, tmux_factory=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['inspect', 'apply'])
    add_instance_arguments(parser)
    parser.add_argument('--json', action='store_true', help='print one JSON document instead of the plain summary')
    parser.add_argument('--only', metavar='AGENT_ID', help='apply to this one agent only')
    parser.add_argument('--dry-run', action='store_true', help='apply: report what would be done and change nothing (same as inspect)')
    parser.add_argument('--force', action='store_true',
                        help='apply: end a client that has a turn running, kill one that ignores SIGTERM, end a pane that holds other processes')
    parser.add_argument('--wait-seconds', type=float, default=300.0, metavar='N', help='apply: how long to wait for a running turn (default 300)')
    parser.add_argument('--allow-conversation-mismatch', action='store_true',
                        help='apply: go on although the process and the database name different conversations (the database one is resumed)')
    parser.add_argument('--keep-mode-files', action='store_true', help='apply: leave each agent\'s mode.json in place')
    parser.add_argument('--tmux-socket', help='the installation\'s own socket name (refused when it is not the configured one)')
    parser.add_argument('--tmux-tmpdir', help='the folder holding the installation\'s tmux sockets (default: TMUX_TMPDIR, or <root>/.run)')
    options = parser.parse_args(argv)
    env = dict(os.environ if env is None else env)
    status, payload = 0, None
    try:
        paths = instance_paths_from_arguments(options, required=())
        common = dict(machine=machine, tmux_factory=tmux_factory, socket=options.tmux_socket, folder=options.tmux_tmpdir)
        if options.command == 'inspect' or options.dry_run:
            state = survey(paths, env, **common)
            state['agents'] = [v for v in state['agents'] if options.only in (None, v['agent_id'])]
            payload = {'ok': True, 'mode': 'inspect', 'servers': state['servers'], 'agents': [public(v) for v in state['agents']],
                       'owner_shell_sessions': state['owner_shell_sessions'], 'other_sessions': state['other_sessions']}
            text = render_inspect(state)
        else:
            document = run_apply(paths, env, only=options.only, force=options.force, wait_seconds=options.wait_seconds,
                                 keep_mode_files=options.keep_mode_files, allow_mismatch=options.allow_conversation_mismatch, **common)
            status = 1 if document['stopped_at'] else 0
            payload = {'ok': status == 0, 'mode': 'apply', **document}
            text = render_apply(document)
    except CutoverError as error:
        status, payload, text = 1, {'ok': False, 'error': {'code': error.code, 'message': error.message}}, f'{error.code}: {error.message}'
    except DomainError as error:
        code = 'service_running' if error.code == 'instance_in_use' else error.code
        message = ('The service owns this installation; stop it first' if code == 'service_running' else str(error))
        status, payload, text = 1, {'ok': False, 'error': {'code': code, 'message': message}}, f'{code}: {message}'
    except (OSError, sqlite3.Error, ValueError) as error:
        status, payload, text = 1, {'ok': False, 'error': {'code': 'invalid_request', 'message': str(error)}}, f'invalid_request: {error}'
    sys.stdout.write((json.dumps(payload, indent=1, sort_keys=True) if options.json else text) + '\n')
    return status


if __name__ == '__main__':
    raise SystemExit(main())
