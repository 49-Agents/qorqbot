"""One private folder per extension: nothing on disk until the first write, owner-only modes, an atomic replace that
keeps one file, plain names only, symlinks refused, and no path or value in an error."""
import os
from pathlib import Path
import stat
import tempfile
import traceback
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut.extension_private import MAX_BYTES, PrivateFolder
from qorqut_operations.contracts import DomainError

SECRET = b"canary-secret-7f3a9c"


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class PrivateFolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.folder = PrivateFolder(self.root, "keyed")

    def assertQuiet(self, problem):
        """The refusal names no path and no value, and carries no chained OS error that would."""
        text = "".join(traceback.format_exception(problem))
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(SECRET.decode(), text)

    def test_construction_and_reads_create_nothing(self):
        self.assertEqual(self.folder.path, self.root / "private" / "extensions" / "keyed")
        self.assertFalse(self.folder.exists("setup.json"))
        self.assertIsNone(self.folder.read("setup.json"))
        self.folder.remove("setup.json")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_write_makes_owner_only_folders_and_file(self):
        self.folder.write("setup.json", SECRET)
        for directory in (self.root / "private", self.root / "private" / "extensions", self.folder.path):
            self.assertEqual(mode(directory), 0o700)
        self.assertEqual(mode(self.folder.path / "setup.json"), 0o600)
        self.assertTrue(self.folder.exists("setup.json"))
        self.assertEqual(self.folder.read("setup.json"), SECRET)

    def test_replace_keeps_one_file(self):
        self.folder.write("setup.json", b"first")
        self.folder.write("setup.json", b"second")
        self.assertEqual(self.folder.read("setup.json"), b"second")
        self.assertEqual(os.listdir(self.folder.path), ["setup.json"])
        self.assertEqual(mode(self.folder.path / "setup.json"), 0o600)

    def test_failed_replace_leaves_no_temporary_file_and_no_path(self):
        self.folder.write("setup.json", b"first")
        with mock.patch("qorqut.extension_private.os.replace", side_effect=OSError(13, "denied", str(self.folder.path))):
            with self.assertRaises(DomainError) as caught:
                self.folder.write("setup.json", SECRET)
        self.assertQuiet(caught.exception)
        self.assertEqual(os.listdir(self.folder.path), ["setup.json"])
        self.assertEqual(self.folder.read("setup.json"), b"first")

    def test_remove(self):
        self.folder.write("setup.json", SECRET)
        self.folder.remove("setup.json")
        self.assertFalse(self.folder.exists("setup.json"))
        self.assertEqual(os.listdir(self.folder.path), [])

    def test_write_keeps_to_the_read_bound(self):
        with self.assertRaises(DomainError) as caught:
            self.folder.write("setup.json", b"x" * (MAX_BYTES + 1))
        self.assertQuiet(caught.exception)
        self.assertEqual(list(self.root.iterdir()), [])
        self.folder.write("setup.json", b"x" * MAX_BYTES)
        self.assertEqual(len(self.folder.read("setup.json")), MAX_BYTES)

    def test_remove_race_names_no_path(self):
        self.folder.write("setup.json", SECRET)
        real_lstat, target = os.lstat, self.folder.path / "setup.json"
        calls = []

        def vanishing(path, *args, **kwargs):     # present when looked for, gone when its kind is read
            if Path(path) == target:
                calls.append(path)
                if len(calls) > 1:
                    raise FileNotFoundError(2, "gone", str(target))
            return real_lstat(path, *args, **kwargs)
        with mock.patch("qorqut.extension_private.os.lstat", side_effect=vanishing):
            with self.assertRaises(DomainError) as caught:
                self.folder.remove("setup.json")
        self.assertQuiet(caught.exception)

    def test_runtime_without_an_instance_folder_refuses(self):
        from qorqut.service.runtime import Runtime
        with self.assertRaises(DomainError) as caught:
            Runtime.extension_private(SimpleNamespace(paths=None), "keyed")
        self.assertEqual(caught.exception.code, "invalid_configuration")
        folder = Runtime.extension_private(SimpleNamespace(paths=SimpleNamespace(instance_root=self.root)), "keyed")
        self.assertEqual(folder.path, self.folder.path)

    def test_names_are_one_plain_segment(self):
        for name in ("", ".", "..", "../x", "a/b", ".hidden", "x" * 200, None):
            for call in (self.folder.exists, self.folder.read, self.folder.remove, self.folder.folder,
                         lambda n: self.folder.write(n, b"x")):
                with self.assertRaises(DomainError):
                    call(name)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_ids_and_root_are_checked(self):
        for bad in ("Keyed", "../x", "", "a-b"):
            with self.assertRaises(DomainError):
                PrivateFolder(self.root, bad)
        with self.assertRaises(DomainError):
            PrivateFolder("relative/root", "keyed")

    def test_exists_reads_nothing(self):
        self.folder.write("setup.json", SECRET)
        with mock.patch("qorqut.extension_private.read_private_file", side_effect=AssertionError("read")), \
                mock.patch("builtins.open", side_effect=AssertionError("open")):
            self.assertTrue(self.folder.exists("setup.json"))

    def test_symlinks_are_refused(self):
        outside = self.root / "outside"
        outside.write_bytes(SECRET)
        os.chmod(outside, 0o600)
        self.folder.write("setup.json", b"x")
        os.symlink(outside, self.folder.path / "linked")
        self.assertFalse(self.folder.exists("linked"))
        with self.assertRaises(DomainError) as caught:
            self.folder.read("linked")
        self.assertQuiet(caught.exception)

    def test_symlinked_folder_is_refused(self):
        real = self.root / "real"
        real.mkdir(mode=0o700)
        (self.root / "private").mkdir(mode=0o700)
        os.chmod(self.root / "private", 0o700)
        os.symlink(real, self.root / "private" / "extensions")
        with self.assertRaises(DomainError) as caught:
            self.folder.write("setup.json", SECRET)
        self.assertQuiet(caught.exception)
        self.assertEqual(list(real.iterdir()), [])

    def test_open_folder_or_file_is_refused(self):
        self.folder.write("setup.json", SECRET)
        os.chmod(self.folder.path / "setup.json", 0o644)
        with self.assertRaises(DomainError) as caught:
            self.folder.read("setup.json")
        self.assertQuiet(caught.exception)
        os.chmod(self.folder.path / "setup.json", 0o600)
        os.chmod(self.folder.path, 0o755)
        with self.assertRaises(DomainError):
            self.folder.read("setup.json")

    def test_sub_folder(self):
        with self.assertRaises(DomainError):
            self.folder.folder("data")
        self.assertEqual(list(self.root.iterdir()), [])
        data = self.folder.folder("data", create=True)
        self.assertEqual(data, self.folder.path / "data")
        self.assertEqual(mode(data), 0o700)
        self.assertEqual(self.folder.folder("data"), data)
        with self.assertRaises(DomainError):
            self.folder.remove("data")


if __name__ == "__main__":
    unittest.main()
