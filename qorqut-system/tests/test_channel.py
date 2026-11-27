"""The local channel: listen, connect, who connected, and the control token."""
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

from qorqut_system import channel, channel_unix, places
from qorqut_system.errors import PathChanged, UnsafePath, UnsupportedSystem


class Channel(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.path = self.root / "control.sock"

    def pair(self):
        listener = channel.listen(self.path, timeout=5)
        self.addCleanup(listener.close)
        connection = channel.connect(self.path, timeout=5)
        self.addCleanup(connection.close)
        accepted, _address = listener.accept()
        self.addCleanup(accepted.close)
        return listener, connection, accepted

    def test_a_caller_connects_the_socket_it_made_itself(self):
        listener = channel.listen(self.path, timeout=5)
        self.addCleanup(listener.close)
        connection = channel.new_socket()
        self.addCleanup(connection.close)
        channel.connect_socket(connection, self.path, timeout=5)
        accepted, _address = listener.accept()
        self.addCleanup(accepted.close)
        connection.sendall(b"made here\n")
        self.assertEqual(accepted.recv(64), b"made here\n")

    def test_a_client_and_a_listener_exchange_bytes(self):
        _listener, connection, accepted = self.pair()
        connection.sendall(b"hello\n")
        self.assertEqual(accepted.recv(64), b"hello\n")
        accepted.sendall(b"world\n")
        self.assertEqual(connection.recv(64), b"world\n")

    @unittest.skipIf(sys.platform == "win32", "a named pipe has an access rule instead of a mode")
    def test_the_channel_is_open_to_this_user_only(self):
        self.pair()
        info = self.path.lstat()
        self.assertTrue(stat.S_ISSOCK(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_uid, os.geteuid())

    def test_the_other_end_is_this_process(self):
        _listener, _connection, accepted = self.pair()
        pid, uid = channel.peer(accepted)
        self.assertEqual(pid, os.getpid())
        if sys.platform != "win32":
            self.assertEqual(uid, os.geteuid())

    def test_the_caller_check_accepts_this_process(self):
        _listener, _connection, accepted = self.pair()
        self.assertTrue(channel.check_caller(accepted))
        self.assertTrue(channel.check_caller(accepted, pid=os.getpid()))

    def test_a_caller_can_bring_its_own_peer_reader(self):
        _listener, _connection, accepted = self.pair()
        uid = None if sys.platform == "win32" else os.geteuid()
        self.assertTrue(channel.check_caller(accepted, read_peer=lambda connection: (os.getpid(), uid)))
        self.assertFalse(channel.check_caller(accepted, read_peer=lambda connection: (os.getpid(), None)
                                              if sys.platform != "win32" else (None, None)))
        if sys.platform != "win32":
            self.assertFalse(channel.check_caller(accepted, read_peer=lambda connection: (os.getpid(), os.geteuid() + 1)))

    @unittest.skipIf(sys.platform == "win32", "Windows reports no user id; the pipe's access rule is the user check")
    def test_the_caller_check_refuses_another_process_or_user(self):
        _listener, _connection, accepted = self.pair()
        self.assertFalse(channel.check_caller(accepted, uid=os.geteuid() + 1))
        self.assertFalse(channel.check_caller(accepted, pid=os.getpid() + 1))

    def test_a_channel_that_nothing_watches_is_refused(self):
        with self.assertRaises(OSError):
            channel.connect(self.root / "missing.sock", timeout=2)

    @unittest.skipIf(sys.platform == "win32", "Windows creates no file for its channel")
    def test_a_socket_a_listener_that_is_gone_left_behind_is_replaced(self):
        first = channel.listen(self.path)
        first.close()
        self.assertTrue(self.path.exists())
        second = channel.listen(self.path)
        second.close()

    @unittest.skipIf(places.channel_path_limit() is None, "this system has no channel path limit")
    def test_a_path_too_long_is_refused(self):
        with self.assertRaises(UnsafePath):
            channel.listen(self.root / ("x" * (places.channel_path_limit() + 1)))


class Token(unittest.TestCase):
    def test_a_token_is_long_and_fresh(self):
        first, second = channel.new_token(), channel.new_token()
        self.assertIsInstance(first, str)
        self.assertGreaterEqual(len(first), 32)
        self.assertNotEqual(first, second)

    def test_a_token_matches_only_itself(self):
        token = channel.new_token()
        self.assertTrue(channel.token_matches(token, token))
        self.assertFalse(channel.token_matches(token, token + "x"))
        self.assertFalse(channel.token_matches(token, None))
        self.assertFalse(channel.token_matches(None, token))


class DirectoryChannel(unittest.TestCase):
    """A channel whose name lies inside an open private directory: the form the recovery administration uses on Linux."""

    def setUp(self):
        if sys.platform == "win32":
            self.skipTest("a named pipe has no name inside a folder")
        if not sys.platform.startswith("linux"):
            self.skipTest("only Linux has the descriptor paths this needs; DirectoryChannelElsewhere checks the refusal")
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.handle = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, self.handle)

    def meet(self):
        listener, identity = channel.listen_in_directory(self.handle, "control.sock", timeout=5)
        self.addCleanup(listener.close)
        connection = channel.connect_in_directory(self.handle, "control.sock", timeout=5)
        self.addCleanup(connection.close)
        accepted, _address = listener.accept()
        self.addCleanup(accepted.close)
        return listener, identity, connection, accepted

    def test_a_listener_and_a_client_meet_inside_the_directory(self):
        _listener, _identity, connection, accepted = self.meet()
        connection.sendall(b"hello\n")
        self.assertEqual(accepted.recv(64), b"hello\n")
        self.assertEqual(stat.S_IMODE((self.root / "control.sock").stat().st_mode), 0o600)

    def test_an_entry_that_is_already_there_is_never_replaced(self):
        (self.root / "taken").write_bytes(b"x")
        with self.assertRaises(FileExistsError):
            channel.listen_in_directory(self.handle, "taken")
        self.assertEqual((self.root / "taken").read_bytes(), b"x")

    def test_only_the_socket_that_was_created_is_removed(self):
        listener, identity = channel.listen_in_directory(self.handle, "control.sock")
        listener.close()
        self.assertTrue((self.root / "control.sock").exists())
        channel.remove_in_directory(self.handle, "control.sock", (identity[0], identity[1] + 1))
        self.assertTrue((self.root / "control.sock").exists(), "a path with another identity was removed")
        channel.remove_in_directory(self.handle, "control.sock", identity)
        self.assertFalse((self.root / "control.sock").exists())

    def test_a_path_that_is_not_the_expected_socket_is_refused(self):
        (self.root / "plain").write_bytes(b"x")
        with self.assertRaises(UnsafePath):
            channel.connect_in_directory(self.handle, "plain")
        with self.assertRaises(UnsafePath):
            channel.connect_in_directory(self.handle, "missing.sock")

    def test_a_path_that_changed_while_it_was_secured_is_told_apart(self):
        real, calls = channel_unix._entry, []

        def renamed(dir_fd, name):
            identity, info = real(dir_fd, name)
            calls.append(identity)
            if len(calls) == 2:                      # the check after the mode was set sees another object
                return (identity[0], identity[1] + 1), info
            return identity, info

        with mock.patch.object(channel_unix, "_entry", renamed):
            with self.assertRaises(PathChanged):
                channel.listen_in_directory(self.handle, "changing.sock")
        self.assertFalse((self.root / "changing.sock").exists())

    def test_a_socket_that_could_not_be_secured_is_a_plain_failure(self):
        real, calls = channel_unix._entry, []

        def not_a_socket(dir_fd, name):
            identity, info = real(dir_fd, name)
            calls.append(identity)
            if len(calls) == 2:
                return identity, types.SimpleNamespace(st_mode=0o100600)
            return identity, info

        with mock.patch.object(channel_unix, "_entry", not_a_socket):
            with self.assertRaises(UnsafePath) as raised:
                channel.listen_in_directory(self.handle, "wrong-kind.sock")
        self.assertNotIsInstance(raised.exception, PathChanged)
        self.assertFalse((self.root / "wrong-kind.sock").exists())


@unittest.skipIf(sys.platform.startswith("linux") or sys.platform == "win32",
                 "Linux listens inside a folder, and Windows has no name inside a folder for its channel")
class DirectoryChannelElsewhere(unittest.TestCase):
    """Another Unix system, a Mac for one, refuses a channel inside a folder before anything is made, as before."""

    def test_listening_and_connecting_inside_a_folder_are_refused(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        handle = os.open(root, os.O_RDONLY)
        self.addCleanup(os.close, handle)
        with self.assertRaises(UnsupportedSystem):
            channel.listen_in_directory(handle, "control.sock")
        with self.assertRaises(UnsupportedSystem):
            channel.connect_in_directory(handle, "control.sock")
        self.assertEqual(list(root.iterdir()), [])


class Loading(unittest.TestCase):
    def test_the_module_names_its_operations(self):
        self.assertEqual(sorted(channel.__all__),
                         ["check_caller", "connect", "connect_in_directory", "connect_socket", "listen", "listen_here",
                          "listen_in_directory", "new_socket", "new_token", "peer", "remove_in_directory",
                          "token_matches"])


if __name__ == "__main__":
    unittest.main()