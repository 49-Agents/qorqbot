"""A self-contained package ships its programs and a ready-made environment; the application finds and accepts both."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from qorqut import native_paths
from qorqut.bootstrap import initialize


class ShippedPrograms(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name).resolve()
        self.bin = self.folder / 'runtime/native/bin'
        self.bin.mkdir(parents=True)
        patch = mock.patch.dict(os.environ, {'QORQUT_FOLDER_ROOT': str(self.folder)})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('QORQUT_NATIVE_ROOT', None)

    def program(self, name, mode=0o755):
        path = self.bin / name
        path.write_text('#!/bin/sh\n')
        path.chmod(mode)
        return str(path)

    def test_a_shipped_program_is_used_before_the_computers_own(self):
        git = self.program('git')
        self.assertEqual(native_paths.shipped('git'), git)
        self.assertEqual(native_paths.executable('git', '/usr/bin/git'), git)

    def test_a_program_the_package_does_not_ship_keeps_its_default(self):
        self.program('notes', mode=0o644)  # present but not a program
        self.assertIsNone(native_paths.shipped('notes'))
        self.assertIsNone(native_paths.shipped('tmux'))
        self.assertEqual(native_paths.executable('tmux', '/usr/bin/tmux'), '/usr/bin/tmux')
        self.assertEqual(native_paths.executable('tmux'), 'tmux')

    def test_without_an_application_folder_nothing_is_shipped(self):
        del os.environ['QORQUT_FOLDER_ROOT']
        self.assertIsNone(native_paths.shipped('git'))


class Distribution:
    def __init__(self, version, origin):
        self.version, self.origin = version, origin

    def read_text(self, name):
        return None if self.origin is None else json.dumps(self.origin)


class ReadyMadeEnvironmentOrigin(unittest.TestCase):
    """The environment was installed on the build machine, so pip recorded that machine's path to the wheel."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.prepared = Path(temporary.name).resolve() / 'runtime'
        (self.prepared / 'wheels').mkdir(parents=True)
        self.file = self.prepared / 'wheels/qorqut-1.0-py3-none-any.whl'
        self.file.write_bytes(b'wheel bytes')
        self.digest = hashlib.sha256(b'wheel bytes').hexdigest()
        self.wheel = {'name': 'qorqut', 'version': '1.0', 'file': 'wheels/' + self.file.name, 'sha256': self.digest}

    def check(self, url, digest='same', version='1.0'):
        origin = {'url': url, 'archive_info': {}}
        if digest is not None:
            origin['archive_info'] = {'hashes': {'sha256': self.digest if digest == 'same' else digest}}
        with mock.patch.object(initialize.metadata, 'distribution', return_value=Distribution(version, origin)):
            initialize._require_wheel_origin(self.prepared, 'qorqut', self.wheel)

    def test_the_wheel_file_itself_is_accepted_as_before(self):
        self.check(self.file.as_uri())
        self.check(self.file.as_uri(), digest=None)

    def test_another_machines_copy_is_accepted_only_with_the_same_name_and_recorded_digest(self):
        elsewhere = 'file:///build/work/package/qorqbot/runtime/wheels/'
        self.check(elsewhere + self.file.name)
        for url, digest in ((elsewhere + self.file.name, None),            # nothing proves it was the same archive
                            (elsewhere + self.file.name, 'f' * 64),        # a different archive
                            (elsewhere + 'qorqut-1.0-py3-none-other.whl', 'same')):
            with self.subTest(url=url, digest=digest), self.assertRaises(initialize.InitializationError):
                self.check(url, digest=digest)

    def test_a_changed_receipt_wheel_is_still_refused(self):
        self.file.write_bytes(b'other bytes')
        with self.assertRaisesRegex(initialize.InitializationError, 'no longer has its recorded SHA-256'):
            self.check('file:///build/work/package/qorqbot/runtime/wheels/' + self.file.name)


if __name__ == '__main__':
    unittest.main()
