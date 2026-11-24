"""A compaction whose refresh outlasts the hook's wait still restores the session's context and skills."""
from concurrent.futures import Future
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest import mock

from qorqut_agentic.behavior.instructions import from_catalog
from qorqut.bootstrap import always_skills, handoff, recovery
from qorqut.bootstrap.claude_session import run
from qorqut.bootstrap.supervisor import Supervisor

SESSION = '00000000-0000-4000-8000-000000000001'
MANDATORY = 'Mandatory instructions.'
SKILLS = 'Full skill bodies.'
INTAKE = 'Operational intake reminder.'
RECOVERY_ROUTE = '/api/agent/inbox/recovery'


class CompactionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.root, self.workspace = base / 'installation', base / 'workspace'
        self.path = self.workspace / '.qorqut/registration.json'
        self.path.parent.mkdir(parents=True)
        self.registration = {'schema_version': 1, 'registration_id': 'synthetic', 'agent_id': 'agent',
                             'workspace': str(self.workspace), 'service_url': 'http://127.0.0.1:1',
                             'credential_file': '.qorqut/credential', 'operational_intake_enabled': True}
        self.path.write_text(json.dumps(self.registration))
        instruction_root = (Path(__file__).resolve().parents[3]
                            / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/runtime-instructions')
        self.instructions = from_catalog(lambda path: (instruction_root / path).read_text())
        (self.path.parent / 'runtime-instructions.json').write_bytes(self.instructions.bytes())
        self.posts = []
        self.addCleanup(mock.patch.stopall)
        policy = NS(SKILL_BUDGET=1000, render_skills=lambda *_args, **_values: SKILLS,
                    from_prepared=lambda _path: NS(render=lambda _key: INTAKE))
        @contextmanager
        def observer(_root, _deadline):
            yield NS(hint=lambda: 'revision-1', observe=lambda: 'revision-1')
        for target, value in (
            ('qorqut.installation.installation_config', mock.Mock(return_value={'root': str(self.root)})),
            ('qorqut.bootstrap.handoff.installation_config', mock.Mock(return_value={'root': str(self.root)})),
            ('qorqut.bootstrap.handoff.running', mock.Mock(return_value=True)),
            ('qorqut.bootstrap.handoff.HOOK_WAIT', .05),
            ('qorqut.bootstrap.catalog_revision.open_observer', observer),
            ('qorqut.bootstrap.native_binding.attest', mock.Mock(side_effect=lambda *_args: dict(self.native))),
            ('qorqut.bootstrap.claude_session.snapshot_transcript', mock.Mock()),
            ('qorqut.bootstrap.always_skills.agentic_policy', mock.Mock(return_value=policy)),
            ('qorqut.bootstrap.supervisor.BootstrapClient', self.service),
        ):
            mock.patch(target, value).start()

    def service(self, _registration, **_values):
        """The account service as the supervisor sees it: unchanged configuration, full context for a lifecycle event."""
        def post(route, body):
            self.posts.append((route, body))
            if route != '/api/agent/bootstrap':
                return {'state': 'delivered'}
            response = {'ready': True, 'effective_config_id': 'config-1', 'agentic_catalog_revision': 'revision-1',
                        'refresh_required': body.get('last_effective_config_id') != 'config-1',
                        'inbox_available': True, 'inbox_session_note': 'Inbox note.'}
            if body['event'] != 'prompt' or response['refresh_required']:
                response['mandatory_context'] = {'rendered': MANDATORY}
            return response
        return NS(post=post)

    def begin(self, runtime):
        """A running session that received its context once, under the configuration that is still current."""
        self.native = {'registration_id': 'synthetic', 'session_id': SESSION, 'owner': '123:456', 'runtime': runtime}
        (self.path.parent / 'native-binding.json').write_text(json.dumps(self.native))
        name = hashlib.sha256(SESSION.encode()).hexdigest() + '.json'
        delivery = self.path.parent / 'delivery'
        delivery.mkdir()
        (delivery / name).write_text(json.dumps({
            'registration_id': 'synthetic', 'session_id': SESSION, 'effective_config_id': 'config-1'}))
        (delivery / ('skills-' + name)).write_text(json.dumps({
            'registration_id': 'synthetic', 'digest': hashlib.sha256(SKILLS.encode()).hexdigest()}))
        self.posts.clear()
        self.supervisor = Supervisor({'root': str(self.root)}, alive=lambda _owner: True)
        self.addCleanup(self.supervisor.pool.shutdown)
        self.supervisor.context_check_at = float('inf')
        self.supervisor.reference_future = Future()

    def hook(self, event, **fields):
        stream = io.StringIO()
        unused = NS(post=mock.Mock(side_effect=AssertionError('the hook must use the supervisor handoff')))
        self.assertEqual(run({'hook_event_name': event, 'session_id': SESSION, **fields}, self.path, stream=stream,
                             diagnostics=io.StringIO(), runtime=self.native['runtime'],
                             client_factory=lambda _registration: unused), 0)
        return json.loads(stream.getvalue())['hookSpecificOutput']['additionalContext']

    def supervise(self):
        """Supervisor passes until the background refresh and its follow-ups have settled."""
        for _ in range(4):
            self.supervisor.tick()
            for future in (*self.supervisor.recoveries.values(), *self.supervisor.followups.values()):
                future.exception(timeout=5)

    def slow_refresh_after_compaction(self, runtime):
        self.begin(runtime)
        pending = self.hook('SessionStart', source='compact')
        self.assertEqual(pending, self.instructions.render('startup-pending'))
        # The earlier receipt says nothing about this compaction: the session still needs its context.
        episode = recovery.read(self.registration, SESSION)
        self.assertIsNotNone(episode)

        self.supervise()
        wakes = [body for route, body in self.posts if route == RECOVERY_ROUTE]
        self.assertEqual([wake['notification_id'] for wake in wakes], [episode['failure_id']])

        recovered = self.hook('UserPromptSubmit')
        self.assertIn(MANDATORY, recovered)
        self.assertIn(SKILLS, recovered)
        self.assertIsNone(recovery.read(self.registration, SESSION))

        # Once delivered, an unchanged prompt is short again and nothing wakes the session twice.
        self.supervise()
        ordinary = self.hook('UserPromptSubmit')
        self.assertNotIn(MANDATORY, ordinary)
        self.assertNotIn(SKILLS, ordinary)
        self.assertIn(INTAKE, ordinary)
        self.assertEqual(len([route for route, _ in self.posts if route == RECOVERY_ROUTE]), 1)

    def test_slow_refresh_after_compaction_is_delivered_to_claude_code(self):
        self.slow_refresh_after_compaction('claude-code')

    def test_slow_refresh_after_compaction_is_delivered_to_codex(self):
        self.slow_refresh_after_compaction('codex')

    def compaction_request(self, runtime):
        """The bootstrap request the service receives for a compaction the hook reported."""
        self.begin(runtime)
        self.hook('SessionStart', source='compact')
        self.supervise()
        return [body for route, body in self.posts if route == '/api/agent/bootstrap' and body['event'] == 'compact']

    def test_a_compaction_reaches_the_service_with_its_event_and_session_for_claude_code(self):
        self.assertEqual([(body['event'], body['session_id'], body['runtime']) for body in self.compaction_request('claude-code')],
                         [('compact', SESSION, 'claude-code')])

    def test_a_compaction_reaches_the_service_with_its_event_and_session_for_codex(self):
        self.assertEqual([(body['event'], body['session_id'], body['runtime']) for body in self.compaction_request('codex')],
                         [('compact', SESSION, 'codex')])

    def test_slow_refresh_on_an_ordinary_prompt_needs_no_recovery(self):
        self.begin('codex')
        with mock.patch('qorqut.bootstrap.handoff.offer', return_value=handoff.result_path(self.root, self.native)):
            self.assertEqual(self.hook('UserPromptSubmit'), self.instructions.render('startup-pending'))
        self.assertIsNone(recovery.read(self.registration, SESSION))

    def test_skills_are_sent_in_full_on_request_despite_an_unchanged_digest(self):
        self.begin('codex')
        prompt = {'hook_event_name': 'UserPromptSubmit', 'session_id': SESSION}
        text = lambda **values: always_skills.handle(prompt, self.path, 'codex', **values)['hookSpecificOutput']['additionalContext']
        self.assertEqual(text(), INTAKE)
        self.assertEqual(text(force=True), INTAKE + '\n\n' + SKILLS)


if __name__ == '__main__':
    unittest.main()
