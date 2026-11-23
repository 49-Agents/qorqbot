"""upload gives the browser a path it can open: Chrome reads the file itself, outside the application's private bind mounts."""

import functools
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from qorqut.bootstrap import agent_browser, browser
from qorqut.bootstrap.browser import BrowserError, upload_arguments, validate_arguments
from qorqut.browser import agent_browsers, idle_tabs

TARGET = ("http://127.0.0.1:1", Path("/bin/agent-browser"))


class Folders(unittest.TestCase):
    """A folder ``kept`` that the application sees as ``seen``, as with ``BindPaths=kept:seen``.

    The two are separate folders holding the same files under two names (hard links), which is what a bind mount looks like
    from inside. ``table`` is the mount table the application would read."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.kept, self.seen, self.work = self.root / "kept", self.root / "seen", self.root / "work"
        for directory in (self.kept / "scratch", self.seen / "scratch", self.work):
            directory.mkdir(parents=True)
        self.table = ("29 1 9:1 / / rw,relatime shared:1 - ext4 /dev/md1 rw\n"
                      f"812 29 9:1 {self.kept} {self.seen} rw,relatime shared:1 - ext4 /dev/md1 rw\n")

    def file(self, name="scratch/invoice.pdf"):
        """One file with both of its names; the one the application sees is returned."""
        (self.kept / name).write_bytes(b"%PDF-1.7 invoice")
        os.link(self.kept / name, self.seen / name)
        return self.seen / name

    def behind_the_mount(self):
        return mock.patch.object(agent_browsers, "outside_private_mounts",
                                 functools.partial(agent_browsers.outside_private_mounts, table=self.table))


@unittest.skipUnless(sys.platform == "linux", "mount namespaces are a Linux feature")
class BehindAPrivateMountTest(Folders):
    def test_a_file_below_the_mount_goes_to_the_browser_by_its_path_outside_it(self):
        seen = self.file()
        with self.behind_the_mount():
            self.assertEqual(upload_arguments(["upload", "e3", str(seen)], self.work),
                             ["upload", "e3", str(self.kept / "scratch/invoice.pdf")])

    def test_every_file_of_one_upload_is_translated(self):
        first, second = self.file(), self.file("scratch/invoice.png")
        outside = self.root / "elsewhere.txt"
        outside.write_text("x")
        with self.behind_the_mount():
            self.assertEqual(upload_arguments(["upload", "#files", str(first), str(outside), str(second)], self.work),
                             ["upload", "#files", str(self.kept / "scratch/invoice.pdf"), str(outside),
                              str(self.kept / "scratch/invoice.png")])

    def test_a_folder_reached_through_a_link_is_followed_and_the_files_name_is_kept(self):
        self.file()
        (self.work / "payments").symlink_to(self.seen / "scratch")
        with self.behind_the_mount():
            self.assertEqual(upload_arguments(["upload", "e3", str(self.work / "payments/invoice.pdf")], self.work),
                             ["upload", "e3", str(self.kept / "scratch/invoice.pdf")])

    def test_another_file_of_the_same_name_outside_is_not_taken_for_it(self):
        # The path is only replaced by one that names the same file. A copy is a different file.
        (self.kept / "scratch/copy.pdf").write_bytes(b"old")
        (self.seen / "scratch/copy.pdf").write_bytes(b"new")
        with self.behind_the_mount():
            self.assertEqual(upload_arguments(["upload", "e3", str(self.seen / "scratch/copy.pdf")], self.work),
                             ["upload", "e3", str(self.seen / "scratch/copy.pdf")])

    def test_the_tool_is_run_with_the_path_outside_the_mount(self):
        seen = self.file()
        with self.behind_the_mount(), mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["tool"], 0, "Done", "")
            result = browser.run(self.work, ["upload", "e3", str(seen)], target=TARGET, capture=True)
        self.assertEqual(result, (0, "Done", ""))
        self.assertEqual(run.call_args.args[0][-3:], ["upload", "@e3", str(self.kept / "scratch/invoice.pdf")])


class AnyInstallationTest(Folders):
    def test_a_file_outside_every_private_mount_keeps_its_path(self):
        plain = self.root / "plain.pdf"
        plain.write_bytes(b"x")
        self.assertEqual(upload_arguments(["upload", "e3", str(plain)], self.work), ["upload", "e3", str(plain)])

    def test_a_relative_path_is_read_from_the_tools_working_folder(self):
        (self.work / "shot.png").write_bytes(b"x")
        self.assertEqual(upload_arguments(["upload", "e3", "shot.png"], self.work),
                         ["upload", "e3", str(self.work / "shot.png")])

    def test_a_file_that_is_not_there_is_refused_by_the_path_that_was_sent(self):
        missing = str(self.root / "nowhere/invoice.pdf")
        for sent in (missing, "invoice.pdf"):
            with self.subTest(sent=sent), self.assertRaises(BrowserError) as raised:
                upload_arguments(["upload", "e3", sent], self.work)
            self.assertIn(f"no file at {sent}.", str(raised.exception))
            self.assertIn("Nothing was sent to the page", str(raised.exception))

    def test_one_missing_file_refuses_the_whole_upload(self):
        plain = self.root / "plain.pdf"
        plain.write_bytes(b"x")
        with self.assertRaises(BrowserError):
            upload_arguments(["upload", "e3", str(plain), str(self.root / "missing.pdf")], self.work)

    def test_a_missing_file_never_reaches_the_tool_or_the_tab(self):
        with mock.patch.object(subprocess, "run") as run, mock.patch.object(idle_tabs, "before_command") as before:
            with self.assertRaises(BrowserError):
                browser.run(self.work, ["upload", "e3", str(self.root / "missing.pdf")], target=TARGET, capture=True)
        run.assert_not_called()
        before.assert_not_called()

    def test_other_commands_are_not_read_as_file_paths(self):
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(["tool"], 0, "", "")
            browser.run(self.work, ["fill", "e3", "/no/such/file"], target=TARGET, capture=True)
        self.assertEqual(run.call_args.args[0][-3:], ["fill", "@e3", "/no/such/file"])


class WordingTest(unittest.TestCase):
    def test_upload_takes_the_field_and_then_the_files(self):
        self.assertEqual(agent_browser.translate(["upload", "e3", "/a.pdf"]), ["upload", "@e3", "/a.pdf"])
        self.assertEqual(agent_browser.translate(["upload", "#files", "/a.pdf", "/b.png"]),
                         ["upload", "#files", "/a.pdf", "/b.png"])

    def test_upload_without_a_file_is_refused_before_anything_runs(self):
        for args in (["upload"], ["upload", "e3"]):
            with self.subTest(args=args), self.assertRaises(BrowserError) as raised:
                validate_arguments(args)
            self.assertIn("upload <ref> <absolute-path>", str(raised.exception))

    def test_an_option_is_still_refused(self):
        with self.assertRaises(BrowserError) as raised:
            validate_arguments(["upload", "e3", "/a.pdf", "--session", "other"])
        self.assertIn("overrides are disabled", str(raised.exception))
        with self.assertRaises(BrowserError):
            validate_arguments(["upload", "e3", "-a.pdf"])


if __name__ == "__main__":
    unittest.main()
