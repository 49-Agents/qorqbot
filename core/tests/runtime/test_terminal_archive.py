"""D41/D50's owning boundary: original terminal chunks and real host ranges.

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

    def ledger(self):
        return [{name: row[name] for name in ("offset", "length", "sha256")}
                for row in self.local_rows("capture_chunks")]

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
        before = self.ledger()
        manifests, pending_calls, receipt_calls = [], [], []
        real_receive = self.app.archive.receive
        pending, receipts = CaptureSpool.pending, CaptureSpool.receipts
        def received(*args):
            manifests.append(json.loads(self.intent()["chunks_json"]))
            return real_receive(*args)
        def pending_page(spool, *args, **kwargs):
            pending_calls.append(kwargs)
            return pending(spool, *args, **kwargs)
        def receipt_page(spool, *args, **kwargs):
            receipt_calls.append(kwargs)
            return receipts(spool, *args, **kwargs)
        with patch.object(terminal_archive, "monotonic", return_value=0.0), \
                patch.object(self.app.archive, "state", side_effect=AssertionError("state cannot authorize ACK")), \
                patch.object(CaptureSpool, "pending", pending_page), \
                patch.object(CaptureSpool, "receipts", receipt_page), \
                patch.object(self.app.archive, "receive", side_effect=received) as receive:
            self.transfer()
        calls = [call.args[2] for call in receive.call_args_list]
        self.assertEqual(len(calls), 1)
        self.assertEqual(manifests, [{"version": 1, "chunks": before[:128]}])
        offsets = [before[i]["offset"] for i in range(0, 128, 16)]
        self.assertEqual([c["after"] for c in pending_calls], offsets * 2)
        self.assertEqual([c["after"] for c in receipt_calls], offsets)
        self.assertTrue(all(c["limit"] == 16 for c in pending_calls + receipt_calls))
        self.assertEqual({c["through"] for c in receipt_calls}, {sum(map(len, payloads[:128]))})
        self.assertEqual(self.state()["host_archived_offset"], sum(map(len, payloads[:128])))
        self.assertEqual(self.payloads(), [None] * 128 + payloads[128:])
        self.assertEqual(self.host_bytes(), b"".join(payloads[:128]))
        self.assertIsNone(self.intent())
        self.restart()
        real_receive = self.app.archive.receive
        with patch.object(terminal_archive, "monotonic", return_value=0.0), \
                patch.object(self.app.archive, "state", side_effect=AssertionError("state cannot authorize ACK")), \
                patch.object(self.app.archive, "receive", side_effect=received) as receive:
            self.transfer()
        calls += [call.args[2] for call in receive.call_args_list]
        self.assertEqual([chunk.payload for chunk in calls], [b"".join(payloads[:128]), b"".join(payloads[128:])])
        self.assertEqual(manifests[-1], {"version": 1, "chunks": before[128:]})
        self.assertEqual(self.ledger(), before)
        self.assertEqual(len(self.host_chunks()), 2)
        self.assertEqual(self.host_bytes(), b"".join(payloads))
        self.assertEqual(self.state()["host_archived_offset"], sum(map(len, payloads)))
        self.assertEqual(self.payloads(), [None] * 130)
        self.assertIsNone(self.intent())

    def test_byte_budget_never_splits_or_skips_the_next_original_chunk(self):
        payloads = [b"x", b"yz"] + [bytes([i]) * CHUNK_BYTES for i in range(4)] + [b"tail"]
        for payload in payloads:
            self.append(payload)
        before = self.ledger()
        ranges = [b"xyz"] + payloads[2:]
        with patch.object(terminal_archive, "monotonic", return_value=0.0), \
                patch.object(self.app.archive, "receive", wraps=self.app.archive.receive) as receive:
            self.transfer()
            self.assertEqual([call.args[2].payload for call in receive.call_args_list], ranges[:4])
            self.assertEqual(self.state()["host_archived_offset"], 3 + 3 * CHUNK_BYTES)
            self.assertEqual(self.payloads(), [None] * 5 + payloads[5:])
            self.assertIsNone(self.intent())
            self.transfer()
            self.assertEqual([call.args[2].payload for call in receive.call_args_list], ranges)
        self.assertEqual(self.host_bytes(), b"".join(payloads))
        self.assertEqual(self.ledger(), before)
        self.assertEqual(self.payloads(), [None] * len(payloads))
        self.assertIsNone(self.intent())

    def test_time_budget_allows_first_range_and_completes_receipt_ack_cleanup(self):
        for setup_delay, chunk_delay, count in ((0.25, 0.0, 1), (0.0, 0.125, 2),
                                               (0.0, 0.2, 1), (0.0, 1.0, 1)):
            with self.subTest(setup_delay=setup_delay, chunk_delay=chunk_delay):
                self.new_terminal()
                payloads = [bytes([i]) * (CHUNK_BYTES // 2) for i in range(6)]
                ranges = [b"".join(payloads[i:i + 2]) for i in range(0, 6, 2)]
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
                self.assertEqual(self.state()["host_archived_offset"], count * CHUNK_BYTES)
                self.assertEqual(self.host_bytes(), b"".join(ranges[:count]))
                self.assertEqual(self.payloads(), [None] * (2 * count) + payloads[2 * count:])
                self.assertIsNone(self.intent())

    def test_interrupted_receive_receipt_ack_reclaim_and_retirement_retry(self):
        for point in ("intent", "receive_before", "receive_after", "receipt", "ack_before", "ack_after",
                      "reclaim_before", "reclaim_after", "retire"):
            with self.subTest(point=point):
                self.new_terminal()
                self.transfer()  # Empty source registration/table initialization only.
                payloads = [b"raw\x00\xff" + bytes([i]) for i in range(17)]
                for part in payloads:
                    self.append(part)
                payload, before = b"".join(payloads), self.ledger()
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
                    self.assertEqual(json.loads(intent["chunks_json"]), {"version": 1, "chunks": before})
                    if point in {"receive_before", "receive_after", "receipt"}:
                        self.assertIsNone(intent["receipt"])
                    else:
                        receipt = ArchiveReceipt.model_validate_json(intent["receipt"])
                        self.assertEqual(receipt.host_archived_offset, len(payload))
                acked = point in {"ack_after", "reclaim_before", "reclaim_after", "retire"}
                self.assertEqual(self.state()["host_archived_offset"], len(payload) if acked else 0)
                self.assertEqual(self.payloads(), [None] * 17 if point in {"reclaim_after", "retire"} else payloads)
                with connect(self.database) as local, local:
                    local.execute("DROP TRIGGER IF EXISTS interrupted")
                self.restart()  # Retry through newly opened host/local connections.
                # New recording bytes must never enlarge a persisted old range.
                for part in (b"new output", b" during retry"):
                    self.append(part)
                retry_intents = []
                real_receive = self.app.archive.receive
                def retry(*args):
                    retry_intents.append(self.intent())
                    return real_receive(*args)
                with patch.object(terminal_archive, "monotonic", return_value=0.0), \
                        patch.object(self.app.archive, "receive", side_effect=retry) as retried:
                    self.transfer()
                if point in {"intent", "receive_before", "receive_after", "receipt"}:
                    self.assertEqual(retried.call_count, 1 if point == "intent" else 2)
                    if calls:
                        self.assertEqual(retried.call_args_list[0].args[1:], calls[0])
                        self.assertEqual(retry_intents[0], intent)
                else:
                    self.assertEqual(retried.call_count, 1)
                    self.assertEqual(retried.call_args.args[2].offset, len(payload))
                self.assertEqual(self.state()["host_archived_offset"], len(payload) + 23)
                self.assertEqual(self.payloads(), [None] * 19)
                self.assertEqual(self.ledger()[:17], before)
                self.assertEqual(self.host_bytes(), payload + b"new output during retry")
                self.assertEqual(len(self.host_chunks()), 1 if point == "intent" else 2)
                self.assertIsNone(self.intent())

    def test_invalid_manifest_ledger_or_payload_never_authorizes_ack(self):
        cases = ("json", "null", "fields", "version", "boolean_version", "empty", "count", "size",
                 "entry_fields", "boolean_offset", "boolean_length", "zero_length", "large_length",
                 "overflow", "gap", "order", "total", "hash", "aggregate", "ledger", "missing", "payload")
        for failure in cases:
            with self.subTest(failure=failure):
                self.new_terminal()
                for i in range(17):
                    self.append(bytes([i, 255, 0]))
                with patch.object(self.app.archive, "receive", side_effect=OSError("offline")), self.assertRaises(OSError):
                    self.transfer()
                manifest = json.loads(self.intent()["chunks_json"])
                entries = manifest["chunks"]
                if failure == "fields":
                    manifest["extra"] = 1
                elif failure in {"version", "boolean_version"}:
                    manifest["version"] = 2 if failure == "version" else True
                elif failure == "empty":
                    manifest["chunks"] = []
                elif failure == "count":
                    manifest["chunks"] = entries * 8
                elif failure == "entry_fields":
                    entries[-1]["extra"] = 1
                elif failure == "boolean_offset":
                    entries[0]["offset"] = False
                elif failure == "boolean_length":
                    entries[-1]["length"] = True
                elif failure in {"zero_length", "large_length"}:
                    entries[-1]["length"] = 0 if failure == "zero_length" else CHUNK_BYTES + 1
                elif failure == "overflow":
                    entries[-1]["offset"] = 2**63 - 1
                elif failure == "gap":
                    entries[-1]["offset"] += 1
                elif failure == "order":
                    entries[0], entries[1] = entries[1], entries[0]
                elif failure == "total":
                    entries.pop()
                elif failure == "hash":
                    entries[-1]["sha256"] = "a" * 64
                raw = {"json": "{", "null": "null", "size": " " * 32769}.get(failure, json.dumps(manifest))
                with connect(self.database) as local, local:
                    local.execute("UPDATE terminal_archive_transfers SET chunks_json=?", (raw,))
                    if failure == "aggregate":
                        local.execute("UPDATE terminal_archive_transfers SET sha256=?", ("a" * 64,))
                    elif failure == "ledger":
                        local.execute("UPDATE capture_chunks SET sha256=? WHERE offset=48", ("a" * 64,))
                    elif failure in {"missing", "payload"}:
                        local.execute("UPDATE capture_chunks SET payload=? WHERE offset=48",
                                      (None if failure == "missing" else b"bad",))
                before, intent = self.payloads(), self.intent()
                with patch.object(self.app.archive, "receive", side_effect=AssertionError("invalid receive")), \
                        patch.object(CaptureSpool, "acknowledge", side_effect=AssertionError("invalid ACK")), \
                        self.assertRaises((CoreError, ValueError, CaptureError)):
                    self.transfer()
                self.assertEqual(self.state()["host_archived_offset"], 0)
                self.assertEqual(self.payloads(), before)
                self.assertEqual(self.intent(), intent)
                self.assertEqual(self.host_chunks(), [])

        # A saved receipt cannot skip manifest/ledger validation after reclaim.
        for failure in ("hash", "ledger", "gap", "null", "duplicate"):
            with self.subTest(reclaimed=failure):
                self.new_terminal()
                self.transfer()
                for i in range(17):
                    self.append(bytes([i]))
                with connect(self.database) as local, local:
                    local.execute("CREATE TRIGGER interrupted BEFORE DELETE ON terminal_archive_transfers "
                                  "BEGIN SELECT RAISE(FAIL,'retirement interrupted'); END")
                with self.assertRaises(sqlite3.Error):
                    self.transfer()
                self.assertEqual(self.payloads(), [None] * 17)
                manifest = json.loads(self.intent()["chunks_json"])
                if failure == "hash":
                    manifest["chunks"][-1]["sha256"] = "a" * 64
                elif failure == "gap":
                    manifest["chunks"][-1]["offset"] += 1
                raw = json.dumps(manifest)
                if failure == "duplicate":
                    raw = raw.replace('"version": 1', '"version": 2, "version": 1')
                with connect(self.database) as local, local:
                    local.execute("DROP TRIGGER interrupted")
                    local.execute("UPDATE terminal_archive_transfers SET chunks_json=?",
                                  (None if failure == "null" else raw,))
                    if failure == "ledger":
                        local.execute("UPDATE capture_chunks SET sha256=? WHERE offset=16", ("a" * 64,))
                intent = self.intent()
                self.restart()
                with patch.object(CaptureSpool, "pending", side_effect=AssertionError("reclaimed payload needed")), \
                        patch.object(CaptureSpool, "acknowledge", side_effect=AssertionError("invalid ACK")), \
                        patch.object(CaptureSpool, "reclaim", side_effect=AssertionError("invalid cleanup")), \
                        self.assertRaises((CoreError, ValueError, CaptureError)):
                    self.transfer()
                self.assertEqual(self.intent(), intent)
                self.assertEqual(self.state()["host_archived_offset"], 17)

    def test_pending_pages_must_keep_source_epoch_contiguity_and_cursor(self):
        for failure in ("source", "epoch", "offset", "hash", "cursor"):
            with self.subTest(failure=failure):
                self.new_terminal()
                for i in range(17):
                    self.append(bytes([i]))
                pending = CaptureSpool.pending
                def changed(spool, *args, **kwargs):
                    page = pending(spool, *args, **kwargs)
                    if kwargs["after"] == 16:
                        chunk = page.chunks[0]
                        if failure == "cursor":
                            return replace(page, next_offset=18)
                        updates = {"source": {"source_id": str(uuid4())},
                                   "epoch": {"source_epoch": str(uuid4())},
                                   "offset": {"offset": 17}, "hash": {"sha256": "a" * 64}}
                        return replace(page, chunks=(replace(chunk, **updates[failure]),))
                    return page
                with patch.object(CaptureSpool, "pending", changed), \
                        patch.object(self.app.archive, "receive", side_effect=AssertionError("invalid receive")), \
                        self.assertRaises(ValueError):
                    self.transfer()
                self.assertEqual(self.state()["host_archived_offset"], 0)
                self.assertIsNone(self.intent())
                self.assertTrue(all(p is not None for p in self.payloads()))

    def test_legacy_intents_upgrade_and_replay_before_and_after_saved_receipt(self):
        for stage in ("before_receipt", "saved_receipt", "acked", "reclaimed"):
            with self.subTest(stage=stage):
                self.new_terminal()
                payloads = [b"earlier host object", b"legacy pending", b"new", b" range"]
                for payload in payloads:
                    self.append(payload)
                before = self.ledger()
                host = self.app.archive.register(self.agent_context, self.binding, self.source,
                                                self.key[1], self.metadata)
                with CaptureSpool(self.database) as spool:
                    chunks = spool.pending(*self.key).chunks
                    prior = self.app.archive.receive(self.agent_context, self.binding, chunks[0])
                    spool.acknowledge(*self.key, prior.host_archived_offset)
                    spool.reclaim(*self.key)
                    legacy = chunks[1]
                    receipt = None
                    if stage != "before_receipt":
                        receipt = self.app.archive.receive(self.agent_context, self.binding, legacy)
                        if stage in {"acked", "reclaimed"}:
                            spool.acknowledge(*self.key, receipt.host_archived_offset)
                        if stage == "reclaimed":
                            spool.reclaim(*self.key)
                # Genuine pre-D50 table and row; no new manifest was ever stored.
                with connect(self.database) as local, local:
                    local.execute("""CREATE TABLE terminal_archive_transfers (
                        source_id TEXT NOT NULL, source_epoch TEXT NOT NULL,
                        binding TEXT NOT NULL, offset INTEGER NOT NULL CHECK(offset >= 0),
                        length INTEGER NOT NULL CHECK(length BETWEEN 1 AND 262144),
                        sha256 TEXT NOT NULL CHECK(length(sha256)=64),
                        recovery_epoch TEXT NOT NULL, receipt TEXT,
                        PRIMARY KEY(source_id,source_epoch),
                        FOREIGN KEY(source_id,source_epoch) REFERENCES capture_epochs(source_id,source_epoch)
                    )""")
                    local.execute("INSERT INTO terminal_archive_transfers VALUES (?,?,?,?,?,?,?,?)",
                        (*self.key, json.dumps(asdict(self.binding)), legacy.offset, legacy.length,
                         legacy.sha256, host.recovery_epoch, receipt.model_dump_json() if receipt else None))
                self.restart()
                receive, received, manifests = self.app.archive.receive, [], []
                def checked(*args):
                    received.append(args[2])
                    manifests.append(self.intent()["chunks_json"])
                    return receive(*args)
                with patch.object(terminal_archive, "monotonic", return_value=0.0), \
                        patch.object(self.app.archive, "receive", side_effect=checked):
                    self.transfer()
                if stage == "before_receipt":
                    self.assertEqual(received[0], legacy)
                    self.assertIsNone(manifests[0])
                else:
                    self.assertEqual(len(received), 1)
                self.assertEqual(received[-1].payload, b"new range")
                self.assertEqual(json.loads(manifests[-1]), {"version": 1, "chunks": before[2:]})
                self.assertEqual(self.host_bytes(), b"".join(payloads))
                self.assertEqual(len(self.host_chunks()), 3)
                self.assertEqual(self.ledger(), before)
                self.assertEqual(self.payloads(), [None] * 4)
                self.assertEqual(self.state()["host_archived_offset"], sum(map(len, payloads)))
                self.assertIsNone(self.intent())
                with connect(self.database) as local:
                    columns = [dict(r) for r in local.execute("PRAGMA table_info(terminal_archive_transfers)")]
                    column, = [c for c in columns if c["name"] == "chunks_json"]
                    self.assertEqual((column["type"], column["notnull"], column["dflt_value"]), ("TEXT", 0, None))

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
