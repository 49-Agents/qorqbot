"""Locks: one holder at a time, shared readers, waiting or failing at once."""
from pathlib import Path
import shutil
import tempfile
import unittest

from qorqut_system.errors import PrivateFileError
from qorqut_system.locks import acquire, held, lock, open_lock, release


class Locks(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(self.ignore_missing_lock_file)

    def ignore_missing_lock_file(self):
        for path in self.root.glob("lock"):
            path.unlink(missing_ok=True)

    def test_an_exclusive_lock_holds_off_a_second_holder(self):
        with lock(self.root, "lock"):
            with self.assertRaises(BlockingIOError):
                with lock(self.root, "lock", wait=False):
                    pass

    def test_the_lock_is_released_when_the_block_ends(self):
        with lock(self.root, "lock"):
            pass
        with lock(self.root, "lock", wait=False):
            pass

    def test_shared_locks_can_be_held_twice(self):
        with lock(self.root, "lock", exclusive=False):
            with lock(self.root, "lock", exclusive=False, wait=False):
                pass

    def test_a_shared_lock_holds_off_an_exclusive_holder(self):
        with lock(self.root, "lock", exclusive=False):
            with self.assertRaises(BlockingIOError):
                with lock(self.root, "lock", wait=False):
                    pass

    def test_the_lock_file_is_created_in_the_folder(self):
        with lock(self.root, "lock"):
            pass
        self.assertTrue((self.root / "lock").is_file())

    def test_an_opened_lock_is_taken_and_let_go_by_hand(self):
        from qorqut_system.locks import open_lock
        handle = open_lock(self.root, "lock")
        self.addCleanup(handle.close)
        handle.acquire()
        with self.assertRaises(BlockingIOError):
            with lock(self.root, "lock", wait=False):
                pass
        handle.release()
        with lock(self.root, "lock", wait=False):
            pass

    def test_a_shared_lock_is_readable_without_the_file_being_created(self):
        with self.assertRaises(FileNotFoundError):
            with lock(self.root, "lock", exclusive=False, create=False):
                pass


class LockHandles(unittest.TestCase):
    """The lock file opened once, then held, released or closed by the caller."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_a_held_handle_holds_off_a_second_holder_and_releasing_lets_it_in(self):
        first = open_lock(self.root, "lock")
        self.addCleanup(first.close)
        first.acquire()
        second = open_lock(self.root, "lock")
        self.addCleanup(second.close)
        with self.assertRaises(BlockingIOError):
            second.acquire(wait=False)
        first.release()
        second.acquire(wait=False)
        second.release()

    def test_closing_a_handle_releases_its_lock(self):
        first = open_lock(self.root, "lock")
        first.acquire()
        first.close()
        with lock(self.root, "lock", wait=False):
            pass

    def test_an_existing_file_can_be_locked_without_creating_one(self):
        with self.assertRaises(FileNotFoundError):
            open_lock(self.root, "lock", create=False)
        with lock(self.root, "lock"):
            pass
        handle = open_lock(self.root, "lock", create=False, write=False)
        self.addCleanup(handle.close)
        handle.acquire(exclusive=False, wait=False)
        handle.release()

    def test_the_handle_reports_which_file_it_opened(self):
        handle = open_lock(self.root, "lock", verify=True)
        self.addCleanup(handle.close)
        info = (self.root / "lock").stat()
        self.assertEqual(handle.identity, (info.st_dev, info.st_ino))
        handle.verify()

    def test_a_lock_file_can_be_opened_without_waiting(self):
        handle = open_lock(self.root, "lock", nonblock=True)
        self.addCleanup(handle.close)
        handle.acquire(exclusive=True, wait=False)
        handle.release()

    def test_a_replaced_file_is_refused_by_verify(self):
        handle = open_lock(self.root, "lock", verify=True)
        self.addCleanup(handle.close)
        (self.root / "lock").unlink()
        open_lock(self.root, "lock").close()
        with self.assertRaises(PrivateFileError):
            handle.verify()

    def test_a_file_the_caller_opened_can_be_held(self):
        path = self.root / "lock"
        with path.open("a") as first:
            with held(first):
                self.assertFalse(first.closed)
                with path.open("a") as second:
                    with self.assertRaises(BlockingIOError):
                        acquire(second, wait=False)
        with path.open("a") as third:
            with held(third):
                pass

    def test_a_shared_lock_can_be_held_twice_on_caller_opened_files(self):
        path = self.root / "lock"
        with path.open("a") as first, path.open("a") as second:
            acquire(first, exclusive=False)
            try:
                acquire(second, exclusive=False, wait=False)
                release(second)
            finally:
                release(first)

    def test_a_shared_lock_can_be_held_twice_through_handles(self):
        first = open_lock(self.root, "lock", write=False)
        self.addCleanup(first.close)
        first.acquire(exclusive=False)
        second = open_lock(self.root, "lock", write=False)
        self.addCleanup(second.close)
        second.acquire(exclusive=False, wait=False)
        second.release()
        first.release()


if __name__ == "__main__":
    unittest.main()