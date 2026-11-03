"""Descriptor-addressed immutable SQLite reads used by managed update inspection.

Run from qorqut-code/app:  python -m unittest tests.test_posix_files
"""
import errno
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from bazarlyk import posix_files


@unittest.skipUnless(sys.platform == 'darwin' or sys.platform.startswith('linux'), 'POSIX descriptor paths')
class ImmutableDatabaseUriTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'state.db'
        with sqlite3.connect(self.path) as db:
            db.execute('create table t(a)')
            db.execute('insert into t values (7)')
        self.descriptor = os.open(self.path, os.O_RDONLY)

    def tearDown(self):
        os.close(self.descriptor)
        self.temporary.cleanup()

    def test_open_descriptor_is_readable_through_its_uri(self):
        uri = posix_files.immutable_database_uri(self.descriptor)
        with sqlite3.connect(uri, uri=True) as db:
            self.assertEqual(db.execute('select a from t').fetchall(), [(7,)])

    def test_different_file_behind_the_path_is_refused(self):
        other = os.stat(self.path)
        fake = os.stat_result((other.st_mode, other.st_ino + 1, *other[2:]))
        with mock.patch.object(posix_files.os, 'stat', return_value=fake):
            with self.assertRaises(OSError) as raised:
                posix_files.immutable_database_uri(self.descriptor)
        self.assertEqual(raised.exception.errno, errno.ESTALE)

    def test_directory_descriptor_is_refused(self):
        directory = os.open(self.temporary.name, os.O_RDONLY)
        try:
            with self.assertRaises(OSError) as raised:
                posix_files.immutable_database_uri(directory)
            self.assertEqual(raised.exception.errno, errno.EINVAL)
        finally:
            os.close(directory)


class FileIdentityTests(unittest.TestCase):
    @staticmethod
    def result(dev, ino, size=10, mtime_ns=5, birth=1.0):
        value = os.stat_result((0o100600, ino, dev, 1, 0, 0, size, 0, 0, 0))
        fake = mock.Mock(wraps=value)
        fake.st_dev, fake.st_ino, fake.st_mode, fake.st_size = dev, ino, 0o100600, size
        fake.st_mtime_ns, fake.st_birthtime = mtime_ns, birth
        return fake

    def test_darwin_ignores_device_number_only(self):
        with mock.patch.object(posix_files.sys, 'platform', 'darwin'):
            same = posix_files._file_identity(self.result(16777230, 5))
            self.assertEqual(same, posix_files._file_identity(self.result(2**64 - 1201951597, 5)))
            self.assertNotEqual(same, posix_files._file_identity(self.result(16777230, 6)))
            self.assertNotEqual(same, posix_files._file_identity(self.result(16777230, 5, size=11)))
            self.assertNotEqual(same, posix_files._file_identity(self.result(16777230, 5, mtime_ns=6)))
            self.assertNotEqual(same, posix_files._file_identity(self.result(16777230, 5, birth=2.0)))

    def test_linux_keeps_device_and_inode(self):
        with mock.patch.object(posix_files.sys, 'platform', 'linux'):
            self.assertNotEqual(posix_files._file_identity(self.result(1, 5)),
                                posix_files._file_identity(self.result(2, 5)))


if __name__ == '__main__':
    unittest.main()
