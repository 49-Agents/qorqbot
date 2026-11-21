"""The one-time move of terminal-mode agents to managed sessions (``qorqut/migration/managed_cutover.py``).

A fake tmux server (an injected runner) and fake processes (a ``Machine`` stand-in with a clock) replace the machine; the instance
is a real folder with a small real SQLite store. Nothing here starts a process or touches a real tmux server. The same tool was also
rehearsed against real tmux, Claude Code and Codex (see the package report).
"""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import unittest
from unittest import mock

from qorqut.instance_paths import InstancePaths
from qorqut.managed_runtime import mode as runtime_mode, options as launch_options
from qorqut.migration import managed_cutover as cutover
from qorqut_operations.contracts import DomainError

CONV_A = '11111111-1111-4111-8111-111111111111'
CONV_B = '22222222-2222-4222-8222-222222222222'
CONV_C = '33333333-3333-4333-8333-333333333333'
OTHER_CONV = '44444444-4444-4444-8444-444444444444'
AGENTS = (  # id, name, project slug (None = global), profile runtime, db conversation, db runtime
    ('agent-a', 'ordubashy', None, 'claude-code', CONV_A, 'claude-code'),
    ('agent-b', 'dev', 'acme', 'codex', CONV_B, 'codex'),
    ('agent-c', 'qa', 'acme', 'codex', CONV_C, 'claude-code'),   # profile says codex, Claude Code really runs
)


class FakeMachine(cutover.Machine):
    """Processes with a clock: ``sleep`` advances time and makes scheduled deaths happen."""

    def __init__(self):
        self.procs, self.clock, self.signals, self.later, self.stubborn = {}, 0.0, [], [], set()
        self.term_delay = 1.0
        self.hooks = []
        self.survivors = set()      # children that outlive their parent (reparented to init) instead of ending with it

    def add(self, pid, ppid, argv, *, comm=None, cwd='/', environ=None, files=(), exe=None, sid=None, uid=None):
        self.procs[pid] = {'ppid': ppid, 'argv': list(argv), 'comm': comm or Path(argv[0]).name, 'cwd': cwd, 'start': str(1000 + pid),
                           'environ': environ or {}, 'files': list(files), 'exe': exe or comm or Path(argv[0]).name,
                           'sid': pid if sid is None else sid, 'uid': os.getuid() if uid is None else uid}
        return pid

    def session(self, pid):
        return self._proc(pid)['sid']

    def owner(self, pid):
        return self._proc(pid)['uid']

    def _proc(self, pid):
        if pid not in self.procs:
            raise ProcessLookupError(pid)
        return self.procs[pid]

    def pids(self):
        return list(self.procs)

    def parent(self, pid):
        return self._proc(pid)['ppid']

    def argv(self, pid):
        return list(self._proc(pid)['argv'])

    def comm(self, pid):
        return self._proc(pid)['comm']

    def exe(self, pid):
        return self._proc(pid)['exe']

    def cwd(self, pid):
        return self._proc(pid)['cwd']

    def environ(self, pid):
        return dict(self._proc(pid)['environ'])

    def start(self, pid):
        return self._proc(pid)['start']

    def zombie(self, pid):
        self._proc(pid)
        return False

    def open_files(self, pid):
        return list(self._proc(pid)['files'])

    def kill_tree(self, pid):
        for child in [p for p, info in self.procs.items() if info['ppid'] == pid]:
            if child in self.survivors:
                self.procs[child]['ppid'] = 1
            else:
                self.kill_tree(child)
        self.procs.pop(pid, None)

    def signal(self, pid, number):
        self._proc(pid)
        self.signals.append((pid, number))
        if number == signal.SIGKILL:
            self.kill_tree(pid)
        elif number == signal.SIGTERM and pid not in self.stubborn:
            self.later.append((self.clock + self.term_delay, pid))

    def sleep(self, seconds):
        self.clock += seconds
        for item in [item for item in self.later if item[0] <= self.clock]:
            self.later.remove(item)
            self.kill_tree(item[1])
        for hook in list(self.hooks):
            hook(self)

    def now(self):
        return self.clock


class FakeServer:
    """One tmux server's answers to list-sessions, list-panes and kill-session; every command line is kept."""

    def __init__(self, machine, socket):
        self.machine, self.socket, self.sessions, self.calls, self.next = machine, socket, {}, [], 1

    def add(self, name, shell_pid):
        ident = f'${self.next}'
        self.next += 1
        self.sessions[ident] = {'name': name, 'panes': [shell_pid]}
        return ident

    def runner(self, argv, env):
        self.calls.append(list(argv))
        assert argv[1:3] == ['-L', self.socket], f'a command left the installation\'s own server: {argv}'
        command = argv[3:]
        if command[0] == 'list-sessions':
            if not self.sessions:
                return 1, '', 'no server running on /tmp/tmux-1000/' + self.socket
            return 0, ''.join(f'{ident}\t{info["name"]}\n' for ident, info in self.sessions.items()), ''
        if command[0] == 'list-panes':
            info = self.sessions.get(command[command.index('-t') + 1])
            return (0, ''.join(f'{pid}\n' for pid in info['panes']), '') if info else (1, '', "can't find session")
        if command[0] == 'kill-session':
            info = self.sessions.pop(command[command.index('-t') + 1], None)
            if info is None:
                return 1, '', "can't find session"
            for pid in info['panes']:
                self.machine.kill_tree(pid)
            return 0, '', ''
        return 1, '', 'unsupported in the fake'

    def kills(self):
        return [call for call in self.calls if 'kill-session' in call]


class Installation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(os.path.realpath(self.directory.name)) / 'instance'
        (self.root / 'operations/.runtime').mkdir(parents=True)
        self.paths = InstancePaths(self.root, {})
        self.cfg = Path(self.directory.name) / 'claude-home'
        (self.cfg / 'sessions').mkdir(parents=True)
        self.codex = Path(self.directory.name) / 'codex-home'
        (self.codex / 'thread-writer-locks').mkdir(parents=True)
        self.machine = FakeMachine()
        self.server = FakeServer(self.machine, 'qorqut')
        self.env = {'PATH': os.environ.get('PATH', ''), 'QORQUT_TMUX_SOCKET': 'qorqut'}
        self.build_database()
        self.workspace = {agent[0]: self.make_workspace(agent) for agent in AGENTS}

    # -- the installation ----------------------------------------------------------------------------------------------
    def build_database(self):
        with sqlite3.connect(self.paths.database) as db:
            db.executescript('''
                CREATE TABLE projects(id TEXT PRIMARY KEY, slug TEXT);
                CREATE TABLE agent_profiles(agent_id TEXT PRIMARY KEY, name TEXT, home_project_id TEXT, status TEXT, profile_json TEXT);
                CREATE TABLE native_session_owners(agent_id TEXT PRIMARY KEY, consumer_id TEXT, binding_json TEXT, generation INTEGER,
                                                   desired_state TEXT);
                INSERT INTO projects VALUES('project-acme', 'acme');''')
            for agent_id, name, slug, runtime, conversation, bound in AGENTS:
                db.execute('INSERT INTO agent_profiles VALUES(?,?,?,?,?)',
                           (agent_id, name, None if slug is None else 'project-acme', 'active', json.dumps({'runtime': runtime})))
                db.execute('INSERT INTO native_session_owners VALUES(?,?,?,?,?)',
                           (agent_id, conversation, json.dumps({'runtime': bound}), 1, 'running'))

    def make_workspace(self, agent):
        from qorqut.composition.locations import agent_workspace_of
        path = agent_workspace_of(self.paths, agent[0], None if agent[2] is None else 'project-acme', agent[2])
        (path / '.qorqut/zsh').mkdir(parents=True)
        for name in ('agent-shell.bash', 'zsh/.zshenv', 'zsh/.zshrc'):
            (path / '.qorqut' / name).write_text('# Generated Qorqut agent shell; keep personal shell setup.\n')
        (path / '.qorqut/claude-launch-settings.json').write_text('{}')
        (path / '.qorqut/terminal-chat.json').write_text('{}')
        return path

    def database_digest(self):
        return hashlib.sha256(Path(self.paths.database).read_bytes()).hexdigest()

    def state_files(self):
        found = {}
        for name in ('instance',):
            for path in sorted((self.root.parent / name).rglob('*')):
                if path.is_file() and path.suffix != '.sqlite3' or path.name == 'operations.sqlite3':
                    found[str(path)] = path.read_bytes()
        return found

    # -- live agents ---------------------------------------------------------------------------------------------------
    SESSIONS = {'agent-a': 'global___agent-a', 'agent-b': 'acme__dev', 'agent-c': 'acme__qa'}

    def live(self, agent_id, client, *, shell=500, argv=None, conversation=None, turn='idle', cwd=None, session=None):
        """A tmux session for the agent: shell pid, the client under it, and the files the client keeps."""
        index = [agent[0] for agent in AGENTS].index(agent_id)
        shell_pid = shell + index * 10
        workspace = str(self.workspace[agent_id])
        self.machine.add(shell_pid, 1, ['bash', '--rcfile', '.qorqut/agent-shell.bash', '-i'], comm='bash', cwd=workspace)  # session leader
        pid = shell_pid + 1
        conversation = conversation or dict((a[0], a[4]) for a in AGENTS)[agent_id]
        if client == 'claude':
            argv = argv or ['/opt/claude', '--settings', workspace + '/.qorqut/claude-launch-settings.json', '--model', 'claude-opus-5-5',
                            '--effort', 'medium', '--resume', conversation]
            self.machine.add(pid, shell_pid, argv, comm='claude', cwd=cwd or workspace, environ={'CLAUDE_CONFIG_DIR': str(self.cfg)}, sid=shell_pid)
            self.claude_status(pid, conversation, turn)
            (self.cfg / 'projects/p').mkdir(parents=True, exist_ok=True)
            (self.cfg / 'projects/p' / f'{conversation}.jsonl').write_text(
                json.dumps({'type': 'assistant', 'message': {'model': 'claude-opus-5-5', 'content': []}}) + '\n')
        elif client == 'codex':
            argv = argv or ['/opt/codex', '-m', 'gpt-5.6-luna', '-c', 'model_reasoning_effort="high"']
            rollout = self.codex / f'sessions/2026/11/19/rollout-2026-11-19T10-00-00-{conversation}.jsonl'
            rollout.parent.mkdir(parents=True, exist_ok=True)
            self.codex_turn(rollout, turn)
            self.machine.add(pid, shell_pid, argv, comm='codex', cwd=cwd or workspace, environ={'CODEX_HOME': str(self.codex)}, sid=shell_pid,
                             files=[str(self.codex / f'thread-writer-locks/{conversation}.lock'), str(rollout)])
        elif client == 'other':
            self.machine.add(pid, shell_pid, argv or ['vim', 'notes.txt'], comm='vim', cwd=workspace)
        self.session_ids = getattr(self, 'session_ids', {})
        self.session_ids[agent_id] = self.server.add(session or self.SESSIONS[agent_id], shell_pid)
        return shell_pid, pid

    def claude_status(self, pid, conversation, status):
        (self.cfg / 'sessions' / f'{pid}.json').write_text(json.dumps(
            {'pid': pid, 'sessionId': conversation, 'procStart': self.machine.procs[pid]['start'] if pid in self.machine.procs else '0',
             'status': 'busy' if status == 'running' else 'idle'}))

    @staticmethod
    def codex_turn(rollout, turn):
        events = [{'type': 'event_msg', 'payload': {'type': 'task_started'}}, {'type': 'turn_context', 'payload': {'model': 'gpt-5.6-luna'}}]
        if turn != 'running':
            events.append({'type': 'event_msg', 'payload': {'type': 'task_complete'}})
        rollout.write_text(''.join(json.dumps(item) + '\n' for item in events))

    # -- running the tool ----------------------------------------------------------------------------------------------
    def factory(self, name):
        self.asked = getattr(self, 'asked', [])
        self.asked.append(name)
        assert name == 'qorqut', 'only the installation\'s own server may be addressed'
        return cutover.Tmux(name, None, runner=self.server.runner, executable='tmux', env={})

    def survey(self, **extra):
        return cutover.survey(self.paths, self.env, machine=self.machine, tmux_factory=self.factory, socket='qorqut', **extra)

    def apply(self, **extra):
        defaults = dict(machine=self.machine, tmux_factory=self.factory, wait_seconds=30.0, poll=1.0, lock=False)
        defaults.update(extra)
        return cutover.run_apply(self.paths, self.env, **defaults)

    def view(self, state, agent_id):
        return next(view for view in state['agents'] if view['agent_id'] == agent_id)


class CommandLines(unittest.TestCase):
    def test_claude_model_effort_and_resume_in_every_spelling(self):
        for argv, expected in (
                (['--model', 'claude-opus-5-5', '--effort', 'medium', '--resume', CONV_A], ('claude-opus-5-5', 'medium', CONV_A)),
                (['--model=sonnet', '--effort=low', f'--resume={CONV_B}'], ('sonnet', 'low', CONV_B)),
                (['--settings', '/w/s.json'], (None, None, None)),
                (['--settings', '/w/s.json', '-r', CONV_C], (None, None, CONV_C))):
            found = cutover.claude_options(argv)
            self.assertEqual((found['model'], found['effort'], found['resume']), expected, argv)
        self.assertTrue(cutover.claude_options(['-p', '--model', 'x'])['headless'])

    def test_codex_model_effort_and_subcommands(self):
        found = cutover.codex_options(['-m', 'gpt-5.6-luna', '-c', 'model_reasoning_effort="high"', '-c', 'check_for_update_on_startup=false'])
        self.assertEqual((found['model'], found['effort'], found['headless']), ('gpt-5.6-luna', 'high', False))
        found = cutover.codex_options(['-c', 'model="gpt-6-astra"', 'resume', CONV_A])
        self.assertEqual((found['model'], found['resume'], found['headless']), ('gpt-6-astra', CONV_A, False))
        for sub in ('app-server', 'exec', 'queue', 'mcp-server'):
            self.assertTrue(cutover.codex_options([sub, '--listen', 'stdio://'])['headless'], sub)

    def test_the_socket_names_are_the_installations_own(self):
        self.assertEqual(cutover.own_sockets({}), ['qorqut', 'bazarlyk'])
        self.assertEqual(cutover.own_sockets({'QORQUT_TMUX_SOCKET': 'mine'}), ['mine'])
        with self.assertRaises(cutover.CutoverError):
            cutover.own_sockets({'QORQUT_TMUX_SOCKET': '../x'})


class Inspect(Installation):
    def test_reports_each_agent_and_changes_nothing(self):
        self.live('agent-a', 'claude')
        self.live('agent-b', 'codex')
        self.live('agent-c', 'claude', argv=['/opt/claude', '--settings', 'x'])                 # started without model and effort
        self.machine.add(900, 1, ['bash'], comm='bash')
        self.server.add('global__workspace', 900)
        self.server.add('someone-elses-session', 900)
        before = (self.database_digest(), self.state_files())
        state = self.survey()
        a, b, c = (self.view(state, key) for key in ('agent-a', 'agent-b', 'agent-c'))
        self.assertEqual((a['client'], a['model'], a['effort'], a['conversation_process'], a['conversation_db'], a['turn']),
                         ('claude', 'claude-opus-5-5', 'medium', CONV_A, CONV_A, 'idle'))
        self.assertEqual(a['conversation_source'], 'client session file')
        self.assertEqual((b['client'], b['model'], b['effort'], b['conversation_process'], b['conversation_source']),
                         ('codex', 'gpt-5.6-luna', 'high', CONV_B, 'client lock file'))
        self.assertEqual(b['observed_model'], 'gpt-5.6-luna')
        self.assertEqual((c['client'], c['model'], c['effort']), ('claude', None, None))
        self.assertTrue(any('profile says codex but claude runs' in item for item in c['warnings']))
        self.assertEqual([item['session'] for item in state['owner_shell_sessions']], ['global__workspace'])
        self.assertEqual([item['session'] for item in state['other_sessions']], ['someone-elses-session'])
        self.assertTrue(all('apply would' not in step for step in a['plan']))
        self.assertEqual(a['plan'][0], 'write options client=claude, model=claude-opus-5-5, effort=medium')
        self.assertEqual((self.database_digest(), self.state_files()), before)
        self.assertEqual(self.server.kills(), [])
        self.assertEqual(self.machine.signals, [])

    def test_a_missing_session_and_a_missing_client_are_told_apart(self):
        self.live('agent-a', 'claude')
        self.machine.add(520, 1, ['bash'], comm='bash')
        self.server.add('acme__dev', 520)                      # a session with only a shell in it
        state = self.survey()
        self.assertFalse(self.view(state, 'agent-c')['session_present'])
        self.assertEqual(self.view(state, 'agent-b')['client'], 'none')
        self.assertIn('no client runs in the session: nothing to record', self.view(state, 'agent-b')['plan'])

    def test_conversation_mismatch_and_unrecognised_pane_are_problems(self):
        self.live('agent-a', 'claude', conversation=OTHER_CONV)                  # the database says CONV_A
        self.live('agent-b', 'other')
        state = self.survey()
        self.assertTrue(any(item.startswith('conversation_mismatch') for item in self.view(state, 'agent-a')['problems']))
        self.assertEqual(self.view(state, 'agent-b')['client'], 'unknown')
        self.assertIn('unrecognised_process_in_pane', self.view(state, 'agent-b')['problems'])

    def test_a_turn_in_progress_is_seen_for_both_clients(self):
        self.live('agent-a', 'claude', turn='running')
        self.live('agent-b', 'codex', turn='running')
        state = self.survey()
        self.assertEqual((self.view(state, 'agent-a')['turn'], self.view(state, 'agent-b')['turn']), ('running', 'running'))

    def test_a_headless_claude_in_a_pane_is_not_the_client(self):
        shell, _ = self.live('agent-a', 'claude')
        self.machine.add(777, shell, ['/opt/claude', '-p', '--resume', CONV_A], comm='claude')
        state = self.survey()
        view = self.view(state, 'agent-a')
        self.assertEqual(view['client'], 'claude')
        self.assertEqual(view['pid'], shell + 1)
        self.assertTrue(any('-p' in item for item in view['other_processes']))


class Apply(Installation):
    def test_cuts_every_agent_over(self):
        self.live('agent-a', 'claude')
        self.live('agent-b', 'codex')
        self.live('agent-c', 'claude', argv=['/opt/claude', '--settings', 'x'])
        self.machine.add(900, 1, ['bash'], comm='bash')
        owner = self.server.add('global__workspace', 900)
        before = self.database_digest()
        document = self.apply()
        self.assertIsNone(document['stopped_at'])
        self.assertEqual([r['status'] for r in document['results']], ['done', 'done', 'done'])
        self.assertEqual(launch_options.read(self.paths, 'agent-a').public(), {'client': 'claude', 'model': 'claude-opus-5-5', 'effort': 'medium'})
        self.assertEqual(launch_options.read(self.paths, 'agent-b').public(), {'client': 'codex', 'model': 'gpt-5.6-luna', 'effort': 'high'})
        self.assertEqual(launch_options.read(self.paths, 'agent-c').public(), {'client': 'claude', 'model': None, 'effort': None})
        opts = launch_options.options_file(self.paths, 'agent-a')
        self.assertEqual(opts.stat().st_mode & 0o777, 0o600)
        self.assertEqual([sig for _, sig in self.machine.signals], [signal.SIGTERM] * 3)         # nothing harsher
        self.assertEqual({pid for pid, _ in self.machine.signals}, {501, 511, 521})
        self.assertEqual(list(self.server.sessions), [owner])                                     # the owner's own shell session stays
        self.assertIn(900, self.machine.procs)
        for agent in AGENTS:
            ws = self.workspace[agent[0]]
            self.assertFalse((ws / '.qorqut/agent-shell.bash').exists() or (ws / '.qorqut/zsh').exists())
            self.assertTrue((ws / '.qorqut/claude-launch-settings.json').exists() and (ws / '.qorqut/terminal-chat.json').exists())
        self.assertEqual(self.database_digest(), before)                                          # no row edited
        record = json.loads(Path(document['record']).read_text())
        self.assertEqual({r['agent_id']: r['conversation_process'] for r in record['results']},
                         {'agent-a': CONV_A, 'agent-b': CONV_B, 'agent-c': CONV_C})

    def test_the_client_is_ended_before_its_session_and_its_exit_is_verified_by_start_time(self):
        self.live('agent-a', 'claude')
        order = []
        real_signal, real_kill = self.machine.signal, self.server.runner

        def noting_signal(pid, number):
            order.append('signal')
            real_signal(pid, number)

        def noting_runner(argv, env):
            if 'kill-session' in argv:
                order.append('kill-session')
                self.assertNotIn(501, self.machine.procs, 'the session was ended while the client still ran')
            return real_kill(argv, env)
        self.machine.signal, self.server.runner = noting_signal, noting_runner
        self.apply(only='agent-a')
        self.assertEqual(order, ['signal', 'kill-session'])

    def test_a_values_the_client_was_not_given_stores_nothing_and_never_clears(self):
        launch_options.write(self.paths, 'agent-c', model='kept-model')
        self.live('agent-c', 'claude', argv=['/opt/claude', '--settings', 'x'])
        self.apply(only='agent-c')
        self.assertEqual(launch_options.read(self.paths, 'agent-c').public(), {'client': 'claude', 'model': 'kept-model', 'effort': None})

    def test_a_stored_effort_and_model_are_never_cleared_by_a_client_that_states_neither(self):
        launch_options.write(self.paths, 'agent-c', model='kept-model', effort='high')
        self.live('agent-c', 'claude', argv=['/opt/claude', '--settings', 'x'])
        self.apply(only='agent-c')
        self.assertEqual(launch_options.read(self.paths, 'agent-c').public(), {'client': 'claude', 'model': 'kept-model', 'effort': 'high'})

    def test_a_client_that_states_an_effort_replaces_only_that_field(self):
        launch_options.write(self.paths, 'agent-c', model='kept-model', effort='low')
        self.live('agent-c', 'claude', argv=['/opt/claude', '--effort', 'max'])
        self.apply(only='agent-c')
        self.assertEqual(launch_options.read(self.paths, 'agent-c').public(), {'client': 'claude', 'model': 'kept-model', 'effort': 'max'})

    def test_dry_run_through_the_command_line_changes_nothing(self):
        self.live('agent-a', 'claude')
        before = (self.database_digest(), self.state_files())
        out = io.StringIO()
        with redirect_stdout(out):
            status = cutover.main(['apply', '--dry-run', '--json', '--instance-root', str(self.root), '--tmux-socket', 'qorqut'],
                                  env=self.env, machine=self.machine, tmux_factory=self.factory)
        payload = json.loads(out.getvalue())
        self.assertEqual((status, payload['ok'], payload['mode']), (0, True, 'inspect'))
        self.assertEqual((self.database_digest(), self.state_files()), before)
        self.assertEqual((self.machine.signals, self.server.kills()), ([], []))

    def test_only_one_agent(self):
        self.live('agent-a', 'claude')
        self.live('agent-b', 'codex')
        document = self.apply(only='agent-b')
        self.assertEqual([r['agent_id'] for r in document['results']], ['agent-b'])
        self.assertIn(self.session_ids['agent-a'], self.server.sessions)
        self.assertTrue((self.workspace['agent-a'] / '.qorqut/agent-shell.bash').exists())
        with self.assertRaises(cutover.CutoverError) as caught:
            self.apply(only='nobody')
        self.assertEqual(caught.exception.code, 'unknown_agent')

    def test_keep_mode_files_and_the_mode_file_otherwise_goes(self):
        mode = cutover._mode_file(self.paths, 'agent-a')
        mode.parent.mkdir(parents=True)
        mode.write_text('{"mode": "managed"}')
        self.apply(keep_mode_files=True)
        self.assertTrue(mode.exists())
        self.apply()
        self.assertFalse(mode.exists())

    def test_a_generated_file_that_was_edited_by_hand_is_left(self):
        (self.workspace['agent-a'] / '.qorqut/agent-shell.bash').write_text('my own file\n')
        self.apply()
        self.assertEqual((self.workspace['agent-a'] / '.qorqut/agent-shell.bash').read_text(), 'my own file\n')

    def test_a_session_with_only_a_shell_is_ended_and_nothing_recorded(self):
        self.machine.add(520, 1, ['bash'], comm='bash')
        self.server.add('acme__dev', 520)
        document = self.apply(only='agent-b')
        self.assertEqual(document['results'][0]['skipped'], ['no client in the session'])
        self.assertEqual(launch_options.read(self.paths, 'agent-b').public(), {'client': None, 'model': None, 'effort': None})
        self.assertEqual(self.server.sessions, {})

    def test_the_fallback_writer_makes_the_same_file(self):
        written = cutover._write_options_fallback(self.paths, 'agent-a', {'client': 'claude', 'model': 'm'})
        self.assertEqual(written, {'client': 'claude', 'model': 'm', 'effort': None})
        self.assertEqual(launch_options.read(self.paths, 'agent-a').public(), written)


class PartialFailureAndRerun(Installation):
    def test_stops_at_the_first_failure_reports_it_and_a_rerun_finishes(self):
        # agents are taken in name order: dev (agent-b), ordubashy (agent-a), qa (agent-c)
        shell_b, pid_b = self.live('agent-a', 'claude')
        self.live('agent-b', 'codex')
        self.live('agent-c', 'claude')
        self.machine.stubborn.add(pid_b)                        # the second agent's client ignores SIGTERM
        document = self.apply(term_wait=5.0)
        statuses = {r['agent_id']: r['status'] for r in document['results']}
        self.assertEqual(statuses, {'agent-b': 'done', 'agent-a': 'failed', 'agent-c': 'not_attempted'})
        self.assertEqual(document['stopped_at']['code'], 'client_did_not_end')
        failed = document['results'][1]
        self.assertEqual(failed['error']['code'], 'client_did_not_end')
        self.assertIn('options written', failed['done'])         # what was done before the failure is reported
        self.assertIn(f'SIGTERM to pid {pid_b}', failed['done'])
        self.assertEqual(self.machine.signals.count((pid_b, signal.SIGKILL)), 0)                   # never harsher without --force
        self.assertIn(pid_b, self.machine.procs)
        self.assertIn(self.session_ids['agent-a'], self.server.sessions)                           # its session was not ended
        self.assertIn(self.session_ids['agent-c'], self.server.sessions)                           # the next agent was not touched
        self.assertEqual(launch_options.read(self.paths, 'agent-c').public()['client'], None)
        self.assertTrue((self.workspace['agent-c'] / '.qorqut/agent-shell.bash').exists())
        # fixed: the client now ends; the rerun skips what is done and finishes the rest
        self.machine.stubborn.clear()
        again = self.apply()
        statuses = {r['agent_id']: r['status'] for r in again['results']}
        self.assertEqual(statuses, {'agent-b': 'nothing_to_do', 'agent-a': 'done', 'agent-c': 'done'})
        self.assertIsNone(again['stopped_at'])
        self.assertEqual(self.server.sessions, {})
        third = self.apply()
        self.assertEqual({r['status'] for r in third['results']}, {'nothing_to_do'})

    def test_force_kills_a_client_that_ignores_sigterm_and_says_so(self):
        _, pid = self.live('agent-a', 'claude')
        self.machine.stubborn.add(pid)
        document = self.apply(only='agent-a', force=True, term_wait=5.0)
        self.assertEqual(document['results'][0]['status'], 'done')
        self.assertEqual([sig for _, sig in self.machine.signals], [signal.SIGTERM, signal.SIGKILL])
        self.assertTrue(any('SIGKILL' in item and '--force' in item for item in document['results'][0]['done']))

    def test_a_session_that_is_already_gone_is_not_an_error(self):
        _, pid = self.live('agent-a', 'claude')
        state = self.survey()
        self.server.sessions.clear()                            # ended by hand between inspect and apply
        self.machine.kill_tree(self.machine.procs[pid]['ppid'])
        view = self.view(state, 'agent-a')
        outcome = {'done': [], 'skipped': []}
        view['_client'] = None
        view['client'] = 'none'
        cutover.apply_agent(self.paths, view, self.machine, self.factory('qorqut'), outcome, force=False, wait_seconds=1, poll=1,
                            keep_mode_files=False, allow_mismatch=False)
        self.assertIn('tmux session already gone', outcome['done'])


class TurnInProgress(Installation):
    def test_waits_for_the_turn_and_then_ends_the_client(self):
        _, pid = self.live('agent-a', 'claude', turn='running')

        def finishes(machine):
            if machine.clock >= 4 and pid in machine.procs:
                self.claude_status(pid, CONV_A, 'idle')
        self.machine.hooks.append(finishes)
        document = self.apply(only='agent-a', wait_seconds=20.0)
        done = document['results'][0]['done']
        self.assertEqual(document['results'][0]['status'], 'done')
        self.assertLess(done.index('turn finished'), done.index(f'SIGTERM to pid {pid}'))
        self.assertGreaterEqual(self.machine.clock, 4)

    def test_without_force_a_turn_that_does_not_finish_is_never_cut_off(self):
        _, pid = self.live('agent-b', 'codex', turn='running')
        document = self.apply(only='agent-b', wait_seconds=5.0)
        self.assertEqual(document['results'][0]['error']['code'], 'turn_in_progress')
        self.assertEqual(self.machine.signals, [])
        self.assertIn(pid, self.machine.procs)
        self.assertEqual(self.server.kills(), [])

    def test_force_cuts_a_running_turn_off(self):
        _, pid = self.live('agent-b', 'codex', turn='running')
        document = self.apply(only='agent-b', force=True)
        self.assertEqual(document['results'][0]['status'], 'done')
        self.assertIn('turn in progress cut off (--force)', document['results'][0]['done'])
        self.assertEqual(self.machine.signals, [(pid, signal.SIGTERM)])


class WrongProcess(Installation):
    def assert_untouched(self, document, code):
        self.assertEqual(document['results'][0]['error']['code'], code)
        self.assertEqual(self.machine.signals, [])
        self.assertEqual(self.server.kills(), [])
        self.assertEqual(launch_options.read(self.paths, document['results'][0]['agent_id']).public()['client'], None)

    def test_a_pane_that_runs_something_else_is_refused(self):
        self.live('agent-a', 'other')
        document = self.apply(only='agent-a', force=True)                    # --force does not make an unknown program a client
        self.assert_untouched(document, 'refused')
        self.assertIn('unrecognised_process_in_pane', document['results'][0]['error']['message'])

    def test_a_client_that_does_not_live_in_the_agents_workspace_is_refused(self):
        self.live('agent-a', 'claude', cwd='/home/owner/other-project')
        self.assert_untouched(self.apply(only='agent-a'), 'refused')

    def test_two_clients_in_one_session_are_refused(self):
        shell, _ = self.live('agent-a', 'claude')
        self.machine.add(777, shell, ['/opt/codex'], comm='codex', cwd=str(self.workspace['agent-a']))
        self.assert_untouched(self.apply(only='agent-a'), 'refused')

    def test_a_different_conversation_in_the_process_is_refused_unless_allowed(self):
        _, pid = self.live('agent-a', 'claude', conversation=OTHER_CONV)
        self.assert_untouched(self.apply(only='agent-a'), 'refused')
        document = self.apply(only='agent-a', allow_mismatch=True)
        self.assertEqual(document['results'][0]['status'], 'done')

    def test_a_pid_taken_over_between_inspect_and_signal_is_not_signalled(self):
        _, pid = self.live('agent-a', 'claude')
        state = self.survey()
        view = self.view(state, 'agent-a')
        self.machine.procs[pid]['start'] = '99999'                           # the pid now belongs to another process
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.end_client(self.machine, view, [], force=True, wait_seconds=1, poll=1)
        self.assertEqual(caught.exception.code, 'client_changed')
        self.assertEqual(self.machine.signals, [])

    def test_a_pid_that_now_runs_another_program_is_not_signalled(self):
        _, pid = self.live('agent-a', 'claude')
        view = self.view(self.survey(), 'agent-a')
        self.machine.procs[pid]['argv'] = ['/usr/bin/vim', 'x']
        self.machine.procs[pid]['comm'] = 'vim'
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.end_client(self.machine, view, [], force=True, wait_seconds=1, poll=1)
        self.assertEqual(caught.exception.code, 'client_changed')
        self.assertEqual(self.machine.signals, [])

    def leave_a_job_behind(self, shell, pid):
        self.machine.hooks.append(lambda m: m.procs.get(pid) is None and 888 not in m.procs and m.add(888, shell, ['make', 'all'], comm='make'))

    def test_another_process_left_in_the_pane_stops_the_session_from_being_ended_without_force(self):
        shell, pid = self.live('agent-a', 'claude')
        self.leave_a_job_behind(shell, pid)
        document = self.apply(only='agent-a')
        self.assertEqual(document['results'][0]['error']['code'], 'other_processes_in_pane')
        self.assertEqual(self.server.kills(), [])
        self.assertEqual(launch_options.read(self.paths, 'agent-a').public()['client'], 'claude')   # the client was recorded and ended
        again = self.apply(only='agent-a')                                  # a rerun still refuses: the owner's job runs there
        self.assertEqual(again['results'][0]['error']['code'], 'refused')
        self.assertEqual(self.server.kills(), [])
        self.machine.kill_tree(888)                                         # once it is gone the rerun finishes
        self.assertEqual(self.apply(only='agent-a')['results'][0]['status'], 'done')

    def test_force_ends_a_session_that_still_holds_another_process(self):
        shell, pid = self.live('agent-a', 'claude')
        self.leave_a_job_behind(shell, pid)
        document = self.apply(only='agent-a', force=True)
        self.assertEqual(document['results'][0]['status'], 'done')
        self.assertEqual(len(self.server.kills()), 1)


class LeftoverProcesses(Installation):
    """What an ended client leaves behind: its own helpers are ended with it, someone else's processes never."""
    BRIDGE = ['python', '-I', '-m', 'qorqut.bootstrap.mcp_reconnect', '--registration', '/x/registration.json']
    DAEMON = ['/home/u/.codex/packages/app-server-daemon/releases/0.1/bin/codex', 'app-server', '--listen', 'unix://', '--managed-daemon']
    UPDATER = ['/home/u/.codex/packages/app-server-daemon/releases/0.1/bin/codex', 'app-server', 'daemon', 'pid-update-loop']

    def signalled(self, pid):
        return [number for target, number in self.machine.signals if target == pid]

    def test_a_helper_that_outlives_its_client_is_ended_by_pid_and_start_time(self):
        shell, client = self.live('agent-a', 'claude')
        bridge = self.machine.add(shell + 5, client, self.BRIDGE, comm='python', sid=shell)
        self.machine.survivors.add(bridge)
        result = self.apply(only='agent-a')
        done = result['results'][0]['done']
        self.assertEqual(result['results'][0]['status'], 'done')
        self.assertNotIn(bridge, self.machine.procs)
        self.assertEqual(self.signalled(bridge), [signal.SIGTERM])
        self.assertTrue(any('leftover process(es) of the client' in item and f'pid {bridge} (python)' in item for item in done), done)
        self.assertLess(done.index(next(item for item in done if 'leftover' in item)), done.index('tmux session ended'))

    def test_an_orphan_of_the_clients_terminal_session_is_found_although_its_parent_is_init(self):
        shell, client = self.live('agent-a', 'claude')
        orphan = self.machine.add(shell + 5, 1, self.BRIDGE, comm='python', sid=shell)
        self.apply(only='agent-a')
        self.assertNotIn(orphan, self.machine.procs)

    def test_a_process_of_another_session_is_never_touched_whatever_it_is_called(self):
        shell, client = self.live('agent-a', 'claude')
        other_shell, other_client = self.live('agent-b', 'codex')
        look_alike = self.machine.add(900, 1, self.BRIDGE, comm='python', sid=900)               # same program, own session
        sibling = self.machine.add(901, other_client, self.BRIDGE, comm='python', sid=other_shell)  # another agent's helper
        self.apply(only='agent-a')
        self.assertEqual(self.signalled(look_alike) + self.signalled(sibling), [])
        self.assertIn(look_alike, self.machine.procs)
        self.assertIn(sibling, self.machine.procs)

    def test_what_the_owner_left_running_in_the_pane_is_not_the_clients_to_end(self):
        shell, client = self.live('agent-a', 'claude')
        editor = self.machine.add(shell + 7, shell, ['vim', 'notes.txt'], comm='vim', sid=shell)
        result = self.apply(only='agent-a')
        self.assertEqual(self.signalled(editor), [])
        self.assertEqual(result['results'][0]['status'], 'failed')
        self.assertEqual(result['results'][0]['error']['code'], 'other_processes_in_pane')

    def test_a_helper_that_ignores_sigterm_is_killed_only_with_force(self):
        shell, client = self.live('agent-a', 'claude')
        bridge = self.machine.add(shell + 5, 1, self.BRIDGE, comm='python', sid=shell)
        self.machine.stubborn.add(bridge)
        refused = self.apply(only='agent-a', term_wait=8.0)
        self.assertEqual(refused['results'][0]['error']['code'], 'client_did_not_end')
        self.assertIn(bridge, self.machine.procs)
        forced = self.apply(only='agent-a', term_wait=8.0, force=True)
        self.assertNotIn(bridge, self.machine.procs)
        self.assertIn(signal.SIGKILL, self.signalled(bridge))
        self.assertEqual(forced['results'][0]['status'], 'done')

    def daemon(self, parent=1):
        server = self.machine.add(700, parent, self.DAEMON, comm='codex', sid=700)
        updater = self.machine.add(701, parent, self.UPDATER, comm='codex', sid=701)
        return server, updater

    def test_the_codex_daemon_is_reported_by_inspect_and_never_signalled(self):
        server, updater = self.daemon()
        self.live('agent-b', 'codex')
        state = self.survey()
        self.assertEqual([(item['pid'], item['role']) for item in state['shared_processes']], [(700, 'server'), (701, 'update-loop')])
        self.assertEqual(state['shared_processes'][0]['codex_home'], '/home/u/.codex')
        text = cutover.render_inspect(state)
        self.assertIn('Codex background server running: pid 700 (server), pid 701 (update-loop)', text)
        self.assertIn('can be left running', text)
        result = self.apply()
        self.assertEqual(self.signalled(server) + self.signalled(updater), [])
        self.assertEqual([item['pid'] for item in result['shared_processes']], [700, 701])
        self.assertIn('still running', cutover.render_apply(result))

    def test_a_daemon_of_another_user_or_a_codex_that_is_no_daemon_is_not_reported(self):
        self.machine.add(700, 1, self.DAEMON, comm='codex', uid=os.getuid() + 1)
        self.machine.add(701, 1, ['/usr/bin/codex', 'app-server', '--listen', 'stdio://'], comm='codex')
        self.assertEqual(self.survey()['shared_processes'], [])
        self.assertNotIn('Codex background server', cutover.render_inspect(self.survey()))


class StoppedHalfWay(Installation):
    def test_a_refused_running_turn_says_the_agents_before_it_are_already_ended(self):
        self.live('agent-b', 'codex')
        self.live('agent-a', 'claude', turn='running')
        self.live('agent-c', 'claude')
        result = self.apply(wait_seconds=3.0)
        self.assertEqual([(item['name'], item['status']) for item in result['results']],
                         [('dev', 'done'), ('ordubashy', 'failed'), ('qa', 'not_attempted')])
        text = cutover.render_apply(result)
        self.assertIn('Already ended earlier in this run, and staying ended (dev)', text)
        self.assertIn('not all-or-nothing', text)

    def test_nothing_is_said_when_the_first_agent_is_the_one_refused(self):
        self.live('agent-b', 'codex', turn='running')
        self.live('agent-a', 'claude')
        text = cutover.render_apply(self.apply(wait_seconds=3.0))
        self.assertIn('FAILED: turn_in_progress', text)
        self.assertNotIn('Already ended earlier', text)


class ForeignServers(Installation):
    def test_a_socket_that_is_not_the_installations_is_refused_before_anything_runs(self):
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.survey(self.paths, self.env, machine=self.machine, tmux_factory=self.factory, socket='default')
        self.assertEqual(caught.exception.code, 'foreign_tmux_server')
        self.assertEqual(self.server.calls, [])
        out = io.StringIO()
        with redirect_stdout(out):
            status = cutover.main(['apply', '--json', '--instance-root', str(self.root), '--tmux-socket', 'default'], env=self.env,
                                  machine=self.machine, tmux_factory=self.factory)
        self.assertEqual((status, json.loads(out.getvalue())['error']['code']), (1, 'foreign_tmux_server'))

    def test_a_configured_socket_is_the_only_one_used(self):
        asked = []
        server = FakeServer(self.machine, 'mine')

        def factory(name):
            asked.append(name)
            return cutover.Tmux(name, '/some/folder', runner=server.runner, executable='tmux', env={})
        cutover.survey(self.paths, {**self.env, 'QORQUT_TMUX_SOCKET': 'mine'}, machine=self.machine, tmux_factory=factory)
        self.assertEqual(asked, ['mine'])
        self.assertTrue(all(call[:3] == ['tmux', '-L', 'mine'] for call in server.calls))

    def test_the_legacy_socket_is_looked_at_when_nothing_is_configured(self):
        asked = []

        def factory(name):
            asked.append(name)
            return cutover.Tmux(name, None, runner=FakeServer(self.machine, name).runner, executable='tmux', env={})
        cutover.survey(self.paths, {'PATH': ''}, machine=self.machine, tmux_factory=factory)
        self.assertEqual(asked, ['qorqut', 'bazarlyk'])

    def test_a_command_cannot_choose_its_own_server(self):
        tmux = cutover.Tmux('qorqut', None, runner=self.server.runner, executable='tmux', env={})
        for flag in ('-S', '-L'):
            with self.assertRaises(cutover.CutoverError):
                tmux.run(flag, '/tmp/other', 'kill-server')
        with self.assertRaises(cutover.CutoverError):
            cutover.Tmux('../other', None, runner=self.server.runner)
        self.assertEqual(self.server.calls, [])

    def test_only_exact_agent_sessions_are_ended_never_the_owners_or_unknown_ones(self):
        self.live('agent-a', 'claude')
        self.machine.add(900, 1, ['bash'], comm='bash')
        self.server.add('global__workspace', 900)
        self.server.add('global__workspace-2', 900)
        self.server.add('acme__dev-extra', 900)                              # looks like an agent session, is not one
        self.apply()
        self.assertEqual(sorted(info['name'] for info in self.server.sessions.values()),
                         ['acme__dev-extra', 'global__workspace', 'global__workspace-2'])
        for call in self.server.kills():
            self.assertTrue(call[call.index('-t') + 1].startswith('$'), 'a session is ended by its id, not by a pattern')

    def test_the_caller_inside_another_tmux_does_not_redirect_commands(self):
        tmux = cutover.Tmux('qorqut', None, runner=lambda argv, env: (0, '', ''), executable='tmux',
                            env={'TMUX': '/tmp/tmux-1000/default,1,0', 'TMUX_PANE': '%1', 'PATH': '/bin'})
        self.assertNotIn('TMUX', tmux.env)
        self.assertNotIn('TMUX_PANE', tmux.env)

    def test_the_folder_comes_from_the_environment_or_the_folder_installation(self):
        self.assertEqual(cutover.tmux_folder(self.root, {'TMUX_TMPDIR': '/x/y'}), '/x/y')
        self.assertIsNone(cutover.tmux_folder(self.root, {}))
        (self.root.parent / '.run' / f'tmux-{os.getuid()}').mkdir(parents=True)
        self.assertEqual(cutover.tmux_folder(self.root, {}), str(self.root.parent / '.run'))
        self.assertEqual(cutover.tmux_folder(self.root, {}, '/explicit'), '/explicit')


class ServiceMustBeStopped(Installation):
    def test_a_running_service_refuses_the_whole_apply(self):
        self.live('agent-a', 'claude')
        out = io.StringIO()
        with mock.patch.object(cutover, 'InstanceOwnership') as owner, redirect_stdout(out):
            owner.return_value.acquire.side_effect = DomainError('instance_in_use', 'Another backend already owns this instance')
            status = cutover.main(['apply', '--json', '--instance-root', str(self.root), '--tmux-socket', 'qorqut'], env=self.env,
                                  machine=self.machine, tmux_factory=self.factory)
        self.assertEqual((status, json.loads(out.getvalue())['error']['code']), (1, 'service_running'))
        self.assertEqual((self.machine.signals, self.server.kills(), self.server.calls), ([], [], []))

    def test_the_lock_is_held_while_applying_and_released_after(self):
        self.live('agent-a', 'claude')
        with mock.patch.object(cutover, 'InstanceOwnership') as owner:
            cutover.run_apply(self.paths, self.env, machine=self.machine, tmux_factory=self.factory, socket='qorqut',
                              wait_seconds=5, poll=1, only='agent-a')
        owner.return_value.acquire.assert_called_once_with()
        owner.return_value.acquire.return_value.release.assert_called_once_with()

    def test_the_real_lock_refuses_a_second_owner(self):
        from qorqut.service.ownership import InstanceOwnership
        held = InstanceOwnership(self.paths).acquire()
        self.addCleanup(held.release)
        with self.assertRaises(DomainError) as caught:
            cutover.run_apply(self.paths, self.env, machine=self.machine, tmux_factory=self.factory, socket='qorqut')
        self.assertEqual(caught.exception.code, 'instance_in_use')


class TerminalsChosenInSettings(Installation):
    def choose_terminals(self):
        runtime_mode.commit(self.paths, runtime_mode.TERMINAL)
        self.addCleanup(runtime_mode._cache.clear)

    def test_apply_by_hand_refuses_and_touches_nothing(self):
        self.live('agent-a', 'claude')
        self.choose_terminals()
        before = self.database_digest()
        with self.assertRaises(cutover.CutoverError) as caught:
            cutover.run_apply(self.paths, self.env, machine=self.machine, tmux_factory=self.factory, socket='qorqut', lock=False)
        self.assertEqual(caught.exception.code, 'terminal_mode_chosen')
        self.assertEqual(caught.exception.message, 'This installation runs agents in terminals by the owner\'s choice. '
                                                   'Switch to Managed sessions in Settings instead.')
        self.assertEqual((self.machine.signals, self.server.kills(), self.database_digest()), ([], [], before))

    def test_the_command_line_says_it_in_the_same_words(self):
        self.live('agent-a', 'claude')
        self.choose_terminals()
        out = io.StringIO()
        with redirect_stdout(out):
            status = cutover.main(['apply', '--json', '--instance-root', str(self.root), '--tmux-socket', 'qorqut'], env=self.env,
                                  machine=self.machine, tmux_factory=self.factory)
        payload = json.loads(out.getvalue())
        self.assertEqual((status, payload['error']['code']), (1, 'terminal_mode_chosen'))
        self.assertEqual(self.machine.signals, [])

    def test_inspect_still_works(self):
        self.live('agent-a', 'claude')
        self.choose_terminals()
        state = cutover.survey(self.paths, self.env, machine=self.machine, tmux_factory=self.factory, socket='qorqut')
        self.assertTrue(any(view['agent_id'] == 'agent-a' and view['client'] == 'claude' for view in state['agents']))

    def test_the_settings_switch_runs_it_inside_the_service(self):
        self.live('agent-a', 'claude')
        self.choose_terminals()
        document = cutover.run_apply(self.paths, self.env, machine=self.machine, tmux_factory=self.factory, socket='qorqut',
                                     wait_seconds=5, poll=1, only='agent-a', lock=False, switching=True)
        self.assertIsNone(document['stopped_at'])
        self.assertEqual(document['results'][0]['status'], 'done')

    def test_a_managed_installation_is_not_refused(self):
        self.live('agent-a', 'claude')
        document = cutover.run_apply(self.paths, self.env, machine=self.machine, tmux_factory=self.factory, socket='qorqut',
                                     wait_seconds=5, poll=1, only='agent-a', lock=False)
        self.assertEqual(document['results'][0]['status'], 'done')


class JsonOutput(Installation):
    def test_inspect_json_has_no_internal_fields(self):
        self.live('agent-a', 'claude')
        out = io.StringIO()
        with redirect_stdout(out):
            status = cutover.main(['inspect', '--json', '--instance-root', str(self.root), '--tmux-socket', 'qorqut'], env=self.env,
                                  machine=self.machine, tmux_factory=self.factory)
        payload = json.loads(out.getvalue())
        self.assertEqual(status, 0)
        agent = next(item for item in payload['agents'] if item['agent_id'] == 'agent-a')
        self.assertFalse([key for key in agent if key.startswith('_')])
        self.assertEqual((agent['client'], agent['pid']), ('claude', 501))

    def test_the_plain_summary_names_the_agent_and_what_apply_would_do(self):
        self.live('agent-a', 'claude')
        out = io.StringIO()
        with redirect_stdout(out):
            cutover.main(['inspect', '--instance-root', str(self.root), '--tmux-socket', 'qorqut'], env=self.env,
                         machine=self.machine, tmux_factory=self.factory)
        text = out.getvalue()
        self.assertIn('ordubashy', text)
        self.assertIn('apply would: end claude pid 501 with SIGTERM', text)
        self.assertIn('apply would: end the tmux session', text)


if __name__ == '__main__':
    unittest.main()
