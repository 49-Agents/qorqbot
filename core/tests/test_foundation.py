"""Focused temporary-data checks for M00. No provider, VM or owner sessions."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from unittest.mock import patch
from uuid import UUID, uuid4

from pydantic import Field

import qorqut_core
from qorqut_core.app import Application, Change, Operation, RecoveryActivation, register_actor
from qorqut_core.config import ICON_KEYS, InstallationSettings, NewAgentDefaults
from qorqut_core.contracts import Actor, CoreError, Invocation, JobProgress, JobResolution, Model, Mutation, canonical
from qorqut_core.storage.database import Database, HostLock, assert_external_io_allowed
from qorqut_core.storage.events import EventInput, append_event
from qorqut_core.storage.jobs import JobHandler, Jobs, after
from qorqut_core.storage.migrations import MIGRATIONS, Migration


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 7, tzinfo=timezone.utc)

    def __call__(self):
        return self.now.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class InjectedCrash(Exception):
    pass


class Faults:
    point = None

    def __call__(self, point):
        if point == self.point:
            self.point = None
            raise InjectedCrash(point)


class ProbeArgs(Model):
    value: Annotated[int, Field(strict=True, ge=1)]


class ProbeMutation(Mutation):
    value: Annotated[int, Field(strict=True, ge=1)]


class FileMutation(Mutation):
    receipt_id: UUID


class FoundationCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock, self.fault = Clock(), Faults()
        self.settings = InstallationSettings(installation_id=uuid4(), source_root=self.root / "source",
            release_root=self.root / "release", data_root=self.root / "data", listen_address="127.0.0.1",
            listen_port=17501, busy_timeout_ms=70)
        self.defaults = NewAgentDefaults(cli={"provider": "codex", "model": "synthetic"},
                                         resources={"cpu": 1, "memory_mib": 512, "disk_gib": 4})
        self.owner = Actor(actor_id=uuid4(), kind="owner")
        self.context = Invocation(actor_id=self.owner.actor_id)
        self.app = Application.initialize(self.settings, self.defaults, self.owner, clock=self.clock, fault=self.fault)
        # Drive only this synthetic installation's reserved workspace job so
        # unrelated job tests retain their exact target/claim assertions.
        with self.app.db.read() as tx:
            onbash_job = tx.connection.execute("SELECT provisioning_job_id FROM agents WHERE kind='host'").fetchone()[0]
        self.assertEqual(str(self.app.jobs.run_next(worker_id=uuid4())), onbash_job)
        self.assertEqual(self.app.jobs.get(UUID(onbash_job))["wait_reason"], "dependency_unavailable")
        self.bootstrap_counts = self.counts()

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def restart(self):
        self.app.close()
        self.app = Application.open(self.settings, clock=self.clock, fault=self.fault)

    def update(self, key="update", revision=1, model="different"):
        value = self.defaults.model_dump(mode="json")
        value["cli"]["model"] = model
        return {"idempotency_key": key, "expected_revision": revision, "defaults": value}

    def config(self):
        return self.app.invoke("config_get", self.context).data

    def counts(self):
        with self.app.db.read() as tx:
            return tuple(tx.connection.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                         for t in ("operations", "events", "jobs", "file_receipts"))

    def plus_bootstrap(self, *deltas):
        return tuple(before + delta for before, delta in zip(self.bootstrap_counts, deltas))

    def probe(self, *, external=True, execute=None, inspect=None):
        self.effects = []

        def run(claim):
            assert_external_io_allowed()
            self.effects.append(claim.arguments.value)
            return JobResolution(state="completed", progress=JobProgress(phase="completed", completed=1, total=1))

        def examine(job):
            return JobResolution(state="uncertain", progress=JobProgress(phase="inspection_required"),
                                 reason="reconciliation_required", next_attempt_at=self.clock())

        self.app.jobs = Jobs(self.app.db, (JobHandler("test_probe", ProbeArgs, external, execute or run, inspect or examine),))

        def change(tx, args, context, operation_id):
            tx.connection.execute("UPDATE defaults SET disk_gib=? WHERE singleton=1", (args.value,))
            seq = append_event(tx, EventInput(kind="test.probe", actor_id=context.actor_id,
                operation_id=operation_id, payload={"value": args.value}))
            job_id = self.app.jobs.enqueue(tx, "test_probe", ProbeArgs(value=args.value), operation_id=operation_id)
            self.fault("test_after_job")
            return Change({"job_id": str(job_id)}, seq, "accepted")

        operation = Operation("test_probe", ProbeMutation, True, change)
        result = self.app.execute(operation, self.context, {"idempotency_key": "probe", "value": 8})
        return operation, result, UUID(result.data["job_id"])

    def attach_file(self, data=b"new bytes", *, name="objects/item", old=None, mode="replace"):
        target = self.settings.data_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if old is not None:
            target.write_bytes(old)
        expected = hashlib.sha256(old).hexdigest() if old is not None else None
        staged = self.app.files.stage(name, (data[:3], data[3:]), expected_sha256=expected, mode=mode)

        def mutate(tx, args, context, operation_id):
            self.assertEqual(args.receipt_id, staged.receipt_id)
            job_id = self.app.files.attach(tx, staged, operation_id=operation_id)
            seq = append_event(tx, EventInput(kind="test.file_requested", actor_id=context.actor_id,
                operation_id=operation_id, payload={"receipt_id": str(staged.receipt_id)}))
            return Change({"job_id": str(job_id), "receipt_id": str(staged.receipt_id)}, seq, "accepted")

        operation = Operation("test_file_install", FileMutation, True, mutate)
        result = self.app.execute(operation, self.context, {"idempotency_key": str(staged.receipt_id), "receipt_id": str(staged.receipt_id)})
        return target, staged, UUID(result.data["job_id"]), result

    def recovery(self, key="restore"):
        return self.app.activate_recovery(self.context, RecoveryActivation(idempotency_key=key,
            expected_installation_id=self.settings.installation_id, expected_recovery_epoch=self.config()["recovery_epoch"]))

    def test_config_validation_secrets_and_snapshot_defaults(self):
        view = self.config()
        self.assertEqual(len(ICON_KEYS), 20)
        self.assertEqual(set(self.app.registry), {"config_get", "config_update", "operations_get", "events_list",
            "agents_create", "agents_get", "agents_list", "agents_update", "agents_status_set",
            "messages_send", "messages_list", "messages_acknowledge", "deliveries_get", "conversation_list", "runtime_get", "terminal_get", "files_reference", "browsers_attach", "browser_get", "input_requests_create",
            "input_requests_list", "input_requests_complete", "input_requests_cancel", "input_requests_resume"})
        self.assertEqual(view["policy"], "allow_all")
        self.assertNotIn(str(self.root), canonical(view))
        snapshot = NewAgentDefaults.model_validate(view["defaults"])
        for value in (0, -1, True, "2", 2**63):
            args = self.update()
            args["defaults"]["resources"]["cpu"] = value
            self.assertEqual(self.app.invoke("config_update", self.context, args).error.code, "invalid_argument")
        args = self.update()
        args["defaults"]["cli"]["secret"] = "synthetic-secret"
        error = self.app.invoke("config_update", self.context, args)
        self.assertNotIn("synthetic-secret", canonical(error))
        self.assertEqual(error.error.code, "invalid_argument")
        for field in ("model", "idempotency_key"):
            invalid_text = self.update()
            if field == "model":
                invalid_text["defaults"]["cli"]["model"] = "bad\ud800"
            else:
                invalid_text[field] = "bad\ud800"
            self.assertEqual(self.app.invoke("config_update", self.context, invalid_text).error.code, "invalid_argument")
        self.assertTrue(self.app.invoke("config_update", self.context, self.update()).ok)
        self.assertEqual(snapshot, self.defaults)  # Saved consumers are values, not a mutable defaults reference.
        self.assertEqual(self.counts()[2], self.bootstrap_counts[2])  # No invented config job.
        self.assertEqual(self.app.invoke("config_get", Invocation(actor_id=uuid4())).error.code, "not_found")

    def test_parallel_identical_config_calls_commit_once_and_replay_history(self):
        args = self.update(model="  trimmed  ")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.app.invoke("config_update", self.context, args), range(16)))
        self.assertTrue(all(r.ok for r in results), results)
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(self.counts(), self.plus_bootstrap(1, 1, 0, 0))
        normalized = self.update(model="trimmed")
        self.assertEqual(self.app.invoke("config_update", self.context, normalized), results[0])
        self.app.invoke("config_update", self.context, self.update(key="later", revision=2))
        self.restart()
        self.assertEqual(self.app.invoke("config_update", self.context, normalized), results[0])
        self.assertEqual(self.config()["revision"], 3)

    def test_hash_conflicts_revision_race_and_actor_scope(self):
        first = self.app.invoke("config_update", self.context, self.update())
        conflict = self.app.invoke("config_update", self.context, self.update(model="conflict"))
        self.assertEqual(conflict.error.code, "idempotency_conflict")
        invalid = self.update(model="x")
        invalid["defaults"]["resources"]["cpu"] = False
        self.assertEqual(self.app.invoke("config_update", self.context, invalid).error.code, "invalid_argument")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda k: self.app.invoke("config_update", self.context, self.update(k, 2)), ["a", "b"]))
        self.assertEqual(sum(r.ok for r in results), 1)
        self.assertEqual([r.error.code for r in results if not r.ok], ["revision_conflict"])
        agent = Actor(actor_id=uuid4(), kind="agent", agent_id=uuid4())
        with self.app.db.transaction() as tx:
            register_actor(tx, agent)
        other = self.app.invoke("config_update", Invocation(actor_id=agent.actor_id), self.update(revision=3))
        self.assertTrue(other.ok)
        self.assertNotEqual(other.operation_id, first.operation_id)

    def test_config_rollback_and_commit_response_failure(self):
        for point in ("config_after_update", "config_after_event", "operation_before_result", "operation_after_result", "transaction_before_commit"):
            with self.subTest(point=point):
                before = self.counts()
                self.fault.point = point
                with self.assertRaises(InjectedCrash):
                    self.app.invoke("config_update", self.context, self.update())
                self.assertEqual(self.config()["revision"], 1)
                self.assertEqual(self.counts(), before)
        self.fault.point = "transaction_after_commit"
        with self.assertRaises(InjectedCrash):
            self.app.invoke("config_update", self.context, self.update())
        self.restart()
        result = self.app.invoke("config_update", self.context, self.update())
        self.assertTrue(result.ok)
        self.assertEqual(result.data["revision"], 2)
        self.assertEqual(self.counts(), self.plus_bootstrap(1, 1, 0, 0))

    def test_change_event_job_result_are_one_commit(self):
        for point in ("test_after_job", "operation_before_result", "operation_after_result"):
            self.fault.point = point
            with self.assertRaises(InjectedCrash):
                self.probe()
            self.assertEqual(self.counts(), self.bootstrap_counts)
            self.assertEqual(self.config()["defaults"]["resources"]["disk_gib"], 4)
        operation, result, job_id = self.probe()
        self.assertEqual(self.counts(), self.plus_bootstrap(1, 1, 1, 0))
        self.assertEqual(self.app.execute(operation, self.context, {"idempotency_key": "probe", "value": 8}), result)
        self.app.jobs.run_next(worker_id=uuid4())
        lookup = self.app.invoke("operations_get", self.context, {"operation_id": str(result.operation_id)})
        self.assertEqual(lookup.data["original_result"], result.model_dump(mode="json"))
        self.assertEqual(lookup.data["jobs"][0]["state"], "completed")
        self.assertNotIn("test_probe", self.app.registry)

    def test_events_are_append_only_paginated_and_provenance_bound(self):
        self.app.invoke("config_update", self.context, self.update())
        page = self.app.invoke("events_list", self.context, {"limit": 1}).data
        self.assertTrue(page["has_more"])
        continuation = {"after_seq": page["next_after_seq"], "recovery_epoch": page["recovery_epoch"]}
        second = self.app.invoke("events_list", self.context, continuation).data
        self.assertGreater(second["items"][0]["event_seq"], page["items"][0]["event_seq"])
        self.assertEqual(self.app.invoke("events_list", self.context, {"after_seq": 1}).error.code, "invalid_argument")
        event = EventInput(kind="test.imported", actor_id=self.owner.actor_id, event_id=uuid4(),
            origin_installation_id=uuid4(), origin_seq=52, occurred_at="2001-01-01T00:00:00Z", payload={"x": 1})
        with self.app.db.transaction() as tx:
            seq = append_event(tx, event)
            self.assertEqual(append_event(tx, event), seq)
        with self.assertRaises(CoreError) as conflict:
            with self.app.db.transaction() as tx:
                append_event(tx, event.model_copy(update={"payload": {"x": 2}}))
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        for sql in ("UPDATE events SET kind='changed'", "DELETE FROM events", "UPDATE operations SET result_json='{}'", "DELETE FROM operations"):
            with self.assertRaises(CoreError):
                with self.app.db.transaction() as tx:
                    tx.connection.execute(sql)
        self.recovery()
        self.assertEqual(self.app.invoke("events_list", self.context, continuation).error.code, "target_changed")

    def test_bounded_busy_and_connection_pragmas(self):
        with self.app.db.read() as tx:
            self.assertEqual(tx.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(tx.connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(tx.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        blocker = sqlite3.connect(self.app.db.path)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            result = self.app.invoke("config_update", self.context, self.update())
            self.assertEqual(result.error.code, "storage_blocked")
            self.assertTrue(result.error.retryable)
            self.assertLess(self.app.db.last_timing.wait_seconds, 1)
            self.assertEqual(self.config()["revision"], 1)
        finally:
            blocker.rollback()
            blocker.close()

    def test_initialization_cannot_rebind_existing_installation(self):
        self.app.close()
        static_file = self.settings.data_root / "installation.json"
        static_file.unlink()  # Synthetic interrupted maintenance, not another installation.
        wrong = self.settings.model_copy(update={"installation_id": uuid4()})
        with self.assertRaises(CoreError) as failed:
            Application.initialize(wrong, self.defaults, self.owner)
        self.assertEqual(failed.exception.code, "target_changed")
        self.assertFalse(static_file.exists())
        self.app = Application.initialize(self.settings, self.defaults, self.owner, clock=self.clock)
        self.assertEqual(self.config()["installation_id"], str(self.settings.installation_id))

    def test_second_host_process_is_excluded(self):
        code = """from pathlib import Path
import sys
from qorqut_core.storage.database import HostLock
from qorqut_core.contracts import CoreError
try:
    HostLock(Path(sys.argv[1])).acquire()
except CoreError as e:
    print(e.code)
else:
    raise SystemExit(2)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.app.db.lock.path)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "unavailable")

    def test_restart_expiration_and_stale_results_do_not_repeat_effects(self):
        operation, result, job_id = self.probe()
        handlers = tuple(self.app.jobs.handlers.values())
        epoch = self.config()["recovery_epoch"]
        self.restart()
        self.app.jobs = Jobs(self.app.db, handlers)
        self.assertEqual(self.config()["recovery_epoch"], epoch)
        claim = self.app.jobs.claim(worker_id=uuid4(), lease_seconds=1)
        self.app.jobs.progress(claim, JobProgress(phase="prepared"), lease_seconds=1)
        self.clock.advance(2)
        self.assertIsNone(self.app.jobs.claim(worker_id=uuid4()))
        self.assertEqual(self.app.jobs.get(job_id)["state"], "uncertain")
        with self.assertRaises(CoreError) as stale:
            self.app.jobs.finish(claim, JobResolution(state="completed", progress=JobProgress(phase="done")))
        self.assertEqual(stale.exception.code, "target_changed")
        self.assertEqual(self.effects, [])
        self.app.jobs.reconcile(job_id)
        self.assertEqual(self.app.jobs.get(job_id)["state"], "uncertain")

    def test_internal_job_waiting_progress_failed_cancelled(self):
        operation, result, job_id = self.probe(external=False)
        claim = self.app.jobs.claim(worker_id=uuid4(), lease_seconds=1)
        self.clock.advance(2)
        newer = self.app.jobs.claim(worker_id=uuid4())
        self.assertNotEqual(claim.token, newer.token)
        self.app.jobs.finish(newer, JobResolution(state="waiting", progress=JobProgress(phase="capacity"),
            reason="retry_delay", next_attempt_at="2026-10-07T00:01:00Z"))
        self.assertIsNone(self.app.jobs.claim(worker_id=uuid4()))
        self.clock.advance(61)
        newer = self.app.jobs.claim(worker_id=uuid4())
        self.app.jobs.finish(newer, JobResolution(state="failed", progress=JobProgress(phase="failed")))
        self.assertEqual(self.app.jobs.get(job_id)["state"], "failed")
        with self.app.db.transaction() as tx:
            another = self.app.jobs.enqueue(tx, "test_probe", ProbeArgs(value=1), operation_id=result.operation_id)
        next_claim = self.app.jobs.claim(worker_id=uuid4())
        self.app.jobs.finish(next_claim, JobResolution(state="cancelled", progress=JobProgress(phase="cancelled")))
        self.assertEqual(self.app.jobs.get(another)["state"], "cancelled")

    def test_due_retry_does_not_starve_newer_work_or_get_starved_by_new_arrivals(self):
        _, result, original = self.probe(external=False)
        claim = self.app.jobs.claim(worker_id=uuid4())
        self.app.jobs.finish(claim, JobResolution(state="waiting", progress=JobProgress(phase="pending"),
            reason="retry_delay", next_attempt_at=after(self.clock(), 5)))
        self.clock.advance(1)
        with self.app.db.transaction() as tx:
            dependency = self.app.jobs.enqueue(tx, "test_probe", ProbeArgs(value=2), operation_id=result.operation_id)
        self.clock.advance(10)  # Slow work makes the old retry due before the next claim.
        with self.app.db.transaction() as tx:
            latest = self.app.jobs.enqueue(tx, "test_probe", ProbeArgs(value=3), operation_id=result.operation_id)
        # Work queued before the retry became due goes first. The overdue retry
        # then precedes later arrivals, so neither side can permanently displace it.
        for expected in (dependency, original, latest):
            claim = self.app.jobs.claim(worker_id=uuid4())
            self.assertEqual(claim.job_id, expected)
            self.app.jobs.finish(claim, JobResolution(state="completed", progress=JobProgress(phase="done")))
        self.assertIsNone(self.app.jobs.claim(worker_id=uuid4()))

    def test_recovery_is_explicit_idempotent_and_holds_snapshot_work(self):
        operation, result, job_id = self.probe()
        old_epoch = self.config()["recovery_epoch"]
        old_receipt = self.app.invoke("operations_get", self.context, {"operation_id": str(result.operation_id)}).data["original_result"]
        args = RecoveryActivation(idempotency_key="activate", expected_installation_id=self.settings.installation_id,
                                  expected_recovery_epoch=old_epoch)
        activated = self.app.activate_recovery(self.context, args)
        self.assertNotEqual(activated.data["recovery_epoch"], old_epoch)
        self.assertEqual(self.app.activate_recovery(self.context, args), activated)
        self.assertEqual(self.app.jobs.get(job_id)["wait_reason"], "recovery_review")
        self.assertIsNone(self.app.jobs.run_next(worker_id=uuid4()))
        self.assertEqual(self.effects, [])
        with self.assertRaises(CoreError):
            self.app.activate_recovery(self.context, args.model_copy(update={"idempotency_key": "new"}))
        self.assertEqual(self.app.invoke("operations_get", self.context, {"operation_id": str(result.operation_id)}).data["original_result"], old_receipt)
        self.assertNotIn("recovery_activate", self.app.registry)

    def test_external_work_does_not_hold_sql_and_maintenance_waits(self):
        entered, release, maintenance_done = threading.Event(), threading.Event(), threading.Event()

        def execute(claim):
            assert_external_io_allowed()
            entered.set()
            self.assertTrue(release.wait(4))
            return JobResolution(state="completed", progress=JobProgress(phase="done"))

        self.probe(execute=execute)
        with ThreadPoolExecutor(max_workers=2) as pool:
            work = pool.submit(self.app.jobs.run_next, worker_id=uuid4())
            self.assertTrue(entered.wait(3))
            self.assertTrue(self.app.invoke("config_update", self.context, self.update()).ok)
            args = RecoveryActivation(idempotency_key="restore", expected_installation_id=self.settings.installation_id,
                                      expected_recovery_epoch=self.config()["recovery_epoch"])

            def activate():
                value = self.app.activate_recovery(self.context, args)
                maintenance_done.set()
                return value

            maintenance = pool.submit(activate)
            self.assertFalse(maintenance_done.wait(0.05))
            release.set()
            work.result(timeout=4)
            self.assertTrue(maintenance.result(timeout=4).ok)

    def test_explicit_io_guard_and_config_path_perform_no_external_effects(self):
        with self.app.db.transaction() as tx:
            with self.assertRaises(CoreError):
                self.app.files.stage("objects/a", [b"x"], expected_sha256=None)
            with self.assertRaises(CoreError):
                self.app.jobs.run_next(worker_id=uuid4())
        with patch("os.replace", side_effect=AssertionError("external rename")), \
             patch("subprocess.Popen", side_effect=AssertionError("process start")), \
             patch("socket.socket", side_effect=AssertionError("network")), \
             patch("builtins.open", side_effect=AssertionError("file streaming")):
            self.assertTrue(self.app.invoke("config_update", self.context, self.update()).ok)

    def test_file_faults_before_and_after_rename_reconcile_after_restart(self):
        for index, point in enumerate(("file_before_rename", "file_after_rename", "file_before_parent_fsync",
                                       "file_after_parent_fsync", "file_before_receipt_commit", "file_after_receipt_commit")):
            with self.subTest(point=point):
                target, staged, job_id, original = self.attach_file(name=f"objects/item{index}", old=b"old")
                self.fault.point = point
                with self.assertRaises(InjectedCrash):
                    self.app.jobs.run_next(worker_id=uuid4(), lease_seconds=1)
                self.assertIn(target.read_bytes(), (b"old", b"new bytes"))
                self.restart()
                self.clock.advance(2)
                self.app.jobs.expire()
                self.assertEqual(self.app.jobs.get(job_id)["state"], "uncertain")
                self.assertEqual(self.app.jobs.reconcile(job_id).state, "completed")
                self.assertEqual(target.read_bytes(), b"new bytes")
                self.assertEqual(self.app.files.get(staged.receipt_id)["state"], "installed")
                self.assertEqual(self.app.invoke("operations_get", self.context, {"operation_id": str(original.operation_id)}).data["original_result"], original.model_dump(mode="json"))

    def test_file_staging_faults_leave_old_file_and_unknown_orphans(self):
        root = self.settings.data_root / "objects"
        root.mkdir()
        target = root / "item"
        target.write_bytes(b"old")
        for point in ("file_before_stage_fsync", "file_after_stage_fsync", "file_after_stage_directory_fsync"):
            self.fault.point = point
            with self.assertRaises(InjectedCrash):
                self.app.files.stage("objects/item", [b"new"], expected_sha256=hashlib.sha256(b"old").hexdigest())
            self.assertEqual(target.read_bytes(), b"old")
        orphans = {p.name: p.read_bytes() for p in root.iterdir()}
        self.restart()
        self.assertIsNone(self.app.jobs.run_next(worker_id=uuid4()))
        self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, orphans)

    def test_receipt_transaction_failure_keeps_unattached_stage(self):
        self.fault.point = "file_after_receipt_insert"
        with self.assertRaises(InjectedCrash):
            self.attach_file(old=b"old")
        self.assertEqual(self.counts(), self.bootstrap_counts)
        root = self.settings.data_root / "objects"
        self.assertEqual((root / "item").read_bytes(), b"old")
        self.assertEqual(len(list(root.glob(".qorqut-stage-*"))), 1)

    def test_restore_cannot_install_historical_pending_file(self):
        target, staged, job_id, _ = self.attach_file(old=b"before")
        self.recovery()
        self.assertIsNone(self.app.jobs.run_next(worker_id=uuid4()))
        self.assertEqual(self.app.jobs.reconcile(job_id).reason, "recovery_review")
        self.assertEqual(target.read_bytes(), b"before")
        # Actual retained external evidence can prove completion without a rename.
        target.write_bytes(b"new bytes")
        with patch("os.replace", side_effect=AssertionError("restore must not rename")):
            self.assertEqual(self.app.jobs.reconcile(job_id).state, "completed")

    def test_file_conflicts_symlinks_and_damaged_stages_are_preserved(self):
        for name in (".", "", "../escape", "/absolute", "state.sqlite3", "locks/new"):
            with self.assertRaises(CoreError):
                self.app.files.stage(name, [b"no"], expected_sha256=None)
        target, staged, job_id, _ = self.attach_file(old=b"old")
        target.write_bytes(b"concurrent authored edit")
        self.app.jobs.run_next(worker_id=uuid4())
        self.assertEqual(self.app.jobs.get(job_id)["state"], "uncertain")
        self.assertEqual(target.read_bytes(), b"concurrent authored edit")
        self.assertEqual(self.app.jobs.reconcile(job_id).state, "uncertain")
        target2, stage2, job2, _ = self.attach_file(name="other/item", mode="immutable")
        (target2.parent / stage2.stage_name).write_bytes(b"damaged")
        self.app.jobs.run_next(worker_id=uuid4())
        self.assertFalse(target2.exists())
        self.assertEqual(self.app.jobs.get(job2)["state"], "uncertain")
        (self.settings.data_root / "link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(CoreError):
            self.app.files.stage("link/outside", [b"no"], expected_sha256=None)
        self.assertFalse((self.root / "outside").exists())

    def test_unsupported_schema_refusal_does_not_modify_database(self):
        self.app.close()
        path = self.app.db.path
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA user_version=900")
        conn.close()
        before = {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
        with self.assertRaises(CoreError) as refused:
            Application.open(self.settings)
        self.assertEqual(refused.exception.code, "unsupported_schema")
        with self.assertRaises(CoreError):
            self.app.db.migrate()
        self.assertEqual({p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}, before)

    def test_unsupported_wal_schema_does_not_create_or_modify_sidecars(self):
        self.app.close()
        path = self.app.db.path
        for keep_connection in (False, True):
            with self.subTest(uncheckpointed=keep_connection):
                conn = sqlite3.connect(path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA user_version=901")
                if not keep_connection:
                    conn.close()
                try:
                    before = {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
                    with self.assertRaises(CoreError) as refused:
                        self.app.db.validate()
                    self.assertEqual(refused.exception.code, "unsupported_schema")
                    self.assertEqual({p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}, before)
                finally:
                    if keep_connection:
                        conn.close()

    def test_process_exit_before_and_after_operation_commit(self):
        self.app.close()
        code = """import os, sys
from uuid import UUID
from qorqut_core.app import Application, load_settings
from qorqut_core.contracts import Invocation
from pathlib import Path
def fault(point):
    if point == sys.argv[3]:
        os._exit(73)
app=Application.open(load_settings(Path(sys.argv[1])), fault=fault)
app.invoke('config_update', Invocation(actor_id=UUID(sys.argv[2])), {
    'idempotency_key':'crash', 'expected_revision':1,
    'defaults':{'cli':{'provider':'codex','model':'after-crash'},
                'resources':{'cpu':1,'memory_mib':512,'disk_gib':4}}})
raise SystemExit(2)
"""
        for point, expected in (("transaction_before_commit", 1), ("transaction_after_commit", 2)):
            with self.subTest(point=point):
                self.app.close()
                result = subprocess.run([sys.executable, "-c", code,
                    str(self.settings.data_root / "installation.json"), str(self.owner.actor_id), point],
                    text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 73, result.stderr)
                self.app = Application.open(self.settings, clock=self.clock, fault=self.fault)
                self.assertEqual(self.config()["revision"], expected)
        replay = self.app.invoke("config_update", self.context, self.update(key="crash", model="after-crash"))
        self.assertTrue(replay.ok)
        self.assertEqual(self.counts(), self.plus_bootstrap(1, 1, 0, 0))

    def test_process_exit_after_rename_reconciles_receipt(self):
        target, staged, job_id, _ = self.attach_file(old=b"old")
        self.app.close()
        code = """import os,sys
from pathlib import Path
from uuid import uuid4
from qorqut_core.app import Application,load_settings
def fault(point):
    if point == 'file_after_rename':
        os._exit(74)
app=Application.open(load_settings(Path(sys.argv[1])), clock=lambda:'2026-10-07T00:00:00.000000Z', fault=fault)
app.jobs.run_next(worker_id=uuid4(), lease_seconds=1)
raise SystemExit(2)
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.settings.data_root / "installation.json")],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 74, result.stderr)
        self.clock.advance(2)
        self.app = Application.open(self.settings, clock=self.clock, fault=self.fault)
        self.assertEqual(target.read_bytes(), b"new bytes")
        self.assertEqual(self.app.files.get(staged.receipt_id)["state"], "staged")
        self.app.jobs.expire()
        self.assertEqual(self.app.jobs.reconcile(job_id).state, "completed")

    def test_lease_expiry_does_not_allow_second_effect_executor(self):
        entered, release = threading.Event(), threading.Event()

        def execute(claim):
            entered.set()
            self.assertTrue(release.wait(4))
            return JobResolution(state="completed", progress=JobProgress(phase="done"))

        _, _, job_id = self.probe(execute=execute)
        alternate = Jobs(self.app.db, tuple(self.app.jobs.handlers.values()))
        with ThreadPoolExecutor(max_workers=2) as pool:
            work = pool.submit(self.app.jobs.run_next, worker_id=uuid4(), lease_seconds=1)
            self.assertTrue(entered.wait(3))
            self.clock.advance(2)
            alternate.expire()
            inspect = pool.submit(alternate.reconcile, job_id)
            self.assertFalse(inspect.done())
            release.set()
            with self.assertRaises(CoreError):
                work.result(timeout=4)
            self.assertEqual(inspect.result(timeout=4).state, "uncertain")

    def test_migration_failure_rolls_back_and_upgrade_preserves_records(self):
        path = self.root / "migration.sqlite3"
        lock = HostLock(self.root / "migration.lock").acquire()
        db = Database(path, lock)
        try:
            db.migrate(migrations=MIGRATIONS[:1])
            conn = sqlite3.connect(path)
            identity = str(uuid4())
            actor_id, operation_id, event_id = str(uuid4()), str(uuid4()), str(uuid4())
            conn.execute("INSERT INTO installation VALUES (1,?,?,?)", (identity, str(uuid4()), self.clock()))
            conn.execute("INSERT INTO actors VALUES (?,'owner',NULL,?)", (actor_id, self.clock()))
            conn.execute("INSERT INTO operations VALUES (?,?,'test_prior','prior',?,?,?,?)",
                         (operation_id, actor_id, 'a' * 64, '{"prior":"result"}', self.clock(), str(uuid4())))
            conn.execute("""INSERT INTO events VALUES (19,?,'test.prior',?,NULL,?,?,?,?,19,1,'{}')""",
                         (event_id, actor_id, operation_id, self.clock(), self.clock(), identity))
            prior_events = conn.execute("SELECT * FROM events").fetchall()
            prior_operations = conn.execute("SELECT * FROM operations").fetchall()
            conn.commit()
            conn.close()
            bad = Migration(2, ("CREATE TABLE should_rollback (x INTEGER)", "not valid SQL"))
            with self.assertRaises(CoreError):
                db.migrate(migrations=(MIGRATIONS[0], bad))
            conn = sqlite3.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone())
            conn.close()
            db.migrate()
            with db.read() as tx:
                self.assertEqual(tx.connection.execute("SELECT installation_id FROM installation").fetchone()[0], identity)
                self.assertEqual([tuple(r) for r in tx.connection.execute("SELECT * FROM events")], prior_events)
                self.assertEqual([tuple(r) for r in tx.connection.execute("SELECT * FROM operations")], prior_operations)
                self.assertEqual(tx.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(tx.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            lock.close()

    def test_imports_and_assembly_have_no_runtime_side_effects(self):
        probe_root = self.root / "import"
        probe_root.mkdir()
        code = """import importlib, pathlib, subprocess, sqlite3, socket, os, sys
from unittest.mock import patch
def audit(event, args):
    if event == 'open' and (args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)):
        raise AssertionError('file write during import')
    if event in ('os.mkdir','os.rename','os.remove'):
        raise AssertionError('filesystem effect during import')
sys.addaudithook(audit)
with patch.object(sqlite3, 'connect', side_effect=AssertionError('database')), \\
     patch.object(subprocess, 'Popen', side_effect=AssertionError('process')), \\
     patch.object(socket, 'socket', side_effect=AssertionError('network')):
    for name in ('qorqut_core', 'qorqut_core.app', 'qorqut_core.config', 'qorqut_core.contracts',
                 'qorqut_core.storage.database', 'qorqut_core.storage.events',
                 'qorqut_core.storage.jobs', 'qorqut_core.storage.files'):
        importlib.import_module(name)
assert list(pathlib.Path('.').iterdir()) == []
"""
        env = dict(os.environ, PYTHONPATH=str(Path(qorqut_core.__file__).resolve().parent.parent))
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=probe_root, env=env,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        with patch.object(sqlite3, "connect", side_effect=AssertionError("assembly opened DB")):
            assembled = Application(self.settings, self.app.db)
        self.assertEqual(set(assembled.registry), set(self.app.registry))


if __name__ == "__main__":
    unittest.main()
