"""Walkthrough progress is stored per owner, validated strictly and written atomically."""
import json
from pathlib import Path
import tempfile
import unittest

from qorqut_operations.contracts import DomainError

from bazarlyk import onboarding_preferences as module


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Path(tempfile.mkdtemp())

    def files(self):
        return sorted(p.name for p in (self.runtime / 'dashboard-preferences').iterdir())

    def test_new_owner_starts_at_the_first_step_without_writing(self):
        self.assertEqual(module.progress(self.runtime, 'owner'), {'version': 1, 'status': 'new', 'step': 'assistant'})
        self.assertFalse((self.runtime / 'dashboard-preferences').exists())

    def test_saved_progress_round_trips_for_every_step(self):
        for step in module.STEPS:
            with self.subTest(step=step):
                saved = module.progress(self.runtime, 'owner', {'status': 'in_progress', 'step': step})
                self.assertEqual(saved, {'version': 1, 'status': 'in_progress', 'step': step})
                self.assertEqual(module.progress(self.runtime, 'owner'), saved)

    def test_owners_are_kept_apart(self):
        module.progress(self.runtime, 'a', {'status': 'completed', 'step': 'settings'})
        self.assertEqual(module.progress(self.runtime, 'b')['status'], 'new')

    def test_earlier_step_ids_stay_readable(self):
        # Progress saved by the first walkthrough version must load after steps were added.
        for step in ('assistant', 'terminal', 'chat', 'focus', 'calendar', 'learning'):
            self.assertIn(step, module.STEPS)

    def test_invalid_changes_are_refused_and_nothing_is_written(self):
        for change in ({'status': 'done', 'step': 'chat'}, {'status': 'skipped', 'step': 'unknown'},
                       {'status': 'skipped'}, {'status': 'skipped', 'step': 'chat', 'extra': 1},
                       {'status': 1, 'step': 'chat'}, ['skipped', 'chat']):
            with self.subTest(change=change), self.assertRaises(DomainError):
                module.progress(self.runtime, 'owner', change)
        self.assertFalse((self.runtime / 'dashboard-preferences').exists())

    def test_corrupt_file_is_reported_not_reset(self):
        module.progress(self.runtime, 'owner', {'status': 'skipped', 'step': 'chat'})
        path = next((self.runtime / 'dashboard-preferences').glob('*-onboarding.json'))
        for content in ('{', json.dumps({'version': 2, 'status': 'new', 'step': 'chat'}), '[]'):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaises(DomainError):
                module.progress(self.runtime, 'owner')
        self.assertEqual(path.read_text(), '[]')

    def test_write_leaves_no_temporary_files(self):
        module.progress(self.runtime, 'owner', {'status': 'in_progress', 'step': 'projects'})
        self.assertFalse([name for name in self.files() if name.startswith('.onboarding-')])


if __name__ == '__main__':
    unittest.main()
