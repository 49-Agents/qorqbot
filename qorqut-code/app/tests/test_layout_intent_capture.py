"""Reading the export manifest into a layout intent (qorqut.agentic.layout_intent._capture_manifest).

Run from the repository root with the source directories on PYTHONPATH:
    python -m unittest discover -s qorqut-code/app/tests -p test_layout_intent_capture.py

The manifest's own validation is replaced by a recorder, so these tests cover the file reading: the bytes and digest
it returns, that it refuses a link or a pipe in the manifest's place, and that it closes the export folder.
"""
import base64
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from qorqut import source_snapshot
from qorqut.agentic import layout_intent
from qorqut.agentic.support import digest

RAW = b'{"components": []}\n'


class CaptureManifestTests(unittest.TestCase):
    def setUp(self):
        # The export path is opened one part at a time without following links, so use the folder's real path.
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.plan = {'export': {'root': str(self.root)}}
        self.checked = []
        patcher = mock.patch.object(layout_intent, '_manifest', side_effect=lambda raw, plan: self.checked.append(raw))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.opened = []
        real = source_snapshot._directory

        def recording(path):
            descriptor = real(path)
            self.opened.append(descriptor)
            return descriptor

        patcher = mock.patch.object(source_snapshot, '_directory', side_effect=recording)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_folder_closed(self):
        self.assertEqual(len(self.opened), 1)
        with self.assertRaises(OSError):
            os.fstat(self.opened[0])

    def test_returns_the_manifest_bytes_and_digest(self):
        (self.root / 'SOURCE-MANIFEST.json').write_bytes(RAW)
        captured = layout_intent._capture_manifest(self.plan)
        self.assertEqual(captured, {'sha256': digest(RAW), 'content_base64': base64.b64encode(RAW).decode('ascii')})
        self.assertEqual(self.checked, [RAW])
        self.assert_folder_closed()

    def test_refuses_a_link_in_place_of_the_manifest(self):
        (self.root / 'elsewhere.json').write_bytes(RAW)
        (self.root / 'SOURCE-MANIFEST.json').symlink_to(self.root / 'elsewhere.json')
        with self.assertRaises(OSError):
            layout_intent._capture_manifest(self.plan)
        self.assertEqual(self.checked, [])
        self.assert_folder_closed()

    def test_refuses_a_pipe_in_place_of_the_manifest_without_waiting(self):
        os.mkfifo(self.root / 'SOURCE-MANIFEST.json')
        with self.assertRaisesRegex(ValueError, 'not a regular file'):
            layout_intent._capture_manifest(self.plan)
        self.assertEqual(self.checked, [])
        self.assert_folder_closed()

    def test_closes_the_folder_when_the_manifest_is_missing(self):
        with self.assertRaises(FileNotFoundError):
            layout_intent._capture_manifest(self.plan)
        self.assert_folder_closed()


if __name__ == '__main__':
    unittest.main()
