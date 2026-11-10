"""Exact process-lifetime retirement never releases active or uncertain references."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from qorqut.release_worker import lifetime, reference_targets


class PinRetirementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / 'state/process-pins'
        self.directory.mkdir(parents=True)
        self.exited = {**lifetime.local_instance(), 'pid': os.getpid(),
                       'started': 'a-different-start-token', 'cached_client': False,
                       'references': ['runtime/agent-tools/synthetic']}

    def write(self, name, **overrides):
        path = self.directory / (name + '.json')
        path.write_text(json.dumps({**self.exited, **overrides}))
        return path

    def test_retire_verified_exit_pid_reuse_and_previous_boot(self):
        path = self.write('reused')
        self.assertEqual(lifetime.process_state(self.exited)[0], 'exited')
        self.assertTrue(lifetime.retire_process_pin(self.root, path))
        self.assertFalse(path.exists())
        path = self.write('previous-boot', boot_id='previous-boot')
        self.assertTrue(lifetime.retire_process_pin(self.root, path))
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            started = lifetime.identity(process.pid)
            self.assertIsNotNone(started)
            path = self.write('actual-exit', pid=process.pid, started=started)
            self.assertFalse(lifetime.retire_process_pin(self.root, path))
        finally:
            process.terminate()
            process.wait(timeout=5)
        self.assertTrue(lifetime.retire_process_pin(self.root, path))

    def test_preserve_live_unknown_foreign_and_cached_references(self):
        cases = {'live': {'started': lifetime.identity(os.getpid())},
                 'foreign-host': {'host': 'another-host'}, 'remote': {'target': 'remote'},
                 'namespace': {'pid_namespace': 'another-namespace'},
                 'legacy': {'target_instance': None}, 'cached': {'cached_client': True},
                 'unknown-kind': {'cached_client': None}, 'malformed-pid': {'pid': '../other'}}
        for name, fields in cases.items():
            with self.subTest(name=name):
                path = self.write(name, **fields)
                self.assertFalse(lifetime.retire_process_pin(self.root, path))
                self.assertTrue(path.exists())
        path = self.write('unreadable')
        with patch.object(lifetime, 'identity', return_value=None), patch.object(lifetime, 'exited_process', return_value=False):
            self.assertFalse(lifetime.retire_process_pin(self.root, path))

    def test_skip_malformed_oversized_symlink_and_wrong_directory(self):
        path = self.write('bad')
        for text in ('broken json', '[]', '{}', ' ' * (1024 * 1024 + 1)):
            path.write_text(text)
            self.assertFalse(lifetime.retire_process_pin(self.root, path))
        external = self.root / 'external.json'
        external.write_text(json.dumps(self.exited))
        link = self.directory / 'link.json'
        link.symlink_to(external)
        self.assertFalse(lifetime.retire_process_pin(self.root, link))
        self.assertFalse(lifetime.retire_process_pin(self.root, external))
        self.assertTrue(external.exists())

    def test_launch_exclusion_defers_retirement_without_waiting(self):
        path = self.write('exited')
        with lifetime.exclusion(self.root, shared=True):
            self.assertFalse(lifetime.retire_process_pin(self.root, path))
            self.assertTrue(path.exists())
        self.assertTrue(lifetime.retire_process_pin(self.root, path))

    def test_revalidate_reference_after_acquiring_exclusion(self):
        from contextlib import contextmanager
        path = self.write('changed')
        real_exclusion = lifetime.exclusion
        @contextmanager
        def concurrent_reference_update(root, **kwargs):
            self.write('changed', cached_client=True)
            with real_exclusion(root, **kwargs):
                yield
        with patch.object(lifetime, 'exclusion', concurrent_reference_update):
            self.assertFalse(lifetime.retire_process_pin(self.root, path))
        self.assertTrue(path.exists())

    def test_only_background_reconciliation_requests_retirement(self):
        path = self.write('exited')
        # The installed stable launcher writes ordinary pins without this flag.
        value = json.loads(path.read_text())
        del value['cached_client']
        path.write_text(json.dumps(value))
        malformed = self.directory / 'malformed.json'
        malformed.write_text('[]')
        reference_targets.reconcile({'root': str(self.root)})
        self.assertTrue(path.exists())
        reference_targets.reconcile({'root': str(self.root)}, retire_exited=True)
        self.assertFalse(path.exists())
        self.assertTrue(malformed.exists())
        reference_targets.reconcile({'root': str(self.root)}, retire_exited=True)

    def test_retirement_restores_bounded_probe_coverage(self):
        from qorqut.bootstrap.reference_probe import probe
        for index in range(1001):
            self.write(f'exited-{index}')
        live = self.write('live', started=lifetime.identity(os.getpid()), references=['retained-live-artifact'])
        request = {'version': 1, 'request_id': 'probe', 'nonce': 'nonce', 'target': 'local',
                   'target_instance': self.exited['target_instance'], 'records': []}
        self.assertIn('target_reference_scan_incomplete', probe(request, reference_root=self.root)['errors'])
        reference_targets.reconcile({'root': str(self.root)}, retire_exited=True)
        self.assertEqual(list(self.directory.glob('*.json')), [live])
        result = probe(request, reference_root=self.root)
        self.assertEqual(result['errors'], [])
        self.assertEqual(result['additional_references'], ['retained-live-artifact'])


if __name__ == '__main__':
    unittest.main()
