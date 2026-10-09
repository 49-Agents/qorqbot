"""D41's owning boundary: synthetic terminal recordings and the real Archive.

Normal installed-server acceptance is recorded separately. No native session,
provider call, live installation or new transfer protocol is a fixture here.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import hashlib
import json
import sqlite3
import threading
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from agents.support import AgentCase
from qorqut_core.contracts import CoreError, Invocation
from qorqut_core.runner.shell_models import ShellControl, ShellSpec, ShellView
from qorqut_core.runner.shell_storage import ensure_schema
from qorqut_core.runner.storage import connect
from qorqut_core.runtime import terminal_archive
from qorqut_core.storage.database import assert_external_io_allowed
from qorqut_core.transcripts.archive_models import ArchiveMetadata, ArchiveReceipt
from qorqut_core.transcripts.probe import ProbeResult
from qorqut_core.transcripts.spool import CHUNK_BYTES, CaptureError, CaptureSpool, Chunk, RunBinding, Source


class TerminalArchiveTests(AgentCase):
    def setUp(self):
        super().setUp()
        self.agent = self.rows("agents")[0]
        self.agent_context = Invocation(actor_id=self.agent["actor_id"])
        self.new_terminal()

    def new_terminal(self):
        terminal, source, epoch = uuid4(), uuid4(), uuid4()
        self.database = self.root / (terminal.hex + ".sqlite3")
        spec = ShellSpec(installation_id=self.settings.installation_id, agent_id=self.agent["id"],
            recovery_epoch=self.config()["recovery_epoch"], environment_generation=1,
            home=self.root / "home", workspace=self.root / "workspace", path="/usr/bin:/bin")
        self.binding = RunBinding(str(spec.installation_id), str(spec.agent_id), str(spec.recovery_epoch),
                                  1, str(uuid4()), str(terminal), 1)
        self.source = Source(str(source), str(spec.installation_id), str(spec.agent_id), "terminal")
        self.key = str(source), str(epoch)
        self.metadata = ArchiveMetadata(capability_evidence="synthetic recorded PTY; native coverage absent",
            locator="saved terminal evidence", file_identity="original recording identity",
            native_created_at="2026-10-08T01:02:03Z")
        ensure_schema(self.database)
        with connect(self.database) as local, local:
            local.execute("""INSERT INTO runner_shell_terminals
                (terminal_id,installation_id,agent_id,spec_hash,spec_json,launch_json,runtime_root,
                 socket_path,boot_id,source_id,source_epoch,launch_marker) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(terminal), str(spec.installation_id), str(spec.agent_id),
                 hashlib.sha256(spec.model_dump_json().encode()).hexdigest(), spec.model_dump_json(), "{}",
                 str(self.root / "runtime"), str(self.root / "socket"), self.binding.boot_id,
                 *self.key, uuid4().hex))
        with CaptureSpool(self.database) as spool:
            spool.register_run(self.binding)
            spool.register_source(self.source, self.key[1], self.binding.run_id,
                                  **self.metadata.model_dump(exclude={"previous_epoch", "divergence_reason"}))
            spool.claim_recording(*self.key, self.binding.run_id)
            spool.activate_recording(*self.key)
        self.shell = ShellView(spec=spec, terminal_id=terminal, state="unknown", reason="unavailable_probe",
            source_id=source, source_epoch=epoch, probe_address="synthetic-unavailable",
            capture=ProbeResult(verified=False, terminal_active=False, reason="unavailable"),
            control=ShellControl(mode="unclaimed", epoch=0, viewer_id=None, last_sequence=0))

    def transfer(self):
        terminal_archive.transfer(self.database, self.shell, self.app.archive, self.agent_context)

    def append(self, payload):
        with CaptureSpool(self.database) as spool:
            return spool.append(*self.key, spool.state(*self.key)["local_captured_offset"], payload)

    def local_rows(self, table):
        with connect(self.database) as local:
            return [dict(row) for row in local.execute(f"SELECT * FROM {table}")]

    def state(self):
        with CaptureSpool(self.database) as spool:
            return spool.state(*self.key)

    def intent(self):
        rows = self.local_rows("terminal_archive_transfers")
        self.assertLessEqual(len(rows), 1)
        return rows[0] if rows else None

    def payloads(self):
        return [row["payload"] for row in self.local_rows("capture_chunks")]

    def host_chunks(self):
        return [row for row in self.rows("archive_chunks") if row["source_id"] == self.key[0]]

    def host_bytes(self):
        return b"".join(self.app.archive.read_chunk(self.agent_context, *self.key, row["offset"]).payload
                        for row in sorted(self.host_chunks(), key=lambda r: r["offset"]))

    def test_binary_bounded_transfer_original_metadata_and_stopped_drain(self):
        payload = bytes(range(256)) * (6 * 1024) + "partial ü\n".encode()
        self.append(payload)
        before = self.local_rows("capture_chunks")
        receive = self.app.archive.receive
        calls = []
        def checked(context, binding, chunk):
            assert_external_io_allowed()
            calls.append(chunk)
            self.assertEqual(binding, self.binding)
            # A separate writer can acquire this file: no local transaction
            # is held while the Archive writes its immutable object.
            with connect(self.database) as local:
                local.execute("BEGIN IMMEDIATE")
                local.rollback()
            return receive(context, binding, chunk)
        with patch.object(terminal_archive, "monotonic", return_value=0.0), \
                patch.object(self.app.archive, "receive", side_effect=checked), \
                patch.object(self.app.archive, "state", side_effect=AssertionError("state cannot authorize ACK")):
            self.transfer()
            self.assertEqual(len(calls), 4)
            self.assertEqual(self.state()["host_archived_offset"], 4 * CHUNK_BYTES)
            self.assertEqual(self.host_bytes(), payload[:4 * CHUNK_BYTES])
            self.assertEqual(self.payloads()[:4], [None] * 4)
            self.assertTrue(all(p is not None for p in self.payloads()[4:]))
            # Finish recording unsuccessfully: neither liveness nor health
            # authorizes changing historical identity or clearing the error.
            with CaptureSpool(self.database) as spool:
                spool.finish_recording(*self.key, 9)
            health = self.state()
            self.shell = self.shell.model_copy(update={"state": "stopped"})
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(self.transfer).result(timeout=5)
            self.transfer()
        self.assertEqual(self.host_bytes(), payload)
        self.assertEqual(len(calls), len(before))
        self.assertEqual(self.state()["last_error"], health["last_error"])
        self.assertEqual(self.state()["health"], "incomplete")
        self.assertEqual(self.payloads(), [None] * len(before))
        self.assertIsNone(self.intent())
        self.assertEqual(self.rows("session_owners"), [])
        run = self.rows("archive_runs")[0]
        self.assertEqual({k: run[k] for k in asdict(self.binding)}, asdict(self.binding))
        epoch = self.rows("archive_epochs")[0]
        self.assertEqual({k: epoch[k] for k in ArchiveMetadata.model_fields}, self.metadata.model_dump())
        self.assertEqual(run["registered_by"], self.agent["actor_id"])
        for kind in ("archive.source_registered", "archive.epoch_registered", "archive.run_linked"):
            self.assertEqual(len([e for e in self.rows("events") if e["kind"] == kind]), 1)
        self.assertEqual([(r["offset"], r["length"], r["sha256"]) for r in before],
                         [(c.offset, c.length, c.sha256) for c in calls])
        refreshed = terminal_archive.saved_capture(self.database, self.shell)
        self.assertEqual((refreshed.local_captured_offset, refreshed.host_archived_offset), (len(payload), len(payload)))

    def test_small_chunks_count_budget_and_receipt_based_continuation(self):
        payloads = [bytes([i]) * (i % 17 + 1) for i in range(130)]
        for payload in payloads:
            self.append(payload)
        before = self.local_rows("capture_chunks")
        with patch.object(terminal_archive, "monotonic", return_value=0.0), \
                patch.object(self.app.archive, "state", side_effect=AssertionError("state cannot authorize ACK")), \
                patch.object(self.app.archive, "receive", wraps=self.app.archive.receive) as receive:
            self.transfer()
        calls = [call.args[2] for call in receive.call_args_list]
        self.assertEqual(len(calls), 128)
        self.assertEqual(self.state()["host_archived_offset"], sum(map(len, payloads[:128])))
        self.assertEqual(self.payloads(), [None] * 128 + payloads[128:])
        self.assertEqual(self.host_bytes(), b"".join(payloads[:128]))
        self.assertIsNone(self.intent())
        self.restart()
        with patch.object(terminal_archive, "monotonic", return_value=0.0), \
                patch.object(self.app.archive, "state", side_effect=AssertionError("state cannot authorize ACK")), \
                patch.object(self.app.archive, "receive", wraps=self.app.archive.receive) as receive:
            self.transfer()
        calls += [call.args[2] for call in receive.call_args_list]
        self.assertEqual([chunk.payload for chunk in calls], payloads)
        self.assertEqual([(r["offset"], r["length"], r["sha256"]) for r in before],
                         [(c.offset, c.length, c.sha256) for c in calls])
        self.assertEqual(len(self.host_chunks()), 130)
        self.assertEqual(self.host_bytes(), b"".join(payloads))
        self.assertEqual(self.state()["host_archived_offset"], sum(map(len, payloads)))
        self.assertEqual(self.payloads(), [None] * 130)
        self.assertIsNone(self.intent())

    def test_byte_budget_never_splits_or_skips_the_next_original_chunk(self):
        payloads = [b"x"] + [bytes([i]) * CHUNK_BYTES for i in range(4)] + [b"tail"]
        for payload in payloads:
            self.append(payload)
        with patch.object(terminal_archive, "monotonic", return_value=0.0), \
                patch.object(self.app.archive, "receive", wraps=self.app.archive.receive) as receive:
            self.transfer()
            self.assertEqual([call.args[2].payload for call in receive.call_args_list], payloads[:4])
            self.assertEqual(self.state()["host_archived_offset"], 1 + 3 * CHUNK_BYTES)
            self.assertEqual(self.payloads(), [None] * 4 + payloads[4:])
            self.assertIsNone(self.intent())
            self.transfer()
            self.assertEqual([call.args[2].payload for call in receive.call_args_list], payloads)
        self.assertEqual(self.host_bytes(), b"".join(payloads))
        self.assertEqual(self.payloads(), [None] * len(payloads))
        self.assertIsNone(self.intent())

    def test_time_budget_allows_first_chunk_and_completes_receipt_ack_cleanup(self):
        for setup_delay, chunk_delay, count in ((0.25, 0.0, 1), (0.0, 0.125, 2),
                                               (0.0, 0.2, 1), (0.0, 1.0, 1)):
            with self.subTest(setup_delay=setup_delay, chunk_delay=chunk_delay):
                self.new_terminal()
                payloads = [b"first", b"second", b"third"]
                for payload in payloads:
                    self.append(payload)
                now = 0.0
                register, receive = self.app.archive.register, self.app.archive.receive
                def delayed_register(*args):
                    nonlocal now
                    result = register(*args)
                    now += setup_delay
                    return result
                def delayed_receive(*args):
                    nonlocal now
                    result = receive(*args)
                    now += chunk_delay
                    return result
                with patch.object(terminal_archive, "monotonic", side_effect=lambda: now), \
                        patch.object(self.app.archive, "register", side_effect=delayed_register), \
                        patch.object(self.app.archive, "receive", side_effect=delayed_receive) as received:
                    self.transfer()
                self.assertEqual(received.call_count, count)
                self.assertEqual(self.state()["host_archived_offset"], sum(map(len, payloads[:count])))
                self.assertEqual(self.host_bytes(), b"".join(payloads[:count]))
                self.assertEqual(self.payloads(), [None] * count + payloads[count:])
                self.assertIsNone(self.intent())

    def test_interrupted_receive_receipt_ack_reclaim_and_retirement_retry(self):
        for point in ("intent", "receive_before", "receive_after", "receipt", "ack_before", "ack_after",
                      "reclaim_before", "reclaim_after", "retire"):
            with self.subTest(point=point):
                self.new_terminal()
                self.transfer()  # Empty source registration/table initialization only.
                payload = b"raw\x00\xff exact partial bytes"
                self.append(payload)
                real_receive, calls = self.app.archive.receive, []
                def receive(context, binding, chunk):
                    calls.append((binding, chunk))
                    if point == "receive_before":
                        raise OSError("synthetic interrupted receive")
                    receipt = real_receive(context, binding, chunk)
                    if point == "receive_after":
                        raise OSError("synthetic lost host response")
                    return receipt
                original = CaptureSpool.acknowledge if point.startswith("ack_") else CaptureSpool.reclaim
                def interrupted(spool, *args):
                    if point.endswith("_before"):
                        raise OSError("synthetic interrupted local operation")
                    original(spool, *args)
                    raise OSError("synthetic lost local response")
                if point in {"intent", "receipt", "retire"}:
                    action = {"intent": "INSERT", "receipt": "UPDATE OF receipt", "retire": "DELETE"}[point]
                    with connect(self.database) as local, local:
                        local.execute(f"CREATE TRIGGER interrupted BEFORE {action} ON terminal_archive_transfers "
                                      "BEGIN SELECT RAISE(FAIL,'synthetic local storage failure'); END")
                with patch.object(self.app.archive, "receive", side_effect=receive):
                    if point.startswith(("ack_", "reclaim_")):
                        method = "acknowledge" if point.startswith("ack_") else "reclaim"
                        with patch.object(CaptureSpool, method, interrupted), self.assertRaises(OSError):
                            self.transfer()
                    else:
                        with self.assertRaises((OSError, sqlite3.Error)):
                            self.transfer()
                intent = self.intent()
                if point == "intent":
                    self.assertIsNone(intent)
                    self.assertEqual(calls, [])
                else:
                    self.assertEqual(json.loads(intent["binding"]), asdict(self.binding))
                    self.assertEqual((intent["offset"], intent["length"], intent["sha256"]),
                                     (0, len(payload), hashlib.sha256(payload).hexdigest()))
                    if point in {"receive_before", "receive_after", "receipt"}:
                        self.assertIsNone(intent["receipt"])
                    else:
                        receipt = ArchiveReceipt.model_validate_json(intent["receipt"])
                        self.assertEqual(receipt.host_archived_offset, len(payload))
                acked = point in {"ack_after", "reclaim_before", "reclaim_after", "retire"}
                self.assertEqual(self.state()["host_archived_offset"], len(payload) if acked else 0)
                self.assertEqual(self.payloads(), [None if point in {"reclaim_after", "retire"} else payload])
                with connect(self.database) as local, local:
                    local.execute("DROP TRIGGER IF EXISTS interrupted")
                self.restart()  # Retry through newly opened host/local connections.
                with patch.object(self.app.archive, "receive", wraps=self.app.archive.receive) as retried:
                    self.transfer()
                if point in {"intent", "receive_before", "receive_after", "receipt"}:
                    self.assertEqual(retried.call_count, 1)
                    if calls:
                        self.assertEqual(retried.call_args.args[1:], calls[0])
                else:
                    self.assertEqual(retried.call_count, 0)
                self.assertEqual(self.state()["host_archived_offset"], len(payload))
                self.assertEqual(self.payloads(), [None])
                self.assertEqual(self.host_bytes(), payload)
                self.assertEqual(len(self.host_chunks()), 1)
                self.assertIsNone(self.intent())

    def test_receipt_mismatch_never_authorizes_ack(self):
        fields = ["binding." + name for name in RunBinding.__dataclass_fields__]
        fields += ["source_id", "source_epoch", "recovery_epoch", "host_archived_offset", "type"]
        for field in fields:
            with self.subTest(field=field):
                self.new_terminal()
                self.append(b"unchanged bytes")
                receive = self.app.archive.receive
                def mismatch(*args):
                    receipt = receive(*args)
                    if field == "type":
                        return receipt.model_dump()
                    if field.startswith("binding."):
                        name = field.split(".")[1]
                        value = 2 if name.endswith("generation") else str(uuid4())
                        return receipt.model_copy(update={"binding": replace(receipt.binding, **{name: value})})
                    return receipt.model_copy(update={field: 1000 if field == "host_archived_offset" else str(uuid4())})
                with patch.object(self.app.archive, "receive", side_effect=mismatch), self.assertRaises(ValueError):
                    self.transfer()
                self.assertEqual(self.state()["host_archived_offset"], 0)
                self.assertEqual(self.payloads(), [b"unchanged bytes"])
                self.assertIsNone(self.intent()["receipt"])
                self.transfer()
                self.assertEqual(self.payloads(), [None])

    def test_conflict_recovery_and_missing_receipt_preserve_acked_payload(self):
        for failure in ("conflict", "host_hold", "host_epoch", "missing_receipt", "wrong_receipt", "wrong_binding"):
            with self.subTest(failure=failure):
                self.new_terminal()
                self.append(b"retain this payload")
                with patch.object(CaptureSpool, "reclaim", side_effect=OSError("interrupted cleanup")), self.assertRaises(OSError):
                    self.transfer()
                self.assertEqual(self.state()["host_archived_offset"], 19)
                if failure == "conflict":
                    other = b"different bytes"
                    conflict = Chunk(*self.key, 0, len(other), hashlib.sha256(other).hexdigest(), other)
                    with self.assertRaises(CoreError):
                        self.app.archive.receive(self.agent_context, self.binding, conflict)
                elif failure in {"host_hold", "host_epoch"}:
                    # Same registered producer, but an admission hold or changed
                    # host epoch must stop even already ACKed local cleanup.
                    register = self.app.archive.register
                    def held(*args):
                        result = register(*args)
                        return result.model_copy(update={"recovery_held": True} if failure == "host_hold"
                                                 else {"recovery_epoch": str(uuid4())})
                else:
                    with connect(self.database) as local, local:
                        if failure == "missing_receipt":
                            local.execute("UPDATE terminal_archive_transfers SET receipt=NULL")
                        elif failure == "wrong_receipt":
                            value = ArchiveReceipt.model_validate_json(self.intent()["receipt"])
                            local.execute("UPDATE terminal_archive_transfers SET receipt=?",
                                (value.model_copy(update={"source_id": str(uuid4())}).model_dump_json(),))
                        else:
                            local.execute("UPDATE terminal_archive_transfers SET binding=?",
                                (json.dumps(asdict(replace(self.binding, boot_id=str(uuid4())))),))
                before, intent = self.payloads(), self.intent()
                with patch.object(CaptureSpool, "reclaim", side_effect=AssertionError("held cleanup")), \
                        patch.object(self.app.archive, "receive", side_effect=AssertionError("held receive")):
                    if failure in {"host_hold", "host_epoch"}:
                        with patch.object(self.app.archive, "register", side_effect=held), self.assertRaises(CoreError):
                            self.transfer()
                    else:
                        with self.assertRaises((CoreError, ValueError)):
                            self.transfer()
                self.assertEqual(self.payloads(), before)
                self.assertEqual(self.intent(), intent)
        self.recovery()
        with self.assertRaises(CoreError):
            self.transfer()
        self.assertEqual(self.payloads(), [b"retain this payload"])

    def test_saved_identity_validation_and_empty_page_never_sweeps_payload(self):
        changes = [("runner_shell_terminals", "boot_id", str(uuid4())),
                   ("runner_shell_terminals", "source_id", str(uuid4())),
                   ("runner_shell_terminals", "spec_hash", "changed"),
                   ("capture_sources", "source_type", "native"),
                   ("capture_sources", "agent_id", str(uuid4())),
                   ("capture_epochs", "recorder_run_id", None)]
        for table, field, value in changes:
            with self.subTest(table=table, field=field):
                self.new_terminal()
                self.append(b"original")
                with connect(self.database) as local, local:
                    local.execute(f"UPDATE {table} SET {field}=?", (value,))
                with patch.object(self.app.archive, "register", side_effect=AssertionError("invalid registration")):
                    with self.assertRaises((CoreError, CaptureError)):
                        self.transfer()
                self.assertEqual(self.payloads(), [b"original"])
        self.new_terminal()
        self.append(b"unconfirmed old payload")
        with CaptureSpool(self.database) as spool:
            spool.acknowledge(*self.key, 23)
        with patch.object(CaptureSpool, "reclaim", side_effect=AssertionError("empty page cleanup")):
            self.transfer()
        self.assertEqual(self.payloads(), [b"unconfirmed old payload"])
        self.append(b"later bytes")
        with patch.object(self.app.archive, "receive", side_effect=AssertionError("unconfirmed prefix")), self.assertRaises(ValueError):
            self.transfer()
        self.assertEqual(self.payloads(), [b"unconfirmed old payload", b"later bytes"])

    def test_host_lifetime_and_effects_guard_covers_local_ack_and_cleanup(self):
        self.append(b"guarded payload")
        entered, release, maintained = threading.Event(), threading.Event(), threading.Event()
        acknowledge = CaptureSpool.acknowledge
        def blocked(spool, *args):
            assert_external_io_allowed()
            entered.set()
            self.assertTrue(release.wait(5))
            acknowledge(spool, *args)
        def maintenance():
            with self.app.db.lock.maintenance():
                maintained.set()
        with ThreadPoolExecutor(max_workers=2) as executor:
            with patch.object(CaptureSpool, "acknowledge", blocked):
                future = executor.submit(self.transfer)
                try:
                    self.assertTrue(entered.wait(5))
                    acquired = self.app.db.lock.effects.acquire(blocking=False)
                    if acquired:
                        self.app.db.lock.effects.release()
                    self.assertFalse(acquired)
                    maintenance_future = executor.submit(maintenance)
                    self.assertFalse(maintained.wait(0.05))
                finally:
                    release.set()
                future.result(timeout=5)
                maintenance_future.result(timeout=5)
        self.assertTrue(maintained.is_set())
        self.assertEqual(self.payloads(), [None])


class TerminalArchiveObservationTests(unittest.TestCase):
    def test_host_storage_failure_preserves_shell_and_refreshes_counters(self):
        # Reuse the accepted disposable recorded-shell fixture and cleanup;
        # only the new composition's failure/counter behavior is exercised.
        from qorqut_core.app import Application
        from transports.test_terminal import OwnedProcesses, TerminalInstallation, wait_for

        OwnedProcesses.setUpClass()
        self.addCleanup(OwnedProcesses.tearDownClass)
        fixture = TerminalInstallation()
        self.addCleanup(fixture.close)
        app = Application.open(fixture.settings)
        self.addCleanup(app.close)
        app.jobs.run_next(worker_id=uuid4())
        with patch.object(app.archive, "receive", side_effect=OSError("private path or raw content")):
            with self.assertLogs("qorqut_core.runtime.terminals", level="WARNING") as logs:
                view = app.terminals.observe()
        self.assertEqual(view.status, "available")
        self.assertEqual(view.recording.state, "healthy")
        self.assertGreater(view.recording.local_captured_offset, 0)
        self.assertEqual(view.recording.host_archived_offset, 0)
        self.assertEqual(logs.output, ["WARNING:qorqut_core.runtime.terminals:terminal_archive_transfer_failed"])
        before = fixture.shell()
        def drained():
            refreshed = app.terminals.observe()
            recording = refreshed.recording
            return refreshed if recording.host_archived_offset == recording.local_captured_offset else None
        after = wait_for(drained)
        self.assertEqual(after.status, "available")
        self.assertGreater(after.recording.host_archived_offset, 0)
        self.assertEqual(after.recording.host_archived_offset, fixture.shell().saved_capture.host_archived_offset)
        self.assertEqual(fixture.shell().shell, before.shell)
        with app.db.transaction() as tx:
            tx.connection.execute("UPDATE jobs SET wait_reason='recovery_review' WHERE job_id="
                                  "(SELECT provisioning_job_id FROM agents WHERE kind='host')")
        with patch("qorqut_core.runtime.terminals.transfer", side_effect=AssertionError("recovery bypass")):
            held = app.terminals.observe()
        self.assertEqual((held.status, held.reason), ("unavailable", "target_changed"))
        self.assertEqual(fixture.shell().shell, before.shell)
