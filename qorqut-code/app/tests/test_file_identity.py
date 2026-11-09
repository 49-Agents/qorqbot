"""Saved file identities survive a restart that renumbers volumes, and still catch replaced objects.

A restart is simulated by adding a constant to every live ``st_dev``: macOS renumbers APFS
volumes at boot, and Linux/WSL2 can attach disks in a different order, without changing any
inode, mode or owner.
"""
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qorqut import file_identity
from qorqut.agentic import layout_files
from qorqut.native_archive import CaptureBinding, CaptureError, TranscriptArchive

SHIFT = 7


class _Shifted:
    def __init__(self, result):
        self._result = result

    def __getattr__(self, name):
        value = getattr(self._result, name)
        return value + SHIFT if name == "st_dev" else value


@contextlib.contextmanager
def restarted():
    """Every live stat reports a different device number, as after a restart."""
    real = {name: getattr(os, name) for name in ("stat", "fstat", "lstat")}
    patches = [mock.patch.object(os, name, lambda *a, _f=fn, **k: _Shifted(_f(*a, **k))) for name, fn in real.items()]
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield


def live(path):
    info = os.stat(path, follow_symlinks=False)
    return {"device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode & 0o7777}


class Helpers(unittest.TestCase):
    saved = {"device": 1, "inode": 10, "mode": 0o700, "uid": 501}

    def test_records_match_ignores_only_the_device(self):
        self.assertTrue(file_identity.records_match(self.saved, {**self.saved, "device": 2}))
        for key, value in (("inode", 11), ("mode", 0o755), ("uid", 0)):
            with self.subTest(key=key):
                self.assertFalse(file_identity.records_match(self.saved, {**self.saved, key: value}))

    def test_records_match_requires_the_same_fields(self):
        self.assertFalse(file_identity.records_match(self.saved, {k: v for k, v in self.saved.items() if k != "uid"}))
        self.assertFalse(file_identity.records_match(None, self.saved))
        self.assertFalse(file_identity.records_match(self.saved, ["device", "inode"]))

    def test_matches_requires_the_live_object_on_its_parents_live_volume(self):
        current = {**self.saved, "device": 5}
        self.assertTrue(file_identity.matches(self.saved, current, 5))
        self.assertFalse(file_identity.matches(self.saved, current, 6))  # a mount or another volume
        self.assertFalse(file_identity.matches(self.saved, {**current, "inode": 11}, 5))

    def test_parent_volumes_are_read_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = Path(tmp, "child")
            child.mkdir()
            self.assertEqual(file_identity.parent_volume(child), os.stat(tmp).st_dev)
            fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.assertEqual(file_identity.fd_parent_volume(fd), os.stat(tmp).st_dev)
            finally:
                os.close(fd)

    def test_tree_matches_checks_each_directory_against_its_parent(self):
        saved = {"a": {**self.saved}, "a/b": {**self.saved, "inode": 11}}
        moved = {path: {**value, "device": 9} for path, value in saved.items()}
        self.assertTrue(file_identity.tree_matches(saved, moved, 9))
        self.assertFalse(file_identity.tree_matches(saved, moved, 8))
        self.assertFalse(file_identity.tree_matches(saved, {**moved, "a/b": {**moved["a/b"], "device": 3}}, 9))
        self.assertFalse(file_identity.tree_matches(saved, {"a": moved["a"]}, 9))
        self.assertFalse(file_identity.tree_matches(saved, {**moved, "a/c": moved["a/b"]}, 9))
        self.assertFalse(file_identity.tree_matches({"x/y": saved["a"]}, {"x/y": moved["a"]}, 9))


class AgenticDirectories(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "agentic")
        (self.root / "packages/a/b").mkdir(parents=True)
        (self.root / "roles").mkdir()
        self.rootfd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.saved_root = layout_files._identity(os.fstat(self.rootfd))
        self.saved_dirs = layout_files._directories(self.rootfd)

    def tearDown(self):
        os.close(self.rootfd)
        self.tmp.cleanup()

    def test_restart_keeps_the_saved_root_and_directories_valid(self):
        with restarted():
            live_dirs = layout_files._directories(self.rootfd)
            self.assertNotEqual(live_dirs, self.saved_dirs)  # the old exact comparison failed here
            self.assertTrue(file_identity.tree_matches(self.saved_dirs, live_dirs, os.fstat(self.rootfd).st_dev))
            self.assertTrue(layout_files._root_matches(self.rootfd, self.root, self.saved_root))
            os.close(layout_files._open_dir(self.rootfd, "packages/a/b", self.saved_dirs))

    def test_a_replaced_directory_is_still_rejected_after_a_restart(self):
        # Keep the old directory alive so the replacement cannot reuse its inode.
        os.rename(self.root / "packages/a/b", self.root / "packages/a/old")
        (self.root / "packages/a/b").mkdir()
        with restarted():
            live_dirs = layout_files._directories(self.rootfd)
            self.assertFalse(file_identity.tree_matches(self.saved_dirs, live_dirs, os.fstat(self.rootfd).st_dev))
            with self.assertRaisesRegex(Exception, "Authored directory identity changed"):
                layout_files._open_dir(self.rootfd, "packages/a/b", self.saved_dirs)

    def test_a_replaced_root_is_still_rejected(self):
        os.rename(self.root, Path(self.tmp.name, "old"))
        self.root.mkdir()
        with restarted():
            self.assertFalse(layout_files._root_matches(self.rootfd, self.root, self.saved_root))


class TranscriptLineage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(os.path.realpath(self.tmp.name))  # the archive refuses symlinked ancestors such as /var on macOS
        self.workspace = str(base / "work")
        self.source = base / "session.jsonl"
        self.archive = TranscriptArchive(str(base / "archive"))
        self.binding = CaptureBinding(agent_id="agent-1", provider="claude", conversation_id="s1", generation=1,
                                      workspace=self.workspace, source_path=str(self.source))
        self.append("first")

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, text, path=None):
        line = {"type": "user", "sessionId": "s1", "cwd": self.workspace, "message": {"content": text}}
        with open(path or self.source, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(line) + "\n")

    def test_capture_continues_the_same_lineage_after_a_restart(self):
        first = self.archive.capture(self.binding)
        self.append("second")
        with restarted():
            second = self.archive.capture(self.binding)
        self.assertGreater(second.length, first.length)
        self.assertEqual(second.source_inode, first.source_inode)
        self.assertNotEqual(second.source_device, first.source_device)
        # A later capture without the shift re-verifies a chain whose receipts hold two device numbers.
        self.append("third")
        third = self.archive.capture(self.binding)
        self.assertGreater(third.length, second.length)

    def test_a_replaced_source_is_still_rejected_after_a_restart(self):
        self.archive.capture(self.binding)
        replacement = self.source.with_name("replacement.jsonl")
        replacement.write_bytes(self.source.read_bytes())
        keep = self.source.with_name("kept.jsonl")
        os.link(self.source, keep)  # keep the old inode alive so it cannot be reused
        os.replace(replacement, self.source)
        with restarted():
            with self.assertRaises(CaptureError) as caught:
                self.archive.capture(self.binding)
        self.assertEqual(caught.exception.code, "source_replaced")


if __name__ == "__main__":
    unittest.main()
