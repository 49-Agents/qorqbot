"""Actual schema-8 source generates the upgrade fixture; old receipts stay exact."""

import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
from unittest.mock import patch
from uuid import UUID, uuid4

from agents.support import AgentCase
from messaging.test_dispatch import SyntheticInput
from qorqut_core.app import Application
from qorqut_core.contracts import CoreError
from test_foundation import InjectedCrash


class Migration(AgentCase):
    def test_schema_eight_receipts_checksums_and_recovery_holds_survive_upgrade(self):
        base = "e74369c0e214570f4a6892650f3ae59e3689787c"
        repo = Path(__file__).resolve().parents[2]
        archived = subprocess.run(["git", "-C", str(repo), "archive", base, "src"], capture_output=True, check=True)
        source = self.root / "schema-eight-source"
        with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
            archive.extractall(source, filter="data")
        self.app.close()
        self.settings = self.settings.model_copy(update={"data_root": self.root / "schema-eight"})
        script = r'''
import json, sys
from uuid import uuid4
from qorqut_core.app import Application, RecoveryActivation
from qorqut_core.config import InstallationSettings, NewAgentDefaults
from qorqut_core.contracts import Actor, Invocation
from qorqut_core.storage.migrations import SCHEMA_VERSION
assert SCHEMA_VERSION == 8
clock = lambda: "2026-10-07T00:00:00.000000Z"
app = Application.initialize(InstallationSettings.model_validate_json(sys.argv[1]),
    NewAgentDefaults.model_validate_json(sys.argv[2]), Actor.model_validate_json(sys.argv[3]), clock=clock)
context = Invocation(actor_id=json.loads(sys.argv[3])["actor_id"])
def pump():
    for _ in range(100):
        if app.jobs.run_next(worker_id=uuid4()) is None:
            return
    raise AssertionError("fixture did not settle")
try:
    pump()
    with app.db.read() as tx:
        host = dict(tx.connection.execute("SELECT * FROM agents WHERE kind='host'").fetchone())
    held_args = {"idempotency_key": "held-before-upgrade", "recipient_agent_id": host["id"], "body": "held exact\x00\x1b\n"}
    held = app.invoke("messages_send", context, held_args)
    assert held.ok
    handled = app.invoke("messages_acknowledge", context, {"idempotency_key": "handled", "message_ids": [held.data["message"]["message_id"]]})
    assert handled.ok
    pump()
    epoch = app.invoke("config_get", context).data["recovery_epoch"]
    assert app.activate_recovery(context, RecoveryActivation(idempotency_key="restore", expected_installation_id=app.settings.installation_id,
        expected_recovery_epoch=epoch)).ok
    current_args = {"idempotency_key": "offline-before-upgrade", "recipient_agent_id": host["id"], "body": "current exact\r\t文"}
    current = app.invoke("messages_send", context, current_args)
    assert current.ok
    pump()
    print(json.dumps({"held": held.model_dump(mode="json"), "held_args": held_args,
        "current": current.model_dump(mode="json"), "current_args": current_args, "handled": handled.model_dump(mode="json")}))
finally:
    app.close()
'''
        result = subprocess.run([sys.executable, "-B", "-c", script, self.settings.model_dump_json(),
            self.defaults.model_dump_json(), self.owner.model_dump_json()], capture_output=True, text=True,
            env={"PATH": os.defpath, "PYTHONPATH": str(source / "src"), "PYTHONNOUSERSITE": "1"}, check=True)
        old_receipts = json.loads(result.stdout)
        # Use the application's configured deterministic database name.
        db = Application._database(self.settings, clock=self.clock, id_factory=uuid4, fault=self.fault)
        with sqlite3.connect(db.path) as connection:
            connection.row_factory = sqlite3.Row
            tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            old = {table: [dict(r) for r in connection.execute(f"SELECT * FROM {table}")] for table in tables}
        connection.close()
        with self.assertRaises(CoreError) as mismatch:
            Application.open(self.settings)
        self.assertEqual(mismatch.exception.code, "unsupported_schema")
        db.lock.acquire()
        try:
            self.fault.point = "migration_before_version"
            with self.assertRaises(InjectedCrash):
                db.migrate()
            with patch("qorqut_core.storage.database.SCHEMA_VERSION", 8), db.read() as tx:
                self.assertIsNone(tx.connection.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_attempts'").fetchone())
            db.migrate()
        finally:
            db.lock.close()
        self.app = Application.open(self.settings, clock=self.clock, fault=self.fault)
        with self.app.db.read() as tx:
            for table, rows in old.items():
                where = " WHERE version<=8" if table == "schema_migrations" else ""
                self.assertEqual([dict(r) for r in tx.connection.execute(f"SELECT * FROM {table}{where}")], rows, table)
            self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(tx.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(tx.connection.execute("PRAGMA user_version").fetchone()[0], 10)
        for kind in ("held", "current"):
            self.assertEqual(self.app.invoke("messages_send", self.context, old_receipts[kind + "_args"]).model_dump(mode="json"),
                             old_receipts[kind])
        self.onbash = next(row for row in self.rows("agents") if row["kind"] == "host")
        runtime = self.app.runtime_input = SyntheticInput(self)
        self.clock.advance(61)
        while self.app.jobs.run_next(worker_id=uuid4()) is not None:
            pass
        current_id = old_receipts["current"]["data"]["message"]["message_id"]
        held_id = old_receipts["held"]["data"]["message"]["message_id"]
        current = self.app.invoke("deliveries_get", self.context, {"message_id": current_id}).data
        held = self.app.invoke("deliveries_get", self.context, {"message_id": held_id}).data
        self.assertEqual(current["delivery"]["state"], "submitted")
        self.assertEqual((held["delivery"]["reason"], held["attempts"]), ("recovery_review", []))
        self.assertEqual([str(mid) for _, mid in runtime.submissions], [current_id])
        held_job = next(row for row in self.rows("jobs") if row["kind"] == "message_delivery"
                        and json.loads(row["args_json"])["message_id"] == held_id)
        self.assertEqual(self.app.jobs.reconcile(UUID(held_job["job_id"])).reason, "recovery_review")
        self.assertEqual(self.app.jobs.reconcile_pending(), 0)
        self.assertEqual(self.rows("message_handling")[0]["message_id"], held_id)
        self.assertEqual((len(self.app.registry), len(self.app.jobs.handlers)), (24, 6))
