"""Registration, setup, status and directory behavior at the owning SQL boundary."""

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from qorqut_core.app import Application, register_actor
from qorqut_core.config import ICON_KEYS
from qorqut_core.contracts import Actor, CoreError, Invocation
from qorqut_core.storage.database import Database, HostLock
from qorqut_core.storage.migrations import MIGRATIONS
from test_foundation import Faults, InjectedCrash
from agents.support import AgentCase


# Last revision whose creation defaulted to start=true. Its source writes the historical receipts, never a hand-made row.
LEGACY_BASE = "862d989edf0fef0bc6c5100a051e936cb5afbcde"
LEGACY_CREATE = """
import json, sys
import qorqut_core
from qorqut_core.app import Application
from qorqut_core.config import InstallationSettings, NewAgentDefaults
from qorqut_core.contracts import Actor, Invocation

settings, defaults, owner = (cls.model_validate_json(arg) for cls, arg in
    zip((InstallationSettings, NewAgentDefaults, Actor), sys.argv[1:]))
app = Application.initialize(settings, defaults, owner)
try:
    context, cli = Invocation(actor_id=owner.actor_id), defaults.cli.model_dump(mode="json")
    requests = {"explicit": {"idempotency_key": "old-explicit", "display_name": "Old explicit", "cli": cli, "start": True},
                "omitted": {"idempotency_key": "old-omitted", "display_name": "Old omitted", "cli": cli}}
    results = {name: app.invoke("agents_create", context, args).model_dump(mode="json") for name, args in requests.items()}
finally:
    app.close()
print(json.dumps({"module": qorqut_core.__file__, "requests": requests, "results": results}))
"""


def table_rows(app, table):
    with app.db.read() as tx:
        return [dict(row) for row in tx.connection.execute(f"SELECT * FROM {table}")]


class RegistryTests(AgentCase):
    def test_atomic_create_concurrent_replay_conflict_and_actor_attribution(self):
        args = {"idempotency_key": "one", "display_name": "\u2003Research\n", "persona": "private persona"}
        before = {table: len(self.rows(table)) for table in ("agents", "actors", "events", "jobs", "operations")}
        for point in ("agent_after_registration", "agent_after_events", "operation_before_result", "operation_after_result", "transaction_before_commit"):
            with self.subTest(point=point):
                self.fault.point = point
                with self.assertRaises(InjectedCrash):
                    self.app.invoke("agents_create", self.context, args)
                self.assertEqual({table: len(self.rows(table)) for table in before}, before)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.app.invoke("agents_create", self.context, args), range(8)))
        self.assertTrue(all(r.ok and r == results[0] for r in results), results)
        first = results[0]
        identity = first.data["agent"]
        self.assertEqual(first.outcome, "accepted")
        self.assertEqual(identity["display_name"], "Research")
        self.assertEqual(identity["kind"], "vm")
        self.assertFalse(identity["start_requested"])
        self.assertEqual(identity["cli"], self.defaults.cli.model_dump(mode="json"))
        self.assertEqual(identity["created_by"], str(self.owner.actor_id))
        self.assertEqual(len({identity[k] for k in ("id", "actor_id", "conversation_id")}), 3)
        for field in ("id", "actor_id", "conversation_id"):
            self.assertEqual(str(UUID(identity[field])), identity[field])
        self.assertEqual({table: len(self.rows(table)) - before[table] for table in before},
                         {"agents": 1, "actors": 1, "events": 2, "jobs": 1, "operations": 1})
        self.assertNotIn("private persona", json.dumps(first.model_dump(mode="json")))
        created_event = json.loads(self.events(identity["id"], "agent.created")[0]["payload_json"])
        self.assertEqual(created_event["persona_sha256"], hashlib.sha256(b"private persona").hexdigest())
        self.assertEqual(created_event["persona_length"], 15)
        self.assertNotIn("private persona", json.dumps(created_event))
        self.assertEqual(self.app.invoke("agents_create", self.context, {**args, "display_name": "Other"}).error.code, "idempotency_conflict")
        self.assertEqual(self.app.invoke("agents_create", self.context, {**args, "display_name": "Research"}), first)
        peer = self.app.invoke("agents_create", Invocation(actor_id=identity["actor_id"]), args)
        self.assertTrue(peer.ok, peer)
        self.assertNotEqual(peer.data["agent"]["id"], identity["id"])
        self.assertEqual(peer.data["agent"]["created_by"], identity["actor_id"])
        self.assertTrue(all(e["actor_id"] == identity["actor_id"] for e in self.events(peer.data["agent"]["id"])))
        # A fresh start=true is refused by the handler before any registration write or receipt.
        settled = {table: self.rows(table) for table in ("agents", "actors", "events", "jobs", "operations")}
        for context in (self.context, Invocation(actor_id=identity["actor_id"])):
            for legacy in ({}, {"cli": self.defaults.cli.model_dump(mode="json")}):
                refused = self.app.invoke("agents_create", context, {**args, **legacy, "idempotency_key": "refused", "start": True})
                self.assertEqual(refused.error.code, "invalid_argument")
                self.assertIn("Terminal", refused.error.message)
        self.assertEqual({table: self.rows(table) for table in settled}, settled)
        self.assertTrue(self.app.invoke("agents_create", self.context, {**args, "idempotency_key": "refused"}).ok)
        self.restart()
        self.assertEqual(self.app.invoke("agents_create", self.context, args), first)
        self.assertEqual(self.get(identity["id"]), identity)
        with ThreadPoolExecutor(max_workers=2) as pool:
            separate = list(pool.map(lambda key: self.app.invoke("agents_create", self.context,
                {**args, "idempotency_key": key}), ["independent-a", "independent-b"]))
        self.assertTrue(all(r.ok for r in separate), separate)
        self.assertEqual(len({r.data["agent"]["id"] for r in separate}), 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            competing = list(pool.map(lambda name: self.app.invoke("agents_create", self.context,
                {**args, "display_name": name, "idempotency_key": "competing"}), ["A", "B"]))
        self.assertEqual(sum(r.ok for r in competing), 1)
        self.assertEqual([r.error.code for r in competing if not r.ok], ["idempotency_conflict"])
        self.assertFalse((self.settings.data_root / identity["workspace_path"]).exists())

    def test_response_loss_replays_identity_and_resolved_defaults_stay_copied(self):
        self.fault.point = "transaction_after_commit"
        args = {"display_name": "First", "idempotency_key": "lost"}
        with self.assertRaises(InjectedCrash):
            self.app.invoke("agents_create", self.context, args)
        first = self.app.invoke("agents_create", self.context, args)
        changed = self.update_defaults()
        changed["defaults"]["resources"]["cpu"] = 7
        self.assertTrue(self.app.invoke("config_update", self.context, changed).ok)
        host = next(row for row in self.rows("agents") if row["kind"] == "host")
        self.app.close()
        self.app = Application.initialize(self.settings, self.defaults, self.owner, clock=self.clock, fault=self.fault)
        self.assertEqual(self.get(host["id"])["resources"], self.defaults.resources.model_dump(mode="json"))
        self.assertEqual(len([row for row in self.rows("agents") if row["kind"] == "host"]), 1)
        second = self.create("second", start=False)
        self.assertEqual(second.data["agent"]["resources"]["cpu"], 7)
        self.assertFalse(second.data["agent"]["start_requested"])
        # Absent legacy cli metadata snapshots the defaults current in the registration transaction.
        self.assertEqual(second.data["agent"]["cli"], {"provider": "codex", "model": "different"})
        self.assertEqual(first.data["agent"]["cli"], self.defaults.cli.model_dump(mode="json"))
        self.assertEqual(self.get(first.data["agent"]["id"])["resources"], self.defaults.resources.model_dump(mode="json"))
        self.assertEqual(self.app.invoke("agents_create", self.context, args), first)
        explicit = self.create("explicit", resources={"cpu": 3, "memory_mib": 1, "disk_gib": 1}, persona="")
        self.assertEqual(explicit.data["agent"]["resources"]["cpu"], 3)
        legacy = self.create("legacy-cli", cli={"provider": "claude", "model": "legacy-metadata"})
        self.assertEqual(legacy.data["agent"]["cli"], {"provider": "claude", "model": "legacy-metadata"})
        # Omitted, false and onbash registrations all store false and none queue anything but provisioning.
        self.assertEqual({row["start_requested"] for row in self.rows("agents")}, {0})
        for created in (first, second, explicit, legacy):
            payload = json.loads(self.events(created.data["agent"]["id"], "agent.created")[0]["payload_json"])
            self.assertIs(payload["start_requested"], False)
        self.assertEqual({row["kind"] for row in self.rows("jobs")}, {"agent_provision"})
        self.assertEqual(next(r for r in self.rows("agents") if r["id"] == explicit.data["agent"]["id"])["persona_seed"], "")

    def test_presentation_catalog_validation_revision_noop_and_stable_identity(self):
        first = self.create()
        original = first.data["agent"]
        agent_id = original["id"]
        for icon in ICON_KEYS:
            self.clock.advance(1)
            before_events, before_jobs = len(self.events(agent_id)), len(self.rows("jobs"))
            args = {"agent_id": agent_id, "expected_revision": self.get(agent_id)["revision"],
                    "icon_key": icon, "display_name": "\tResearch\u2003", "idempotency_key": "icon-" + icon}
            result = self.app.invoke("agents_update", self.context, args)
            self.assertTrue(result.ok, result)
            changed = icon != original["icon_key"]
            self.assertEqual(result.outcome, "accepted" if changed else "completed")
            self.assertEqual(len(self.events(agent_id)) - before_events, int(changed))
            self.assertEqual(len(self.rows("jobs")) - before_jobs, int(changed))
            self.assertEqual(result.data["revision"], original["revision"] + int(changed))
            if not changed:
                self.assertEqual(result.data["updated_at"], original["updated_at"])
            for key in ("id", "actor_id", "conversation_id", "workspace_path", "cli", "resources", "lifecycle_revision", "start_requested", "status"):
                self.assertEqual(result.data[key], original[key])
            original = result.data
        stale = {"agent_id": agent_id, "expected_revision": 1, "display_name": "Research", "idempotency_key": "stale"}
        self.assertEqual(self.app.invoke("agents_update", self.context, stale).error.code, "revision_conflict")
        duplicate = self.create("duplicate", display_name="Research")
        self.assertNotEqual(duplicate.data["agent"]["id"], agent_id)
        for field, bad in (("display_name", " \u2003"), ("display_name", "a" * 121), ("display_name", "bad\ud800"),
                           ("icon_key", "unknown"), ("persona", "bad\ud800"), ("persona", "é" * 32769),
                           ("start", 1), ("start", "false"), ("start", None), ("kind", "host"), ("actor_id", str(uuid4())),
                           ("workspace_path", "/tmp"), ("cli", {"provider": "codex"}), ("resources", {"cpu": 1})):
            with self.subTest(field=field, bad=repr(bad)[:40]):
                args = {"idempotency_key": "invalid", "display_name": "Name", field: bad}
                self.assertEqual(self.app.invoke("agents_create", self.context, args).error.code, "invalid_argument")
        for bad in ({}, {"display_name": None}, {"icon_key": None}, {"icon_key": "bad"}, {"display_name": " "}):
            args = {"agent_id": agent_id, "expected_revision": original["revision"], "idempotency_key": "bad-update", **bad}
            self.assertEqual(self.app.invoke("agents_update", self.context, args).error.code, "invalid_argument")
        self.assertTrue(self.create("max-persona", persona="é" * 32768).ok)
        self.assertTrue(self.create("nul-name", display_name="\0valid UTF-8").ok)

    def test_status_boundaries_review_and_metadata_are_independent(self):
        first = self.create()
        agent_id, initial = first.data["agent"]["id"], first.data["agent"]
        other = Actor(actor_id=uuid4(), kind="agent", agent_id=uuid4())
        with self.app.db.transaction() as tx:
            register_actor(tx, other)
        revision = 1
        for index, text in enumerate(("\u2003Hello\n\tworld\u00a0", " ".join(["word"] * 100), "é" * 4096, "One")):
            self.clock.advance(1)
            args = {"agent_id": agent_id, "expected_revision": revision, "text": text, "idempotency_key": str(index)}
            result = self.app.invoke("agents_status_set", Invocation(actor_id=other.actor_id), args)
            self.assertTrue(result.ok, result)
            revision += 1
            self.assertEqual(result.data["status"]["revision"], revision)
            self.assertEqual(result.data["status"]["text"], " ".join(text.split()))
            self.assertEqual(result.data["status"]["actor_id"], str(other.actor_id))
            self.assertEqual(result.data["revision"], initial["revision"])
            self.assertEqual(result.data["updated_at"], initial["updated_at"])
        before = self.get(agent_id)
        events, jobs = len(self.events(agent_id)), len(self.rows("jobs"))
        self.clock.advance(1)
        confirmation = {"agent_id": agent_id, "expected_revision": revision, "text": "\nOne\t", "idempotency_key": "review"}
        reviewed = self.app.invoke("agents_status_set", self.context, confirmation)
        self.assertTrue(reviewed.ok)
        self.assertEqual(reviewed.outcome, "completed")
        self.assertEqual({k: v for k, v in reviewed.data["status"].items() if k != "reviewed_at"},
                         {k: v for k, v in before["status"].items() if k != "reviewed_at"})
        self.assertNotEqual(reviewed.data["status"]["reviewed_at"], before["status"]["reviewed_at"])
        self.clock.advance(1)
        self.assertEqual(self.app.invoke("agents_status_set", self.context, confirmation), reviewed)
        self.assertEqual(self.get(agent_id), reviewed.data)
        self.assertEqual((len(self.events(agent_id)), len(self.rows("jobs"))), (events, jobs))
        for text in ("", "\u2003\n", " ".join(["a"] * 101), "é" * 4097, "\ud800"):
            args = {**confirmation, "text": text, "idempotency_key": "invalid"}
            self.assertEqual(self.app.invoke("agents_status_set", self.context, args).error.code, "invalid_argument")
        # Current revision is checked before status word/character semantics.
        self.assertEqual(self.app.invoke("agents_status_set", self.context,
            {**confirmation, "expected_revision": 1, "text": "", "idempotency_key": "stale"}).error.code, "revision_conflict")
        for value in (True, "1", 1.0, 0, 2**63):
            self.assertEqual(self.app.invoke("agents_status_set", self.context,
                {**confirmation, "expected_revision": value, "idempotency_key": "bad-revision"}).error.code, "invalid_argument")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda text: self.app.invoke("agents_status_set", self.context,
                {**confirmation, "text": text, "idempotency_key": text}), ["race-a", "race-b"]))
        self.assertEqual(sum(r.ok for r in results), 1)
        self.assertEqual([r.error.code for r in results if not r.ok], ["revision_conflict"])
        snapshot = self.get(agent_id)
        self.restart()
        self.assertEqual(self.get(agent_id), snapshot)
        all_events, cursor = [], {}
        while True:
            page = self.app.invoke("events_list", self.context, {**cursor, "limit": 2}).data
            all_events += page["items"]
            if not page["has_more"]:
                break
            cursor = {"after_seq": page["next_after_seq"], "recovery_epoch": page["recovery_epoch"]}
        history = [e for e in all_events if e["agent_id"] == agent_id and e["kind"] == "agent.status_changed"]
        self.assertEqual([e["payload"]["status_revision"] for e in history], list(range(1, revision + 2)))
        self.assertIsNone(history[0]["payload"]["old_text"])
        self.assertEqual([e["actor_id"] for e in history[1:5]], [str(other.actor_id)] * 4)

    def test_directory_keyset_recovery_and_unknown_observations(self):
        created = [self.create(str(i), display_name="Same") for i in range(5)]
        items, cursor = [], {}
        while True:
            page = self.app.invoke("agents_list", self.context, {**cursor, "limit": 2}).data
            items += page["items"]
            cursor = {"after_id": page["next_after_id"], "recovery_epoch": page["recovery_epoch"]}
            if not page["has_more"]:
                break
        self.assertEqual(len(items), 6)
        self.assertEqual([a["id"] for a in items], sorted(a["id"] for a in items))
        empty = self.app.invoke("agents_list", self.context, cursor).data
        self.assertEqual(empty["items"], [])
        self.assertFalse(empty["has_more"])
        self.assertEqual(empty["next_after_id"], cursor["after_id"])
        for agent in items:
            self.assertEqual(agent["activity"], "unknown")
            self.assertEqual(agent["capture_health"], "unknown")
            self.assertFalse(agent["needs_user_input"])
            for key in ("activity_observed_at", "last_contact_at", "current_run"):
                self.assertIsNone(agent[key])
        for args in ({"after_id": cursor["after_id"]}, {"limit": True}, {"limit": 201}, {"after_id": "not-a-uuid"}):
            self.assertEqual(self.app.invoke("agents_list", self.context, args).error.code, "invalid_argument")
        self.recovery()
        self.assertEqual(self.app.invoke("agents_list", self.context, cursor).error.code, "target_changed")
        self.assertEqual(self.app.invoke("agents_list", self.context, {"recovery_epoch": cursor["recovery_epoch"]}).error.code, "target_changed")
        self.assertEqual(self.app.invoke("agents_get", self.context, {"agent_id": str(uuid4())}).error.code, "not_found")

    def test_onbash_setup_failure_retry_rename_and_database_uniqueness(self):
        for index, point in enumerate(("onbash_before_registration", "agent_after_registration", "onbash_after_registration")):
            with self.subTest(point=point):
                settings = self.settings.model_copy(update={"data_root": self.root / f"setup-{index}", "installation_id": uuid4()})
                fault = Faults()
                fault.point = point
                with self.assertRaises(InjectedCrash):
                    Application.initialize(settings, self.defaults, self.owner, fault=fault)
                if point != "onbash_after_registration":
                    with self.assertRaises(CoreError) as refused:
                        Application.open(settings)
                    self.assertEqual(refused.exception.code, "not_ready")
                app = Application.initialize(settings, self.defaults, self.owner)
                try:
                    page = app.invoke("agents_list", self.context).data
                    host = page["items"][0]
                    self.assertEqual(page["onbash_agent_id"], host["id"])
                    self.assertEqual((host["kind"], host["display_name"], host["icon_key"], host["start_requested"]), ("host", "onbash", "terminal", False))
                    self.assertFalse((settings.data_root / "agents").exists())
                    app.invoke("agents_update", self.context, {"agent_id": host["id"], "expected_revision": 1,
                        "display_name": "Renamed", "idempotency_key": "rename"})
                    saved_id = host["id"]
                finally:
                    app.close()
                app = Application.initialize(settings, self.defaults, self.owner)
                try:
                    page = app.invoke("agents_list", self.context).data
                    self.assertEqual((page["onbash_agent_id"], len(page["items"])), (saved_id, 1))
                    self.assertEqual(page["items"][0]["display_name"], "Renamed")
                    with app.db.read() as tx:
                        self.assertEqual(tx.connection.execute("SELECT count(*) FROM operations WHERE name='installation_ensure_onbash'").fetchone()[0], 1)
                        self.assertEqual(tx.connection.execute("SELECT count(*) FROM jobs WHERE kind='agent_provision'").fetchone()[0], 1)
                finally:
                    app.close()
        vm = self.create().data["agent"]["id"]
        host = next(row for row in self.rows("agents") if row["kind"] == "host")
        for sql, args in (("UPDATE installation SET onbash_agent_id=?", (vm,)),
                          ("UPDATE installation SET onbash_agent_id=NULL", ()),
                          ("UPDATE agents SET kind='host' WHERE id=?", (vm,)),
                          ("DELETE FROM agents WHERE id=?", (host["id"],))):
            with self.assertRaises(CoreError):
                with self.app.db.transaction() as tx:
                    tx.connection.execute(sql, args)
        with self.assertRaises(CoreError):
            with self.app.db.transaction() as tx:
                columns = list(host)
                other = dict(host, id=str(uuid4()), actor_id=str(uuid4()), conversation_id=str(uuid4()),
                    creation_operation_id=str(uuid4()), provisioning_job_id=str(uuid4()))
                register_actor(tx, Actor(actor_id=other["actor_id"], kind="agent", agent_id=other["id"]))
                tx.connection.execute(f"INSERT INTO agents ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [other[k] for k in columns])
        self.assertNotIn("installation_ensure_onbash", self.app.registry)
        # Deliberate corruption of a disposable fixture proves open refuses an
        # inconsistent pointer instead of replaying setup or adopting by name.
        self.app.close()
        with sqlite3.connect(self.app.db.path) as conn:
            conn.execute("DROP TRIGGER installation_onbash_binding")
            conn.execute("UPDATE installation SET onbash_agent_id=?", (str(uuid4()),))
        with self.assertRaises(CoreError) as inconsistent:
            Application.open(self.settings)
        self.assertEqual(inconsistent.exception.code, "target_changed")

    def test_v2_explicit_migration_open_never_repairs_and_synthetic_actors_survive(self):
        settings = self.settings.model_copy(update={"data_root": self.root / "v2", "installation_id": uuid4()})
        settings.data_root.mkdir()
        (settings.data_root / "installation.json").write_text(settings.model_dump_json())
        lock = HostLock(settings.data_root / "locks" / "host.lock").acquire()
        db = Database(settings.data_root / "state.sqlite3", lock)
        db.migrate(migrations=MIGRATIONS[:2])
        actor = Actor(actor_id=uuid4(), kind="agent", agent_id=uuid4())
        with sqlite3.connect(db.path) as conn:
            conn.execute("INSERT INTO installation VALUES(1,?,?,?)", (str(settings.installation_id), str(uuid4()), self.clock()))
            conn.execute("INSERT INTO defaults VALUES(1,1,'codex','prior',1,512,4)")
            conn.execute("INSERT INTO actors VALUES(?,?,?,?)", (str(self.owner.actor_id), "owner", None, self.clock()))
            conn.execute("INSERT INTO actors VALUES(?,?,?,?)", (str(actor.actor_id), "agent", str(actor.agent_id), self.clock()))
            checksums = conn.execute("SELECT version,checksum FROM schema_migrations").fetchall()
        lock.close()
        with self.assertRaises(CoreError) as old_schema:
            Application.open(settings)
        self.assertEqual(old_schema.exception.code, "unsupported_schema")
        lock.acquire()
        db.migrate()
        lock.close()
        with self.assertRaises(CoreError) as missing_host:
            Application.open(settings)
        self.assertEqual(missing_host.exception.code, "not_ready")
        with sqlite3.connect(db.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM agents").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT version,checksum FROM schema_migrations WHERE version<=2").fetchall(), checksums)
        app = Application.initialize(settings, self.defaults, self.owner)
        try:
            self.assertTrue(app.invoke("agents_list", Invocation(actor_id=actor.actor_id)).ok)
            page = app.invoke("agents_list", self.context).data
            self.assertEqual(len(page["items"]), 1)
            self.assertEqual(page["items"][0]["cli"]["model"], "prior")
            with app.db.read() as tx:
                self.assertEqual(tx.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            app.close()

    def test_old_code_start_true_receipts_replay_unchanged_after_reopen_and_never_launch(self):
        repo = Path(__file__).resolve().parents[2]
        archive = subprocess.run(["git", "-C", str(repo), "archive", LEGACY_BASE, "src"], capture_output=True)
        if archive.returncode:
            self.skipTest("The pinned pre-D36 revision is unavailable in this checkout")
        legacy_source = self.root / "legacy-source"
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(legacy_source, filter="data")
        settings = self.settings.model_copy(update={"data_root": self.root / "legacy", "installation_id": uuid4()})
        produced = subprocess.run([sys.executable, "-c", LEGACY_CREATE, settings.model_dump_json(),
            self.defaults.model_dump_json(), self.owner.model_dump_json()], capture_output=True, text=True,
            env={"PATH": os.defpath, "PYTHONPATH": str(legacy_source / "src"), "PYTHONNOUSERSITE": "1"})
        self.assertEqual(produced.returncode, 0, produced.stderr)
        old = json.loads(produced.stdout)
        self.assertTrue(Path(old["module"]).is_relative_to(legacy_source))
        self.assertTrue(all(r["ok"] and r["data"]["agent"]["start_requested"] for r in old["results"].values()))
        watched = ("agents", "actors", "events", "jobs", "operations")

        def replay(app):
            before = {table: table_rows(app, table) for table in watched}
            saved = {r["idempotency_key"]: json.loads(r["result_json"]) for r in before["operations"] if r["name"] == "agents_create"}
            self.assertEqual(saved, {r["idempotency_key"]: old["results"][name] for name, r in old["requests"].items()})
            self.assertEqual(app.invoke("agents_create", self.context, old["requests"]["explicit"]).model_dump(mode="json"),
                             old["results"]["explicit"])
            # Omitted start was committed as the then-default true; the strict-false default now hashes differently.
            self.assertEqual(app.invoke("agents_create", self.context, old["requests"]["omitted"]).error.code, "idempotency_conflict")
            normalized = {**old["requests"]["omitted"], "start": True}
            self.assertEqual(app.invoke("agents_create", self.context, normalized).model_dump(mode="json"), old["results"]["omitted"])
            self.assertEqual({table: table_rows(app, table) for table in watched}, before)
            return before["operations"]

        app = Application.open(settings)
        try:
            receipts = replay(app)
            with patch("subprocess.Popen", side_effect=AssertionError("provider/process launch")), \
                 patch("socket.socket", side_effect=AssertionError("network launch")):
                while (job_id := app.jobs.run_next(worker_id=uuid4())) is not None:
                    self.assertEqual(app.jobs.get(job_id)["wait_reason"], "dependency_unavailable")
            self.assertEqual(table_rows(app, "operations"), receipts)
        finally:
            app.close()
        app = Application.open(settings)
        try:
            self.assertEqual(replay(app), receipts)
            for result in old["results"].values():
                legacy = app.invoke("agents_get", self.context, {"agent_id": result["data"]["agent"]["id"]}).data
                self.assertTrue(legacy["start_requested"])
                self.assertIsNotNone(legacy["workspace_initialized_at"])
        finally:
            app.close()
