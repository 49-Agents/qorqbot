"""Places: the data, run and temporary folders, the channel's path limit, the mount table and the service manager."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from qorqut_system import places, places_unix
from qorqut_system.errors import UnsupportedSystem


class Folders(unittest.TestCase):
    def test_the_temporary_folder_is_the_one_the_process_uses(self):
        self.assertEqual(places.temporary_folder(), Path(tempfile.gettempdir()))
        self.assertTrue(places.temporary_folder().is_dir())

    @unittest.skipIf(sys.platform == "win32", "the state folder under the home folder is the Unix one")
    def test_xdg_state_home_is_the_data_folder_when_set(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"XDG_STATE_HOME": root}):
                self.assertEqual(places.data_folder(), Path(root))

    @unittest.skipUnless(sys.platform == "win32", "Windows keeps a per-user installation under its own folder")
    def test_local_app_data_is_the_data_folder_when_set(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": root}):
                self.assertEqual(places.data_folder(), Path(root))

    def test_the_data_folder_is_absolute(self):
        self.assertTrue(places.data_folder().is_absolute())

    def test_a_run_folder_is_a_real_folder_of_this_user(self):
        folder = places.run_folder()
        if folder is None:
            return                                  # no runtime folder here, or a system that has none
        self.assertTrue(folder.is_absolute())
        self.assertTrue(folder.is_dir())
        self.assertFalse(folder.is_symlink())
        if sys.platform != "win32":
            self.assertEqual(folder.stat().st_uid, os.geteuid())


class ChannelPath(unittest.TestCase):
    def test_the_limit_is_the_one_of_this_system(self):
        if sys.platform == "win32":
            self.assertIsNone(places.channel_path_limit())
        elif sys.platform == "darwin":
            self.assertEqual(places.channel_path_limit(), 104)
        else:
            self.assertEqual(places.channel_path_limit(), 108)

    def test_a_short_path_fits_and_a_long_one_does_not(self):
        self.assertTrue(places.channel_path_fits("/a/control.sock"))
        limit = places.channel_path_limit()
        long_path = "/a/" + "x" * (limit + 1 if limit is not None else 200)
        self.assertEqual(places.channel_path_fits(long_path), limit is None)


class ServiceManager(unittest.TestCase):
    def test_the_service_manager_is_offered_only_on_linux(self):
        if sys.platform.startswith("linux"):
            self.assertIsInstance(places.service_manager_available(), bool)
            return
        self.assertFalse(places.service_manager_available())
        with self.assertRaises(UnsupportedSystem):
            places.require_service_manager()

    @unittest.skipIf(places.service_manager_available(), "this system has a service manager; no unit is started here")
    def test_a_session_host_cannot_run_without_a_service_manager(self):
        # Windows refuses it; elsewhere systemd-run is not there to start, which is the OSError the callers handle.
        with self.assertRaises(UnsupportedSystem if os.name == "nt" else OSError):
            places.run_session_host(unit="qorqut-managed-check.service", description="check", working_directory="/",
                                    stop_timeout=5, command=["true"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "a session host runs under systemd on Linux only")
    def test_the_session_host_command_holds_todays_lines(self):
        command = ["/usr/bin/python3", "-B", "-m", "qorqut.managed_runtime.host", "--state", "/s"]
        argv = places.session_host_command(unit="qorqut-managed-x.service", description="Qorqut managed agent host",
                                           working_directory="/", stop_timeout=40, command=command,
                                           log_directory="/s", log_name="host.log", environment={"HOME": "/home/a"})
        self.assertEqual(argv[:6], ["/usr/bin/systemd-run", "--user", "--unit=qorqut-managed-x.service", "--collect",
                                    "--quiet", "--description=Qorqut managed agent host"])
        self.assertIn("--working-directory=/", argv)
        self.assertIn("--property=TimeoutStopSec=40", argv)
        self.assertIn("--property=StandardOutput=append:/s/host.log", argv)
        self.assertIn("--property=StandardError=append:/s/host.log", argv)
        self.assertIn("--setenv=HOME=/home/a", argv)
        self.assertEqual(argv[argv.index("--") + 1:], command)

    @unittest.skipUnless(sys.platform.startswith("linux"), "a session host runs under systemd on Linux only")
    def test_a_unit_without_a_log_file_has_no_output_property(self):
        argv = places.session_host_command(unit="u.service", description="d", working_directory="/", stop_timeout=30,
                                           command=["prog"])
        self.assertFalse([part for part in argv if part.startswith("--property=Standard")])

    @unittest.skipUnless(sys.platform.startswith("linux"), "a session host runs under systemd on Linux only")
    def test_the_service_manager_environment_names_the_user_runtime_folder(self):
        environment = places.service_manager_environment()
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")
        self.assertEqual(environment["LANG"], "C.UTF-8")
        self.assertEqual(environment["XDG_RUNTIME_DIR"], "/run/user/%d" % os.getuid())


class ServiceManagerCalls(unittest.TestCase):
    """The calls the layer makes to the user's service manager, through the runner its caller brings."""

    def runner(self, returncode=0, stdout="", stderr=""):
        calls = []

        def run(argv, **options):
            calls.append((argv, options))
            return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
        return run, calls

    @unittest.skipUnless(sys.platform.startswith("linux"), "a session host runs under systemd on Linux only")
    def test_a_units_state_is_read_from_the_property_list(self):
        run, calls = self.runner(stdout="LoadState=loaded\nActiveState=active\n")
        self.assertEqual(places.session_host_state("qorqut-managed-x.service", runner=run), "active")
        self.assertEqual(calls[0][0], ["/usr/bin/systemctl", "--user", "show", "qorqut-managed-x.service",
                                       "--property=LoadState,ActiveState"])
        self.assertEqual(calls[0][1]["timeout"], 10)
        self.assertEqual(calls[0][1]["env"], places.service_manager_environment())

    @unittest.skipUnless(sys.platform.startswith("linux"), "a session host runs under systemd on Linux only")
    def test_a_unit_that_is_not_loaded_is_inactive(self):
        run, _ = self.runner(stdout="LoadState=not-found\nActiveState=inactive\n")
        self.assertEqual(places.session_host_state("u.service", runner=run), "inactive")
        run, _ = self.runner(stdout="LoadState=loaded\n")
        self.assertEqual(places.session_host_state("u.service", runner=run), "inactive")

    @unittest.skipUnless(sys.platform.startswith("linux"), "a session host runs under systemd on Linux only")
    def test_a_manager_that_cannot_be_reached_is_refused(self):
        run, _ = self.runner(1, "", "Failed to connect to bus")
        with self.assertRaises(UnsupportedSystem):
            places.session_host_state("u.service", runner=run)

    @unittest.skipUnless(sys.platform.startswith("linux"), "a session host runs under systemd on Linux only")
    def test_a_unit_is_stopped_by_its_name(self):
        run, calls = self.runner()
        places.stop_session_host("u.service", runner=run)
        self.assertEqual(calls[0][0], ["/usr/bin/systemctl", "--user", "stop", "u.service"])
        self.assertEqual(calls[0][1]["timeout"], 60)

    @unittest.skipUnless(sys.platform.startswith("linux"), "a session host runs under systemd on Linux only")
    def test_a_session_host_is_started_through_the_callers_runner(self):
        run, calls = self.runner()
        done = places.run_session_host(unit="u.service", description="d", working_directory="/", stop_timeout=30,
                                       command=["prog", "arg"], environment={"HOME": "/home/a"}, runner=run)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(calls[0][0][0], places.SYSTEMD_RUN)
        self.assertEqual(calls[0][0][-2:], ["prog", "arg"])
        self.assertIn("--setenv=HOME=/home/a", calls[0][0])

    @unittest.skipIf(sys.platform == "win32", "Windows has no service manager to ask")
    def test_the_service_managers_own_main_process_is_read(self):
        with mock.patch.object(places_unix.subprocess, "check_output", return_value=b"4242\n") as output:
            self.assertEqual(places.service_main_pid("qorqut.service"), 4242)
        output.assert_called_once_with(["systemctl", "show", "qorqut.service", "-p", "MainPID", "--value"])


class PrivateMounts(unittest.TestCase):
    """A path this service sees through a bind mount of its own, by the path it also has outside that namespace."""

    TABLE = ("29 1 9:1 / / rw,relatime shared:1 - ext4 /dev/md1 rw\n"
             "812 29 9:1 /srv/copies/instance\\040one /srv/app rw,relatime shared:1 - ext4 /dev/md1 rw\n")

    def test_a_path_below_a_bind_mount_becomes_its_path_on_the_filesystem(self):
        if sys.platform != "linux":
            self.assertEqual(places.outside_private_mounts(Path("/srv/app"), self.TABLE, lambda _a, _b: True),
                             Path("/srv/app"))
            return
        self.assertEqual(places.outside_private_mounts(Path("/srv/app/x"), self.TABLE, lambda _a, _b: True),
                         Path("/srv/copies/instance one/x"))

    def test_an_ordinary_path_is_left_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(places.outside_private_mounts(Path(directory)), Path(directory))


class Loading(unittest.TestCase):
    def test_the_module_imports_without_side_effects(self):
        self.assertEqual(sorted(places.__all__), sorted([
            "SYSTEMCTL", "SYSTEMCTL_NAME", "SYSTEMD_RUN", "SYSTEMD_RUN_NAME", "channel_path_fits", "channel_path_limit",
            "data_folder", "outside_private_mounts", "require_service_manager", "run_folder", "run_session_host",
            "service_main_pid", "service_manager_available", "service_manager_environment", "session_host_command",
            "session_host_state", "stop_session_host", "temporary_folder"]))


if __name__ == "__main__":
    unittest.main()