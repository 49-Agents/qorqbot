import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest

from qorqut_operations.contracts import DomainError

from bazarlyk import posix_files


@unittest.skipUnless(posix_files.publication_supported(), "exclusive publication needs Linux O_TMPFILE or macOS")
class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.target = self.root / "target"
        self.target.mkdir(mode=0o700)
        self.fd = os.open(self.target, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.staging = self.root / "staging"
        posix_files.prepare_publication(self.staging)

    def write(self, data=b"content"):
        pending = posix_files.anonymous_file(self.fd, read=True)
        os.write(pending.fd, data)
        os.fsync(pending.fd)
        return pending

    def test_nothing_is_named_at_the_destination_until_publication(self):
        pending = self.write()
        try:
            self.assertEqual(os.listdir(self.target), [])
            identity = os.fstat(pending.fd).st_ino
            posix_files.publish(pending, self.fd, "file")
            self.assertEqual((self.target / "file").read_bytes(), b"content")
            # The recorded identity of the written file is the published file's (git index journal relies on it).
            self.assertEqual(os.stat(self.target / "file").st_ino, identity)
            self.assertEqual(stat.S_IMODE(os.stat(self.target / "file").st_mode), 0o600)
        finally:
            posix_files.discard(pending)
        self.assertEqual(os.listdir(self.target), ["file"])
        if sys.platform == "darwin":
            self.assertEqual(os.listdir(self.staging), [])

    def test_an_existing_name_is_never_replaced(self):
        (self.target / "file").write_bytes(b"original")
        pending = self.write(b"new")
        try:
            with self.assertRaises(FileExistsError):
                posix_files.publish(pending, self.fd, "file")
        finally:
            posix_files.discard(pending)
        self.assertEqual((self.target / "file").read_bytes(), b"original")
        if sys.platform == "darwin":
            self.assertEqual(os.listdir(self.staging), [])

    def test_a_discarded_file_leaves_nothing(self):
        posix_files.discard(self.write())
        self.assertEqual(os.listdir(self.target), [])
        if sys.platform == "darwin":
            self.assertEqual(os.listdir(self.staging), [])

    def test_publishing_twice_is_refused(self):
        pending = self.write()
        try:
            posix_files.publish(pending, self.fd, "one")
            with self.assertRaises(OSError):
                posix_files.publish(pending, self.fd, "two")
        finally:
            posix_files.discard(pending)


@unittest.skipUnless(sys.platform == "darwin", "macOS staging")
class DarwinStagingTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_staging_is_private_and_only_its_own_leftovers_are_cleared(self):
        staging = self.root / "staging"
        staging.mkdir(mode=0o700)
        (staging / ".publish-interrupted").write_bytes(b"partial")
        (staging / "unrelated").write_bytes(b"kept")
        posix_files.prepare_publication(staging)
        self.assertEqual(sorted(os.listdir(staging)), ["unrelated"])

    def test_a_shared_staging_directory_is_refused(self):
        staging = self.root / "shared"
        staging.mkdir()
        os.chmod(staging, 0o755)
        with self.assertRaises(DomainError):
            posix_files.prepare_publication(staging)

    def test_unprepared_staging_fails_closed(self):
        posix_files._staging.pop("fd", None)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(DomainError):
                posix_files.anonymous_file(fd)
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
