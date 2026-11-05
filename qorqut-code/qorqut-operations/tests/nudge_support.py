"""A synthetic host for the inbox nudge engine: stand-in inbox tables and a fake visibility port. No native session exists."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from qorqut_operations import inbox_nudges
from qorqut_operations.inbox_nudges import Binding, InboxNudges, Policy
from qorqut_operations.storage import AssemblyBuilder, Database, StorageContribution, TableSpec
from support import BUSINESS, HUMAN, OTHER, Demo

UTC = timezone.utc
START = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
AGENT_A = "11111111-1111-4111-8111-111111111111"
AGENT_B = "22222222-2222-4222-8222-222222222222"
SHORT_AGENT = "agent-7"  # a registered id of any shape: nothing may assume six characters or a uuid

HOST_SCHEMA = (
    """CREATE TABLE inbox_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, business_id TEXT REFERENCES businesses(id),
        source_type TEXT NOT NULL, source_id TEXT NOT NULL, source_revision INTEGER, actor_id TEXT,
        event_type TEXT NOT NULL, summary TEXT NOT NULL, visibility_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""",
    """CREATE TABLE inbox_deliveries (
        agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id), event_id INTEGER NOT NULL REFERENCES inbox_events(id),
        acknowledged_at TEXT, outcome TEXT, PRIMARY KEY(agent_id, event_id))""",
    """CREATE INDEX inbox_pending ON inbox_deliveries(agent_id, acknowledged_at, event_id)""",
)


def host_contribution():
    return StorageContribution("synthetic-inbox", {
        "inbox_events": TableSpec(("id",), "business_id", False, None),
        "inbox_deliveries": TableSpec(("agent_id", "event_id"), revision_column=None)}, schema=HOST_SCHEMA)


class FakeVisibility:
    """The host's authorization, as a set of (agent, event) pairs revoked or denied, plus a record of every question asked."""

    def __init__(self):
        self.denied: set[tuple[str, int]] = set()
        self.asked: list[tuple[str, tuple[int, ...]]] = []
        self.in_write = []

    def authorized(self, uow, agent_id, events):
        self.asked.append((agent_id, tuple(row["id"] for row in events)))
        self.in_write.append(getattr(uow, "writable", False))
        return {row["id"] for row in events if (agent_id, row["id"]) not in self.denied}


class Clock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def binding(agent_id=AGENT_A, *, generation=1, provider="claude", pid=4242, conversation="conversation-1", transport=None):
    return Binding(f"binding-{agent_id}-{generation}", agent_id, provider, f"{conversation}-g{generation}", generation,
                   transport or ("terminal" if provider == "claude" else "codex_queue"), pid, "123456")


class NudgeHost:
    def __init__(self, tmp_path, *, policy=None):
        demo = Demo()
        builder = demo.builder(extra=(host_contribution(), inbox_nudges.storage_contribution()))
        self.database = Database(Path(tmp_path) / "state.sqlite3", assembly=builder.freeze()).initialize_target()
        self.visibility, self.clock = FakeVisibility(), Clock()
        self.nudges = InboxNudges(self.database, self.visibility, policy=policy or Policy(), clock=self.clock)
        with self.database.transaction() as uow:
            uow.repository("principals").insert({"id": HUMAN, "kind": "human", "display_name": "Owner"})
            for identity, name in ((BUSINESS, "first"), (OTHER, "second")):
                uow.repository("businesses").insert({"id": identity, "slug": name, "name": name,
                    "workspace": str(Path(tmp_path) / name), "hq_path": str(Path(tmp_path) / name / "hq")})
            # Only global agents receive deliveries, so every nudge recipient is a global registration.
            for agent in (AGENT_A, AGENT_B, SHORT_AGENT):
                uow.repository("principals").insert({"id": agent, "kind": "agent"})
                uow.repository("agent_profiles", business_id=None).insert({"agent_id": agent, "principal_id": agent,
                    "namespace": "ecosystem", "name": "n" + agent[:6], "workspace": str(Path(tmp_path) / ("w" + agent)),
                    "profile_json": {"status": "active", "role": {"id": "global-worker"},
                                     "scope": {"kind": "ecosystem", "allowed_business_ids": [], "global_capabilities": []}}})

    def restart(self):
        """A new service object over the same database: nothing in memory survives."""
        self.nudges = InboxNudges(self.database, self.visibility, clock=self.clock)
        return self.nudges

    def event(self, agents, *, summary="secret summary", business=BUSINESS, source="messages", visibility=None):
        with self.database.transaction() as uow:
            cursor = uow.connection.execute(
                "INSERT INTO inbox_events(business_id,source_type,source_id,event_type,summary,visibility_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)", (business, source, "s-1", "created", summary, json.dumps(visibility or {}), "2026-10-12T12:00:00Z"))
            for agent in agents:
                uow.connection.execute("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)", (agent, cursor.lastrowid))
            return cursor.lastrowid

    def bulk(self, agent, count, *, business=BUSINESS, source="messages"):
        """``count`` pending events for one agent in a single unit of work; returns the first and last event id."""
        with self.database.transaction() as uow:
            first = None
            for _ in range(count):
                cursor = uow.connection.execute(
                    "INSERT INTO inbox_events(business_id,source_type,source_id,event_type,summary,visibility_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?)", (business, source, "s-1", "created", "secret summary", "{}", "2026-10-12T12:00:00Z"))
                first = first or cursor.lastrowid
                uow.connection.execute("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)", (agent, cursor.lastrowid))
            return first, cursor.lastrowid

    def need(self, agent=AGENT_A):
        with self.database.read() as uow:
            row = uow.connection.execute("SELECT * FROM inbox_nudge_needs WHERE agent_id=?", (agent,)).fetchone()
            return dict(row) if row else None

    def acknowledge(self, agent, *event_ids):
        with self.database.transaction() as uow:
            for event_id in event_ids:
                uow.connection.execute("UPDATE inbox_deliveries SET acknowledged_at='2026-10-12T12:30:00Z',outcome='handled' "
                                       "WHERE agent_id=? AND event_id=?", (agent, event_id))

    def deliveries(self):
        with self.database.read() as uow:
            return [dict(row) for row in uow.connection.execute("SELECT * FROM inbox_deliveries ORDER BY agent_id,event_id")]

    def attempts(self, agent=AGENT_A):
        with self.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(
                "SELECT * FROM inbox_nudge_attempts WHERE agent_id=? ORDER BY sequence", (agent,))]

    def count(self, table):
        with self.database.read() as uow:
            return uow.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
