"""D62 acceptance with real tmux and the real console process: ordinary agents are local folders with their own recorded shells.

Disposable installation only: no owner session, profile, credential or database, and no Codex/Claude is started.
"""

import json
import os
from pathlib import Path
import sqlite3
import unittest
from uuid import UUID

from websockets.exceptions import InvalidStatus

from qorqut_core.agents.workspace import SHELL_READY_STATUS

from transports.test_terminal import OwnedProcesses, TerminalInstallation, input_frame, received, take, wait_for


class LocalAgents(OwnedProcesses, unittest.TestCase):
    def create(self, f, key, name):
        result = f.post("agents_create", {"display_name": name, "idempotency_key": key}).json()
        self.assertTrue(result["ok"], result)
        return result["data"]["agent"]["id"]

    def agent(self, f, agent_id):
        result = f.post("agents_get", {"agent_id": agent_id}).json()
        self.assertTrue(result["ok"], result)
        return result["data"]

    def viewer(self, f, agent_id):
        """A transient `observation_stale` refusal on a loaded host is honest; retry within a bound."""
        def attempt():
            try:
                return f.websocket(agent=agent_id)
            except InvalidStatus:
                return None
        return wait_for(attempt, 30)

    def type_line(self, f, agent_id, line):
        with self.viewer(f, agent_id) as ws:
            hello = received(ws, "hello")
            control = take(ws, hello["terminal"]["control"]["epoch"])
            self.assertEqual(input_frame(ws, line.encode() + b"\r", control["epoch"], 1)["outcome"], "submitted")

    @staticmethod
    def descendants_of(pid):
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        return children + [d for child in children for d in LocalAgents.descendants_of(int(child))]

    def native_clis(self, root):
        """Any process of this disposable installation that looks like a provider CLI."""
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                argv = (entry / "cmdline").read_bytes().split(b"\0")
                cwd = os.readlink(entry / "cwd")
            except OSError:
                continue
            if cwd.startswith(str(root)) and Path(argv[0].decode(errors="replace")).name in {"codex", "claude"}:
                found.append((entry.name, argv))
        return found

    def test_two_ordinary_agents_get_distinct_workspaces_homes_and_recorded_tmux_shells(self):
        f = TerminalInstallation()
        self.addCleanup(f.close)
        f.start()
        onbash = f.agent_id
        onbash_view = f.available()
        pointer = f.post("agents_list").json()["data"]["onbash_agent_id"]
        self.assertEqual(pointer, onbash)

        first, second = self.create(f, "local-a", "Local A"), self.create(f, "local-b", "Local B")
        self.assertNotEqual(first, second)
        # Creation is idempotent: the same request is the same agent and prepares nothing twice.
        self.assertEqual(self.create(f, "local-a", "Local A"), first)
        views = {onbash: onbash_view, first: f.available(first, 60), second: f.available(second, 60)}
        self.assertEqual(len({v["terminal_id"] for v in views.values()}), 3)
        wait_for(lambda: all(self.agent(f, a)["lifecycle"] == "active" for a in (first, second)), 60)
        for agent_id in (first, second):
            agent = self.agent(f, agent_id)
            self.assertEqual((agent["kind"], agent["lifecycle"], agent["start_requested"], agent["lifecycle_revision"]),
                             ("host", "active", False, 2))
            self.assertEqual(agent["status"]["text"], SHELL_READY_STATUS)
            self.assertIsNone(agent["current_run"])
            # Ready is a recorded shell only: no CLI owner is connected.
            self.assertFalse(f.post("runtime_get", {"agent_id": agent_id}).json()["data"]["input_ready"])
        listing = f.post("agents_list").json()["data"]
        self.assertEqual(listing["onbash_agent_id"], onbash)
        self.assertEqual({a["id"]: a["kind"] for a in listing["items"]}, {onbash: "host", first: "host", second: "host"})

        shells = {a: f.shell(a) for a in (onbash, first, second)}
        runtime = {a: f.local.runtime_root / UUID(a).hex for a in shells}
        self.assertEqual(len({s.shell.pid for s in shells.values()}), 3)
        self.assertEqual(len({s.tmux.server.pid for s in shells.values()}), 3)
        self.assertEqual(len({str(runtime[a] / "tmux.sock") for a in shells}), 3)
        for agent_id in shells:
            self.assertTrue((runtime[agent_id] / "tmux.sock").exists())
            self.assertTrue(f.database_of(agent_id).is_file())
            self.assertTrue(f.workspace_of(agent_id).is_dir())
        self.assertEqual(len({f.database_of(a) for a in shells}), 3)

        # Harmless commands through each agent's own Terminal: cwd, private HOME and tmux target differ.
        for agent_id, mark in ((first, "MARK_FIRST_9a1"), (second, "MARK_SECOND_4c7")):
            self.type_line(f, agent_id, f"printf '%s|%s|%s\\n' \"$PWD\" \"$HOME\" \"$TMUX\" > probe.txt; printf '{mark}\\n' > mark.txt; pwd")
        for agent_id, mark in ((first, "MARK_FIRST_9a1"), (second, "MARK_SECOND_4c7")):
            workspace = f.workspace_of(agent_id)
            wait_for(lambda: (workspace / "mark.txt").exists() and (workspace / "probe.txt").read_text().endswith("\n"), 20)
            cwd, home, tmux = (workspace / "probe.txt").read_text().strip().split("|")
            self.assertEqual(Path(cwd).resolve(), workspace.resolve())
            self.assertEqual(Path(home), workspace.parent / "runtime/home")
            self.assertTrue(tmux.startswith(str(runtime[agent_id] / "tmux.sock") + ","), tmux)
            self.assertEqual((workspace / "mark.txt").read_text(), mark + "\n")
        other = {first: second, second: first}
        for agent_id, mark in ((first, b"MARK_FIRST_9a1"), (second, b"MARK_SECOND_4c7")):
            wait_for(lambda: mark in f.captured(agent_id), 20)
            self.assertNotIn(b"MARK_" + (b"SECOND_4c7" if mark.endswith(b"9a1") else b"FIRST_9a1"), f.captured(agent_id))
            self.assertNotEqual((f.workspace_of(other[agent_id]) / "mark.txt").read_text(), mark.decode() + "\n")
        self.assertFalse((f.workspace_of(onbash) / "mark.txt").exists())

        # No provider CLI was started, and the shells are idle: nothing under any of them.
        for shell in shells.values():
            self.assertEqual(self.descendants_of(shell.shell.pid), [])
        self.assertEqual(self.native_clis(f.settings.data_root), [])

        # Reopening the core keeps every terminal identity and starts no second shell.
        identities = {a: (s.terminal_id, s.tmux, s.recorder, s.shell) for a, s in shells.items()}
        f.stop()
        f.start()
        for agent_id in shells:
            f.available(agent_id, 60)
        self.assertEqual({a: (s.terminal_id, s.tmux, s.recorder, s.shell) for a, s in
                          ((a, f.shell(a)) for a in shells)}, identities)
        self.assertEqual(f.post("agents_list").json()["data"]["onbash_agent_id"], onbash)
        self.assertEqual(self.create(f, "local-a", "Local A"), first)
        self.assertEqual(len(f.post("agents_list").json()["data"]["items"]), 3)
        self.type_line(f, first, "printf 'AFTER_REOPEN\\n' > after.txt")
        wait_for(lambda: (f.workspace_of(first) / "after.txt").exists(), 20)
        self.assertFalse((f.workspace_of(second) / "after.txt").exists())
        # A damaged terminal store for one agent makes only that agent unavailable; the others keep working
        # and the recorded shell is neither restarted nor replaced when the damage is removed.
        alias = f.database_of(first).with_name("alias.sqlite3")
        os.link(f.database_of(first), alias)
        broken = wait_for(lambda: (v if (v := f.terminal(first))["status"] == "unavailable" else None), 20)
        self.assertEqual(broken["reason"], "target_changed")
        self.type_line(f, second, "printf 'STILL_WORKS\\n' > still.txt")
        wait_for(lambda: (f.workspace_of(second) / "still.txt").exists(), 20)
        for agent_id in (second, onbash):
            f.available(agent_id, 20)
        alias.unlink()
        self.assertEqual(f.available(first, 20)["terminal_id"], str(shells[first].terminal_id))
        self.assertEqual(f.shell(first).shell, shells[first].shell)
        self.assertEqual(self.native_clis(f.settings.data_root), [])
        with sqlite3.connect((f.settings.data_root / "state.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
            lifecycle = db.execute("SELECT payload_json FROM events WHERE kind='agent.lifecycle_changed' AND agent_id=?",
                                   (first,)).fetchall()
        self.assertEqual([json.loads(row[0])["reason"] for row in lifecycle], ["recorded_shell_verified"])
