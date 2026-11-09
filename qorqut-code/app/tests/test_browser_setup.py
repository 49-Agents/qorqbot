"""Built-in browser setup converges eligible agents without altering healthy browsers."""

from contextlib import nullcontext
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.bootstrap.browser import BrowserError
from qorqut.browser import setup
from qorqut.browser.agent_browsers import AgentBrowserManager


class BrowserSetupTest(unittest.TestCase):
    def setUp(self):
        setup.LAST_FAILURES.clear()
        self.addCleanup(setup.LAST_FAILURES.clear)
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE installation (id TEXT, owner_principal_id TEXT);
            INSERT INTO installation VALUES ('service', 'owner');
            CREATE TABLE principals (id TEXT, display_name TEXT, active INTEGER);
            CREATE TABLE agent_profiles (agent_id TEXT, status TEXT);
            INSERT INTO principals VALUES ('lead', 'Ordubashy', 1), ('worker', 'Atlas', 1),
                ('learning', 'Learning Applier', 1), ('retired', 'Old worker', 0);
            INSERT INTO agent_profiles VALUES ('lead', 'active'), ('worker', 'active'),
                ('learning', 'active'), ('retired', 'inactive');
        """)
        self.addCleanup(connection.close)
        class Read:
            def __enter__(self):
                return SimpleNamespace(connection=connection)
            def __exit__(self, *_):
                return False
        self.runtime = SimpleNamespace(database=SimpleNamespace(read=Read), browser_manager=mock.Mock())
        role = {'lead': 'ordubashy', 'worker': 'global-worker', 'learning': 'learning-applier'}
        patch = mock.patch.object(setup, 'active_global', side_effect=lambda _db, agent: {'role': {'id': role[agent]}} if agent in role else None)
        patch.start(); self.addCleanup(patch.stop)

    def test_every_explanation_is_written_for_the_owner(self):
        states = ['missing', 'stopped', 'target_missing', 'unsupported', 'busy', 'conflict', 'invalid', 'unavailable', 'new']
        texts = [setup.explanation(state) for state in states] + list(setup.FAILURE_TEXT.values())
        for text in texts:
            with self.subTest(text=text):
                self.assertTrue(text)
                self.assertNotRegex(text, r'action=|[a-z]+_[a-z]+|docs/|QORQUT')
        self.assertEqual(setup.explanation('healthy'), '')

    def test_a_deliberate_stop_is_explained_as_one(self):
        self.assertIn('on purpose', setup.explanation('stopped', stop_requested=True))
        self.assertNotIn('on purpose', setup.explanation('stopped'))

    def test_the_real_failure_reason_reaches_settings_until_setup_succeeds(self):
        states = {'lead': {'state': 'missing', 'diagnostic': 'x'}, 'worker': {'state': 'healthy', 'diagnostic': 'x'}}
        fail = {'lead': DomainError('browser_workspace_unavailable', 'internal wording')}
        self.run_setup(states, fail=fail)
        def status(_owner, arguments, **_k):
            return states[arguments['agent_id']]
        lead = self.read_view(status)['agents'][0]
        self.assertIn('Start the agent once', lead['explanation'])
        self.assertNotIn('internal wording', lead['explanation'])
        self.run_setup(states)
        self.assertNotIn('Start the agent once', self.read_view(status)['agents'][0]['explanation'])

    def test_an_unknown_failure_still_tells_the_owner_what_to_do(self):
        text = setup.explanation('missing', failure_code='unexpected')
        self.assertIn('Retry setup', text)
        self.assertIn('restart Qorqbot', text)

    def test_targets_include_lead_and_active_workers_only(self):
        owner, agents = setup.targets(self.runtime)
        self.assertEqual(owner, 'owner')
        self.assertEqual([item['agent_id'] for item in agents], ['lead', 'worker'])

    def test_ensure_only_missing_or_stopped_browsers(self):
        states = {'lead': 'healthy', 'worker': 'missing'}
        calls = []
        def manage(_owner, arguments, idempotency_key=None, owner_control=False):
            self.assertTrue(owner_control)
            calls.append((arguments['agent_id'], arguments['action']))
            if arguments['action'] == 'ensure':
                states[arguments['agent_id']] = 'healthy'
            return {'state': states[arguments['agent_id']], 'diagnostic': 'Ready.'}
        self.runtime.browser_manager.manage.side_effect = manage
        with mock.patch.object(setup, 'view', return_value={'ready': True}) as view:
            self.assertEqual(setup.ensure_all(self.runtime), {'ready': True})
        self.assertIn(('worker', 'ensure'), calls)
        self.assertNotIn(('lead', 'ensure'), calls)
        view.assert_called_once_with(self.runtime)

    def run_setup(self, states, *args, fail=(), **kwargs):
        """Run ``ensure_all`` over scripted states and return the agents it tried to start."""
        ensured = []
        def manage(_owner, arguments, idempotency_key=None, owner_control=False):
            agent = arguments['agent_id']
            if arguments['action'] == 'status':
                return dict(states[agent])
            ensured.append(agent)
            if agent in fail:
                raise fail[agent]
            return {'state': 'healthy', 'diagnostic': 'Ready.'}
        self.runtime.browser_manager.manage.side_effect = manage
        with mock.patch.object(setup, 'view', return_value={}):
            setup.ensure_all(self.runtime, *args, **kwargs)
        return ensured

    def test_automatic_setup_leaves_a_deliberately_stopped_browser_alone(self):
        states = {'lead': {'state': 'stopped', 'stop_requested': True}, 'worker': {'state': 'stopped'}}
        self.assertEqual(self.run_setup(states, {}), ['worker'])

    def test_owner_retry_starts_a_deliberately_stopped_browser(self):
        states = {'lead': {'state': 'stopped', 'stop_requested': True}, 'worker': {'state': 'healthy'}}
        self.assertEqual(self.run_setup(states), ['lead'])

    def test_one_failure_does_not_block_other_agents(self):
        for error in (DomainError('browser_not_ready', 'no'), RuntimeError('unexpected')):
            states = {'lead': {'state': 'missing'}, 'worker': {'state': 'missing'}}
            with self.subTest(error=type(error).__name__), self.assertNoLogs(setup.logger, 'CRITICAL'):
                self.assertEqual(self.run_setup(states, fail={'lead': error}), ['lead', 'worker'])

    def test_failures_back_off_until_success_and_cap(self):
        states = {'lead': {'state': 'missing'}, 'worker': {'state': 'healthy'}}
        fail = {'lead': DomainError('browser_not_ready', 'no')}
        backoff, now = {}, [1000.0]
        run = lambda failing=fail: self.run_setup(states, backoff, fail=failing, clock=lambda: now[0])
        self.assertEqual(run(), ['lead'])
        self.assertEqual(backoff['lead']['next_attempt'], 1000 + setup.BACKOFF_BASE)
        now[0] += setup.BACKOFF_BASE - 1
        self.assertEqual(run(), [])
        now[0] += 1
        self.assertEqual(run(), ['lead'])
        self.assertEqual(backoff['lead']['next_attempt'], now[0] + setup.BACKOFF_BASE * 2)
        backoff['lead'].update(failures=20, next_attempt=0)
        self.assertEqual(run(), ['lead'])
        self.assertEqual(backoff['lead']['next_attempt'], now[0] + setup.BACKOFF_LIMIT)
        backoff['lead']['next_attempt'] = 0
        self.assertEqual(run(failing={}), ['lead'])
        self.assertNotIn('lead', backoff)

    def test_owner_retry_ignores_backoff(self):
        states = {'lead': {'state': 'missing'}, 'worker': {'state': 'healthy'}}
        self.assertEqual(self.run_setup(states), ['lead'])

    def read_view(self, status, tool=True):
        self.runtime.browser_access = mock.Mock()
        self.runtime.browser_manager.manage.side_effect = status
        check = mock.Mock(side_effect=None if tool else BrowserError('missing'))
        with mock.patch('qorqut.bootstrap.browser.runtime_executable', check), \
                mock.patch('qorqut.native_paths.installed_chrome', return_value='/chrome'):
            return setup.view(self.runtime)

    def test_ready_requires_every_browser_healthy_and_the_tool(self):
        healthy = lambda *_a, **_k: {'state': 'healthy', 'diagnostic': 'Ready.'}
        self.assertTrue(self.read_view(healthy)['ready'])
        missing_tool = self.read_view(healthy, tool=False)
        self.assertFalse(missing_tool['ready'])
        self.assertFalse(missing_tool['tool_installed'])
        def mixed(_owner, arguments, **_k):
            return {'state': 'healthy' if arguments['agent_id'] == 'lead' else 'stopped', 'diagnostic': 'x'}
        self.assertFalse(self.read_view(mixed)['ready'])

    def test_a_status_failure_is_reported_for_that_agent_only(self):
        def status(_owner, arguments, **_k):
            if arguments['agent_id'] == 'lead':
                raise OSError('secret path')
            return {'state': 'healthy', 'diagnostic': 'Ready.'}
        with self.assertLogs(setup.logger, 'ERROR'):
            data = self.read_view(status)
        lead, worker = data['agents']
        self.assertEqual(lead['state'], 'unavailable')
        self.assertNotIn('secret', lead['diagnostic'])
        self.assertEqual(worker['state'], 'healthy')
        self.assertFalse(data['ready'])


class StopRequestTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        self.manager = AgentBrowserManager.__new__(AgentBrowserManager)
        self.manager._exclusive_use = lambda _s: nullcontext()
        self.manager._stop_managed = mock.Mock(return_value=True)
        self.manager._start_managed = mock.Mock(return_value=True)
        self.manager._finish = mock.Mock(return_value={})
        self.manager._advice = lambda *_: 'advice'
        self.snapshot = SimpleNamespace(
            meta_error=None, conflict=None, target_state='valid', target_error=None, meta={'mode': 'managed'},
            directory=self.directory, running=False, healthy=False, agent_id='agent', ownership='managed', unit=None)

    def apply(self, action):
        self.manager._apply(SimpleNamespace(effect=False), action, self.snapshot)

    def requested(self):
        return 'stop_requested' in self.manager._result('status', self.snapshot, 'stopped', changed=False)

    def test_stop_is_remembered_and_every_explicit_start_clears_it(self):
        self.assertFalse(self.requested())
        for action in ('ensure', 'repair', 'restart'):
            with self.subTest(action=action):
                self.apply('stop')
                self.assertTrue(self.requested())
                self.apply(action)
                self.assertFalse(self.requested())

    def test_a_stop_that_fails_is_not_remembered(self):
        self.manager._stop_managed.side_effect = DomainError('browser_in_use', 'busy')
        with self.assertRaises(DomainError):
            self.apply('stop')
        self.assertFalse(self.requested())


class OwnerAuthorizationTest(unittest.TestCase):
    def setUp(self):
        connection = mock.Mock()
        class Read:
            def __enter__(self):
                return SimpleNamespace(connection=connection)
            def __exit__(self, *_):
                return False
        self.application = mock.Mock()
        self.application.policy.context_for.return_value = SimpleNamespace(principal_id='caller', principal_kind='human')
        self.manager = AgentBrowserManager.__new__(AgentBrowserManager)
        self.manager.runtime = SimpleNamespace(application=self.application, database=SimpleNamespace(read=Read),
                                               registry=mock.Mock())
        self.manager_check = self.patch('qorqut.agents.worker_access.require_worker_manager',
                                        side_effect=DomainError('reached_worker_check', 'stop here'))
        self.role = self.patch('qorqut.agents.global_scope.active_global',
                               return_value={'role': {'id': 'global-worker'}})
        self.owner = self.patch('qorqut.management.identity.is_active_installation_owner', return_value=True)

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def authorize(self, **kwargs):
        with self.assertRaises(DomainError) as caught:
            self.manager._authorize('caller', 'target', **kwargs)
        return caught.exception.code

    def test_owner_control_is_refused_for_anyone_but_the_owner(self):
        self.owner.return_value = False
        self.assertEqual(self.authorize(owner_control=True), 'forbidden')
        self.application.authorize.assert_not_called()
        self.manager_check.assert_not_called()

    def test_owner_control_replaces_only_the_operation_grant(self):
        self.assertEqual(self.authorize(owner_control=True), 'reached_worker_check')
        self.application.authorize.assert_not_called()

    def test_agent_callers_still_need_the_operation_grant(self):
        self.assertEqual(self.authorize(), 'reached_worker_check')
        self.application.authorize.assert_called_once()

    def test_only_the_owner_manages_ordubashys_browser(self):
        self.role.return_value = {'role': {'id': 'ordubashy'}}
        self.owner.return_value = False
        self.assertEqual(self.authorize(), 'forbidden')
        self.manager_check.assert_not_called()
