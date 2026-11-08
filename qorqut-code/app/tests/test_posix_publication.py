import contextlib
import errno
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

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


@contextlib.contextmanager
def foreign_device(*directories):
    """Make descriptors of these directories report another st_dev (a second file system without needing one)."""
    marked = {os.stat(directory).st_ino for directory in directories}
    real = os.fstat

    def fstat(fd):
        result = real(fd)
        if result.st_ino in marked:
            return os.stat_result((result.st_mode, result.st_ino, result.st_dev + 1, *result[3:]))
        return result
    with mock.patch.object(os, "fstat", fstat):
        yield


@contextlib.contextmanager
def tracked_opens():
    """Record every descriptor ``os.open`` returns while active."""
    opened, real = [], os.open

    def tracking(*args, **kwargs):
        fd = real(*args, **kwargs)
        opened.append(fd)
        return fd
    with mock.patch.object(os, "open", tracking):
        yield opened


class SimulatedDarwinTest(unittest.TestCase):
    """macOS staging checks run on any POSIX host by simulating the platform test; this is not a native macOS run."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.previous = dict(posix_files._staging)
        posix_files._staging.clear()  # Isolate the fixture without closing another test's staging fd.
        self.addCleanup(self.restore_staging)
        simulated = mock.patch.object(posix_files, "_darwin", return_value=True)
        simulated.start()
        self.addCleanup(simulated.stop)
        self.staging = self.root / "staging"
        posix_files.prepare_publication(self.staging)
        self.registered = posix_files._staging["fd"]
        self.destination = self.root / "destination"
        self.destination.mkdir(mode=0o700)

    def restore_staging(self):
        current = posix_files._staging.get("fd")
        if current is not None and current != self.previous.get("fd"):
            os.close(current)
        posix_files._staging.clear()
        posix_files._staging.update(self.previous)

    def assertClosed(self, descriptors):
        for fd in descriptors:
            with self.assertRaises(OSError) as raised:
                os.fstat(fd)
            self.assertEqual(raised.exception.errno, errno.EBADF)

    def test_matching_devices_pass_and_leave_no_descriptor_open(self):
        with tracked_opens() as opened:
            posix_files.require_same_device(self.destination, self.root)
        self.assertEqual(len(opened), 2)
        self.assertClosed(opened)

    def test_a_different_device_is_refused_with_exdev_and_closes_its_descriptor(self):
        with foreign_device(self.destination), tracked_opens() as opened:
            with self.assertRaises(OSError) as raised:
                posix_files.require_same_device(self.root, self.destination)
        self.assertEqual(raised.exception.errno, errno.EXDEV)
        self.assertIn(str(self.destination), str(raised.exception))
        self.assertEqual(len(opened), 2)
        self.assertClosed(opened)

    def test_the_preflight_creates_and_removes_nothing(self):
        (self.staging / ".publish-stale").write_bytes(b"partial")
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        with foreign_device(self.destination):
            with self.assertRaises(OSError):
                posix_files.require_same_device(self.destination)
        posix_files.require_same_device(self.destination)
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")), before)

    def test_a_symlinked_destination_is_not_followed(self):
        link = self.root / "link"
        link.symlink_to(self.destination)
        with tracked_opens() as opened:
            with self.assertRaises(OSError):
                posix_files.require_same_device(link)
        self.assertClosed(opened)

    def test_a_missing_destination_fails_closed(self):
        with self.assertRaises(FileNotFoundError):
            posix_files.require_same_device(self.root / "missing")

    @unittest.skipUnless(os.path.isdir("/dev/shm"), "needs a second file system")
    def test_a_real_second_file_system_is_refused(self):
        try:
            other = Path(tempfile.mkdtemp(dir="/dev/shm"))
        except OSError:
            self.skipTest("/dev/shm is not writable")
        self.addCleanup(shutil.rmtree, other)
        if os.stat(other).st_dev == os.stat(self.staging).st_dev:
            self.skipTest("/dev/shm shares the temporary directory's file system")
        with self.assertRaises(OSError) as raised:
            posix_files.require_same_device(other)
        self.assertEqual(raised.exception.errno, errno.EXDEV)

    def test_unprepared_staging_fails_closed(self):
        posix_files._staging.pop("fd")
        with tracked_opens() as opened:
            with self.assertRaises(DomainError):
                posix_files.require_same_device(self.destination)
        self.assertEqual(opened, [])

    def test_a_failed_listing_closes_the_new_descriptor_and_keeps_the_previous_staging(self):
        with tracked_opens() as opened, mock.patch.object(os, "listdir", side_effect=OSError(errno.EIO, "listing")):
            with self.assertRaises(OSError):
                posix_files.prepare_publication(self.root / "second")
        self.assertEqual(len(opened), 1)
        self.assertClosed(opened)
        self.assertEqual(posix_files._staging["fd"], self.registered)
        os.fstat(self.registered)

    def test_a_failed_removal_closes_the_new_descriptor_and_keeps_the_previous_staging(self):
        second = self.root / "second"
        second.mkdir(mode=0o700)
        (second / ".publish-stale").write_bytes(b"partial")
        with tracked_opens() as opened, mock.patch.object(os, "unlink", side_effect=PermissionError(errno.EPERM, "unlink")):
            with self.assertRaises(PermissionError):
                posix_files.prepare_publication(second)
        self.assertEqual(len(opened), 1)
        self.assertClosed(opened)
        self.assertEqual(posix_files._staging["fd"], self.registered)
        os.fstat(self.registered)
        self.assertEqual(os.listdir(second), [".publish-stale"])

    def test_an_unsafe_staging_directory_closes_its_descriptor_and_keeps_the_previous_staging(self):
        shared = self.root / "shared"
        shared.mkdir()
        os.chmod(shared, 0o755)
        with tracked_opens() as opened:
            with self.assertRaises(DomainError):
                posix_files.prepare_publication(shared)
        self.assertClosed(opened)
        self.assertEqual(posix_files._staging["fd"], self.registered)

    def test_a_successful_preparation_replaces_and_closes_the_previous_staging(self):
        (self.staging / ".publish-stale").write_bytes(b"partial")
        posix_files.prepare_publication(self.root / "second")
        self.assertNotEqual(posix_files._staging["fd"], self.registered)
        self.assertClosed([self.registered])
        os.fstat(posix_files._staging["fd"])


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux publication path")
class LinuxUnchangedTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.previous = dict(posix_files._staging)
        self.addCleanup(lambda: (posix_files._staging.clear(), posix_files._staging.update(self.previous)))

    def test_the_preflight_is_a_no_op_without_staging_or_directories(self):
        posix_files._staging.pop("fd", None)
        missing = self.root / "missing"
        with tracked_opens() as opened, foreign_device(self.root):
            posix_files.require_same_device(self.root, missing)
        self.assertEqual(opened, [])

    def test_preparation_is_a_no_op(self):
        posix_files._staging.pop("fd", None)
        posix_files.prepare_publication(self.root / "staging")
        self.assertFalse((self.root / "staging").exists())
        self.assertNotIn("fd", posix_files._staging)

    @unittest.skipUnless(hasattr(os, "O_TMPFILE"), "needs O_TMPFILE")
    def test_publication_does_not_use_or_require_staging(self):
        posix_files._staging.pop("fd", None)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        with foreign_device(self.root):
            pending = posix_files.anonymous_file(fd)
        try:
            os.write(pending.fd, b"linux")
            posix_files.publish(pending, fd, "file")
        finally:
            posix_files.discard(pending)
        self.assertEqual((self.root / "file").read_bytes(), b"linux")


if __name__ == "__main__":
    unittest.main()
