"""On a Mac the Shared browser is a visible Chrome window on Qorqbot's own profile; everywhere else it stays headless.

The Mac path is run here on Linux by patching ``sys.platform`` and the Chrome paths. What only a real Mac shows (the window,
the Dock, focus) is not tested here."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from qorqut import native_paths
from qorqut.browser import agent_browsers, agent_supervisor as supervisor, managed_chrome
from qorqut.browser.agent_browsers import SHARED_ID

from test_browser_shared_profile import SharedProfileTest

AGENT = "8fdca155-2f3b-4735-800f-41624c6d26cc"


def meta(mode=None, chrome="/chrome"):
    managed = {"chrome": chrome, "port": 20400, "profile": "/state/profile", **({"mode": mode} if mode else {})}
    return {"mode": "managed", "managed": managed}


class ModeTest(unittest.TestCase):
    def test_a_mac_is_visible_and_every_other_computer_headless(self):
        self.assertEqual(supervisor.default_mode("darwin"), "visible")
        for platform in ("linux", "win32", "freebsd14"):
            self.assertEqual(supervisor.default_mode(platform), "headless")
        with mock.patch.object(supervisor.sys, "platform", "darwin"):
            self.assertEqual(supervisor.default_mode(), "visible")

    def test_a_record_without_a_mode_is_headless(self):
        self.assertEqual(supervisor.recorded_mode(meta()["managed"]), "headless")
        self.assertEqual(supervisor.recorded_mode(meta("visible")["managed"]), "visible")


class CommandTest(unittest.TestCase):
    def test_headless_is_exactly_what_it_was(self):
        with mock.patch.object(supervisor.sys, "platform", "linux"):
            expected = ["/chrome", "--headless=new", "--no-first-run", "--no-default-browser-check", "--password-store=basic",
                        "--user-data-dir=/state/profile", "--disable-sync", "--disable-extensions",
                        "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=20400",
                        "--disable-background-networking", "--disable-3d-apis", "--enable-features=OverlayScrollbar",
                        "--user-agent=UA", "about:blank"]
            self.assertEqual(supervisor.chrome_command(meta(), "UA"), expected)
            self.assertEqual(supervisor.chrome_command(meta("headless"), "UA"), expected)
            self.assertEqual(supervisor.chrome_command(meta("headless"), "UA", "file:///start.html"), expected)

    def test_visible_is_a_normal_window_that_keeps_working_when_covered(self):
        with mock.patch.object(supervisor.sys, "platform", "darwin"):
            command = supervisor.chrome_command(meta("visible"), "UA", "file:///state/start.html")
        self.assertEqual(command[0], "/chrome")
        self.assertEqual(command[-1], "file:///state/start.html")
        for absent in ("--headless", "--disable-3d-apis", "--user-agent"):
            self.assertFalse([part for part in command if part.startswith(absent)], absent)
        for present in ("--window-size=1440,900", "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
                        "--disable-background-timer-throttling", "--disable-background-networking",
                        "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=20400",
                        "--user-data-dir=/state/profile", "--use-mock-keychain", "--no-first-run", "--no-default-browser-check"):
            self.assertIn(present, command)


class Folder(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(os.path.realpath(temporary.name))
        self.state = self.folder / AGENT
        self.state.mkdir(mode=0o700)
        self.profile = self.state / "profile"
        self.profile.mkdir(mode=0o700)


class RecordTest(Folder):
    def write(self, **managed):
        (self.state / "meta.json").write_text(json.dumps({
            "schema_version": 1, "mode": "managed", "agent_id": AGENT,
            "managed": {"chrome": "/chrome", "port": 20400, "profile": str(self.profile), **managed},
            "target": {"content": {"cdp_endpoint": "http://127.0.0.1:20400"}}}))

    def test_the_supervisor_reads_either_mode_and_a_record_without_one(self):
        for managed in ({}, {"mode": "headless"}, {"mode": "visible"}):
            with self.subTest(managed=managed):
                self.write(**managed)
                self.assertEqual(supervisor.recorded_mode(supervisor.load_meta(self.state)["managed"]),
                                 managed.get("mode", "headless"))

    def test_an_unknown_mode_is_refused(self):
        for mode in ("window", "", None, 1):
            with self.subTest(mode=mode):
                self.write(mode=mode)
                with self.assertRaises(ValueError):
                    supervisor.load_meta(self.state)


class ProfileTest(Folder):
    def preferences(self):
        return json.loads((self.profile / "Default/Preferences").read_text())

    def local_state(self):
        return json.loads((self.profile / "Local State").read_text())

    def test_a_new_profile_is_named_and_coloured(self):
        self.assertEqual(supervisor.mark_profile(self.state, self.profile), "marked")
        self.assertEqual(self.preferences(), {"profile": {"name": "Qorqbot agents"}, "browser": {"theme": {
            "user_color": supervisor.THEME_COLOR, "user_color2": supervisor.THEME_COLOR}}})
        self.assertEqual(self.local_state(), {"profile": {"info_cache": {"Default": {
            "name": "Qorqbot agents", "is_using_default_name": False}}}})
        self.assertEqual(supervisor.THEME_COLOR, -1543926)                      # 0xFFE8710A as Chrome stores it
        self.assertEqual((self.profile / "Default/Preferences").stat().st_mode & 0o777, 0o600)

    def test_every_other_setting_and_sign_in_is_kept(self):
        (self.profile / "Default").mkdir()
        (self.profile / "Default/Preferences").write_text(json.dumps({
            "profile": {"name": "Your Chrome", "exit_type": "Normal", "content_settings": {"x": 1}},
            "browser": {"window_placement": {"left": 10}, "theme": {"color_scheme2": 2}}, "extensions": {"a": [1]}}))
        (self.profile / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Default": {
            "name": "Your Chrome", "is_using_default_name": True, "avatar_icon": "x"}}, "profiles_order": ["Default"]},
            "os_crypt": {"key": "kept"}}))
        (self.profile / "Default/Cookies").write_bytes(b"cookies")
        supervisor.mark_profile(self.state, self.profile)
        self.assertEqual(self.preferences(), {
            "profile": {"name": "Qorqbot agents", "exit_type": "Normal", "content_settings": {"x": 1}},
            "browser": {"window_placement": {"left": 10}, "theme": {"color_scheme2": 2, "user_color": supervisor.THEME_COLOR,
                                                                    "user_color2": supervisor.THEME_COLOR}},
            "extensions": {"a": [1]}})
        self.assertEqual(self.local_state(), {"profile": {"info_cache": {"Default": {
            "name": "Qorqbot agents", "is_using_default_name": False, "avatar_icon": "x"}}, "profiles_order": ["Default"]},
            "os_crypt": {"key": "kept"}})
        self.assertEqual((self.profile / "Default/Cookies").read_bytes(), b"cookies")

    def test_it_is_done_once_so_the_owners_own_choice_stays(self):
        supervisor.mark_profile(self.state, self.profile)
        changed = {**self.preferences(), "browser": {"theme": {"user_color2": 5}}}
        (self.profile / "Default/Preferences").write_text(json.dumps(changed))
        self.assertEqual(supervisor.mark_profile(self.state, self.profile), "kept")
        self.assertEqual(self.preferences(), changed)

    def test_a_profile_a_chrome_may_be_using_is_not_touched(self):
        os.symlink("host-123", self.profile / "SingletonLock")
        self.assertEqual(supervisor.mark_profile(self.state, self.profile), "in_use")
        self.assertFalse((self.profile / "Default").exists())
        self.assertFalse((self.state / supervisor.IDENTITY_FILE).exists())     # the next start tries again

    def test_a_settings_file_that_is_not_json_is_left_to_chrome(self):
        (self.profile / "Default").mkdir()
        (self.profile / "Default/Preferences").write_text("{broken")
        self.assertEqual(supervisor.mark_profile(self.state, self.profile), "unreadable")
        self.assertEqual((self.profile / "Default/Preferences").read_text(), "{broken")
        self.assertFalse((self.profile / "Local State").exists())

    def test_nothing_is_written_through_a_symbolic_link(self):
        elsewhere = self.folder / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.profile / "Default")
        self.assertEqual(supervisor.mark_profile(self.state, self.profile), "unwritable")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_the_first_tab_is_a_small_local_page(self):
        url = supervisor.start_page_url(self.state)
        self.assertEqual(url, (self.state / "start.html").as_uri())
        self.assertIn("Qorqbot's agents work", (self.state / "start.html").read_text())
        self.assertLess(len(supervisor.START_PAGE_TEXT), 1024)


class ServeTest(Folder):
    """What the supervisor starts for each mode; Chrome itself is not started here. The mode comes from the record alone, so
    this runs as Linux (reading process identities the Mac way needs a Mac)."""

    def started(self, mode):
        chrome = self.folder / "chrome"
        chrome.write_text('#!/bin/sh\n[ "$1" = --version ] && echo "Google Chrome 153.0.8010.36"\n')
        chrome.chmod(0o700)
        (self.state / "meta.json").write_text(json.dumps({
            "schema_version": 1, "mode": "managed", "agent_id": AGENT,
            "managed": {"chrome": str(chrome), "port": 20400, "profile": str(self.profile), **({"mode": mode} if mode else {})},
            "target": {"content": {"cdp_endpoint": "http://127.0.0.1:20400"}}}))
        popen, commands = supervisor.subprocess.Popen, []

        def start(command, **options):
            commands.append(command)
            if command[1:] == ["--version"]:
                return popen(command, **options)
            raise OSError("the browser itself is not started in this test")

        with mock.patch.object(supervisor.sys, "platform", "linux"), mock.patch.object(supervisor, "port_free", return_value=True), \
                mock.patch.object(supervisor, "chrome_environment", return_value={"PATH": "/usr/bin:/bin"}), \
                mock.patch.object(supervisor.subprocess, "Popen", side_effect=start):
            self.assertEqual(supervisor.serve(self.state, threading.Event()), supervisor.EXIT_FAILED)
        return commands

    def test_a_visible_browser_is_not_asked_its_version_and_opens_on_its_page(self):
        (command,) = self.started("visible")
        self.assertNotIn("--headless=new", command)
        self.assertEqual(command[-1], (self.state / "start.html").as_uri())
        self.assertEqual(json.loads((self.profile / "Default/Preferences").read_text())["profile"]["name"], "Qorqbot agents")

    def test_a_record_without_a_mode_starts_headless_and_the_profile_is_not_touched(self):
        version, command = self.started(None)
        self.assertEqual(version[1:], ["--version"])
        self.assertIn("--headless=new", command)
        self.assertEqual(command[-1], "about:blank")
        self.assertFalse((self.profile / "Default").exists())
        self.assertFalse((self.state / "start.html").exists())


class Programs(SharedProfileTest):
    """Stand-ins for Chrome programs: shell scripts that print a version."""

    def program(self, path, version):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\necho "Google Chrome {version}"\n' if version else "#!/bin/sh\nexit 1\n")
        path.chmod(0o700)
        return str(path)

    def own_build(self, version="158.0.8127.36"):
        return self.program(self.paths.runtime / "chrome/current/chrome-mac-arm64" / managed_chrome.MAC_PROGRAM, version)

    def installed(self, version="160.0.1.2"):
        return self.program(self.root / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome", version)

    def on(self, platform, installed=None, preferred=None, external=False):
        """Patches for one kind of computer: its platform, its installed Chrome, Qorqbot's own build and an explicit choice."""
        patches = [mock.patch.object(sys, "platform", platform),
                   mock.patch.object(native_paths, "installed_chrome", return_value=installed),
                   mock.patch.object(managed_chrome, "preferred", return_value=preferred),
                   mock.patch.object(managed_chrome, "external_choice", return_value=external),
                   mock.patch.object(supervisor, "chrome_environment", return_value={"PATH": "/usr/bin:/bin"})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def record(self):
        return json.loads((self.state(SHARED_ID) / "meta.json").read_text())["managed"]

    def existing(self, chrome, **managed):
        """A Shared browser created earlier and stopped now."""
        self.manage("ensure")
        self.launcher.running.clear()
        path = self.state(SHARED_ID) / "meta.json"
        record = json.loads(path.read_text())
        record["managed"]["chrome"] = chrome
        record["managed"].pop("mode")
        record["managed"].update(managed)
        agent_browsers.write_json_private(path, record)
        return record


class CreateTest(Programs):
    def test_a_new_linux_browser_is_recorded_headless_on_the_chrome_it_had(self):
        own = self.own_build()
        self.on("linux", installed="/usr/bin/google-chrome-stable", preferred=own)
        self.manage("ensure")
        self.assertEqual((self.record()["mode"], self.record()["chrome"]), ("headless", own))

    def test_a_new_mac_browser_is_visible_on_the_installed_google_chrome(self):
        installed = self.installed()
        self.on("darwin", installed=installed, preferred=self.own_build())
        self.manage("ensure")
        self.assertEqual((self.record()["mode"], self.record()["chrome"]), ("visible", installed))

    def test_a_mac_without_google_chrome_falls_back_to_qorqbots_own(self):
        own = self.own_build()
        self.on("darwin", installed=None, preferred=own)
        self.manage("ensure")
        self.assertEqual((self.record()["mode"], self.record()["chrome"]), ("visible", own))

    def test_an_explicit_choice_wins_on_a_mac(self):
        self.on("darwin", installed=self.installed(), preferred=None, external=True)
        self.manage("ensure")
        self.assertEqual((self.record()["mode"], self.record()["chrome"]), ("visible", sys.executable))


class MoveTest(Programs):
    def test_a_linux_record_without_a_mode_is_left_exactly_as_it_was(self):
        self.on("linux", installed="/usr/bin/google-chrome-stable")
        before = self.existing(self.own_build())
        self.manage("ensure")
        self.assertEqual(json.loads((self.state(SHARED_ID) / "meta.json").read_text()), before)
        self.assertEqual(self.launcher.starts, [SHARED_ID, SHARED_ID])

    def test_an_existing_mac_browser_becomes_visible_on_newer_google_chrome_at_its_next_start(self):
        installed = self.installed("160.0.1.2")
        self.on("darwin", installed=installed)
        before = self.existing(self.own_build("158.0.8127.36"))
        (self.state(SHARED_ID) / "profile/Cookies").write_bytes(b"sign-ins")
        result = self.manage("ensure")
        self.assertEqual(result["state"], "healthy")
        after = json.loads((self.state(SHARED_ID) / "meta.json").read_text())
        self.assertEqual((after["managed"]["mode"], after["managed"]["chrome"]), ("visible", installed))
        self.assertEqual({**after, "managed": {**after["managed"], "chrome": None, "mode": None}},
                         {**before, "managed": {**before["managed"], "chrome": None, "mode": None}})
        self.assertEqual((self.state(SHARED_ID) / "profile/Cookies").read_bytes(), b"sign-ins")
        self.assertTrue(agent_browsers.meta_valid(after, SHARED_ID, self.state(SHARED_ID)))

    def test_the_same_major_version_moves(self):
        installed = self.installed("158.0.8200.1")
        self.on("darwin", installed=installed)
        self.existing(self.own_build("158.0.8127.36"))
        self.manage("ensure")
        self.assertEqual(self.record()["chrome"], installed)

    def test_an_older_google_chrome_is_never_given_the_profile(self):
        self.on("darwin", installed=self.installed("157.0.1.2"))
        own = self.own_build("158.0.8127.36")
        self.existing(own)
        self.manage("ensure")
        self.assertEqual((self.record()["mode"], self.record()["chrome"]), ("visible", own))

    def test_a_profile_last_opened_by_a_newer_chrome_stays(self):
        self.on("darwin", installed=self.installed("159.0.1.2"))
        own = self.own_build("158.0.8127.36")
        self.existing(own)
        (self.state(SHARED_ID) / "profile/Last Version").write_text("161.0.1.2")
        self.manage("ensure")
        self.assertEqual(self.record()["chrome"], own)

    def test_a_version_that_cannot_be_told_stays(self):
        for installed_version, own_version in ((None, "158.0.8127.36"), ("160.0.1.2", None)):
            with self.subTest(installed=installed_version, own=own_version):
                self.on("darwin", installed=self.installed(installed_version))
                own = self.own_build(own_version)
                self.existing(own)
                self.manage("ensure")
                self.assertEqual(self.record()["chrome"], own)

    def test_a_browser_on_another_chrome_keeps_it(self):
        self.on("darwin", installed=self.installed("160.0.1.2"))
        other = self.program(self.root / "elsewhere/chrome", "170.0.1.2")
        self.existing(other)
        self.manage("ensure")
        self.assertEqual(self.record()["chrome"], other)

    def test_an_explicit_choice_keeps_the_recorded_chrome(self):
        self.on("darwin", installed=self.installed("160.0.1.2"), external=True)
        own = self.own_build("158.0.8127.36")
        self.existing(own)
        self.manage("ensure")
        self.assertEqual(self.record()["chrome"], own)

    def test_a_running_browser_is_not_rewritten(self):
        self.on("darwin", installed=self.installed("160.0.1.2"))
        own = self.own_build("158.0.8127.36")
        before = self.existing(own)
        self.launcher.running.add(str(self.state(SHARED_ID)))       # it kept running: headless until it starts again
        self.manage("ensure")
        self.assertEqual(json.loads((self.state(SHARED_ID) / "meta.json").read_text()), before)

    def test_an_unknown_mode_makes_the_record_invalid(self):
        self.on("linux")
        record = self.existing(sys.executable, mode="window")
        self.assertFalse(agent_browsers.meta_valid(record, SHARED_ID, self.state(SHARED_ID)))


class InUseTest(Programs):
    def test_settings_learn_which_chrome_and_which_mode(self):
        installed = self.installed()
        self.on("darwin", installed=installed, preferred=self.own_build())
        self.assertEqual(self.manager.chrome_in_use(), {"kind": "google_chrome", "mode": "visible"})   # before it exists
        self.manage("ensure")
        self.assertEqual(self.manager.chrome_in_use(), {"kind": "google_chrome", "mode": "visible"})
        self.existing(self.own_build())
        self.assertEqual(self.manager.chrome_in_use(), {"kind": "chrome_for_testing", "mode": "headless"})

    def test_linux_names_its_chrome_too(self):
        self.on("linux", installed="/usr/bin/google-chrome-stable", preferred=None)
        with mock.patch.object(self.manager, "chrome", "/usr/bin/google-chrome-stable"):
            self.assertEqual(self.manager.chrome_in_use(), {"kind": "google_chrome", "mode": "headless"})


class OwnBuildTest(unittest.TestCase):
    def test_only_paths_under_the_runtime_chrome_folder_are_qorqbots_own(self):
        runtime = Path("/srv/runtime")
        self.assertTrue(managed_chrome.is_own("/srv/runtime/chrome/current/chrome-linux64/chrome", runtime))
        for path in ("/srv/runtime/chromeX/chrome", "/srv/runtime/chrome/../other/chrome", native_paths.MAC_CHROME,
                     "/usr/bin/google-chrome-stable"):
            with self.subTest(path=path):
                self.assertFalse(managed_chrome.is_own(path, runtime))


if __name__ == "__main__":
    unittest.main()
