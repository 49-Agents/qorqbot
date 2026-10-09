from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from qorqut_core.transcripts.spool import (
    CHUNK_BYTES, BindingConflict, CaptureBlocked, CaptureSpool, CoordinateConflict,
    InvalidAcknowledgment, Source, ensure_schema,
)
from support import child_env, new_id, raw, registered


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "shared.sqlite3"
        self.spool, self.source, self.epoch, self.run = registered(self.path)
        self.addCleanup(self.spool.close)
        self.sid = self.source.source_id

    def test_schema_coexists_with_runner_and_bounds_lock_waits(self):
        with sqlite3.connect(self.path, isolation_level=None) as connection:
            connection.execute("PRAGMA user_version=91")
            connection.execute("CREATE TABLE runner_fixture (value TEXT)")
            connection.execute("INSERT INTO runner_fixture VALUES ('retained')")
            ensure_schema(connection, busy_timeout_ms=35)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 91)
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 35)
            self.assertEqual(connection.execute("SELECT value FROM runner_fixture").fetchone()[0], "retained")
            with CaptureSpool(self.path, busy_timeout_ms=35) as writer:
                connection.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                with self.assertRaises(CaptureBlocked):
                    writer.append(self.sid, self.epoch, 0, b"cannot commit")
                self.assertLess(time.monotonic() - started, 1)
                self.assertIsNotNone(writer.last_error)
                connection.execute("ROLLBACK")
                with self.assertRaises(CaptureBlocked):
                    writer.append(self.sid, self.epoch, 0, b"still blocked")
        self.assertEqual(raw(self.path, self.sid, self.epoch), b"")

    def test_giant_and_partial_records_stream_beyond_eight_mib(self):
        giant = b'{"tool":"' + (b"0123456789" * (1024 * 1024)) + b'"}\n'
        partial = b'{"partial":"\xf0\x9f'
        cursor = self.spool.append(self.sid, self.epoch, 0, giant)
        cursor = self.spool.append(self.sid, self.epoch, cursor, partial)
        self.assertGreater(cursor, 8 * 1024 * 1024)
        self.assertEqual(raw(self.path, self.sid, self.epoch), giant + partial)
        after = 0
        collected = bytearray()
        while True:
            page = self.spool.pending(self.sid, self.epoch, after=after, limit=3)
            if not page.chunks:
                break
            for chunk in page.chunks:
                self.assertEqual(chunk.offset, len(collected))
                self.assertLessEqual(chunk.length, CHUNK_BYTES)
                self.assertEqual(hashlib.sha256(chunk.payload).hexdigest(), chunk.sha256)
                collected.extend(chunk.payload)
            after = page.next_offset
        self.assertEqual(collected, giant + partial)
        self.assertEqual(self.spool.state(self.sid, self.epoch)["host_archived_offset"], 0)
        self.assertEqual(self.spool.reclaim(self.sid, self.epoch), 0)
        self.assertEqual(self.spool.pending(self.sid, self.epoch).chunks[0].offset, 0)
        with CaptureSpool(self.path) as reopened:
            self.assertEqual(reopened.state(self.sid, self.epoch)["local_captured_offset"], cursor)
            self.assertEqual(reopened.append(self.sid, self.epoch, 0, giant), cursor)
            self.assertEqual(reopened.append(self.sid, self.epoch, len(giant), partial), cursor)

    def test_existing_store_gains_retained_index_without_changing_receipts(self):
        self.spool.append(self.sid, self.epoch, 0, b"first")
        self.spool.append(self.sid, self.epoch, 5, b"second")
        self.spool.acknowledge(self.sid, self.epoch, 5)
        self.spool.reclaim(self.sid, self.epoch)
        connection = self.spool._connection
        connection.execute("DROP INDEX capture_chunks_retained")
        before = [tuple(row) for row in connection.execute("SELECT * FROM capture_chunks ORDER BY offset")]
        state = self.spool.state(self.sid, self.epoch)
        with CaptureSpool(self.path) as reopened:
            self.assertEqual([tuple(row) for row in reopened._connection.execute(
                "SELECT * FROM capture_chunks ORDER BY offset")], before)
            self.assertEqual(reopened.state(self.sid, self.epoch), state)
            self.assertEqual(reopened._connection.execute(
                "SELECT count(*) FROM capture_chunks INDEXED BY capture_chunks_retained "
                "WHERE payload IS NOT NULL").fetchone()[0], 1)
            self.assertEqual(reopened.pending(self.sid, self.epoch).chunks[0].payload, b"second")

    def test_interior_cursors_keep_whole_chunks_and_partial_ack_cannot_reclaim(self):
        payloads = (b"first", b"second", b"third")
        cursor = 0
        for payload in payloads:
            cursor = self.spool.append(self.sid, self.epoch, cursor, payload)
        for after, expected, end in ((0, b"first", 5), (3, b"first", 5), (5, b"second", 11),
                                     (6, b"second", 11), (11, b"third", 16), (15, b"third", 16)):
            page = self.spool.pending(self.sid, self.epoch, after=after, limit=1)
            self.assertEqual([c.payload for c in page.chunks], [expected])
            self.assertEqual(page.next_offset, end)
        self.assertEqual(self.spool.pending(self.sid, self.epoch, after=16).chunks, ())
        self.spool.acknowledge(self.sid, self.epoch, 7)
        self.assertEqual(self.spool.reclaim(self.sid, self.epoch), 5)
        self.spool.acknowledge(self.sid, self.epoch, 9)
        self.spool.acknowledge(self.sid, self.epoch, 9)
        self.assertEqual(self.spool.reclaim(self.sid, self.epoch), 0)
        self.assertEqual([c.payload for c in self.spool.pending(self.sid, self.epoch, after=0).chunks],
                         [b"second", b"third"])
        self.spool.acknowledge(self.sid, self.epoch, 12)
        self.assertEqual(self.spool.reclaim(self.sid, self.epoch), 6)
        self.assertEqual(raw(self.path, self.sid, self.epoch), b"third")
        self.assertEqual(self.spool.append(self.sid, self.epoch, 5, b"second"), 16)
        # Deleting the overlapping predecessor must not authorize an ACK
        # through its missing suffix, even when the target has a later chunk.
        self.spool._connection.execute("DELETE FROM capture_chunks WHERE offset=11")
        with self.assertRaises(InvalidAcknowledgment):
            self.spool.acknowledge(self.sid, self.epoch, 16)
        self.assertEqual(self.spool.state(self.sid, self.epoch)["host_archived_offset"], 12)

    def test_relink_identity_parent_and_explicit_epoch_do_not_rewrite_history(self):
        original = "same native bytes 🧭".encode()
        self.spool.append(self.sid, self.epoch, 0, original)
        resumed = replace(self.run, run_id=new_id(), run_generation=2, recovery_epoch=new_id())
        self.spool.register_run(resumed)
        self.spool.link_run(self.sid, self.epoch, resumed.run_id)
        self.spool.link_run(self.sid, self.epoch, resumed.run_id)
        self.assertEqual(self.spool.append(self.sid, self.epoch, 0, original), len(original))
        with self.assertRaises(BindingConflict):
            self.spool.register_run(replace(self.run, boot_id=new_id()))
        foreign = replace(resumed, run_id=new_id(), agent_id=new_id())
        self.spool.register_run(foreign)
        with self.assertRaises(BindingConflict):
            self.spool.link_run(self.sid, self.epoch, foreign.run_id)
        child = Source(new_id(), self.run.installation_id, self.run.agent_id, "native",
                       "synthetic-child", self.sid)
        self.spool.register_source(child, new_id(), resumed.run_id, capability_evidence="synthetic child")
        with self.assertRaises(BindingConflict):
            self.spool.register_source(replace(child, source_id=new_id(), agent_id=foreign.agent_id),
                                       new_id(), foreign.run_id, capability_evidence="wrong parent")
        next_epoch = new_id()
        with self.assertRaises(BindingConflict):
            self.spool.register_source(self.source, next_epoch, resumed.run_id,
                                       capability_evidence="synthetic fixture")
        for _ in range(2):
            self.spool.begin_epoch(self.sid, next_epoch, previous_epoch=self.epoch,
                                   reason="observed replacement", run_id=resumed.run_id,
                                   capability_evidence="synthetic replacement", file_identity="dev:ino:2")
        self.spool.append(self.sid, next_epoch, 0, b"replaced")
        # An authenticated caller may still drain late bytes into the old lineage.
        self.spool.append(self.sid, self.epoch, len(original), b" late")
        self.assertEqual(raw(self.path, self.sid, self.epoch), original + b" late")
        self.assertEqual(raw(self.path, self.sid, next_epoch), b"replaced")
        with self.assertRaises(BindingConflict):
            self.spool.begin_epoch(self.sid, new_id(), previous_epoch=self.epoch,
                                   reason="stale", run_id=resumed.run_id, capability_evidence="fixture")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM capture_run_sources WHERE source_id=? AND source_epoch=?",
                (self.sid, self.epoch),
            ).fetchone()[0], 2)

    def test_conflict_evidence_and_receipts_survive_ack_and_reclamation(self):
        data = b"a" * (CHUNK_BYTES + 77)
        self.spool.append(self.sid, self.epoch, 0, data)
        with self.assertRaises(CoordinateConflict):
            self.spool.append(self.sid, self.epoch, 1, b"shifted")
        self.spool.acknowledge(self.sid, self.epoch, len(data))
        self.assertEqual(self.spool.reclaim(self.sid, self.epoch), len(data))
        self.assertEqual(self.spool.append(self.sid, self.epoch, 0, data), len(data))
        for _ in range(2):
            with self.assertRaises(CoordinateConflict):
                self.spool.append(self.sid, self.epoch, 0, b"b" * CHUNK_BYTES)
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute("SELECT offset, length, sha256, payload FROM capture_chunks ORDER BY offset").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertIsNone(rows[0][3])
            self.assertEqual(rows[0][2], hashlib.sha256(data[:CHUNK_BYTES]).hexdigest())
            self.assertEqual(connection.execute("SELECT count(*) FROM capture_conflicts").fetchone()[0], 2)
        self.assertEqual(self.spool.pending(self.sid, self.epoch).chunks, ())

    def test_ack_is_contiguous_monotonic_epoch_bound_and_not_a_page_cursor(self):
        self.spool.append(self.sid, self.epoch, 0, b"first")
        self.spool.append(self.sid, self.epoch, 5, b"second")
        page = self.spool.pending(self.sid, self.epoch, limit=1)
        self.assertEqual(page.next_offset, 5)
        self.assertEqual(self.spool.reclaim(self.sid, self.epoch), 0)
        for sid, epoch, end in ((new_id(), self.epoch, 5), (self.sid, new_id(), 5),
                                (self.sid, self.epoch, 12)):
            with self.assertRaises(InvalidAcknowledgment):
                self.spool.acknowledge(sid, epoch, end)
        self.spool.acknowledge(self.sid, self.epoch, 3)
        self.spool.acknowledge(self.sid, self.epoch, 3)
        self.assertEqual(self.spool.reclaim(self.sid, self.epoch), 0)
        self.assertEqual(self.spool.pending(self.sid, self.epoch).chunks[0].payload, b"first")
        with self.assertRaises(InvalidAcknowledgment):
            self.spool.acknowledge(self.sid, self.epoch, 2)
        self.spool.acknowledge(self.sid, self.epoch, 5)
        self.assertEqual(raw(self.path, self.sid, self.epoch), b"firstsecond")  # ACK commit alone retains bytes
        self.assertEqual(self.spool.reclaim(self.sid, self.epoch), 5)
        self.assertEqual(raw(self.path, self.sid, self.epoch), b"second")
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM capture_chunks WHERE offset=5")
        with self.assertRaises(InvalidAcknowledgment):
            self.spool.acknowledge(self.sid, self.epoch, 11)

    def test_sqlite_abort_cannot_advance_cursor_and_health_fails_closed(self):
        self.spool.append(self.sid, self.epoch, 0, b"committed")
        self.spool._connection.execute("""CREATE TRIGGER fail_cursor BEFORE UPDATE OF local_captured_offset
            ON capture_epochs BEGIN SELECT RAISE(ABORT, 'injected cursor failure'); END""")
        with self.assertRaises(CaptureBlocked):
            self.spool.append(self.sid, self.epoch, 9, b"not displayed")
        self.assertEqual(raw(self.path, self.sid, self.epoch), b"committed")
        state = self.spool.state(self.sid, self.epoch)
        self.assertEqual(state["local_captured_offset"], 9)
        self.assertEqual(state["health"], "blocked")
        self.assertEqual(state["last_error"], "IntegrityError: SQLITE_CONSTRAINT_TRIGGER")
        with self.assertRaises(CaptureBlocked):
            self.spool.require_healthy(self.sid, self.epoch)
        with CaptureSpool(self.path) as reopened:
            with self.assertRaises(CaptureBlocked):
                reopened.append(self.sid, self.epoch, 9, b"no implicit recovery")

    def test_pending_snapshot_survives_concurrent_ack_and_reclaim(self):
        self.spool.append(self.sid, self.epoch, 0, b"retained snapshot")
        with CaptureSpool(self.path) as other:
            real_epoch = self.spool._epoch
            def interleaved_epoch(sid, epoch):
                row = real_epoch(sid, epoch)
                other.acknowledge(sid, epoch, 17)
                other.reclaim(sid, epoch)
                return row
            self.spool._epoch = interleaved_epoch
            page = self.spool.pending(self.sid, self.epoch)
            self.assertEqual(page.chunks[0].payload, b"retained snapshot")
            self.spool._epoch = real_epoch
            self.assertEqual(self.spool.pending(self.sid, self.epoch).chunks, ())

    def test_sqlite_full_preserves_accepted_prefix_and_latches_capture_closed(self):
        self.spool.append(self.sid, self.epoch, 0, b"accepted prefix")
        connection = self.spool._connection
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        connection.execute(f"PRAGMA max_page_count={pages}")
        with self.assertRaises(CaptureBlocked) as failure:
            self.spool.append(self.sid, self.epoch, 15, b"x" * CHUNK_BYTES)
        self.assertEqual(failure.exception.__cause__.sqlite_errorcode, sqlite3.SQLITE_FULL)
        self.assertEqual(raw(self.path, self.sid, self.epoch), b"accepted prefix")
        self.assertEqual(self.spool.state(self.sid, self.epoch)["local_captured_offset"], 15)
        self.assertEqual(self.spool.last_error, "OperationalError: SQLITE_FULL")
        with self.assertRaises(CaptureBlocked):
            self.spool.require_healthy(self.sid, self.epoch)

    def test_process_crashes_at_sqlite_commit_ack_and_reclaim_boundaries(self):
        # Real abrupt process exit, not a mocked successful commit. The wrapper
        # surrounds only SQLite statement boundaries and never changes results.
        worker = r'''
import os, sys
from qorqut_core.transcripts.spool import CaptureSpool
path, sid, epoch, action, point = sys.argv[1:]
spool = CaptureSpool(path)
real = spool._connection
class CrashConnection:
    @property
    def in_transaction(self): return real.in_transaction
    def execute(self, sql, args=()):
        match = ((point == "after_chunk" and sql.startswith("INSERT INTO capture_chunks"))
            or (point == "after_cursor" and "SET local_captured_offset" in sql)
            or (point == "after_ack" and "SET host_archived_offset" in sql)
            or (point == "after_reclaim" and "SET payload=NULL" in sql)
            or (point in ("before_commit", "after_commit") and sql == "COMMIT"))
        if match and point == "before_commit": os._exit(83)
        result = real.execute(sql, args)
        if match: os._exit(83)
        return result
spool._connection = CrashConnection()
if action == "append": spool.append(sid, epoch, 6, b"new-bytes")
elif action == "ack": spool.acknowledge(sid, epoch, 6)
elif action == "reclaim": spool.reclaim(sid, epoch)
raise RuntimeError("fault did not fire")
'''
        cases = [("append", p) for p in ("after_chunk", "after_cursor", "before_commit", "after_commit")]
        cases += [("ack", p) for p in ("after_ack", "before_commit", "after_commit")]
        cases += [("reclaim", p) for p in ("after_reclaim", "before_commit", "after_commit")]
        for index, (action, point) in enumerate(cases):
            with self.subTest(action=action, point=point):
                path = Path(self.directory.name) / f"fault-{index}.sqlite3"
                spool, source, epoch, _ = registered(path)
                sid = source.source_id
                spool.append(sid, epoch, 0, b"saved!")
                if action == "reclaim":
                    spool.acknowledge(sid, epoch, 6)
                spool.close()
                process = subprocess.run([sys.executable, "-c", worker, str(path), sid, epoch, action, point],
                                         env=child_env(), capture_output=True, timeout=5)
                self.assertEqual(process.returncode, 83, process.stderr)
                with CaptureSpool(path) as recovered:
                    state = recovered.state(sid, epoch)
                    committed = point == "after_commit"
                    expected = b"saved!" + (b"new-bytes" if action == "append" and committed else b"")
                    if action == "reclaim" and committed:
                        expected = b""
                    self.assertEqual(raw(path, sid, epoch), expected)
                    self.assertEqual(state["local_captured_offset"], 15 if action == "append" and committed else 6)
                    self.assertEqual(state["host_archived_offset"],
                                     6 if action == "reclaim" or action == "ack" and committed else 0)
                    if action == "append":
                        recovered.append(sid, epoch, 6, b"new-bytes")
                        self.assertEqual(raw(path, sid, epoch), b"saved!new-bytes")
                    elif action == "ack":
                        recovered.acknowledge(sid, epoch, 6)
                        self.assertEqual(raw(path, sid, epoch), b"saved!")
                    else:
                        recovered.reclaim(sid, epoch)
                        self.assertEqual(recovered.append(sid, epoch, 0, b"saved!"), 6)
                        self.assertEqual(raw(path, sid, epoch), b"")
                    self.assertEqual(recovered._connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
