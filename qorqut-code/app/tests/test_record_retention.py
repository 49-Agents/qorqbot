"""Retention removes exactly the settled bookkeeping its rules name, and only when it is asked to."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qorqut.migration import record_retention as command
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import retention
from qorqut_operations.storage.core_schema import CORE_SCHEMA
from qorqut_operations.storage.database import APPLICATION_ID

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
OLD, RECENT = "2026-09-20T10:00:00.123456Z", "2026-11-01T10:00:00.123456Z"
TABLES = ("CREATE TABLE idempotency_receipts", "CREATE TABLE apply_receipts", "CREATE TABLE resource_claims",
          "CREATE TABLE jobs", "CREATE TABLE routines (", "CREATE TABLE routine_runs", "CREATE TRIGGER idempotency_receipts",
          "CREATE TRIGGER apply_receipts", "CREATE TRIGGER jobs_", "CREATE INDEX jobs_", "CREATE INDEX routine_runs_")


def store(path):
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute("PRAGMA foreign_keys = ON")
    for table in ("principals", "businesses", "signoffs"):
        connection.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY)")
    for statement in CORE_SCHEMA:
        if statement.startswith(TABLES):
            connection.execute(statement)
    # The same kinds of reference the installed domains hold on jobs and routine runs.
    connection.execute("CREATE TABLE filings(id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id))")
    connection.execute("CREATE TABLE occurrences(id TEXT PRIMARY KEY, routine_run_id TEXT NOT NULL UNIQUE "
                       "REFERENCES routine_runs(id), job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id))")
    connection.execute("INSERT INTO principals VALUES ('agent')")
    connection.execute("INSERT INTO signoffs VALUES ('signoff')")
    connection.execute("INSERT INTO routines(namespace,id,name,scope,target,schedule,timezone,status,job_kind,arguments_json,"
                       "timeout_seconds,created_at,updated_at) VALUES ('global','routine','Sync','global','root','* * * * *',"
                       "'UTC','active','email.sync','{}',60,?,?)", (OLD, OLD))
    return connection


class Records:
    def __init__(self, connection):
        self.connection = connection

    def run(self, name, *, at=OLD, state="succeeded", job_state="succeeded", job=True, result=None, dedupe=None,
            names_run=True):
        self.connection.execute(
            "INSERT INTO routine_runs(id,namespace,routine_id,trigger,state,started_at,finished_at,result_json,created_at,"
            "updated_at) VALUES (?, 'global', 'routine', 'schedule', ?, ?, ?, ?, ?, ?)",
            (f"run-{name}", state, at, at, json.dumps({"job_id": f"job-{name}"}), at, at))
        if job:
            self.job(name, at=at, state=job_state, result=result, dedupe=dedupe,
                     definition={"routine_run_id": f"run-{name}" if names_run else "run-other", "routine_id": "routine"})

    def job(self, name, *, at=OLD, state="succeeded", result=None, dedupe=None, definition=None):
        self.connection.execute(
            "INSERT INTO jobs(id,target_namespace,kind,principal_id,arguments_json,definition_ref_json,dedupe_key,state,"
            "result_json,created_at,updated_at) VALUES (?, 'global', 'email.sync', 'agent', '{}', ?, ?, ?, ?, ?, ?)",
            (f"job-{name}", json.dumps(definition or {}), dedupe or f"job-{name}", state,
             json.dumps(result) if result is not None else None, at, at))

    def idempotency(self, key, at=OLD):
        self.connection.execute("INSERT INTO idempotency_receipts VALUES ('agent','global','tasks_update',?,'hash','{}',?)",
                                (key, at))

    def receipt(self, name, *, at=OLD, state="applied", signoff=None, claim=False):
        self.connection.execute(
            "INSERT INTO apply_receipts(id,principal_id,target_namespace,operation_name,payload_hash,change_set,signoff_id,"
            "state,created_at,updated_at) VALUES (?, 'agent', 'global', 'notes_update', 'hash', ?, ?, ?, ?, ?)",
            (name, b"x" * 1000, signoff, state, at, at))
        if claim:
            self.connection.execute("INSERT INTO resource_claims VALUES ('global','note',?,?,?)", (name, name, at))

    def ids(self, table, column="id"):
        return sorted(row[0] for row in self.connection.execute(f"SELECT {column} FROM {table}"))


class RetentionCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "operations.sqlite3"
        self.connection = store(self.path)
        self.addCleanup(self.connection.close)
        self.records = Records(self.connection)

    def apply(self, *rules, days=30):
        return retention.apply(self.connection, now=NOW, days=days, rules=rules)


class RoutineRunTests(RetentionCase):
    def setUp(self):
        super().setUp()
        records = self.records
        records.run("old-a")
        records.run("old-b")
        records.run("recent", at=RECENT)
        records.run("failed", state="failed", job_state="failed")
        records.run("job-running", job_state="running")
        records.run("external", result={"external_receipt": {"id": "sent"}})
        records.run("dispatch", result={"dispatch_intent": {"to": "someone"}})
        records.run("plain-result", result={"external_receipt": None, "summary": "nothing sent"})
        records.run("shared-key", dedupe="once-only")
        records.run("other-run", names_run=False)
        records.run("no-job", job=False)
        records.run("filed")
        records.run("occurred")
        records.job("manual-old")                               # a job no routine run started
        self.connection.execute("INSERT INTO filings VALUES ('filing', 'job-filed')")
        self.connection.execute("INSERT INTO occurrences VALUES ('occurrence', 'run-occurred', 'job-occurred')")

    def test_only_old_succeeded_pairs_that_nothing_references_are_removed(self):
        runs, jobs = self.records.ids("routine_runs"), self.records.ids("jobs")
        report = self.apply("routine-runs")
        self.assertEqual({"removed": 3}, report["rules"]["routine-runs"])
        removed = {"old-a", "old-b", "plain-result"}
        self.assertEqual(sorted(set(runs) - {f"run-{name}" for name in removed}), self.records.ids("routine_runs"))
        self.assertEqual(sorted(set(jobs) - {f"job-{name}" for name in removed}), self.records.ids("jobs"))
        self.assertEqual({"removed": 0}, self.apply("routine-runs")["rules"]["routine-runs"])

    def test_inspect_reports_what_apply_removes_and_changes_nothing(self):
        before = self.connection.total_changes
        report = retention.inspect(self.connection, now=NOW, rules=("routine-runs",))
        self.assertEqual(before, self.connection.total_changes)
        self.assertEqual((3, OLD, OLD), tuple(report["rules"]["routine-runs"][key] for key in ("rows", "oldest", "newest")))
        self.assertEqual("2026-10-11T12:00:00Z", report["before"])
        self.assertGreater(report["rules"]["routine-runs"]["stored_bytes"], 0)

    def test_a_longer_window_keeps_more(self):
        self.assertEqual({"removed": 0}, self.apply("routine-runs", days=60)["rules"]["routine-runs"])
        self.assertEqual({"removed": 4}, self.apply("routine-runs", days=7)["rules"]["routine-runs"])   # now also "recent"

    def test_a_reference_it_does_not_understand_makes_the_rule_refuse(self):
        self.connection.execute("CREATE TABLE strange(namespace TEXT, kind TEXT, key TEXT, "
                                "FOREIGN KEY (namespace, kind, key) REFERENCES jobs(target_namespace, kind, dedupe_key))")
        jobs = self.records.ids("jobs")
        with self.assertRaises(DomainError) as caught:
            self.apply("routine-runs")
        self.assertEqual("invalid_database", caught.exception.code)
        self.assertEqual(jobs, self.records.ids("jobs"))


class ReceiptTests(RetentionCase):
    def setUp(self):
        super().setUp()
        records = self.records
        records.idempotency("old-1")
        records.idempotency("old-2")
        records.idempotency("recent", RECENT)
        records.receipt("applied-old")
        records.receipt("applied-recent", at=RECENT)
        records.receipt("signoff-bound", signoff="signoff")
        records.receipt("claimed", claim=True)
        for state in ("prepared", "applying", "failed", "conflicted"):
            records.receipt(state, state=state)

    def test_idempotency_receipts_older_than_the_window_are_removed(self):
        self.assertEqual({"removed": 2}, self.apply("idempotency-receipts")["rules"]["idempotency-receipts"])
        self.assertEqual(["recent"], self.records.ids("idempotency_receipts", "idempotency_key"))
        self.assertEqual(8, len(self.records.ids("apply_receipts")))        # another rule's records are untouched

    def test_only_applied_unbound_unclaimed_receipts_are_removed(self):
        report = self.apply("apply-receipts")
        self.assertEqual({"removed": 1}, report["rules"]["apply-receipts"])
        self.assertEqual(sorted(["applied-recent", "signoff-bound", "claimed", "prepared", "applying", "failed", "conflicted"]),
                         self.records.ids("apply_receipts"))
        self.assertEqual(3, len(self.records.ids("idempotency_receipts", "idempotency_key")))
        self.assertIn("free_pages", report)

    def test_several_rules_and_small_batches(self):
        for index in range(25):
            self.records.idempotency(f"bulk-{index}")
        report = retention.apply(self.connection, now=NOW, rules=("idempotency-receipts", "apply-receipts"), batch=4)
        self.assertEqual({"idempotency-receipts": {"removed": 27}, "apply-receipts": {"removed": 1}}, report["rules"])
        self.assertFalse(self.connection.in_transaction)

    def test_inspect_covers_every_rule(self):
        report = retention.inspect(self.connection, now=NOW)
        self.assertEqual({"routine-runs": 0, "idempotency-receipts": 2, "apply-receipts": 1},
                         {rule: value["rows"] for rule, value in report["rules"].items()})
        self.assertGreaterEqual(report["rules"]["apply-receipts"]["stored_bytes"], 1000)

    def test_refusals(self):
        for call, code in ((lambda: self.apply(), "validation_error"),
                           (lambda: self.apply("everything"), "validation_error"),
                           (lambda: self.apply("apply-receipts", days=6), "validation_error"),
                           (lambda: retention.apply(self.connection, now=datetime(2026, 11, 10), rules=("apply-receipts",)),
                            "validation_error"),
                           (lambda: retention.apply(self.connection, now=NOW, rules=("apply-receipts",), batch=0),
                            "validation_error")):
            with self.assertRaises(DomainError) as caught:
                call()
            self.assertEqual(code, caught.exception.code)
        self.connection.execute("ALTER TABLE apply_receipts RENAME COLUMN signoff_id TO approval_id")
        with self.assertRaises(DomainError) as caught:
            self.apply("apply-receipts")
        self.assertEqual("invalid_database", caught.exception.code)
        self.assertEqual(8, len(self.records.ids("apply_receipts")))

    def test_vacuum_returns_the_freed_pages(self):
        for index in range(300):
            self.records.receipt(f"bulk-{index}")
        freed = self.apply("apply-receipts")["free_pages"]
        self.assertGreater(freed, 0)
        result = retention.vacuum(self.connection)
        self.assertLess(result["pages_after"], result["pages_before"])
        self.assertEqual(0, self.connection.execute("PRAGMA freelist_count").fetchone()[0])


class CommandTests(RetentionCase):
    def setUp(self):
        super().setUp()
        self.records.idempotency("old")
        self.records.receipt("applied-old")
        self.connection.close()

    def main(self, *arguments):
        paths = SimpleNamespace(database=self.path)
        with patch.object(command, "instance_paths_from_arguments", return_value=paths), \
                patch.object(command, "InstanceOwnership") as ownership, redirect_stdout(io.StringIO()) as output:
            status = command.main([*arguments, "--instance-root", str(self.path.parent)])
        return status, json.loads(output.getvalue()), ownership

    def count(self, table):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        finally:
            connection.close()

    def test_inspect_is_read_only_and_takes_no_ownership(self):
        status, response, ownership = self.main("inspect")
        self.assertEqual((0, True), (status, response["ok"]))
        self.assertEqual({"routine-runs": 0, "idempotency-receipts": 1, "apply-receipts": 1},
                         {rule: value["rows"] for rule, value in response["result"]["rules"].items()})
        ownership.assert_not_called()
        self.assertEqual((1, 1), (self.count("idempotency_receipts"), self.count("apply_receipts")))

    def test_apply_without_a_rule_removes_nothing(self):
        status, response, ownership = self.main("apply")
        self.assertEqual((1, "validation_error"), (status, response["error"]["code"]))
        ownership.assert_not_called()
        self.assertEqual((1, 1), (self.count("idempotency_receipts"), self.count("apply_receipts")))

    def test_apply_removes_the_named_rule_under_ownership(self):
        status, response, ownership = self.main("apply", "--rule", "idempotency-receipts", "--vacuum")
        self.assertEqual((0, {"removed": 1}), (status, response["result"]["rules"]["idempotency-receipts"]))
        self.assertIn("vacuum", response["result"])
        ownership.return_value.acquire.assert_called_once()
        ownership.return_value.acquire.return_value.release.assert_called_once()
        self.assertEqual((0, 1), (self.count("idempotency_receipts"), self.count("apply_receipts")))

    def test_another_database_is_refused(self):
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA application_id = 7")
        connection.close()
        status, response, _ = self.main("apply", "--rule", "apply-receipts")
        self.assertEqual((1, "invalid_database"), (status, response["error"]["code"]))
        self.assertEqual(1, self.count("apply_receipts"))


if __name__ == "__main__":
    unittest.main()
