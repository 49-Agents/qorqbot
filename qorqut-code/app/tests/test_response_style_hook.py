"""Prompt reminders survive cached delivery without changing configuration receipts."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError
from qorqut.agents.context import MAX_CONTEXT_CHARACTERS
from qorqut_agentic.behavior.instructions import from_catalog
from qorqut_agentic.behavior.composition import split_sections
from qorqut.bootstrap.claude_session import handle_hook
from qorqut.catalog.packages import content_hash


class ResponseStyleHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.session = '00000000-0000-4000-8000-000000000001'
        self.registration = {'registration_id': 'synthetic', 'workspace': str(self.root)}
        instruction_root = Path(__file__).resolve().parents[3] / 'qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/runtime-instructions'
        self.instructions = from_catalog(lambda path: (instruction_root / path).read_text())
        self.reminder = self.instructions.render('response-style-reminder').strip()
        (self.root / 'runtime-instructions.json').write_bytes(self.instructions.bytes())
        self.response = {'ready': True, 'effective_config_id': 'config-1',
                         'mandatory_context': {'rendered': 'Mandatory instructions.'}}
        for target, value in (
            ('qorqut.bootstrap.claude_session.load_registration', self.registration),
            ('qorqut.bootstrap.native_binding.attest', None),
            ('qorqut.bootstrap.handoff.request', None),
            ('qorqut.bootstrap.recovery.read', None),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def cached_receipt(self):
        path = self.root / '.qorqut/delivery' / (hashlib.sha256(self.session.encode()).hexdigest() + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**self.registration, 'effective_config_id': 'config-1'}))

    def deliver(self, runtime, event='UserPromptSubmit', **hook_fields):
        client = SimpleNamespace(post=lambda *_args: self.response)
        return handle_hook({'hook_event_name': event, 'session_id': self.session, **hook_fields},
                           self.root / 'registration.json', runtime=runtime, client_factory=lambda _: client)

    def test_cached_prompt_delivers_only_reminder_without_new_receipt(self):
        self.cached_receipt()
        self.response.pop('mandatory_context')
        for runtime in ('claude-code', 'codex'):
            with self.subTest(runtime=runtime):
                output, receipt, _ = self.deliver(runtime)
                self.assertEqual(output['hookSpecificOutput'], {
                    'hookEventName': 'UserPromptSubmit',
                    'additionalContext': self.reminder})
                self.assertIsNone(receipt)

    def test_refresh_delivers_reminder_last_and_preserves_configuration_hash(self):
        self.cached_receipt()
        self.response['refresh_required'] = True
        for runtime in ('claude-code', 'codex'):
            with self.subTest(runtime=runtime):
                output, receipt, _ = self.deliver(runtime)
                text = output['hookSpecificOutput']['additionalContext']
                self.assertTrue(text.startswith('Mandatory instructions.'))
                self.assertTrue(text.endswith(self.reminder))
                self.assertEqual(text.count(self.reminder), 1)
                self.assertEqual(receipt['bundle_hash'], content_hash(b'Mandatory instructions.'))

    def test_startup_and_compaction_do_not_add_prompt_reminder(self):
        for runtime in ('claude-code', 'codex'):
            for source in ('startup', 'compact'):
                with self.subTest(runtime=runtime, source=source):
                    output, receipt, _ = self.deliver(runtime, 'SessionStart', source=source)
                    self.assertNotIn(self.reminder, output['hookSpecificOutput']['additionalContext'])
                    self.assertIsNotNone(receipt)

    @mock.patch('qorqut.bootstrap.claude_session.BOOTSTRAP_BUDGET', MAX_CONTEXT_CHARACTERS)
    def test_reminder_has_space_before_optional_briefing_and_environment(self):
        reminder = self.reminder
        mandatory = 'x' * (MAX_CONTEXT_CHARACTERS - len(reminder) - 2)
        self.response['mandatory_context'] = mandatory
        self.response['work_briefing'] = {'rendered': 'Optional briefing.', 'compact': True}
        for runtime in ('claude-code', 'codex'):
            with self.subTest(runtime=runtime):
                output, receipt, _ = self.deliver(runtime)
                self.assertEqual(output['hookSpecificOutput']['additionalContext'], mandatory + '\n\n' + reminder)
                self.assertIsNone(receipt['briefing_hash'])

    def test_cached_prompt_preserves_operational_skills(self):
        self.cached_receipt()
        self.registration['operational_intake_enabled'] = True
        with mock.patch('qorqut.bootstrap.always_skills.handle', return_value={
                'hookSpecificOutput': {'additionalContext': 'Operational skills.'}}):
            for runtime in ('claude-code', 'codex'):
                with self.subTest(runtime=runtime):
                    output, receipt, _ = self.deliver(runtime)
                    self.assertEqual(output['hookSpecificOutput']['additionalContext'],
                                     'Operational skills.\n\n' + self.reminder)
                    self.assertIsNone(receipt)

    def test_oversized_reminder_is_rejected_before_cached_return(self):
        self.cached_receipt()
        self.instructions.document['messages']['response-style-reminder']['body'] = 'x' * 1001
        (self.root / 'runtime-instructions.json').write_bytes(self.instructions.bytes())
        with self.assertRaises(DomainError) as caught:
            self.deliver('codex')
        self.assertEqual(caught.exception.code, 'invalid_bootstrap')

    def test_unready_bootstrap_cannot_be_replaced_by_a_style_reminder(self):
        self.cached_receipt()
        self.response['ready'] = False
        with self.assertRaises(DomainError):
            self.deliver('codex')

    def test_prepared_reminder_overrides_packaged_text(self):
        self.cached_receipt()
        self.instructions.document['messages']['response-style-reminder']['body'] = 'Current managed reminder.'
        (self.root / 'runtime-instructions.json').write_bytes(self.instructions.bytes())
        output, receipt, _ = self.deliver('codex')
        self.assertEqual(output['hookSpecificOutput']['additionalContext'], 'Current managed reminder.')
        self.assertIsNone(receipt)

    def test_style_is_an_instruction_before_user_preferences(self):
        rules, facts = split_sections('Configuration', [
            ('User Preferences', 'Personal preferences'),
            ('Identity And Assignment', 'Current identity'),
            ('Response Style', 'Shared style'),
            ('Engineering workspace and authority', 'Existing authority rules')])
        self.assertLess(rules.index('Shared style'), rules.index('Personal preferences'))
        self.assertIn('Existing authority rules', rules)
        self.assertNotIn('Shared style', facts)
        self.assertIn('Current identity', facts)


if __name__ == '__main__':
    unittest.main()
