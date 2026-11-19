"""A browser command's reply lists only the files that command wrote; earlier files stay readable by their handle.

The folder of an agent's own browser never empties, so listing all of it put the same 64 entries (about 20,000 characters)
on every reply, even for a click that prints "Done"."""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from qorqut.agents.roles import GLOBAL_WORKER_ROLE, ORDUBASHY_ROLE
from qorqut.bootstrap import browser
from qorqut.browser import agent_browsers, setup
from qorqut.browser.access import BrowserAccess
from qorqut.browser.handles import _compact

ORDUBASHY = "0a6f3c52-9d1e-4b7a-8c35-2f4e6d8b1a90"
ATLAS = "8fdca155-2f3b-4735-800f-41624c6d26cc"
ASSIGNMENT = "5d2e7c1a-6b30-4f9e-8a41-0c7d9e3b2f18"
TURN = "e41b0a37-52c8-4d6f-9b13-7a8c0d5e2f64"
FOLDER = "work/browser/qorqut-test"       # where the runner keeps one session's files, under the handle's folder


def actor(principal_id, *, assignment=None):
    return SimpleNamespace(principal=SimpleNamespace(id=principal_id), registration_id="registration", credential_id="credential",
                           delegation_assignment_id=assignment, delegation_generation=1 if assignment else None,
                           delegation_binding_id=None)


def write(path, text):
    """Write a file and move its change time a second past the earlier one, so a rewrite never hides in one clock tick."""
    path.parent.mkdir(parents=True, exist_ok=True)
    earlier = path.stat().st_mtime_ns if path.exists() else time.time_ns()
    path.write_text(text)
    os.utime(path, ns=(earlier + 1_000_000_000,) * 2)


class Folder(unittest.TestCase):
    """A BrowserAccess whose command runner is a stand-in that does what the real one does to the session folder."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE browser_handles (id TEXT, turn_id TEXT, binding_json TEXT, arguments_json TEXT, superseded INT)")
        connection.execute("CREATE TABLE browser_turns (id TEXT, state TEXT, lease_until TEXT)")
        connection.execute("INSERT INTO browser_turns VALUES (?, 'released', NULL)", (TURN,))
        self.addCleanup(connection.close)

        @contextmanager
        def unit(**_arguments):
            yield SimpleNamespace(connection=connection)
        runtime = SimpleNamespace(database=SimpleNamespace(read=unit, transaction=unit),
                                  paths=SimpleNamespace(runtime=self.root, instance_root=self.root))
        self.access = BrowserAccess(runtime)
        self.effects = []                   # one callable per command still to run; it gets the session folder

        def target(browser_key, _owner):
            return {"cdp_endpoint": "http://127.0.0.1:20400", "generation": "configured", "instance": browser_key,
                    "runtime_dir": "/runtime", "browser_key": browser_key}

        @contextmanager
        def use(_paths, _agent_id):
            yield

        def run(workspace, command, *, target, instance, capture, pinned=False):
            directory = workspace / FOLDER
            directory.mkdir(parents=True, exist_ok=True)
            # Every real command opens its lock and rewrites its tool config, whatever else it does.
            (directory / "command.lock").touch()
            write(directory / "agent-browser.json", "{}\n")
            if self.effects:
                self.effects.pop(0)(directory)
            return 0, "done", ""
        for owner, name, value in ((self.access, "_worker_target", target), (agent_browsers, "command_use", use),
                                   (browser, "run", run), (browser, "validate_arguments", lambda _command: None),
                                   (browser, "runtime_executable", lambda directory: directory)):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def handle_folder(self, handle):
        return self.root / "browser-turns" / ATLAS / handle / FOLDER

    @staticmethod
    def listed(reply):
        return [item["artifact_path"] for item in reply.get("artifacts", [])]


class OwnBrowserArtifactsTest(Folder):
    def setUp(self):
        super().setUp()
        self.handle = self.access.tool(actor(ATLAS), "acquire", {"action": "acquire", "protocol": "handles"})["handle"]

    def command(self, *effects):
        self.effects.extend(effects)
        return self.access.tool(actor(ATLAS), "command", {"action": "command", "handle": self.handle, "command": ["click", "e1"]})

    def test_a_command_that_writes_nothing_returns_no_artifacts_key(self):
        write(self.handle_folder(self.handle) / "older.txt", "from long ago")
        reply = self.command()
        self.assertNotIn("artifacts", reply)
        self.assertEqual((reply["exit_code"], reply["stdout"]), (0, "done"))

    def test_a_command_that_writes_a_file_returns_exactly_that_file(self):
        write(self.handle_folder(self.handle) / "older.txt", "from long ago")
        write(self.handle_folder(self.handle) / "nested" / "also-older.txt", "x")
        reply = self.command(lambda directory: write(directory / "shot.png", "12345"))
        self.assertEqual(reply["artifacts"], [{
            "artifact_handle": f"browser-artifact:{ATLAS}:{self.handle}:{FOLDER}/shot.png",
            "artifact_path": FOLDER + "/shot.png", "mime_type": "image/png", "size": 5}])

    def test_a_file_the_command_changed_is_listed_and_an_untouched_one_is_not(self):
        self.command(lambda directory: write(directory / "page.txt", "one"))
        self.assertEqual(self.listed(self.command(lambda directory: write(directory / "page.txt", "three"))), [FOLDER + "/page.txt"])
        # Same size, later change time: still a change.
        self.assertEqual(self.listed(self.command(lambda directory: write(directory / "page.txt", "fives"))), [FOLDER + "/page.txt"])
        self.assertNotIn("artifacts", self.command())

    def test_bookkeeping_files_are_never_listed(self):
        def bookkeeping(directory):
            write(directory / "observation-0123456789abcdef.meta.json", "{}")
            write(directory / "nested" / "other.meta.json", "{}")
            write(directory / "nested" / "command.lock", "")
            write(directory / "nested" / "agent-browser.json", "{}")
        self.assertNotIn("artifacts", self.command(bookkeeping))
        reply = self.command(lambda directory: (bookkeeping(directory), write(directory / "observation-0123456789abcdef.yml", "- a")))
        self.assertEqual(self.listed(reply), [FOLDER + "/observation-0123456789abcdef.yml"])

    def test_a_file_from_an_earlier_command_is_still_readable_by_its_handle(self):
        first = self.command(lambda directory: write(directory / "older.txt", "kept text"))
        self.assertNotIn("artifacts", self.command())
        read = self.access.tool(actor(ATLAS), "artifact", {"action": "artifact", "handle": self.handle,
                                                           "artifact_handle": first["artifacts"][0]["artifact_handle"]})
        self.assertEqual((read["content"], read["size"]), ("kept text", 9))

    def test_a_reply_lists_at_most_64_files(self):
        reply = self.command(lambda directory: [write(directory / f"f{number:03}.txt", "x") for number in range(70)])
        self.assertEqual(self.listed(reply), [f"{FOLDER}/f{number:03}.txt" for number in range(64)])


class SelectedWorkerArtifactsTest(Folder):
    """Ordubashy operating a worker's own browser is lease-free too; the files stay under Ordubashy's handle."""

    def test_the_listing_is_the_same_for_a_selected_workers_browser(self):
        workers = [{"agent_id": ORDUBASHY, "name": "Ordubashy", "role": ORDUBASHY_ROLE},
                   {"agent_id": ATLAS, "name": "Atlas", "role": GLOBAL_WORKER_ROLE}]
        for owner, name, value in ((self.access, "authorize_ordubashy", lambda _caller: None),
                                   (self.access.targets, "registry", lambda: None),
                                   (setup, "targets", lambda _runtime: ("owner", workers))):
            patcher = mock.patch.object(owner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        handle = self.access.tool(actor(ORDUBASHY), "acquire", {"action": "acquire", "protocol": "handles", "browser_id": ATLAS})["handle"]

        def command(*effects):
            self.effects.extend(effects)
            return self.access.tool(actor(ORDUBASHY), "command", {"action": "command", "handle": handle, "command": ["click", "e1"]})
        (self.root / "browser-turns" / ORDUBASHY / handle / FOLDER).mkdir(parents=True)
        write(self.root / "browser-turns" / ORDUBASHY / handle / FOLDER / "older.txt", "from long ago")
        self.assertNotIn("artifacts", command())
        reply = command(lambda directory: write(directory / "shot.png", "12345"))
        self.assertEqual(self.listed(reply), [FOLDER + "/shot.png"])
        self.assertEqual(reply["artifacts"][0]["artifact_handle"], f"browser-artifact:{ORDUBASHY}:{handle}:{FOLDER}/shot.png")


class LeasedArtifactsTest(Folder):
    """A delegated assignment's command, which keeps its durable record, lists files the same way."""

    def setUp(self):
        super().setUp()

        class Turns:
            clock = staticmethod(lambda: 0)
            _timestamp = staticmethod(lambda _value: 0)

            def begin_action(self, _uow, _turn_id, *, action_id, **_arguments):
                return {"action_id": action_id, "state": "running"}, True

            def settle_action(self, _uow, _turn_id, *, action_id, state, **_arguments):
                return {"action_id": action_id, "state": state}

            def _compact(self, _uow, row):
                return {"id": row["id"], "state": row["state"]}
        self.access.turns = Turns()

    def command(self, *effects):
        self.effects.extend(effects)
        return self.access._leased_command(actor(ATLAS, assignment=ASSIGNMENT), {"command": ["click", "e1"], "action_id": f"a{time.time_ns()}"},
                                           turn_id=TURN, lease_id="lease", requester=ASSIGNMENT, generation=1,
                                           browser_key="configured:" + ATLAS, owner_id=ASSIGNMENT)

    def folder(self):
        return self.root / "browser-turns" / ASSIGNMENT / TURN / FOLDER

    def test_a_command_that_writes_nothing_leaves_no_artifacts_in_the_compact_reply(self):
        write(self.folder() / "older.txt", "from long ago")
        reply = self.command()
        self.assertEqual(reply["artifacts"], [])
        self.assertNotIn("artifacts", _compact(reply, "b0123456789ab", operation="command"))

    def test_a_command_that_writes_a_file_returns_exactly_that_file(self):
        write(self.folder() / "older.txt", "from long ago")
        reply = self.command(lambda directory: write(directory / "shot.png", "12345"))
        self.assertEqual(reply["artifacts"], [{
            "artifact_handle": f"browser-artifact:{ASSIGNMENT}:{TURN}:{FOLDER}/shot.png",
            "artifact_path": FOLDER + "/shot.png", "mime_type": "image/png", "size": 5}])

    def test_a_changed_file_is_listed_bookkeeping_is_not_and_an_old_file_stays_readable(self):
        first = self.command(lambda directory: (write(directory / "old.txt", "kept text"), write(directory / "page.txt", "one")))
        self.assertEqual(self.listed(first), [FOLDER + "/old.txt", FOLDER + "/page.txt"])
        changed = self.command(lambda directory: (write(directory / "page.txt", "three"),
                                                  write(directory / "observation-0123456789abcdef.meta.json", "{}")))
        self.assertEqual(self.listed(changed), [FOLDER + "/page.txt"])
        self.assertEqual(self.command()["artifacts"], [])
        read = self.access._read_artifact(ASSIGNMENT, TURN, {"artifact_handle": first["artifacts"][0]["artifact_handle"]})
        self.assertEqual(read["content"], "kept text")


if __name__ == "__main__":
    unittest.main()
