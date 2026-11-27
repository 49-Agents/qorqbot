"""Processes: start in the background, observe, identify and stop, with everything a process started."""
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from qorqut_system import processes, processes_unix
from qorqut_system.errors import UnsupportedSystem

WINDOWS = os.name == "nt"
PYTHON = sys.executable
SLEEP = [PYTHON, "-c", "import time; time.sleep(30)"]


def ended(pid):
    """Whether the process is gone or only its zombie remains."""
    try:
        return processes.is_zombie(pid)
    except (ProcessLookupError, FileNotFoundError):
        return True


class Started(unittest.TestCase):
    """Starts children that the test stops again, and waits for an observation to become possible."""

    def setUp(self):
        self.started = []

    def tearDown(self):
        for process in self.started:
            if process.poll() is None:
                processes.stop_process_group(process)

    def start(self, argv=None, **options):
        options.setdefault("stdin", subprocess.DEVNULL)
        options.setdefault("stdout", subprocess.DEVNULL)
        options.setdefault("stderr", subprocess.DEVNULL)
        process = processes.start_in_background(argv or list(SLEEP), **options)
        self.started.append(process)
        return process

    def wait_for(self, check, message, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                value = check()
            except (ProcessLookupError, FileNotFoundError):
                value = None
            if value:
                return value
            time.sleep(0.02)
        self.fail(message)


class BackgroundProcesses(Started):
    def test_a_started_process_is_alive_and_is_stopped(self):
        process = self.start()
        self.wait_for(lambda: processes.identity(process.pid), "the process did not start")
        self.assertIsNone(process.poll())
        processes.stop_process_group(process)
        self.assertIsNotNone(process.poll())

    def test_a_process_that_already_ended_is_not_an_error(self):
        process = self.start([PYTHON, "-c", "pass"])
        process.wait()
        processes.stop_process_group(process)
        self.assertIsNotNone(process.poll())

    @unittest.skipIf(WINDOWS, "Windows has no process group to signal")
    def test_the_group_is_signalled_even_when_the_leader_has_ended(self):
        process = self.start([PYTHON, "-c", "pass"])
        process.wait()                                       # a leader that has exited, as a caller's loop may find it
        with mock.patch.object(processes_unix.os, "killpg", side_effect=ProcessLookupError) as group:
            processes.stop_process_group(process, even_when_ended=True)
        self.assertEqual(group.call_args_list[0].args, (process.pid, signal.SIGTERM))
        self.assertIsNotNone(process.poll())

    def test_the_group_is_not_signalled_when_the_caller_does_not_ask(self):
        process = self.start([PYTHON, "-c", "pass"])
        process.wait()
        with mock.patch.object(processes_unix.os, "killpg") as group:
            processes.stop_process_group(process)
        group.assert_not_called()

    def test_identity_names_the_boot_and_the_start(self):
        process = self.start()
        token = self.wait_for(lambda: processes.start_token(process.pid), "the process did not start")
        self.assertEqual(processes.identity(process.pid), processes.boot_id() + ":" + token)
        self.assertEqual(processes.pid_identity(process.pid), f"{process.pid}:{token}")

    def test_boot_and_machine_identity_are_stable(self):
        self.assertTrue(processes.boot_id())
        self.assertEqual(processes.boot_id(), processes.boot_id())
        self.assertTrue(processes.machine_id())
        if not WINDOWS:
            self.assertTrue(processes.namespace_id())

    @unittest.skipIf(WINDOWS, "Windows has no /proc stat line")
    def test_a_process_id_that_is_not_a_number_is_read_as_the_old_lines_did(self):
        process = self.start()
        fields = self.wait_for(lambda: processes.fields_as_given(process.pid), "the process did not start")
        self.assertEqual(processes.fields_as_given(process.pid)[19], fields[19])
        self.assertFalse(processes.is_zombie_as_given(process.pid))
        if sys.platform == "darwin":
            return      # a Mac's old lines went through the process reader, which converts the value as fields() does
        for value in ("abc", 12.0, None):
            with self.assertRaises(OSError):
                processes.fields_as_given(value)
            with self.assertRaises(OSError):
                processes.is_zombie_as_given(value)

    @unittest.skipIf(WINDOWS, "Windows has no /proc descriptor table")
    def test_the_open_links_of_this_process_are_raw_targets(self):
        descriptor = os.open("/dev/null", os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        if sys.platform == "darwin":
            self.assertEqual(processes.open_links(os.getpid()), [])     # macOS shows no descriptor table
            return
        self.assertIn("/dev/null", processes.open_links(os.getpid()))
        with self.assertRaises(ValueError):                 # the old line converted the value into the path
            processes.open_links("abc")

    def test_the_effective_user_is_the_one_the_process_acts_as(self):
        self.assertEqual(processes.effective_user_id(), os.geteuid() if not WINDOWS else None)

    def test_a_live_process_is_not_a_zombie(self):
        process = self.start()
        self.wait_for(lambda: processes.start_token(process.pid), "the process did not start")
        self.assertFalse(processes.is_zombie(process.pid))

    @unittest.skipIf(WINDOWS, "Windows has no sessions")
    def test_a_started_process_leads_its_own_session(self):
        process = self.start()
        self.wait_for(lambda: processes.start_token(process.pid), "the process did not start")
        self.assertEqual(os.getsid(process.pid), process.pid)
        self.assertEqual(os.getpgid(process.pid), process.pid)

    @unittest.skipIf(WINDOWS, "the first Windows version cannot read a process group")
    def test_the_process_group_is_read_for_another_process(self):
        process = self.start()
        self.wait_for(lambda: processes.start_token(process.pid), "the process did not start")
        self.assertEqual(processes.process_group(process.pid), process.pid)
        self.assertEqual(processes.process_group(os.getpid()), os.getpgrp())

    @unittest.skipIf(WINDOWS, "Windows has no gentle signal for one process")
    def test_one_process_is_asked_to_stop(self):
        process = self.start()
        self.wait_for(lambda: processes.start_token(process.pid), "the process did not start")
        processes.terminate_pid(process.pid)
        self.assertEqual(process.wait(timeout=10), -signal.SIGTERM)

    @unittest.skipIf(WINDOWS, "Windows ends the process without a signal, so it reports an exit code")
    def test_one_process_is_stopped_by_force(self):
        process = self.start()
        self.wait_for(lambda: processes.start_token(process.pid), "the process did not start")
        processes.kill_pid(process.pid)
        self.assertEqual(process.wait(timeout=10), -signal.SIGKILL)

    @unittest.skipIf(WINDOWS, "the first Windows version cannot watch a child exit without reaping it")
    def test_a_child_is_observed_as_exited_without_being_reaped(self):
        process = self.start([PYTHON, "-c", "pass"])
        self.addCleanup(process.wait)
        self.assertTrue(processes.wait_for_exit_without_reaping(process.pid))

    @unittest.skipIf(WINDOWS, "Windows stops the job instead of signalling")
    def test_a_process_that_ignores_the_gentle_signal_is_killed(self):
        script = ("import signal, time\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "print('ready', flush=True)\n"
                  "time.sleep(30)\n")
        process = self.start([PYTHON, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(process.stdout.close)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        processes.stop_process_group(process, grace=0.5)
        self.assertIsNotNone(process.poll())

    @unittest.skipIf(WINDOWS, "Windows stops the job instead of signalling")
    def test_stopping_takes_every_process_the_group_started(self):
        script = ("import subprocess, sys, time\n"
                  "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                  "print(child.pid, flush=True)\n"
                  "time.sleep(30)\n")
        process = self.start([PYTHON, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(process.stdout.close)
        child = int(process.stdout.readline().strip())
        self.wait_for(lambda: not ended(child) and processes.start_token(child), "the child did not start")
        processes.stop_process_group(process, grace=1.0)
        self.wait_for(lambda: ended(child), "the process the group started survived the stop", timeout=5)

    @unittest.skipIf(WINDOWS, "Windows has no zombie state")
    def test_an_ended_process_has_no_identity(self):
        process = self.start([PYTHON, "-c", "pass"])
        try:
            self.wait_for(lambda: self.identity_ended(process.pid), "the process kept its identity after it ended")
            self.assertTrue(processes.is_zombie(process.pid))
        finally:
            process.wait()

    @staticmethod
    def identity_ended(pid):
        try:
            processes.pid_identity(pid)
        except (ProcessLookupError, FileNotFoundError):
            return True
        return False


class Observations(Started):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        self.process = self.start(list(SLEEP), cwd=str(self.root),
                                  env=dict(os.environ, QORQUT_TEST_MARKER=str(self.root)))
        self.wait_for(lambda: processes.exe(self.process.pid), "the process did not start")

    def test_the_process_list_holds_the_process(self):
        self.assertIn(self.process.pid, processes.pids())

    def test_name_program_and_parent(self):
        pid = self.process.pid
        self.assertEqual(processes.parent_pid(pid), os.getpid())
        self.assertTrue(os.path.samefile(processes.exe(pid), PYTHON))
        name = processes.name(pid)
        self.assertTrue(name)
        self.assertIn(name.lower(), processes.exe(pid).name.lower())

    @unittest.skipIf(WINDOWS, "the first Windows version does not read the other observations")
    def test_command_line_folder_environment_and_owner(self):
        pid = self.process.pid
        self.assertEqual(processes.cmdline(pid), list(SLEEP))
        self.assertEqual(processes.cwd(pid).resolve(), self.root)
        self.assertEqual(processes.environ(pid).get("QORQUT_TEST_MARKER"), str(self.root))
        self.assertIn(os.geteuid(), processes.uids(pid))

    @unittest.skipIf(WINDOWS, "Windows has no stat fields")
    def test_the_stat_fields_agree_with_the_start_token(self):
        values = processes.fields(self.process.pid)
        self.assertEqual(values[19], processes.start_token(self.process.pid))
        self.assertEqual(int(values[1]), os.getpid())

    @unittest.skipIf(WINDOWS, "the first Windows version does not read the sockets of a process")
    def test_the_listening_socket_of_this_process_is_found(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        self.assertTrue(processes.owns_listener(os.getpid(), port, "127.0.0.1"))
        self.assertTrue(processes.owns_listener(os.getpid(), port))
        self.assertFalse(processes.owns_listener(os.getpid(), port + 1, "127.0.0.1"))


class Signalling(unittest.TestCase):
    """Asking a process to stop, and forcing it: only a group this layer made is signalled as a group."""

    def test_a_group_is_signalled_only_when_asked_and_only_when_the_process_leads_it(self):
        process = mock.Mock(pid=123)
        with mock.patch.object(processes_unix.os, "getpgid", return_value=123), \
                mock.patch.object(processes_unix.os, "killpg") as killpg:
            processes.terminate_process(process, group=False)
            killpg.assert_not_called()
            process.terminate.assert_called_once_with()
            processes.terminate_process(process, group=True)
            killpg.assert_called_once()
            self.assertEqual(killpg.call_args.args[0], 123)
        with mock.patch.object(processes_unix.os, "getpgid", return_value=1), \
                mock.patch.object(processes_unix.os, "killpg") as killpg:
            processes.kill_process(process, group=True)      # not ours: never the group
            killpg.assert_not_called()
            process.kill.assert_called_once_with()

    def test_a_process_that_has_gone_is_not_an_error(self):
        process = mock.Mock(pid=123)
        with mock.patch.object(processes_unix.os, "getpgid", side_effect=ProcessLookupError):
            processes.terminate_process(process)
        process.terminate.assert_not_called()

    def test_a_whole_group_is_signalled_by_the_id_it_leads(self):
        import signal
        with mock.patch.object(processes_unix.os, "killpg") as killpg:
            processes.terminate_process_group(123)
            self.assertEqual(killpg.call_args.args, (123, signal.SIGTERM))
            processes.kill_process_group(123)
            self.assertEqual(killpg.call_args.args, (123, signal.SIGKILL))
        with mock.patch.object(processes_unix.os, "killpg", side_effect=ProcessLookupError):
            with self.assertRaises(ProcessLookupError):
                processes.terminate_process_group(123)     # a group that has gone raises, as os.killpg does
            with self.assertRaises(ProcessLookupError):
                processes.kill_process_group(123)

    @unittest.skipIf(WINDOWS, "Windows has no signal of its own to give one process")
    def test_one_process_is_signalled_by_its_id_and_not_its_group(self):
        import signal
        with mock.patch.object(processes_unix.os, "kill") as kill, \
                mock.patch.object(processes_unix.os, "killpg") as killpg:
            processes.terminate_pid(123)
            self.assertEqual(kill.call_args.args, (123, signal.SIGTERM))
            processes.kill_pid(123)
            self.assertEqual(kill.call_args.args, (123, signal.SIGKILL))
            killpg.assert_not_called()

    @unittest.skipIf(WINDOWS, "Windows has no signal of its own to give one process")
    def test_a_process_that_has_gone_raises_as_its_signal_does(self):
        with mock.patch.object(processes_unix.os, "kill", side_effect=ProcessLookupError):
            with self.assertRaises(ProcessLookupError):
                processes.terminate_pid(123)
            with self.assertRaises(ProcessLookupError):
                processes.kill_pid(123)

    @unittest.skipIf(WINDOWS, "Windows stops the job instead of signalling")
    def test_one_process_is_stopped_by_its_id_alone(self):
        process = processes.start_in_background(list(SLEEP), stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(processes.kill_process, process)
        processes.terminate_pid(process.pid)
        process.wait(timeout=10)
        self.assertIsNotNone(process.poll())
        with self.assertRaises(ProcessLookupError):
            processes.kill_pid(process.pid)

    @unittest.skipIf(WINDOWS, "Windows stops the job instead of signalling")
    def test_the_gentle_signal_stops_a_process_and_the_forced_one_stops_a_stubborn_one(self):
        polite = processes.start_in_background(list(SLEEP), stdin=subprocess.DEVNULL,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(processes.kill_process, polite)
        processes.terminate_process(polite)
        polite.wait(timeout=10)
        self.assertIsNotNone(polite.poll())
        stubborn = processes.start_in_background([PYTHON, "-c", "import signal, time\n"
                                                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                                  "print('ready', flush=True)\ntime.sleep(30)\n"],
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
        self.addCleanup(stubborn.stdout.close)
        self.assertEqual(stubborn.stdout.readline().strip(), "ready")
        processes.terminate_process(stubborn)
        time.sleep(0.2)
        self.assertIsNone(stubborn.poll())          # the gentle signal is ignored
        processes.kill_process(stubborn)
        stubborn.wait(timeout=10)
        self.assertIsNotNone(stubborn.poll())

    @unittest.skipIf(WINDOWS, "Windows has no hang-up signal")
    def test_a_hang_up_can_be_ignored(self):
        import signal
        previous = signal.getsignal(signal.SIGHUP)
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        processes.ignore_hangup()
        self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)


class Tokens(Started):
    """A token names one process: the same computer, the same process id and the same start."""

    def test_a_token_names_this_process_and_is_alive(self):
        token = processes.process_token()
        host, pid, start = token.rsplit(":", 2)
        self.assertEqual(host, socket.gethostname())
        self.assertEqual(int(pid), os.getpid())
        if not WINDOWS:
            self.assertEqual(start, processes.start_token(os.getpid()))
        self.assertTrue(processes.token_alive(token))

    def test_a_foreign_or_unreadable_token_counts_as_alive_and_a_changed_start_as_gone(self):
        host, pid, start = processes.process_token().rsplit(":", 2)
        self.assertTrue(processes.token_alive(""))
        self.assertTrue(processes.token_alive("nonsense"))
        self.assertTrue(processes.token_alive(f"another-computer:{pid}:{start}"))
        self.assertFalse(processes.token_alive(f"{host}:{pid}:{start}1") if start else False)

    @unittest.skipIf(WINDOWS, "the first Windows version cannot compare a token read through this test")
    def test_the_token_of_a_started_process_is_alive_until_it_is_gone(self):
        process = self.start()
        self.wait_for(lambda: processes.start_token(process.pid), "the process did not start")
        token = f"{socket.gethostname()}:{process.pid}:{processes.start_token(process.pid)}"
        self.assertTrue(processes.token_alive(token))
        processes.stop_process_group(process)
        self.wait_for(lambda: not processes.token_alive(token), "the ended process still reads as alive", timeout=5)


@unittest.skipIf(WINDOWS, "the first Windows version does not list the files a process holds open")
class OpenFileTable(Started):
    def test_the_files_this_process_holds_open_are_listed(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, root)
        with (root / "held").open("w") as stream:
            opened = processes.open_files(os.getpid())
        if sys.platform == "darwin":
            self.assertEqual(opened, [])        # macOS shows no descriptor table, so none is found there
            return
        found = [item for item in opened if item.path == root / "held"]
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].device, found[0].inode), (os.stat(root / "held").st_dev, os.stat(root / "held").st_ino))



@unittest.skipIf(WINDOWS, "the first Windows version does not scan for the processes of a folder")
class FolderOwnership(unittest.TestCase):
    def test_the_executable_and_the_folder_marker_select_a_process(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, root)
        table = {2: {"exe": root / "runtime/python", "env": {}},
                 3: {"exe": Path("/usr/bin/tmux"), "env": {"QORQUT_FOLDER_ROOT": str(root)}},
                 4: {"exe": Path("/usr/bin/node"), "env": {}}}
        with mock.patch.object(processes_unix, "darwin", return_value=False), \
                mock.patch.object(processes_unix, "pids", return_value=list(table)), \
                mock.patch.object(processes_unix, "uids", side_effect=lambda pid: (os.geteuid(),) * 3), \
                mock.patch.object(processes_unix, "is_zombie", return_value=False), \
                mock.patch.object(processes_unix, "identity", side_effect=lambda pid: f"boot:{pid}"), \
                mock.patch.object(processes_unix, "exe", side_effect=lambda pid: table[pid]["exe"]), \
                mock.patch.object(processes_unix, "environ", side_effect=lambda pid: table[pid]["env"]), \
                mock.patch.object(processes_unix, "parent_pid", side_effect=lambda pid: 1):
            found = [item["pid"] for item in processes.owned(root)]
        self.assertEqual(found, [2, 3])


@unittest.skipUnless(WINDOWS, "the first Windows version refuses what it cannot read")
class WindowsLimits(unittest.TestCase):
    def test_operations_without_a_first_windows_version_are_refused(self):
        cases = ((processes.fields, (os.getpid(),)),
                 (processes.cmdline, (os.getpid(),)),
                 (processes.cwd, (os.getpid(),)),
                 (processes.environ, (os.getpid(),)),
                 (processes.uids, (os.getpid(),)),
                 (processes.namespace_id, ()),
                 (processes.owns_listener, (os.getpid(), 80, None)),
                 (processes.owned, (Path(tempfile.gettempdir()),)))
        for operation, arguments in cases:
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(UnsupportedSystem) as raised:
                    operation(*arguments)
                self.assertTrue(str(raised.exception))

    def test_a_process_outside_a_job_object_cannot_be_stopped(self):
        process = subprocess.Popen([PYTHON, "-c", "import time; time.sleep(30)"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.end, process)
        with self.assertRaises(UnsupportedSystem):
            processes.stop_process_group(process)

    @staticmethod
    def end(process):
        if process.poll() is None:
            process.kill()
        process.wait()


if __name__ == "__main__":
    unittest.main()


class IdentifiedStopping(Started):
    """Stopping one exact process by its identity, and the signals a detached session host takes."""

    def test_the_exact_process_is_stopped(self):
        process = self.start()
        identity = self.wait_for(lambda: processes.identity(process.pid), "the process did not start")
        self.assertTrue(processes.stop_identified_process(process.pid, identity))
        self.assertIsNotNone(process.wait(10))
        self.assertTrue(ended(process.pid))

    def test_a_process_with_another_start_time_is_never_signalled(self):
        process = self.start()
        identity = self.wait_for(lambda: processes.identity(process.pid), "the process did not start")
        self.assertFalse(processes.stop_identified_process(process.pid, identity + "0"))
        self.assertFalse(processes.stop_identified_process(process.pid, None))
        self.assertFalse(processes.stop_identified_process(os.getpid(), identity))
        self.assertIsNone(process.poll())

    @unittest.skipIf(WINDOWS, "Windows ends the process instead of signalling it")
    def test_a_gentle_stop_moves_on_to_the_stronger_one(self):
        script = ("import signal, time\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "print('ready', flush=True)\n"
                  "time.sleep(30)\n")
        process = self.start([PYTHON, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(process.stdout.close)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        identity = self.wait_for(lambda: processes.identity(process.pid), "the process did not start")
        self.assertTrue(processes.stop_identified_process(process.pid, identity, term_wait=0.3, kill_wait=1.0))
        self.assertIsNotNone(process.wait(10))

    @unittest.skipIf(WINDOWS, "Windows ends the process instead of signalling it")
    def test_stopping_takes_every_process_the_group_started(self):
        script = ("import subprocess, sys, time\n"
                  "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                  "print(child.pid, flush=True)\n"
                  "time.sleep(30)\n")
        process = self.start([PYTHON, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(process.stdout.close)
        child = int(process.stdout.readline().strip())
        self.wait_for(lambda: not ended(child) and processes.start_token(child), "the child did not start")
        identity = self.wait_for(lambda: processes.identity(process.pid), "the process did not start")
        self.assertTrue(processes.stop_identified_process(process.pid, identity, term_wait=1.0, kill_wait=1.0))
        self.wait_for(lambda: ended(child), "the process the group started survived the stop", timeout=5)


if __name__ == "__main__":
    unittest.main()
