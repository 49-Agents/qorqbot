"""Private files: a folder and a file that only this user can use."""
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest

from qorqut_system import private_files
from qorqut_system.errors import PrivateFileError


class PrivateFiles(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_a_created_file_reads_back(self):
        path = private_files.write_private_file(self.root, "token", b"secret")
        self.assertEqual(path, self.root / "token")
        self.assertEqual(private_files.read_private_file(path), b"secret")

    def test_an_existing_file_is_never_replaced(self):
        private_files.write_private_file(self.root, "token", b"first")
        with self.assertRaises(FileExistsError):
            private_files.write_private_file(self.root, "token", b"second")
        self.assertEqual(private_files.read_private_file(self.root / "token"), b"first")

    def test_a_file_over_its_bound_is_refused(self):
        path = private_files.write_private_file(self.root, "token", b"0123456789")
        with self.assertRaises(PrivateFileError):
            private_files.read_private_file(path, max_bytes=5)

    def test_a_missing_directory_is_refused_and_can_be_created(self):
        missing = self.root / "later"
        with self.assertRaises(PrivateFileError):
            private_files.require_private_directory(missing)
        private_files.require_private_directory(missing, create=True)
        self.assertTrue(missing.is_dir())

    def test_a_relative_path_is_refused(self):
        with self.assertRaises(PrivateFileError):
            private_files.require_private_directory("relative")
        with self.assertRaises(PrivateFileError):
            private_files.read_private_file("relative")

    def test_a_name_may_not_hold_a_separator(self):
        with self.assertRaises(PrivateFileError):
            private_files.write_private_file(self.root, os.sep + "escape", b"x")

    def test_the_folder_is_synced(self):
        private_files.write_private_file(self.root, "token", b"secret")
        private_files.fsync_directory(self.root)
        with self.assertRaises(OSError):
            private_files.fsync_directory(self.root / "missing")

    def test_a_link_is_not_followed(self):
        target = private_files.write_private_file(self.root, "target", b"secret")
        link = self.root / "link"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("This system does not let this user make a symbolic link")
        with self.assertRaises(PrivateFileError):
            private_files.read_private_file(link)

    @unittest.skipIf(os.name == "nt", "Windows has no mode bits")
    def test_a_world_readable_file_is_refused(self):
        path = private_files.write_private_file(self.root, "token", b"secret")
        os.chmod(path, 0o644)
        with self.assertRaises(PrivateFileError):
            private_files.read_private_file(path)

    @unittest.skipIf(os.name == "nt", "Windows has no mode bits")
    def test_a_shared_directory_is_refused(self):
        shared = self.root / "shared"
        shared.mkdir()
        os.chmod(shared, 0o755)
        with self.assertRaises(PrivateFileError):
            private_files.require_private_directory(shared)

    @unittest.skipIf(os.name == "nt", "Windows has no mode bits")
    def test_the_file_is_created_private(self):
        path = private_files.write_private_file(self.root, "token", b"secret")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), private_files.FILE_MODE)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), private_files.DIRECTORY_MODE)


class FolderOperations(unittest.TestCase):
    """The folder opened once and the operations inside it, as the feature files use them."""

    def setUp(self):
        self.parent = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.parent)
        self.folder = private_files.open_folder(self.parent / "preferences", create=True)
        self.addCleanup(self.folder.close)

    def test_the_folder_is_created_private(self):
        self.assertTrue((self.parent / "preferences").is_dir())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.parent / "preferences").stat().st_mode),
                             private_files.DIRECTORY_MODE)

    def test_a_created_file_reads_back_through_the_folder(self):
        private_files.create_entry(self.folder, "token", b"secret")
        descriptor = private_files.open_read(self.folder, "token")
        with os.fdopen(descriptor, "rb") as stream:
            self.assertEqual(stream.read(), b"secret")

    def test_an_existing_file_is_never_created_again(self):
        private_files.create_entry(self.folder, "token", b"first")
        with self.assertRaises(FileExistsError):
            private_files.create_entry(self.folder, "token", b"second")

    def test_a_replacement_swaps_the_content_and_leaves_no_temporary(self):
        private_files.create_entry(self.folder, "store", b"first")
        private_files.replace_entry(self.folder, "store", b"second", prefix=".store-")
        private_files.sync_folder(self.folder)
        descriptor = private_files.open_read(self.folder, "store")
        with os.fdopen(descriptor, "rb") as stream:
            self.assertEqual(stream.read(), b"second")
        self.assertEqual(private_files.list_entries(self.folder), ["store"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.parent / "preferences" / "store").stat().st_mode),
                             private_files.FILE_MODE)

    def test_an_append_adds_to_the_file(self):
        private_files.create_entry(self.folder, "log", b"one\n")
        private_files.append_entry(self.folder, "log", b"two\n")
        descriptor = private_files.open_read(self.folder, "log")
        with os.fdopen(descriptor, "rb") as stream:
            self.assertEqual(stream.read(), b"one\ntwo\n")

    def test_a_missing_file_is_removed_without_an_error(self):
        private_files.remove_entry(self.folder, "missing", missing_ok=True)
        with self.assertRaises(FileNotFoundError):
            private_files.remove_entry(self.folder, "missing")
        private_files.create_entry(self.folder, "token", b"secret")
        private_files.remove_entry(self.folder, "token")
        self.assertEqual(private_files.list_entries(self.folder), [])

    def test_a_link_is_not_read_through(self):
        private_files.create_entry(self.folder, "target", b"secret")
        target = self.parent / "preferences" / "target"
        link = self.parent / "preferences" / "link"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("This system does not let this user make a symbolic link")
        with self.assertRaises((OSError, PrivateFileError)):
            private_files.open_read(self.folder, "link")

    @unittest.skipIf(os.name == "nt", "Windows has no mode bits")
    def test_privacy_of_a_file_is_reported_and_can_be_set(self):
        private_files.create_entry(self.folder, "token", b"secret")
        self.assertTrue(private_files.is_mine(private_files.stat_folder(self.folder)))
        self.assertTrue(private_files.is_private_and_mine(private_files.stat_folder(self.folder)))
        descriptor = private_files.open_read(self.folder, "token")
        try:
            info = os.fstat(descriptor)
            self.assertTrue(private_files.is_private_and_mine(info))
            self.assertEqual(private_files.mode_of(info), private_files.FILE_MODE)
            private_files.set_private(descriptor, mode=0o400)
            self.assertEqual(private_files.mode_of(os.fstat(descriptor)), 0o400)
        finally:
            os.close(descriptor)
        os.chmod(self.parent / "preferences", 0o755)
        self.assertTrue(private_files.is_mine(private_files.stat_folder(self.folder)))
        self.assertFalse(private_files.is_private_and_mine(private_files.stat_folder(self.folder)))

    def test_a_file_opened_for_writing_is_cut_back_and_for_appending_is_kept(self):
        with private_files.open_private_file(self.folder, "events.jsonl") as stream:
            stream.write(b"one\n")
        with private_files.open_private_file(self.folder, "events.jsonl", append=True) as stream:
            stream.write(b"two\n")
        descriptor = private_files.open_read(self.folder, "events.jsonl")
        with os.fdopen(descriptor, "rb") as stream:
            self.assertEqual(stream.read(), b"one\ntwo\n")
        with private_files.open_private_file(self.folder, "events.jsonl") as stream:
            stream.write(b"three\n")
        descriptor = private_files.open_read(self.folder, "events.jsonl")
        with os.fdopen(descriptor, "rb") as stream:
            self.assertEqual(stream.read(), b"three\n")

    @unittest.skipIf(os.name == "nt", "Windows has no mode bits")
    def test_a_file_opened_for_writing_is_private(self):
        with private_files.open_private_file(self.folder, "token"):
            pass
        descriptor = private_files.open_read(self.folder, "token")
        try:
            self.assertEqual(private_files.mode_of(os.fstat(descriptor)), private_files.FILE_MODE)
        finally:
            os.close(descriptor)

    def test_a_link_is_not_written_through(self):
        private_files.create_entry(self.folder, "target", b"secret")
        link = self.parent / "preferences" / "link"
        try:
            link.symlink_to(self.parent / "preferences" / "target")
        except (OSError, NotImplementedError):
            self.skipTest("This system does not let this user make a symbolic link")
        with self.assertRaises((OSError, PrivateFileError)):
            private_files.open_private_file(self.folder, "link")

    def test_a_folder_that_does_not_exist_cannot_be_opened(self):
        with self.assertRaises(FileNotFoundError):
            private_files.open_folder(self.parent / "gone")

    def test_the_user_name_is_not_empty(self):
        self.assertTrue(private_files.user_key())


class FoldersInsideTheFolder(unittest.TestCase):
    """Folders made, opened and removed inside the opened folder."""

    def setUp(self):
        self.parent = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.parent)
        self.folder = private_files.open_folder(self.parent, create=True)
        self.addCleanup(self.folder.close)

    def test_a_child_folder_is_made_opened_and_listed(self):
        private_files.make_folder(self.folder, "events")
        self.assertIn("events", private_files.list_entries(self.folder))
        child = private_files.open_child_folder(self.folder, "events")
        self.addCleanup(child.close)
        self.assertTrue(private_files.is_private_and_mine(private_files.stat_folder(child)))
        if os.name != "nt":
            self.assertEqual(private_files.mode_of(private_files.stat_entry(self.folder, "events")),
                             private_files.DIRECTORY_MODE)

    def test_a_child_folder_can_be_created_with_its_parent_opened(self):
        child = private_files.open_child_folder(self.folder, "questions", create=True)
        self.addCleanup(child.close)
        private_files.make_folder(child, "later")
        self.assertEqual(private_files.list_entries(child), ["later"])

    def test_a_made_folder_is_never_made_twice(self):
        private_files.make_folder(self.folder, "events")
        with self.assertRaises(FileExistsError):
            private_files.make_folder(self.folder, "events")

    def test_a_child_folder_that_does_not_exist_cannot_be_opened(self):
        with self.assertRaises(FileNotFoundError):
            private_files.open_child_folder(self.folder, "gone")

    def test_a_folder_is_removed_and_a_missing_one_kept_quiet(self):
        private_files.make_folder(self.folder, "events")
        private_files.remove_folder(self.folder, "events")
        self.assertEqual(private_files.list_entries(self.folder), [])
        private_files.remove_folder(self.folder, "events", missing_ok=True)
        with self.assertRaises(FileNotFoundError):
            private_files.remove_folder(self.folder, "events")

    def test_a_tree_is_removed_with_what_it_holds(self):
        private_files.make_folder(self.folder, "scratch")
        private_files.create_entry(self.folder, "scratch/file", b"data")
        private_files.remove_tree(self.folder, "scratch")
        self.assertEqual(private_files.list_entries(self.folder), [])
        private_files.remove_tree(self.folder, "scratch", ignore_errors=True)
        with self.assertRaises(FileNotFoundError):
            private_files.remove_tree(self.folder, "scratch")

    def test_a_stat_of_a_name_does_not_follow_it(self):
        private_files.create_entry(self.folder, "token", b"secret")
        self.assertTrue(private_files.is_mine(private_files.stat_entry(self.folder, "token")))
        with self.assertRaises(FileNotFoundError):
            private_files.stat_entry(self.folder, "gone")


class EntriesAreLinkedRenamedAndOpened(unittest.TestCase):
    """The operations a caller that publishes a staged file needs."""

    def setUp(self):
        self.parent = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.parent)
        self.folder = private_files.open_folder(self.parent, create=True)
        self.addCleanup(self.folder.close)

    def read(self, name):
        descriptor = private_files.open_read(self.folder, name)
        with os.fdopen(descriptor, "rb") as stream:
            return stream.read()

    @unittest.skipIf(os.name == "nt", "the first Windows version links the entry it is given")
    def test_a_link_is_followed_when_the_caller_asks(self):
        private_files.create_entry(self.folder, "target", b"secret")
        link = self.parent / "link"
        try:
            private_files.make_link(self.parent / "target", link)
        except (OSError, NotImplementedError):
            self.skipTest("This system does not let this user make a symbolic link")
        private_files.link_entry(self.folder, "link", "published", follow_symlinks=True)
        self.assertEqual(self.read("published"), b"secret")
        self.assertFalse(stat.S_ISLNK(private_files.stat_entry(self.folder, "published").st_mode))

    def test_a_file_is_linked_and_the_link_is_never_replaced(self):
        private_files.create_entry(self.folder, "staged", b"secret")
        private_files.link_entry(self.folder, "staged", "published")
        self.assertEqual(self.read("published"), b"secret")
        with self.assertRaises(FileExistsError):
            private_files.link_entry(self.folder, "staged", "published")

    def test_a_file_is_linked_into_another_folder(self):
        receipts = private_files.open_child_folder(self.folder, "receipts", create=True)
        self.addCleanup(receipts.close)
        private_files.create_entry(receipts, "staged", b"secret")
        private_files.link_entry(receipts, "staged", "published", target_folder=self.folder)
        self.assertEqual(self.read("published"), b"secret")

    def test_a_file_is_renamed(self):
        private_files.create_entry(self.folder, "staged", b"secret")
        private_files.rename_entry(self.folder, "staged", "published")
        self.assertEqual(self.read("published"), b"secret")
        self.assertEqual(private_files.list_entries(self.folder), ["published"])

    def test_a_rename_replaces_the_name_it_is_given(self):
        private_files.create_entry(self.folder, "staged", b"new")
        private_files.create_entry(self.folder, "published", b"old")
        private_files.rename_entry(self.folder, "staged", "published")
        self.assertEqual(self.read("published"), b"new")
        self.assertEqual(private_files.list_entries(self.folder), ["published"])

    def test_a_name_is_opened_with_the_callers_own_flags(self):
        private_files.create_entry(self.folder, "log", b"first")
        descriptor = private_files.open_entry(self.folder, "log", os.O_RDWR | os.O_APPEND)
        try:
            os.write(descriptor, b"\nsecond")
        finally:
            os.close(descriptor)
        self.assertEqual(self.read("log"), b"first\nsecond")

    def test_a_name_is_opened_or_refused_by_the_system(self):
        private_files.create_entry(self.folder, "token", b"secret")
        with self.assertRaises(FileExistsError):
            private_files.open_entry(self.folder, "token", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with self.assertRaises(FileNotFoundError):
            private_files.open_entry(self.folder, "gone", os.O_RDONLY)

    def test_a_missing_file_is_made_and_a_present_one_kept(self):
        private_files.ensure_entry(self.folder, "capture-index.sqlite3")
        self.assertEqual(private_files.list_entries(self.folder), ["capture-index.sqlite3"])
        private_files.replace_entry(self.folder, "capture-index.sqlite3", b"kept")
        private_files.ensure_entry(self.folder, "capture-index.sqlite3")
        self.assertEqual(self.read("capture-index.sqlite3"), b"kept")
        if os.name != "nt":
            self.assertEqual(private_files.mode_of(private_files.stat_entry(self.folder, "capture-index.sqlite3")),
                             private_files.FILE_MODE)

    def test_the_owner_of_a_stat_is_reported(self):
        private_files.create_entry(self.folder, "token", b"secret")
        private_files.create_entry(self.folder, "other", b"secret")
        token = private_files.stat_entry(self.folder, "token")
        self.assertEqual(private_files.owner_of(token), token.st_uid)
        self.assertEqual(private_files.owner_of(token), private_files.owner_of(private_files.stat_entry(self.folder, "other")))

    def link(self, link, target):
        try:
            private_files.make_link(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("This system does not let this user make a symbolic link")

    def test_a_link_is_made_and_read_back(self):
        target = self.parent / "dataset"
        target.mkdir()
        link = self.parent / "binding"
        self.link(link, target)
        self.assertEqual(private_files.read_link(link), str(target))

    def test_a_name_is_not_linked_twice(self):
        target = self.parent / "dataset"
        target.mkdir()
        link = self.parent / "binding"
        self.link(link, target)
        with self.assertRaises(FileExistsError):
            private_files.make_link(target, link)


if __name__ == "__main__":
    unittest.main()

class FolderTree(unittest.TestCase):
    """Walking a tree and streaming a file through the folder, as the instruction sources and the Chrome install do."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "docs").mkdir(mode=private_files.DIRECTORY_MODE)
        (self.root / "docs" / "one.md").write_text("hello")
        (self.root / "docs" / "sub").mkdir()
        (self.root / "docs" / "sub" / "two.md").write_text("world")

    def test_a_folder_inside_an_opened_folder_is_opened_by_name(self):
        folder = private_files.open_folder(self.root)
        self.addCleanup(folder.close)
        docs = private_files.open_folder(folder, "docs")
        self.addCleanup(docs.close)
        self.assertEqual(sorted(private_files.list_entries(docs)), ["one.md", "sub"])
        self.assertEqual(private_files.mode_of(private_files.stat_folder(docs)) & 0o777,
                         private_files.DIRECTORY_MODE)

    def test_a_link_is_neither_entered_nor_statted_as_its_target(self):
        (self.root / "docs" / "link").symlink_to(self.root / "docs" / "one.md")
        folder = private_files.open_folder(self.root)
        self.addCleanup(folder.close)
        docs = private_files.open_folder(folder, "docs")
        self.addCleanup(docs.close)
        self.assertTrue(stat.S_ISLNK(private_files.stat_entry(docs, "link").st_mode))
        with self.assertRaises(OSError):
            private_files.open_read(docs, "link")
        with self.assertRaises(OSError):
            private_files.open_folder(docs, "link")

    def test_a_file_is_created_for_streaming_and_never_replaced(self):
        folder = private_files.open_folder(self.root)
        self.addCleanup(folder.close)
        descriptor = private_files.create_file(folder, "member", mode=private_files.FILE_MODE)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"first")
        with self.assertRaises(FileExistsError):
            private_files.create_file(folder, "member")
        self.assertEqual((self.root / "member").read_bytes(), b"first")

    def test_a_pipe_is_opened_without_waiting_for_a_writer(self):
        if os.name == "nt":
            self.skipTest("this system has no non-blocking read of a pipe")
        os.mkfifo(self.root / "pipe")
        folder = private_files.open_folder(self.root)
        self.addCleanup(folder.close)
        descriptor = private_files.open_read(folder, "pipe", nonblock=True)
        try:
            self.assertEqual(os.read(descriptor, 1), b"")      # no writer: the open did not wait, and nothing is read
        finally:
            os.close(descriptor)

    def test_a_link_is_made_and_read_back(self):
        folder = private_files.open_folder(self.root)
        self.addCleanup(folder.close)
        private_files.make_link("one.md", self.root / "shortcut")
        self.assertEqual(private_files.read_link(self.root / "shortcut"), "one.md")
        with self.assertRaises(FileExistsError):
            private_files.make_link("sub", self.root / "shortcut")
        # Linux gives every link the mode 777; a Mac gives a new link the bits the umask leaves.
        made = 0o777 if sys.platform.startswith("linux") else stat.S_IMODE(os.lstat(self.root / "shortcut").st_mode)
        self.assertEqual(private_files.mode_of(private_files.stat_entry(folder, "shortcut"), mask=0o777), made & 0o777)

    def test_a_link_inside_the_folder_is_read(self):
        try:
            (self.root / "docs" / "shortcut").symlink_to("one.md")
        except (OSError, NotImplementedError):
            self.skipTest("This system does not let this user make a symbolic link")
        folder = private_files.open_folder(self.root)
        self.addCleanup(folder.close)
        docs = private_files.open_child_folder(folder, "docs")
        self.addCleanup(docs.close)
        self.assertEqual(private_files.read_link(docs, "shortcut"), "one.md")
        with self.assertRaises(FileNotFoundError):
            private_files.read_link(docs, "gone")

    def test_a_root_that_is_a_link_is_opened_only_when_the_caller_asks(self):
        link = self.root / "current"
        try:
            link.symlink_to(self.root / "docs", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("This system does not let this user make a symbolic link")
        with self.assertRaises((OSError, PrivateFileError)):
            private_files.open_folder(link)
        folder = private_files.open_folder(link, follow_link=True)
        self.addCleanup(folder.close)
        self.assertEqual(sorted(private_files.list_entries(folder)), ["one.md", "sub"])


class TheMaskInUse(unittest.TestCase):
    """A block in which another library creates files this code never opens."""



class CreatingPrivateFolders(unittest.TestCase):
    """Making a run folder private, as the Context pipeline and its call receipts do."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    @unittest.skipIf(os.name == "nt", "Windows has no mask")
    def test_what_is_created_inside_the_block_is_private_and_the_mask_returns(self):
        previous = os.umask(0o022)
        try:
            with private_files.private_umask():
                (self.root / "inside").mkdir()
            (self.root / "after").mkdir()
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE((self.root / "inside").stat().st_mode), private_files.DIRECTORY_MODE)
        self.assertEqual(stat.S_IMODE((self.root / "after").stat().st_mode), 0o755)

    @unittest.skipIf(os.name == "nt", "Windows has no mask")
    def test_the_mask_returns_when_the_block_fails(self):
        previous = os.umask(0o022)
        try:
            with self.assertRaises(RuntimeError):
                with private_files.private_umask():
                    raise RuntimeError("the block stopped")
            (self.root / "after").mkdir()
            self.assertEqual(stat.S_IMODE((self.root / "after").stat().st_mode), 0o755)
        finally:
            os.umask(previous)

    def test_the_block_passes_a_value_through(self):
        with private_files.private_umask() as value:
            self.assertIsNone(value)


    def test_a_folder_and_its_parents_are_created_private(self):
        path = private_files.make_private_directory(self.root / "runs" / "event", parents=True)
        self.assertEqual(path, self.root / "runs" / "event")
        self.assertTrue(path.is_dir())
        if os.name != "nt":
            self.assertEqual(private_files.mode_of(path.stat()), private_files.DIRECTORY_MODE)

    def test_an_existing_folder_is_kept_and_made_private(self):
        path = self.root / "calls"
        path.mkdir()
        if os.name != "nt":
            os.chmod(path, 0o755)
        self.assertEqual(private_files.make_private_directory(path), path)
        if os.name != "nt":
            self.assertEqual(private_files.mode_of(path.stat()), private_files.DIRECTORY_MODE)

    def test_a_missing_parent_is_not_created_unless_asked(self):
        with self.assertRaises(FileNotFoundError):
            private_files.make_private_directory(self.root / "gone" / "calls")

    def test_a_file_where_the_folder_belongs_is_refused(self):
        path = self.root / "file"
        path.write_text("x")
        with self.assertRaises(FileExistsError):
            private_files.make_private_directory(path)

    @unittest.skipIf(os.name == "nt", "A Windows stat names no owner number")
    def test_the_owner_of_a_folder_is_this_user(self):
        self.assertEqual(private_files.ownership_of(private_files.stat_folder(self.root))[0], os.getuid())


class RenamingWithoutReplacing(unittest.TestCase):
    """Publishing a staged folder: the new name is taken only when it is still absent."""

    def setUp(self):
        self.parent = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.parent)
        self.staging = private_files.open_folder(self.parent / "staging", create=True)
        self.addCleanup(self.staging.close)
        self.destination = private_files.open_folder(self.parent / "destination", create=True)
        self.addCleanup(self.destination.close)

    def test_a_staged_folder_takes_its_name(self):
        private_files.make_folder(self.staging, "receipt-1")
        private_files.rename_entry_exclusive(self.staging, "receipt-1", "published", target_folder=self.destination)
        self.assertEqual(private_files.list_entries(self.staging), [])
        self.assertEqual(private_files.list_entries(self.destination), ["published"])

    def test_an_existing_name_is_refused_and_nothing_moves(self):
        private_files.make_folder(self.staging, "receipt-1")
        private_files.make_folder(self.destination, "published")
        with self.assertRaises(FileExistsError):
            private_files.rename_entry_exclusive(self.staging, "receipt-1", "published",
                                                 target_folder=self.destination)
        self.assertEqual(private_files.list_entries(self.staging), ["receipt-1"])
        self.assertEqual(private_files.list_entries(self.destination), ["published"])

    def test_a_name_that_does_not_exist_cannot_be_renamed(self):
        with self.assertRaises(FileNotFoundError):
            private_files.rename_entry_exclusive(self.staging, "gone", "published",
                                                 target_folder=self.destination)
