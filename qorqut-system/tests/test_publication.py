"""Publication, retained descriptors and file identity."""
import errno
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

from qorqut_system import identity, publication, retained
from qorqut_system.errors import UnsupportedSystem


@unittest.skipUnless(publication.publication_supported(), "exclusive publication needs Linux O_TMPFILE or macOS")
class Publication(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.target = self.root / "target"
        self.target.mkdir(mode=0o700)
        self.fd = os.open(self.target, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        publication.prepare_publication(self.root / "staging")

    def written(self, data=b"content"):
        pending = publication.anonymous_file(self.fd, read=True)
        os.write(pending.fd, data)
        os.fsync(pending.fd)
        return pending

    def test_a_file_exists_only_once_it_is_published(self):
        pending = self.written()
        try:
            self.assertEqual(os.listdir(self.target), [])
            publication.publish(pending, self.fd, "file")
            self.assertEqual((self.target / "file").read_bytes(), b"content")
        finally:
            publication.discard(pending)
        self.assertEqual(os.listdir(self.target), ["file"])

    def test_an_existing_name_is_never_replaced(self):
        (self.target / "file").write_bytes(b"original")
        pending = self.written(b"new")
        try:
            with self.assertRaises(FileExistsError):
                publication.publish(pending, self.fd, "file")
        finally:
            publication.discard(pending)
        self.assertEqual((self.target / "file").read_bytes(), b"original")

    def test_a_discarded_file_leaves_nothing(self):
        publication.discard(self.written())
        self.assertEqual(os.listdir(self.target), [])



class Retained(unittest.TestCase):
    @unittest.skipUnless(os.name != "nt", "retained descriptors need a folder descriptor path")
    def test_an_open_database_file_is_readable_through_its_uri(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.db"
            with sqlite3.connect(path) as db:
                db.execute("create table t(a)")
                db.execute("insert into t values (7)")
            descriptor = os.open(path, os.O_RDONLY)
            try:
                uri = retained.immutable_database_uri(descriptor)
                with sqlite3.connect(uri, uri=True) as db:
                    self.assertEqual(db.execute("select a from t").fetchall(), [(7,)])
            finally:
                os.close(descriptor)

    @unittest.skipUnless(os.name != "nt", "a folder is a descriptor only on Unix")
    def test_a_folder_descriptor_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            descriptor = os.open(folder, os.O_RDONLY)
            try:
                with self.assertRaises(OSError) as raised:
                    retained.immutable_database_uri(descriptor)
                self.assertEqual(raised.exception.errno, errno.EINVAL)
            finally:
                os.close(descriptor)

    def test_descriptor_socket_paths_stay_linux_only(self):
        if sys.platform.startswith("linux"):
            retained.require_descriptor_socket_paths()
        else:
            with self.assertRaises(UnsupportedSystem):
                retained.require_descriptor_socket_paths()


class Identity(unittest.TestCase):
    @staticmethod
    def record(device, inode):
        return {"device": device, "inode": inode}

    def test_a_saved_device_number_is_ignored(self):
        self.assertTrue(identity.records_match(self.record(1, 5), self.record(9, 5)))
        self.assertFalse(identity.records_match(self.record(1, 5), self.record(1, 6)))

    def test_the_live_parent_volume_must_match(self):
        self.assertTrue(identity.matches(self.record(1, 5), self.record(9, 5), 9))
        self.assertFalse(identity.matches(self.record(1, 5), self.record(9, 5), 8))

    def test_the_parent_volume_is_read_live(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "file"
            path.write_bytes(b"x")
            self.assertEqual(identity.parent_volume(path), os.stat(folder).st_dev)

    def test_a_directory_tree_matches_its_live_parents(self):
        saved = {"a": self.record(1, 1), "a/b": self.record(1, 2)}
        live = {"a": self.record(7, 1), "a/b": self.record(7, 2)}
        self.assertTrue(identity.tree_matches(saved, live, 7))
        self.assertFalse(identity.tree_matches(saved, live, 8))
        self.assertFalse(identity.tree_matches(saved, {"a": live["a"]}, 7))


if __name__ == "__main__":
    unittest.main()