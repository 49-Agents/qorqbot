"""The installation-wide runtime setting (``managed_runtime/mode.py``) and the tmux check (``terminal_support.py``).

Nothing here starts a service, a host or tmux."""
import isolated_cli_environment  # before any Qorqbot import

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from qorqut import terminal_support
from qorqut.managed_runtime import mode


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.paths = SimpleNamespace(runtime=Path(self.folder.name) / "runtime")
        self.paths.runtime.mkdir()
        self.file = mode.mode_file(self.paths)

    def put(self, value):
        self.file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file.write_text(value if isinstance(value, str) else json.dumps(value))
        os.chmod(self.file, 0o600)

    def test_no_file_means_managed(self):
        self.assertEqual(mode.current(self.paths), mode.MANAGED)
        self.assertTrue(mode.is_managed(self.paths))
        self.assertFalse(mode.is_terminal(self.paths))
        self.assertEqual(mode.mode_of(self.paths, "any-agent"), mode.MANAGED)
        self.assertEqual(mode.document(self.paths), {})
        self.assertFalse(self.file.exists())      # a read creates nothing

    def test_no_paths_means_managed_and_never_switching(self):
        self.assertEqual(mode.current(None), mode.MANAGED)
        self.assertFalse(mode.switching(None))
        self.assertIsNone(mode.recover(None))

    def test_anything_unreadable_means_managed(self):
        for bad in ("", "{", "[]", json.dumps({"mode": "terminal"}), json.dumps({"schema_version": 2, "mode": "terminal"}),
                    json.dumps({"schema_version": 1, "mode": "tmux"}), json.dumps({"schema_version": 1, "mode": "terminal", "x": "y" * 5000})):
            with self.subTest(bad=bad[:30]):
                self.put(bad)
                self.assertEqual(mode.current(self.paths), mode.MANAGED)

    def test_commit_records_the_mode_privately_and_reads_see_it_at_once(self):
        self.assertEqual(mode.current(self.paths), mode.MANAGED)      # cached as absent
        written = mode.commit(self.paths, mode.TERMINAL)
        self.assertEqual(written["mode"], mode.TERMINAL)
        self.assertEqual(written["previous"], mode.MANAGED)
        self.assertEqual(written["last"]["to"], mode.TERMINAL)
        self.assertTrue(written["last"]["ok"])
        self.assertEqual(mode.current(self.paths), mode.TERMINAL)
        self.assertTrue(mode.is_terminal(self.paths))
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.file.parent.stat().st_mode), 0o700)
        mode.commit(self.paths, mode.MANAGED)
        self.assertEqual(mode.current(self.paths), mode.MANAGED)
        self.assertEqual(mode.document(self.paths)["previous"], mode.TERMINAL)

    def test_an_unknown_mode_is_refused(self):
        for call in (lambda: mode.commit(self.paths, "tmux"), lambda: mode.begin(self.paths, "per-agent")):
            with self.assertRaises(ValueError):
                call()
        self.assertFalse(self.file.exists())

    def test_begin_journals_and_closes_the_gate_without_changing_the_mode(self):
        self.assertFalse(mode.switching(self.paths))
        mode.begin(self.paths, mode.TERMINAL, force=True)
        self.assertTrue(mode.switching(self.paths))
        self.assertEqual(mode.current(self.paths), mode.MANAGED)
        pending = mode.document(self.paths)["pending"]
        self.assertEqual((pending["to"], pending["force"], pending["pid"]), (mode.TERMINAL, True, os.getpid()))
        mode.commit(self.paths, mode.TERMINAL)
        self.assertFalse(mode.switching(self.paths))
        self.assertNotIn("pending", mode.document(self.paths))

    def test_abort_keeps_the_mode_opens_the_gate_and_says_why(self):
        mode.begin(self.paths, mode.TERMINAL)
        after = mode.abort(self.paths, message="Atlas is in the middle of a turn. " + "x" * 600)
        self.assertFalse(mode.switching(self.paths))
        self.assertEqual(mode.current(self.paths), mode.MANAGED)
        self.assertNotIn("pending", after)
        self.assertEqual((after["last"]["to"], after["last"]["ok"]), (mode.TERMINAL, False))
        self.assertEqual(len(after["last"]["message"]), mode.MESSAGE_LIMIT)

    def test_the_gate_opens_even_when_the_final_write_fails(self):
        mode.begin(self.paths, mode.TERMINAL)
        os.chmod(self.file.parent, 0o500)
        self.addCleanup(os.chmod, self.file.parent, 0o700)
        with self.assertRaises(OSError):
            mode.commit(self.paths, mode.TERMINAL)
        self.assertFalse(mode.switching(self.paths))

    def test_recover_drops_a_journal_left_by_a_dead_process_and_keeps_the_mode(self):
        self.put({"schema_version": 1, "mode": "terminal", "pending": {"to": "managed", "started": "x", "pid": 1, "force": False}})
        self.assertFalse(mode.switching(self.paths))        # the flag is in memory: another process's journal closes nothing
        dropped = mode.recover(self.paths)
        self.assertEqual(dropped["to"], mode.MANAGED)
        self.assertEqual(mode.current(self.paths), mode.TERMINAL)
        self.assertNotIn("pending", mode.document(self.paths))
        self.assertFalse(mode.document(self.paths)["last"]["ok"])
        self.assertIsNone(mode.recover(self.paths))

    def test_a_symlink_in_place_of_the_file_is_not_written_through(self):
        self.file.parent.mkdir(parents=True, mode=0o700)
        target = Path(self.folder.name) / "elsewhere.json"
        target.write_text("{}")
        self.file.symlink_to(target)
        with self.assertRaises(ValueError):
            mode.commit(self.paths, mode.TERMINAL)
        self.assertEqual(target.read_text(), "{}")

    def test_per_agent_folders_beside_the_file_are_untouched(self):
        self.file.parent.mkdir(mode=0o700)
        agent = self.file.parent / "0b0b0b0b-0000-4000-8000-000000000001"
        agent.mkdir(mode=0o700)
        (agent / "options.json").write_text("{}")
        mode.commit(self.paths, mode.TERMINAL)
        self.assertEqual(sorted(p.name for p in self.file.parent.iterdir()), [agent.name, mode.FILE])
        self.assertEqual((agent / "options.json").read_text(), "{}")


class TmuxTests(unittest.TestCase):
    def ask(self, which, run=None):
        return terminal_support.availability(refresh=True, which=which, run=run or self.fail)

    def test_no_tmux(self):
        found = self.ask(lambda: None)
        self.assertEqual((found["available"], found["reason"], found["path"]), (False, terminal_support.TMUX_MISSING, None))
        self.assertIn("tmux", found["detail"])
        self.assertNotIn("apt", found["detail"])      # the owner is told what is missing, never a command

    def test_tmux_that_runs(self):
        done = subprocess.CompletedProcess(["tmux", "-V"], 0, stdout="tmux 3.4\n", stderr="")
        calls = []
        found = self.ask(lambda: "/usr/bin/tmux", lambda argv, **kw: calls.append(argv) or done)
        self.assertEqual((found["available"], found["reason"], found["version"], found["path"]), (True, None, "3.4", "/usr/bin/tmux"))
        self.assertEqual(calls, [["/usr/bin/tmux", "-V"]])      # the version only: no server is contacted

    def test_tmux_that_does_not_start(self):
        def broken(argv, **kw):
            raise OSError("exec format error")
        for run in (broken, lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="no")):
            found = self.ask(lambda: "/opt/tmux", run)
            self.assertEqual((found["available"], found["reason"]), (False, terminal_support.TMUX_BROKEN))

    def test_the_answer_is_cached_briefly(self):
        clock = [100.0]
        asked = []
        which = lambda: asked.append(1) or None
        terminal_support.availability(refresh=True, which=which, clock=lambda: clock[0])
        terminal_support.availability(which=which, clock=lambda: clock[0])
        self.assertEqual(len(asked), 1)
        clock[0] += terminal_support.CACHE_SECONDS + 1
        terminal_support.availability(which=which, clock=lambda: clock[0])
        self.assertEqual(len(asked), 2)


if __name__ == "__main__":
    unittest.main()
