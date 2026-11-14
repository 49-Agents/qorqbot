"""A worker's browser supervisor starts in the user manager, which does not see the application's private bind mounts."""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut.browser import agent_browsers, agent_supervisor as supervisor
from qorqut.browser.agent_browsers import SYSTEMD_RUN, UserUnitLauncher, outside_private_mounts

AGENT = "8fdca155-2f3b-4735-800f-41624c6d26cc"
UNIT = f"qorqut-agent-browser-0123456789ab-{AGENT}.service"
# The application's view: the instance kept in /srv/copies/instance one is bound over /srv/app.
TABLE = ("29 1 9:1 / / rw,relatime shared:1 - ext4 /dev/md1 rw\n"
         "40 29 0:25 / /run rw,nosuid shared:5 - tmpfs tmpfs rw\n"
         "812 29 9:1 /srv/copies/instance\\040one /srv/app rw,relatime shared:1 - ext4 /dev/md1 rw\n")


@unittest.skipUnless(sys.platform == "linux", "mount namespaces are a Linux feature")
class OutsidePrivateMountsTest(unittest.TestCase):
    def resolve(self, path, table=TABLE, same=lambda _a, _b: True):
        return outside_private_mounts(Path(path), table, same)

    def test_a_path_below_a_bind_mount_becomes_its_path_on_the_filesystem(self):
        self.assertEqual(self.resolve("/srv/app/operations/.runtime/agent-browsers"),
                         Path("/srv/copies/instance one/operations/.runtime/agent-browsers"))
        self.assertEqual(self.resolve("/srv/app"), Path("/srv/copies/instance one"))

    def test_other_paths_are_left_alone_without_touching_the_disk(self):
        same = mock.Mock(side_effect=AssertionError("not consulted"))
        for path in ("/srv/application/x", "/home/user/instance", "/run/user/1000", "/"):
            with self.subTest(path=path):
                self.assertEqual(self.resolve(path, same=same), Path(path))

    def test_a_candidate_that_is_another_directory_is_refused(self):
        self.assertEqual(self.resolve("/srv/app/x", same=lambda _a, _b: False), Path("/srv/app/x"))
        self.assertEqual(self.resolve("/srv/app/x", same=mock.Mock(side_effect=FileNotFoundError)), Path("/srv/app/x"))

    def test_a_subvolume_mounted_on_its_own_is_not_a_bind_mount(self):
        table = "29 1 0:30 /@ / rw - btrfs /dev/sda2 rw\n31 29 0:30 /@home /home rw - btrfs /dev/sda2 rw\n"
        self.assertEqual(self.resolve("/home/user/instance", table), Path("/home/user/instance"))

    def test_the_later_mount_on_the_same_point_is_the_visible_one(self):
        covered = TABLE + "900 29 0:77 / /srv/app rw - tmpfs tmpfs rw\n"
        self.assertEqual(self.resolve("/srv/app/x", covered), Path("/srv/app/x"))

    def test_a_bind_of_another_device_uses_that_device_only(self):
        table = TABLE + ("50 29 8:17 / /mnt/data rw - ext4 /dev/sdb1 rw\n"
                         "813 29 8:17 /instances/main /srv/app/data rw - ext4 /dev/sdb1 rw\n")
        self.assertEqual(self.resolve("/srv/app/data/store", table), Path("/mnt/data/instances/main/store"))

    def test_the_real_mount_table_leaves_an_ordinary_directory_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(outside_private_mounts(Path(directory)), Path(directory))


class UserUnitLauncherTest(unittest.TestCase):
    def start(self, directory, outside):
        calls = []

        def runner(argv, **_kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="LoadState=not-found\nActiveState=inactive\n", stderr="")
        with mock.patch.object(agent_browsers, "outside_private_mounts",
                               side_effect=lambda path: outside if Path(path) == directory else Path(path)):
            self.assertTrue(UserUnitLauncher(runner=runner, sleep=lambda _seconds: None).start(UNIT, directory))
        return next(argv for argv in calls if argv[0] == SYSTEMD_RUN)

    def test_the_user_manager_is_given_the_path_outside_the_private_mount(self):
        directory, outside = Path("/srv/app/agent-browsers") / AGENT, Path("/srv/copies/instance/agent-browsers") / AGENT
        argv = self.start(directory, outside)
        self.assertIn(f"--working-directory={outside}", argv)
        self.assertIn(f"--setenv=HOME={outside}", argv)
        self.assertEqual(argv[argv.index("--state"):], ["--state", str(outside), "--recorded-state", str(directory)])
        self.assertFalse([value for value in argv if value.startswith("--") and str(directory) in value])

    def test_an_installation_without_a_private_mount_starts_as_before(self):
        directory = Path("/srv/app/agent-browsers") / AGENT
        argv = self.start(directory, directory)
        self.assertIn(f"--working-directory={directory}", argv)
        self.assertEqual(argv[argv.index("--state"):], ["--state", str(directory)])


class SupervisorRecordTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(os.path.realpath(temporary.name)) / AGENT
        self.state.mkdir(mode=0o700)
        self.recorded = Path("/srv/app/agent-browsers") / AGENT

    def write(self, profile):
        (self.state / "meta.json").write_text(json.dumps({
            "schema_version": 1, "mode": "managed", "agent_id": AGENT,
            "managed": {"chrome": "/usr/bin/chrome", "port": 20400, "profile": str(profile)},
            "target": {"content": {"cdp_endpoint": "http://127.0.0.1:20400"}}}))

    def test_a_record_written_behind_a_private_mount_is_accepted_and_uses_the_real_profile(self):
        self.write(self.recorded / "profile")
        meta = supervisor.load_meta(self.state, self.recorded)
        self.assertEqual(meta["managed"]["profile"], str(self.state / "profile"))
        self.assertIn(f"--user-data-dir={self.state / 'profile'}", supervisor.chrome_command(meta))

    def test_the_record_must_still_match_exactly(self):
        self.write(self.recorded / "profile")
        with self.assertRaises(ValueError):
            supervisor.load_meta(self.state)
        self.write(self.state / "profile")
        with self.assertRaises(ValueError):
            supervisor.load_meta(self.state, self.recorded)
        self.write(self.recorded.parent / "other" / "profile")
        with self.assertRaises(ValueError):
            supervisor.load_meta(self.state, self.recorded)

    def test_the_recorded_path_must_name_the_same_agent(self):
        other = self.recorded.parent / "3b2f6a1e-3d58-4c0e-9a55-5f0e8f1f7c11"
        self.write(other / "profile")
        with self.assertRaises(ValueError):
            supervisor.load_meta(self.state, other)
        with self.assertRaises(ValueError):
            supervisor.load_meta(self.state, Path("agent-browsers") / AGENT)

    def test_an_unchanged_installation_reads_its_record_as_before(self):
        self.write(self.state / "profile")
        self.assertEqual(supervisor.load_meta(self.state)["managed"]["profile"], str(self.state / "profile"))


class TemporaryDirectoryTest(unittest.TestCase):
    """Chrome aborts when its singleton socket, kept under TMPDIR, has a path longer than a Unix socket address."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(os.path.realpath(temporary.name))
        for name in ("folder_root", "root"):
            patch = mock.patch(f"qorqut.native_paths.{name}", return_value=None)
            patch.start()
            self.addCleanup(patch.stop)

    def environment(self, state, short):
        with mock.patch.object(supervisor.tempfile, "gettempdir", return_value=str(short)), \
                mock.patch.object(supervisor.os, "getuid", return_value=2 ** 31 - 7):       # no /run/user directory for it
            return supervisor.chrome_environment(state)

    def test_a_shallow_state_directory_keeps_its_own_temporary_folder(self):
        state = self.root / "s"
        state.mkdir(mode=0o700)
        if not supervisor.socket_fits(state / "tmp"):
            self.skipTest("the test directory is already too deep")
        self.assertEqual(self.environment(state, self.root / "unused")["TMPDIR"], str(state / "tmp"))

    def test_a_deep_state_directory_gets_a_short_private_one(self):
        state, short = self.root / ("d" * 90) / AGENT, self.root / "t"
        state.mkdir(mode=0o700, parents=True)
        short.mkdir()
        if not supervisor.socket_fits(short / "qorqut-browser-0123456789ab"):
            self.skipTest("the test directory is already too deep")
        environment = self.environment(state, short)
        temporary = Path(environment["TMPDIR"])
        self.assertEqual(temporary.parent, short)
        self.assertTrue(supervisor.socket_fits(temporary))
        self.assertEqual(temporary.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.environment(state, short)["TMPDIR"], str(temporary))        # stable for the same agent
        self.assertEqual(environment["XDG_RUNTIME_DIR"], str(state / "run"))

    def test_a_short_directory_someone_else_can_enter_is_refused(self):
        state, short = self.root / ("d" * 90) / AGENT, self.root / "t"
        state.mkdir(mode=0o700, parents=True)
        short.mkdir()
        if not supervisor.socket_fits(short / "qorqut-browser-0123456789ab"):
            self.skipTest("the test directory is already too deep")
        taken = Path(self.environment(state, short)["TMPDIR"])
        taken.chmod(0o755)
        with self.assertRaises(RuntimeError):
            self.environment(state, short)
        taken.rmdir()
        taken.symlink_to(self.root)
        with self.assertRaises(RuntimeError):
            self.environment(state, short)


if __name__ == "__main__":
    unittest.main()
