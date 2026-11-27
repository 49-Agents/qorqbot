"""Built-in browser setup converges the one browser and connects eligible agents without altering a healthy one."""

from contextlib import ExitStack, nullcontext
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
        setup.LAST_FAILURE.clear()
        self.addCleanup(setup.LAST_FAILURE.clear)
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
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runtime = SimpleNamespace(database=SimpleNamespace(read=Read), browser_manager=mock.Mock(),
                                       browser_access=mock.Mock(), paths=SimpleNamespace(runtime=Path(folder.name)))
        # Setup starts the download of Qorqbot's own Chrome; here it is recorded, never run.
        patch = mock.patch.object(setup.managed_chrome, 'start')
        self.chrome_download = patch.start(); self.addCleanup(patch.stop)
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

    def test_ordubashy_reads_every_explanation_without_a_button_to_press(self):
        states = ['missing', 'stopped', 'target_missing', 'unsupported', 'busy', 'conflict', 'invalid', 'unavailable', 'new']
        texts = ([setup.explanation(state) for state in states] + [setup.explanation('stopped', stop_requested=True)]
                 + list(setup.FAILURE_TEXT.values()))
        for text in texts:
            with self.subTest(text=text):
                self.assertNotRegex(setup.for_agent(text), r'(?i)retry setup|this card')
        self.assertIn('action=ensure', setup.for_agent(setup.explanation('missing')))
        self.assertIn('action=status', setup.for_agent(setup.explanation('busy')))

    def test_the_owner_is_not_told_to_install_chrome_which_qorqbot_downloads_itself(self):
        # Shown only while a Chrome is there; with none the card shows the download's own explanation.
        for text in (setup.explanation('unsupported'), setup.explanation('missing', failure_code='browser_capability_missing')):
            self.assertNotIn('Install Chrome', text)
            self.assertIn('Retry setup', text)

    def test_a_deliberate_stop_is_explained_as_one(self):
        self.assertIn('on purpose', setup.explanation('stopped', stop_requested=True))
        self.assertNotIn('on purpose', setup.explanation('stopped'))

    def test_the_real_failure_reason_reaches_settings_until_setup_succeeds(self):
        missing = {'state': 'missing', 'diagnostic': 'x', 'agents': []}
        self.assertEqual(self.run_setup(missing, fail=DomainError('browser_not_ready', 'internal wording')), ['ensure'])
        self.assertEqual(setup.LAST_FAILURE, {'code': 'browser_not_ready'})
        text = self.read_view(lambda *_a, **_k: dict(missing))['explanation']
        self.assertIn("didn't respond in time", text)
        self.assertNotIn('internal wording', text)
        self.run_setup(missing)
        self.assertEqual(setup.LAST_FAILURE, {})
        self.assertNotIn("didn't respond in time", self.read_view(lambda *_a, **_k: dict(missing))['explanation'])

    def test_an_unknown_failure_still_tells_the_owner_what_to_do(self):
        text = setup.explanation('missing', failure_code='unexpected')
        self.assertIn('Retry setup', text)
        self.assertIn('restart Qorqbot', text)

    def test_targets_include_lead_and_active_workers_only(self):
        owner, agents = setup.targets(self.runtime)
        self.assertEqual(owner, 'owner')
        self.assertEqual([item['agent_id'] for item in agents], ['lead', 'worker'])

    def run_setup(self, status, *args, fail=None, **kwargs):
        """Run ``ensure_all`` over a scripted status and return the actions it ran on the browser, as the owner."""
        actions = []
        def manage(owner, arguments, idempotency_key=None, owner_control=False):
            self.assertEqual((owner, owner_control), ('owner', True))
            self.assertEqual(list(arguments), ['action'])
            if arguments['action'] == 'status':
                return dict(status)
            self.assertTrue(idempotency_key)
            actions.append(arguments['action'])
            if fail is not None:
                raise fail
            return {'state': 'healthy', 'diagnostic': 'Ready.', 'agents': []}
        self.runtime.browser_manager.manage.side_effect = manage
        with mock.patch.object(setup, 'view', return_value={'ready': True}) as view:
            self.assertEqual(setup.ensure_all(self.runtime, *args, **kwargs), {'ready': True})
        view.assert_called_once_with(self.runtime)
        return actions

    def test_ensure_only_a_browser_that_is_missing_stopped_or_not_connected(self):
        for state in ('missing', 'stopped', 'target_missing'):
            with self.subTest(state=state):
                self.assertEqual(self.run_setup({'state': state}), ['ensure'])
                self.assertEqual(self.run_setup({'state': state}, {}), ['ensure'])
        for state in ('healthy', 'busy', 'unsupported'):
            with self.subTest(state=state):
                self.assertEqual(self.run_setup({'state': state}), [])
                self.assertEqual(self.run_setup({'state': state}, {}), [])

    def test_only_the_owners_retry_repairs_saved_settings(self):
        for state in ('conflict', 'invalid'):
            with self.subTest(state=state):
                self.assertEqual(self.run_setup({'state': state}), ['repair'])
                self.assertEqual(self.run_setup({'state': state}, {}), [], 'automatic setup never repairs')

    def test_automatic_setup_leaves_a_deliberately_stopped_browser_alone(self):
        self.assertEqual(self.run_setup({'state': 'stopped', 'stop_requested': True}, {}), [])
        self.assertEqual(self.run_setup({'state': 'stopped'}, {}), ['ensure'])

    def test_owner_retry_starts_a_deliberately_stopped_browser(self):
        self.assertEqual(self.run_setup({'state': 'stopped', 'stop_requested': True}), ['ensure'])

    def test_a_failed_setup_is_recorded_not_raised(self):
        for error, code in ((DomainError('browser_not_ready', 'no'), 'browser_not_ready'), (RuntimeError('unexpected'), 'unexpected')):
            with self.subTest(error=type(error).__name__), self.assertNoLogs(setup.logger, 'CRITICAL'):
                self.assertEqual(self.run_setup({'state': 'missing'}, fail=error), ['ensure'])
                self.assertEqual(setup.LAST_FAILURE, {'code': code})

    def test_failures_back_off_until_success_and_cap(self):
        status, key = {'state': 'missing'}, setup.BROWSER_BACKOFF
        fail = DomainError('browser_not_ready', 'no')
        backoff, now = {}, [1000.0]
        run = lambda failing=fail: self.run_setup(status, backoff, fail=failing, clock=lambda: now[0])
        self.assertEqual(run(), ['ensure'])
        self.assertEqual(backoff[key]['next_attempt'], 1000 + setup.BACKOFF_BASE)
        now[0] += setup.BACKOFF_BASE - 1
        calls = self.runtime.browser_manager.manage.call_count
        self.assertEqual(run(), [])
        self.assertEqual(self.runtime.browser_manager.manage.call_count, calls, 'not even a status call before its time')
        now[0] += 1
        self.assertEqual(run(), ['ensure'])
        self.assertEqual(backoff[key]['next_attempt'], now[0] + setup.BACKOFF_BASE * 2)
        backoff[key].update(failures=20, next_attempt=0)
        self.assertEqual(run(), ['ensure'])
        self.assertEqual(backoff[key]['next_attempt'], now[0] + setup.BACKOFF_LIMIT)
        backoff[key]['next_attempt'] = 0
        self.assertEqual(run(failing=None), ['ensure'])
        self.assertNotIn(key, backoff)

    def test_owner_retry_ignores_backoff(self):
        backoff = {}
        self.assertEqual(self.run_setup({'state': 'missing'}, backoff, fail=DomainError('browser_not_ready', 'no'),
                                        clock=lambda: 1000.0), ['ensure'])
        self.assertEqual(self.run_setup({'state': 'missing'}, backoff, clock=lambda: 1000.0), [])
        self.assertEqual(self.run_setup({'state': 'missing'}), ['ensure'])
        self.assertEqual(backoff[setup.BROWSER_BACKOFF]['failures'], 1, "the owner's retry does not touch the automatic wait")

    def test_setup_starts_the_chrome_download_without_waiting_for_it(self):
        self.assertEqual(self.run_setup({'state': 'missing'}, {}), ['ensure'])
        self.chrome_download.assert_called_once()
        self.assertEqual(self.chrome_download.call_args.args[0], self.runtime.paths.runtime)

    def test_a_failed_chrome_download_backs_off_like_the_browser_and_owner_retry_ignores_it(self):
        backoff, now = {}, [1000.0]
        run = lambda *args: self.run_setup({'state': 'healthy'}, *args, clock=lambda: now[0])
        finished = lambda: self.chrome_download.call_args.args[1]
        run(backoff)
        finished()(DomainError('chrome_download_failed', 'offline'))
        self.assertEqual(backoff[setup.CHROME_BACKOFF], {'failures': 1, 'next_attempt': 1000 + setup.BACKOFF_BASE})
        run(backoff)
        self.assertEqual(self.chrome_download.call_count, 1, 'not tried again before its time')
        run()
        self.assertEqual(self.chrome_download.call_count, 2, 'the owner pressed Retry setup')
        finished()(DomainError('chrome_download_failed', 'offline'))
        self.assertEqual(backoff[setup.CHROME_BACKOFF]['failures'], 1, "the owner's retry does not lengthen the wait")
        now[0] += setup.BACKOFF_BASE
        run(backoff)
        finished()(DomainError('chrome_sandbox_unavailable', 'no sandbox'))
        self.assertEqual(backoff[setup.CHROME_BACKOFF], {'failures': 2, 'next_attempt': now[0] + setup.BACKOFF_BASE * 2})
        now[0] += setup.BACKOFF_BASE * 2
        run(backoff)
        finished()(None)
        self.assertNotIn(setup.CHROME_BACKOFF, backoff)

    def computer(self, tool=True, chrome='/chrome'):
        """What this computer has: the browser tool or not, and an installed Chrome or none."""
        stack = ExitStack()
        stack.enter_context(mock.patch('qorqut.bootstrap.browser.runtime_executable',
                                       mock.Mock(side_effect=None if tool else BrowserError('missing'))))
        stack.enter_context(mock.patch('qorqut.native_paths.installed_chrome', return_value=chrome))
        return stack

    def read_view(self, status, tool=True, chrome='/chrome'):
        self.runtime.browser_manager.manage.side_effect = status
        with self.computer(tool, chrome):
            return setup.view(self.runtime)

    def test_settings_show_the_download_in_place_of_install_chrome(self):
        status = lambda *_a, **_k: {'state': 'unsupported', 'diagnostic': 'x', 'agents': []}
        own = lambda state, text='': mock.patch.object(setup.managed_chrome, 'view', return_value={'state': state, 'explanation': text})
        with own('downloading', 'Qorqbot is downloading its own browser.'):
            view = self.read_view(status, chrome=None)
        self.assertFalse(view['chrome_installed'])
        self.assertEqual(view['managed_chrome'], {'state': 'downloading', 'explanation': 'Qorqbot is downloading its own browser.'})
        with own('ready'):
            self.assertTrue(self.read_view(status, chrome=None)['chrome_installed'], "Qorqbot's own Chrome is enough")
        with own('unusable', 'It cannot start here.'):
            self.assertTrue(self.read_view(status)['chrome_installed'], 'the installed Chrome is used instead')
            self.assertFalse(self.read_view(status, chrome=None)['chrome_installed'])

    def test_the_owner_is_not_asked_to_install_chrome_when_the_installed_one_is_used_in_place_of_qorqbots_own(self):
        status = lambda *_a, **_k: {'state': 'healthy', 'diagnostic': 'Ready.', 'agents': []}
        text = "This computer's security settings don't let Qorqbot's own browser start safely. Install Google Chrome."
        with mock.patch.object(setup.managed_chrome, 'view', return_value={'state': 'unusable', 'explanation': text}):
            self.assertEqual(self.read_view(status)['managed_chrome']['explanation'], setup.INSTALLED_CHROME_USED)
            self.assertEqual(self.read_view(status, chrome=None)['managed_chrome']['explanation'], text)

    def test_ready_requires_a_healthy_browser_its_agents_and_the_tool(self):
        state = lambda value: lambda *_a, **_k: {'state': value, 'diagnostic': 'x', 'agents': []}
        self.assertTrue(self.read_view(state('healthy'))['ready'])
        missing_tool = self.read_view(state('healthy'), tool=False)
        self.assertFalse(missing_tool['ready'])
        self.assertFalse(missing_tool['tool_installed'])
        for value in ('stopped', 'target_missing', 'conflict', 'busy'):
            self.assertFalse(self.read_view(state(value))['ready'], value)
        with mock.patch.object(setup, 'active_global', return_value=None):       # no permanent agent uses the browser
            self.assertFalse(self.read_view(state('healthy'))['ready'])

    def test_the_view_is_one_browser_with_the_agents_the_status_reports(self):
        agents = [{'agent_id': 'lead', 'name': 'Ordubashy', 'connected': True},
                  {'agent_id': 'worker', 'name': 'Atlas', 'connected': False, 'problem': "This agent's folder isn't ready yet."}]
        def status(owner, arguments, owner_control=False):
            self.assertEqual((owner, arguments, owner_control), ('owner', {'action': 'status'}, True))
            return {'state': 'healthy', 'diagnostic': 'Ready.', 'browser': 'shared', 'agents': agents}
        view = self.read_view(status)
        self.assertEqual(set(view), {'kind', 'tool_installed', 'chrome_installed', 'managed_chrome', 'chrome_in_use',
                                     'diagnostic', 'state', 'explanation', 'ready', 'agents'})
        self.assertEqual((view['kind'], view['state'], view['diagnostic']), ('built_in', 'healthy', 'Ready.'))
        self.assertEqual(view['agents'], agents)
        self.assertEqual(view['explanation'], "Atlas: This agent's folder isn't ready yet.")
        self.assertTrue(view['ready'])
        self.runtime.browser_manager.manage.assert_called_once()

    def test_the_view_says_which_chrome_the_browser_uses(self):
        self.runtime.browser_manager.chrome_in_use.return_value = {'kind': 'google_chrome', 'mode': 'visible'}
        view = self.read_view(lambda *_a, **_k: {'state': 'healthy', 'diagnostic': '', 'browser': 'shared', 'agents': []})
        self.assertEqual(view['chrome_in_use'], {'kind': 'google_chrome', 'mode': 'visible'})
        self.runtime.browser_manager.chrome_in_use.side_effect = OSError('unreadable')
        view = self.read_view(lambda *_a, **_k: {'state': 'healthy', 'diagnostic': '', 'browser': 'shared', 'agents': []})
        self.assertIsNone(view['chrome_in_use'])

    def test_setup_offers_no_browser_choice(self):
        self.assertFalse(hasattr(setup, 'choose'))
        self.assertFalse(hasattr(setup, 'CHOICES'))

    def test_a_status_failure_is_reported_without_its_internals(self):
        def status(*_a, **_k):
            raise OSError('secret path')
        with self.assertLogs(setup.logger, 'ERROR'):
            data = self.read_view(status)
        self.assertEqual(data['state'], 'unavailable')
        self.assertNotIn('secret', data['diagnostic'] + data['explanation'])
        self.assertEqual([agent['agent_id'] for agent in data['agents']], ['lead', 'worker'])
        self.assertFalse(data['ready'])
        def refused(*_a, **_k):
            raise DomainError('maintenance', 'Paused for maintenance')
        data = self.read_view(refused)
        self.assertEqual((data['state'], data['diagnostic'], data['ready']), ('unavailable', 'Paused for maintenance', False))

    # -- Ordubashy's agent_browser_manage --------------------------------------------------------------------------------

    def agent_manage(self, action, state='healthy', fail=None, **computer):
        """Run ``agent_manage`` as Ordubashy over a manager that answers ``state``, or refuses the action with ``fail``."""
        def manage(_principal, arguments, idempotency_key=None):
            if fail is not None and arguments['action'] == action:
                raise fail
            return {'action': arguments['action'], 'state': state, 'diagnostic': 'x', 'agents': []}
        self.runtime.browser_manager.manage.reset_mock()
        self.runtime.browser_manager.manage.side_effect = manage
        with self.computer(**computer):
            return setup.agent_manage(self.runtime, 'lead', {'action': action}, None if action == 'status' else 'key-1')

    def managed(self):
        return self.runtime.browser_manager.manage.call_args_list

    def test_a_refused_caller_starts_no_chrome_download(self):
        refusal = DomainError('forbidden', 'Only the installation owner or Ordubashy can manage the browser')
        self.runtime.browser_manager.manage.side_effect = refusal
        for action in ('ensure', 'repair', 'restart', 'stop'):
            with self.subTest(action=action):
                self.runtime.browser_manager.manage.reset_mock()
                with mock.patch.object(setup, 'prepare_chrome') as prepare, self.assertRaises(DomainError) as caught:
                    setup.agent_manage(self.runtime, 'worker', {'action': action}, 'key-1')
                self.assertEqual(caught.exception.code, 'forbidden')
                prepare.assert_not_called()
                self.assertEqual(self.managed(), [mock.call('worker', {'action': 'status'})], 'refused before the action')
        self.chrome_download.assert_not_called()
        self.assertEqual(setup.LAST_FAILURE, {}, "someone else's refusal is not a setup failure")

    def test_ensure_and_repair_start_the_chrome_download_and_other_actions_do_not(self):
        for action, prepared in (('ensure', True), ('repair', True), ('restart', False), ('stop', False), ('status', False)):
            with self.subTest(action=action), mock.patch.object(setup, 'prepare_chrome') as prepare:
                self.assertEqual(self.agent_manage(action)['action'], action)
                self.assertEqual(prepare.call_args_list, [mock.call(self.runtime)] if prepared else [])
                self.assertEqual(self.managed()[-1], mock.call('lead', {'action': action}, None if action == 'status' else 'key-1'))
        self.agent_manage('ensure')
        self.chrome_download.assert_called_once()
        self.assertIs(self.chrome_download.call_args.kwargs['automatic'], False, 'like Retry setup, it does not wait')

    def test_the_answer_carries_what_the_settings_card_knows_in_ordubashys_words(self):
        own = {'state': 'failed', 'explanation': "Qorqbot's own browser couldn't be downloaded. Press Retry setup."}
        with mock.patch.object(setup.managed_chrome, 'view', return_value=own):
            result = self.agent_manage('status', state='healthy')
            self.assertEqual(result['setup'], {'tool_installed': True, 'chrome_installed': True, 'own_chrome': {
                'state': 'failed', 'explanation': "Qorqbot's own browser couldn't be downloaded. Use action=ensure."}})
            result = self.agent_manage('status', state='healthy', tool=False, chrome=None)
            self.assertEqual((result['setup']['tool_installed'], result['setup']['chrome_installed']), (False, False))
            setup.LAST_FAILURE['code'] = 'browser_capability_missing'
            result = self.agent_manage('status', state='missing')
            self.assertIn('action=ensure', result['setup']['last_failure'])
            self.assertNotRegex(str(result['setup']), '(?i)retry setup')
            self.assertNotIn('last_failure', self.agent_manage('status', state='healthy')['setup'])
        self.assertEqual(setup.LAST_FAILURE, {'code': 'browser_capability_missing'}, 'a status call settles nothing')

    def test_a_failed_action_is_remembered_until_one_leaves_the_browser_healthy(self):
        with self.assertRaises(DomainError):
            self.agent_manage('ensure', fail=DomainError('browser_not_ready', 'no'))
        self.assertEqual(setup.LAST_FAILURE, {'code': 'browser_not_ready'})
        self.assertIn("didn't respond in time", self.agent_manage('stop', state='stopped')['setup']['last_failure'])
        self.assertNotIn('last_failure', self.agent_manage('ensure')['setup'])
        self.assertEqual(setup.LAST_FAILURE, {})


class StopRequestTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.directory = Path(folder.name).resolve()
        self.manager = AgentBrowserManager.__new__(AgentBrowserManager)
        self.manager._exclusive_use = lambda *_: nullcontext()
        self.manager._stop_managed = mock.Mock(return_value=True)
        self.manager._start_managed = mock.Mock(return_value=True)
        self.manager._advice = lambda *_: 'advice'
        self.snapshot = SimpleNamespace(
            meta_error=None, conflict=None, busy=None, target_state='valid', target_error=None, meta={'mode': 'managed'},
            directory=self.directory, workspace=self.directory / 'workspace', running=False, healthy=False)

    def apply(self, action):
        self.manager._apply(SimpleNamespace(effect=False), action, self.snapshot)

    def requested(self):
        return 'stop_requested' in self.manager._result('status', self.snapshot, [], changed=False)

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
        rows = [{'agent_id': agent, 'display_name': name, 'slug': None}
                for agent, name in (('learning', 'Learning Applier'), ('scout', 'Scout'), ('lead', 'Ordubashy'), ('atlas', 'Atlas'))]
        connection = mock.Mock()
        connection.execute.return_value.fetchall.return_value = rows
        class Read:
            def __enter__(self):
                return SimpleNamespace(connection=connection)
            def __exit__(self, *_):
                return False
        self.application = mock.Mock()
        self.context = SimpleNamespace(principal_id='caller', principal_kind='human')
        self.application.policy.context_for.return_value = self.context
        self.manager = AgentBrowserManager.__new__(AgentBrowserManager)
        self.manager.runtime = SimpleNamespace(application=self.application, database=SimpleNamespace(read=Read),
                                               registry=mock.Mock(), paths=SimpleNamespace())
        role = {'lead': 'ordubashy', 'atlas': 'global-worker', 'scout': 'global-worker', 'learning': 'learning-applier'}
        self.patch('qorqut.agents.global_scope.active_global', side_effect=lambda _db, agent: {'role': {'id': role[agent]}})
        self.patch('qorqut.browser.agent_browsers.agent_workspace_of', side_effect=lambda _paths, agent, *_: Path('/agents') / agent)
        self.owner = self.patch('qorqut.management.identity.is_active_installation_owner', return_value=True)
        self.ordubashy = self.patch('qorqut.management.identity.configured_ordubashy', return_value='lead')

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def agent(self, agent_id):
        self.context.principal_id, self.context.principal_kind = agent_id, 'agent'
        self.owner.return_value = False

    def refused(self, **kwargs):
        with self.assertRaises(DomainError) as caught:
            self.manager._authorize(self.context.principal_id, **kwargs)
        return caught.exception.code

    def test_owner_control_is_refused_for_anyone_but_the_owner(self):
        self.owner.return_value = False
        self.assertEqual(self.refused(owner_control=True), 'forbidden')
        self.agent('lead')
        self.assertEqual(self.refused(owner_control=True), 'forbidden', "Ordubashy has the tool, not the owner's button")
        self.application.authorize.assert_not_called()

    def test_owner_control_replaces_the_operation_grant_and_names_the_permanent_agents(self):
        agents = self.manager._authorize('caller', owner_control=True)
        self.application.authorize.assert_not_called()
        self.assertEqual([(agent.agent_id, agent.name, agent.lead) for agent in agents],
                         [('lead', 'Ordubashy', True), ('atlas', 'Atlas', False), ('scout', 'Scout', False)])
        self.assertEqual(agents[0].workspace, Path('/agents/lead'))

    def test_agent_callers_still_need_the_operation_grant(self):
        self.agent('lead')
        self.application.authorize.side_effect = DomainError('forbidden', 'no grant')
        self.assertEqual(self.refused(), 'forbidden')
        self.application.authorize.assert_called_once()
        self.ordubashy.assert_not_called()

    def test_ordubashy_manages_the_browser(self):
        self.agent('lead')
        self.assertEqual([agent.agent_id for agent in self.manager._authorize('lead')], ['lead', 'atlas', 'scout'])
        self.application.authorize.assert_called_once()

    def test_no_other_agent_manages_the_browser_even_with_the_grant(self):
        self.agent('atlas')
        self.assertEqual(self.refused(), 'forbidden')
        self.application.authorize.assert_called_once()

    def test_the_owner_without_owner_control_is_not_ordubashy(self):
        self.assertEqual(self.refused(), 'forbidden')
