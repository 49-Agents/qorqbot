"""Atomic replacement of one small file."""
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest

from qorqut_system import atomic


class AtomicWrite(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_the_content_is_replaced(self):
        path = self.root / "preferences.json"
        path.write_bytes(b"old")
        atomic.atomic_write(path, b'{"new": true}', mode=0o600)
        self.assertEqual(path.read_bytes(), b'{"new": true}')

    def test_a_missing_folder_is_created(self):
        path = self.root / "a" / "b" / "preferences.json"
        atomic.atomic_write(path, b"x", mode=0o600)
        self.assertEqual(path.read_bytes(), b"x")

    @unittest.skipIf(os.name == "nt", "Windows has no mode bits")
    def test_the_mode_is_applied(self):
        path = self.root / "preferences.json"
        atomic.atomic_write(path, b"x", mode=0o600)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_no_temporary_file_is_left_behind(self):
        path = self.root / "preferences.json"
        atomic.atomic_write(path, b"x", mode=0o600)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["preferences.json"])


if __name__ == "__main__":
    unittest.main()