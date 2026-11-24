"""The Shared browser sends the user agent of a visible Chrome, because websites refuse one that says ``HeadlessChrome``."""

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from qorqut.browser import agent_supervisor as supervisor

AGENT = "8fdca155-2f3b-4735-800f-41624c6d26cc"
META = {"mode": "managed", "managed": {"chrome": "/chrome", "port": 20400, "profile": "/state/profile"}}
LINUX = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"


class Folder(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(os.path.realpath(temporary.name))

    def chrome(self, script):
        """A program standing in for Chrome: a shell script that is given Chrome's arguments."""
        program = self.folder / "chrome"
        program.write_text("#!/bin/sh\n" + script + "\n")
        program.chmod(0o700)
        return str(program)

    def ask(self, script, platform="linux"):
        with mock.patch.object(supervisor.sys, "platform", platform):
            return supervisor.normal_user_agent(self.chrome(script), {"PATH": "/usr/bin:/bin"})


class NormalUserAgentTest(Folder):
    def test_it_is_what_a_visible_chrome_of_that_version_sends(self):
        self.assertEqual(self.ask('[ "$1" = --version ] && echo "Google Chrome 153.0.8010.36 "'), LINUX)

    def test_qorqbots_own_chrome_is_read_the_same_way(self):
        self.assertIn(" Chrome/158.0.0.0 ", self.ask('echo "Google Chrome for Testing 158.0.8127.36 "'))

    def test_a_mac_names_itself_a_mac(self):
        self.assertEqual(self.ask('echo "Google Chrome 153.0.8010.36"', platform="darwin"),
                         "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/153.0.0.0 Safari/537.36")

    def test_only_a_whole_chrome_version_is_taken(self):
        self.assertIn(" Chrome/120.0.0.0 ", self.ask('echo "Chromium 120.0.6099.71 built on Debian 12.2, running on 12.4"'))
        for answer in ("", "Google Chrome", "version 12.2", "1.2.3.4.5", "Chrome 15300.0.1.2"):
            with self.subTest(answer=answer):
                self.assertIsNone(self.ask(f'echo "{answer}"'))

    def test_nothing_is_made_up_when_chrome_does_not_answer(self):
        self.assertIsNone(self.ask("exit 1"))
        with mock.patch.object(supervisor.sys, "platform", "linux"):
            self.assertIsNone(supervisor.normal_user_agent(str(self.folder / "absent"), {}))

    def test_a_chrome_that_hangs_is_stopped_with_what_it_started(self):
        marker = self.folder / "pid"
        with mock.patch.object(supervisor, "VERSION_TIMEOUT", 0.3):
            self.assertIsNone(self.ask(f"sleep 30 & echo $! > {marker}; wait"))
        with self.assertRaises(ProcessLookupError):       # its own process group went with it
            for _ in range(50):
                os.kill(int(marker.read_text()), 0)
                threading.Event().wait(0.05)

    def test_another_kind_of_computer_keeps_chromes_own_user_agent(self):
        self.assertIsNone(self.ask('echo "Google Chrome 153.0.8010.36"', platform="win32"))


class CommandTest(unittest.TestCase):
    def test_the_headless_browser_is_given_it_as_one_argument(self):
        command = supervisor.chrome_command(META, LINUX)
        self.assertIn(f"--user-agent={LINUX}", command)
        self.assertEqual(command[-1], "about:blank")
        self.assertEqual(command[0], "/chrome")

    def test_without_one_the_command_is_what_it_was(self):
        self.assertFalse([part for part in supervisor.chrome_command(META) if part.startswith("--user-agent")])
        self.assertEqual(supervisor.chrome_command(META, None), supervisor.chrome_command(META))

    def test_the_sign_in_window_sends_its_own(self):
        window = supervisor.visible_chrome_command(META, "https://slack.com", 55555)
        self.assertFalse([part for part in window if part.startswith("--user-agent")])


class ServeTest(Folder):
    """The supervisor asks the Chrome it is about to start, and starts it either way."""

    def started(self, script):
        state = self.folder / AGENT
        state.mkdir(mode=0o700)
        chrome = self.chrome(script)
        (state / "meta.json").write_text(json.dumps({
            "schema_version": 1, "mode": "managed", "agent_id": AGENT,
            "managed": {"chrome": chrome, "port": 20400, "profile": str(state / "profile")},
            "target": {"content": {"cdp_endpoint": "http://127.0.0.1:20400"}}}))
        popen, commands = supervisor.subprocess.Popen, []

        def start(command, **options):
            if command[1:] == ["--version"]:
                return popen(command, **options)
            commands.append((command, options))
            raise OSError("the browser itself is not started in this test")

        with mock.patch.object(supervisor.sys, "platform", "linux"), mock.patch.object(supervisor, "port_free", return_value=True), \
                mock.patch.object(supervisor, "chrome_environment", return_value={"PATH": "/usr/bin:/bin"}), \
                mock.patch.object(supervisor.subprocess, "Popen", side_effect=start):
            self.assertEqual(supervisor.serve(state, threading.Event()), supervisor.EXIT_FAILED)
        (command, options), = commands
        self.assertEqual(options["env"], {"PATH": "/usr/bin:/bin"})
        return command

    def test_the_browser_starts_with_the_user_agent_of_its_own_version(self):
        command = self.started('[ "$1" = --version ] && echo "Google Chrome 153.0.8010.36 "')
        self.assertIn(f"--user-agent={LINUX}", command)
        self.assertIn("--headless=new", command)

    def test_the_browser_still_starts_when_chrome_does_not_say_its_version(self):
        command = self.started("exit 3")
        self.assertIn("--headless=new", command)
        self.assertFalse([part for part in command if part.startswith("--user-agent")])


if __name__ == "__main__":
    unittest.main()
