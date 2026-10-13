"""Schema ten to eleven: historical 'vm' agents become local 'host' agents, nothing else moves (D62)."""

import hashlib
import json
import re
import sqlite3
from unittest.mock import patch
from uuid import UUID, uuid4

from messaging.support import ChatCase
from qorqut_core.agents.registry import Agents
from qorqut_core.app import Application
from qorqut_core.contracts import CoreError
from qorqut_core.storage.database import Database, HostLock
from qorqut_core.storage.migrations import MIGRATIONS, SCHEMA_VERSION
from test_foundation import InjectedCrash

# Checksums already recorded by installed schema-10 databases; migration 11 must not rewrite them.
BASELINE = {
    1: "64faf705fa367df07470bb80d59c2363d97d11e8b1958661d86bcfd700479663",
    2: "5e91778f3e01521f5a19d7d272ee5a15d0ef9432125a34abe9aab6811356db4c",
    3: "18ca408f82d5c231eceb76357c9fd98bd4bc7a3ab5f43b2719550c31209cb0d5",
    4: "68b55717b82af97d735125b64125b71ee6c6ca94a7d02bf313d0b859c2804dca",
    5: "6ab896e40cd32f03f1bbd4fac83faa7dffc1c7eb98408c3c0b48a7fb48e37a37",
    6: "a23bbef5262bab650b4306653da57a07632949f49dd8fead716a413a4c56c14a",
    7: "0035c0273f2d18f41e617eefb20817ec150f920705be5e9daf5cb2a22948b589",
    8: "73297626852b91971383e2d12121e427f4c18f1886493a01762f2088c3c75f60",
    9: "b49ec9c900b0b5993e26e7e63d2def2619c04c619cd27292b8334c1955487663",
    10: "47629e202b3b2c9b919ca118848497f1c7e0aea8c9e73d8ef153a60696a5c812",
}
AGENT_TRIGGERS = {"agents_actor_binding", "agents_permanent_identity", "agents_no_delete"}


def historical_vm(registry, tx, args, context, operation_id):
    """Registration as the pre-D62 code did it: an ordinary agent was a 'vm'."""
    defaults = registry.app._view(tx).defaults
    return registry._register(tx, context, operation_id, display_name=args.display_name, icon_key=args.icon_key,
        cli=defaults.cli, resources=defaults.resources, persona=args.persona, kind="vm")


class Recording(sqlite3.Connection):
    """Records the order of statements, commits and closes on the migration connection."""
    log = None

    def execute(self, sql, *rest):
        if self.log is not None:
            self.log.append(" ".join(sql.split())[:60])
        return super().execute(sql, *rest)

    def commit(self):
        if self.log is not None:
            self.log.append("COMMIT")
        return super().commit()

    def rollback(self):
        if self.log is not None:
            self.log.append("ROLLBACK")
        return super().rollback()

    def close(self):
        if self.log is not None:
            self.log.append("CLOSE")
        return super().close()


class Eleven(ChatCase):
    def tables(self, tx):
        names = [r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {name: [dict(r) for r in tx.connection.execute(f"SELECT * FROM {name}")] for name in names}

    @staticmethod
    def schema(tx):
        return {(r["type"], r["name"], r["tbl_name"]): r["sql"] for r in tx.connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}

    def tree(self, agent):
        root = self.settings.data_root / "agents" / agent["id"]
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file()}

    def legacy(self, name="schema-ten"):
        """A populated schema-10 installation: onbash plus two historical 'vm' agents with owner-edited files."""
        self.app.close()
        self.settings = self.settings.model_copy(update={"data_root": self.root / name})
        migrate = Database.migrate
        patches = (patch("qorqut_core.storage.database.SCHEMA_VERSION", 10),
                   patch.object(Database, "migrate", lambda db: migrate(db, migrations=MIGRATIONS[:10])),
                   patch.object(Agents, "create", historical_vm))
        for item in patches:
            item.start()
        try:
            self.app = Application.initialize(self.settings, self.defaults, self.owner, clock=self.clock, fault=self.fault)
            ids = [self.create(key, display_name=key).data["agent"]["id"] for key in ("legacy-a", "legacy-b")]
            self.pump()
            by_id = {row["id"]: row for row in self.rows("agents")}
            self.legacy_a, self.legacy_b = by_id[ids[0]], by_id[ids[1]]
            self.onbash = next(row for row in by_id.values() if row["kind"] == "host")
            self.assertEqual({row["kind"] for row in by_id.values()}, {"host", "vm"})
            for agent in (self.legacy_a, self.legacy_b):
                workspace = self.settings.data_root / "agents" / agent["id"] / "workspace"
                (workspace / "persona.md").write_text("owner persona " + agent["display_name"])
                (workspace / "memory.md").write_text("owner memory " + agent["display_name"])
            self.send("old-message", "Immutable text\n", recipient=self.legacy_a)
            self.pump()
            with self.app.db.read() as tx:
                self.before = self.tables(tx)
                self.triggers = {r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
                self.indexes = {r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.files = {row["id"]: self.tree(row) for row in (self.onbash, self.legacy_a, self.legacy_b)}
            self.history = self.conversation(self.legacy_a)["items"]
            self.app.close()
        finally:
            for item in reversed(patches):
                item.stop()

    def offline(self, action):
        """The explicit offline workflow: the exclusive host lock is held by the migrating process."""
        lock = HostLock(self.settings.data_root / "locks" / "host.lock").acquire()
        try:
            db = Database(self.settings.data_root / "state.sqlite3", lock, busy_timeout_ms=self.settings.busy_timeout_ms,
                          clock=self.clock, fault=self.fault)
            return action(db)
        finally:
            lock.close()

    def raw(self, sql, *args):
        with sqlite3.connect(self.settings.data_root / "state.sqlite3") as conn:
            conn.row_factory = sqlite3.Row
            return [tuple(r) for r in conn.execute(sql, args)]

    def test_baseline_checksums_and_rebuild_flag_are_not_part_of_history(self):
        self.assertEqual(SCHEMA_VERSION, 11)
        self.assertEqual({m.version: m.checksum for m in MIGRATIONS[:10]}, BASELINE)
        self.assertEqual([m.version for m in MIGRATIONS if m.rebuild], [11])

    def test_populated_ten_upgrade_preserves_everything_but_kind_and_queues_projection(self):
        self.legacy()
        self.assertEqual(self.raw("PRAGMA user_version"), [(10,)])
        with self.assertRaises(CoreError) as refused:
            Application.open(self.settings, clock=self.clock, fault=self.fault)
        self.assertEqual(refused.exception.code, "unsupported_schema")
        self.assertEqual(self.raw("PRAGMA user_version"), [(10,)])
        self.assertEqual(self.raw("SELECT count(*) FROM sqlite_master WHERE name='agents_v11'"), [(0,)])
        self.offline(lambda db: db.migrate())
        self.app = Application.open(self.settings, clock=self.clock, fault=self.fault)
        self.assertEqual(self.app.db.validate(), 11)
        converted = {self.legacy_a["id"], self.legacy_b["id"]}
        # An upgraded database has exactly the schema objects of a fresh schema-11 install.
        with self.app.db.read() as tx:
            upgraded = self.schema(tx)
        fresh = Application.initialize(self.settings.model_copy(update={"data_root": self.root / "fresh",
            "installation_id": uuid4()}), self.defaults, self.owner, clock=self.clock)
        try:
            with fresh.db.read() as tx:
                self.assertEqual(upgraded, self.schema(tx))
                self.assertEqual([n for n, sql in self.schema(tx).items() if re.search(r"_v1[01]\b", sql or "")], [])
        finally:
            fresh.close()
        with self.app.db.read() as tx:
            after = self.tables(tx)
            triggers = {r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            indexes = {r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(tx.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(tx.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(tx.connection.execute("PRAGMA legacy_alter_table").fetchone()[0], 0)
            definition = tx.connection.execute("SELECT sql FROM sqlite_master WHERE name='agents'").fetchone()[0]
        self.assertNotIn("agents_v11", definition)
        self.assertIn("'active'", definition)
        self.assertTrue(AGENT_TRIGGERS <= triggers)
        self.assertTrue(self.triggers <= triggers)
        self.assertNotIn("agents_one_host", indexes)
        self.assertTrue((self.indexes - {"agents_one_host"}) <= indexes)
        # Only kind changed on converted rows; every other table is byte-for-byte what it was.
        for table, rows in self.before.items():
            if table in ("agents", "jobs", "schema_migrations"):
                continue
            self.assertEqual(after[table], rows, table)
        self.assertEqual([dict(r, kind="vm") if r["id"] in converted else r for r in after["agents"]], self.before["agents"])
        self.assertEqual({r["kind"] for r in after["agents"]}, {"host"})
        self.assertEqual([r for r in after["schema_migrations"] if r["version"] <= 10], self.before["schema_migrations"])
        self.assertEqual([r["version"] for r in after["schema_migrations"]][-1], 11)
        self.assertEqual({r["version"]: r["checksum"] for r in after["schema_migrations"] if r["version"] <= 10}, BASELINE)
        added = [r for r in after["jobs"] if r not in self.before["jobs"]]
        self.assertEqual(len(after["jobs"]), len(self.before["jobs"]) + 2)
        self.assertEqual([r for r in self.before["jobs"] if r in after["jobs"]], self.before["jobs"])
        self.assertEqual({(r["kind"], r["target_id"], r["state"], r["external_effect"]) for r in added},
                         {("agent_project", agent, "queued", 1) for agent in converted})
        for job in added:
            UUID(job["job_id"])
            self.assertEqual(json.loads(job["args_json"])["agent_id"], job["target_id"])
        self.assertEqual(len(self.rows("messages")), len(self.before["messages"]))
        onbash = next(r for r in after["agents"] if r["id"] == self.onbash["id"])
        with self.app.db.read() as tx:
            self.assertEqual(tx.connection.execute("SELECT onbash_agent_id FROM installation").fetchone()[0], onbash["id"])
        # Identity is permanent afterwards, including kind; the pointer and rows stay.
        for sql, args in (("UPDATE agents SET kind='vm' WHERE id=?", (self.legacy_a["id"],)),
                          ("UPDATE agents SET actor_id=? WHERE id=?", (str(uuid4()), self.legacy_a["id"])),
                          ("DELETE FROM agents WHERE id=?", (self.legacy_a["id"],))):
            with self.assertRaises(CoreError):
                with self.app.db.transaction() as tx:
                    tx.connection.execute(sql, args)
        # The generated projection follows through the existing typed job and leaves owner files alone.
        self.assertEqual(self.files[self.legacy_a["id"]]["workspace/persona.md"],
                         hashlib.sha256(b"owner persona legacy-a").hexdigest())
        self.pump()
        for agent in (self.legacy_a, self.legacy_b):
            projected = json.loads((self.settings.data_root / "agents" / agent["id"] / "agent.json").read_text())
            self.assertEqual(projected["kind"], "host")
            now = self.tree(agent)
            for name, digest in self.files[agent["id"]].items():
                if name != "agent.json":
                    self.assertEqual(now[name], digest, name)
        self.assertEqual(self.tree(self.onbash), self.files[self.onbash["id"]])
        self.assertEqual({r["state"] for r in self.rows("jobs") if r["job_id"] in {j["job_id"] for j in added}}, {"completed"})
        self.assertEqual(self.conversation(self.legacy_a)["items"], self.history)

    def test_pragmas_surround_the_transaction_and_are_restored_on_every_exit(self):
        self.legacy()
        real = sqlite3.connect

        def recording(log):
            Logged = type("Logged", (Recording,), {"log": log})
            return lambda *args, **kwargs: real(*args, factory=Logged, **kwargs)

        for failure in ("migration_before_version", None):
            log = []
            with patch("qorqut_core.storage.database.sqlite3.connect", recording(log)):
                self.fault.point = failure
                if failure:
                    with self.assertRaises(InjectedCrash):
                        self.offline(lambda db: db.migrate())
                else:
                    self.offline(lambda db: db.migrate())
            migration = log[log.index("PRAGMA foreign_keys=OFF"):]
            begin = migration.index("BEGIN IMMEDIATE")
            self.assertEqual(migration[:begin], ["PRAGMA foreign_keys=OFF"])
            end = migration.index("ROLLBACK" if failure else "COMMIT")
            inside = migration[begin:end]
            # Legacy rename semantics apply to the rebuild alone, never to earlier migrations.
            self.assertLess(inside.index("PRAGMA legacy_alter_table=ON"), inside.index("PRAGMA legacy_alter_table=OFF"))
            self.assertEqual(inside.count("PRAGMA legacy_alter_table=ON"), 1)
            if not failure:
                self.assertLess(inside.index("PRAGMA legacy_alter_table=OFF"), inside.index("PRAGMA foreign_key_check"))
                self.assertLess(inside.index("PRAGMA foreign_key_check"), inside.index("PRAGMA integrity_check"))
            self.assertEqual(migration[end + 1:], ["PRAGMA legacy_alter_table=OFF", "PRAGMA foreign_keys=ON", "CLOSE"])
            if failure:
                # A fault after the new table exists rolls the whole version back: still schema 10, no leftovers.
                self.assertEqual(self.raw("PRAGMA user_version"), [(10,)])
                self.assertEqual(self.raw("SELECT count(*) FROM sqlite_master WHERE name='agents_v11'"), [(0,)])
                self.assertEqual(self.raw("SELECT DISTINCT kind FROM agents ORDER BY kind"), [("host",), ("vm",)])
                self.assertEqual(self.raw("SELECT count(*) FROM sqlite_master WHERE name='agents_one_host'"), [(1,)])
                self.assertEqual(self.raw("SELECT count(*) FROM jobs"), [(len(self.before["jobs"]),)])
        self.assertEqual(self.raw("PRAGMA user_version"), [(11,)])
        # Re-running at the head touches no pragma and writes nothing.
        log = []
        with patch("qorqut_core.storage.database.sqlite3.connect", recording(log)):
            self.offline(lambda db: db.migrate())
        self.assertNotIn("PRAGMA foreign_keys=OFF", log)

    def test_foreign_key_violation_aborts_the_whole_migration(self):
        self.legacy()
        with sqlite3.connect(self.settings.data_root / "state.sqlite3") as conn:
            conn.execute("DROP TRIGGER installation_onbash_binding")
            conn.execute("UPDATE installation SET onbash_agent_id=?", (str(uuid4()),))
        self.assertEqual(self.raw("PRAGMA foreign_key_check")[0][0], "installation")
        with self.assertRaises(CoreError) as refused:
            self.offline(lambda db: db.migrate())
        self.assertEqual(refused.exception.code, "storage_blocked")
        self.assertEqual(self.raw("PRAGMA user_version"), [(10,)])
        self.assertEqual(self.raw("SELECT DISTINCT kind FROM agents ORDER BY kind"), [("host",), ("vm",)])
        self.assertEqual(self.raw("SELECT count(*) FROM sqlite_master WHERE name='agents_v11'"), [(0,)])
        self.assertEqual(self.raw("SELECT count(*) FROM schema_migrations WHERE version=11"), [(0,)])

    def test_fresh_install_is_schema_eleven_with_many_hosts_and_no_unique_host_index(self):
        self.assertEqual(self.app.db.validate(), 11)
        with self.app.db.read() as tx:
            names = {r[0] for r in tx.connection.execute("SELECT name FROM sqlite_master")}
            self.assertNotIn("agents_one_host", names)
            self.assertNotIn("agents_v11", names)
            self.assertTrue(AGENT_TRIGGERS <= names)
            self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual([r[0] for r in tx.connection.execute("SELECT version FROM schema_migrations")], list(range(1, 12)))
        kinds = {row["kind"] for row in self.rows("agents")}
        self.assertEqual(kinds, {"host"})
        self.assertGreaterEqual(len(self.rows("agents")), 3)
