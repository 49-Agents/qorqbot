"""Three grouped D39 flows; real temporary native files, spool and host Archive."""

from dataclasses import FrozenInstanceError, asdict, replace
import hashlib
import json
import os
import sqlite3
from unittest.mock import patch
from uuid import uuid4

from archive.support import ArchiveCase
from test_foundation import InjectedCrash
from qorqut_core.adapters.discovery import discover
from qorqut_core.adapters.types import ManagedRoots, NativeBinding, RunBinding as AdapterRun
from qorqut_core.transcripts.native_capture import NativeCapture, NativeCaptureError
from qorqut_core.transcripts.spool import CHUNK_BYTES, CaptureSpool


class NativeCaptureTests(ArchiveCase):
    def setUp(self):
        super().setUp()
        self.roots = ManagedRoots(home=self.root / "native-home", workspace=self.root / "native-workspace")
        self.roots.home.mkdir()
        self.roots.workspace.mkdir()
        self.runner_db = self.root / "runner.sqlite3"
        # Share a real initialized runner capture file; do not borrow host SQL.
        with CaptureSpool(self.runner_db):
            pass
        self.capture = NativeCapture(self.runner_db, self.roots)

    def tearDown(self):
        self.capture.close()
        super().tearDown()

    def reopen_capture(self):
        self.capture.close()
        self.capture = NativeCapture(self.runner_db, self.roots)

    def native(self, suffix=b"", provider="codex"):
        self.provider, self.native_id = provider, uuid4()
        if provider == "codex":
            relative = f".codex/sessions/2026/10/08/rollout-2026-10-08T12-00-00-{self.native_id}.jsonl"
            record = {"type": "session_meta", "payload": {"id": str(self.native_id), "source": "cli"}}
        else:
            relative = f".claude/projects/synthetic/{self.native_id}.jsonl"
            record = {"type": "user", "sessionId": str(self.native_id), "isSidechain": False}
        self.header = json.dumps(record).encode() + b"\n"
        self.native_path = self.roots.home / relative
        self.native_path.parent.mkdir(parents=True, exist_ok=True)
        self.native_path.write_bytes(self.header + suffix)
        return self.observation()

    def observation(self, run=None):
        run = run or self.binding
        adapter = AdapterRun(**{key: value for key, value in asdict(run).items() if key != "installation_id"},
                             conversation_id=self.agent["conversation_id"])
        result = discover(self.provider, "0.160.0" if self.provider == "codex" else "2.1.287",
                          self.roots, NativeBinding(run=adapter, native_session_id=self.native_id))
        self.assertEqual(len(result.sources), 1, result.gaps)
        return result.sources[0]

    def local_bytes(self, registration):
        with sqlite3.connect(self.runner_db) as db:
            rows = db.execute("SELECT payload FROM capture_chunks WHERE source_id=? AND source_epoch=? ORDER BY offset",
                              (registration.source_id, registration.source_epoch))
            return b"".join(row[0] for row in rows if row[0] is not None)

    def host_bytes(self, registration):
        offset, parts = 0, []
        end = self.app.archive.state(self.context, registration.source_id, registration.source_epoch).host_archived_offset
        while offset < end:
            chunk = self.app.archive.read_chunk(self.context, registration.source_id, registration.source_epoch, offset)
            self.assertLessEqual(chunk.length, CHUNK_BYTES)
            parts.append(chunk.payload)
            offset += chunk.length
        return b"".join(parts)

    def drain(self, registration, expected):
        for _ in range(len(expected) // 1048576 + 2):
            state = self.capture.poll(registration.source_id)
            self.assertIsNone(state.gap)
            if state.local_captured_offset == len(expected):
                return
        self.fail("bounded polls did not capture the finite fixture")

    def transfer_all(self, registration):
        for _ in range(30):
            state = self.capture.transfer(registration.source_id, self.app.archive, self.context)
            if state.host_archived_offset == state.local_captured_offset:
                return
        self.fail("bounded transfers did not archive the finite fixture")

    def test_append_partial_giant_reopen_registration_intent_and_resume(self):
        observation = self.native(b'{"partial":"\xf0\x9f')
        # Crash after saving IDs, before and after the idempotent spool effect.
        for after in (False, True):
            if after:
                observation = self.native(b'{"partial":"\xf0\x9f', provider="claude")
            real = self.capture.spool.register_source

            def interrupted(*args, **kwargs):
                if after:
                    real(*args, **kwargs)
                raise InjectedCrash("registration")

            with patch.object(self.capture.spool, "register_source", side_effect=interrupted), self.assertRaises(InjectedCrash):
                self.capture.register(observation, self.binding)
            registration = self.capture.list_registered()[-1]
            self.reopen_capture()
            self.assertEqual(self.capture.register(observation, self.binding), registration)
            self.assertEqual(self.capture.state(registration.source_id).local_captured_offset, 0)
        with self.assertRaises(FrozenInstanceError):
            registration.source_epoch = str(uuid4())
        original = self.native_path.read_bytes()
        state = self.capture.poll(registration.source_id, max_bytes=len(original))
        self.assertEqual(state.local_captured_offset, len(original))
        self.assertEqual(self.local_bytes(registration), original)
        checkpoint = self.capture.checkpoint(registration.source_id)
        self.assertEqual(checkpoint.proof.prefix_sha256, hashlib.sha256(original).hexdigest())
        # Complete the split UTF-8 character and append a >8 MiB line plus a tail.
        suffix = b'\x8c\xbf"}\n{"giant":"' + b"x" * (9 * 1024 * 1024) + b'"}\n{"tail":'
        with self.native_path.open("ab") as stream:
            stream.write(suffix)
        expected = original + suffix
        self.reopen_capture()
        resumed = replace(self.binding, run_id=str(uuid4()), run_generation=2)
        self.assertEqual(self.capture.register(self.observation(resumed), resumed), registration)
        self.assertEqual(self.capture.state(registration.source_id).checkpoint, checkpoint)
        self.drain(registration, expected)
        self.assertEqual(self.local_bytes(registration), expected)
        self.assertEqual(self.capture.state(registration.source_id).runs, (self.binding, resumed))
        before = self.capture._catalog.total_changes
        self.capture.list_registered()
        self.capture.state(registration.source_id)
        self.assertEqual(self.capture._catalog.total_changes, before)
        self.transfer_all(registration)
        self.assertEqual(self.host_bytes(registration), expected)
        # Prefix verification uses retained receipts after all payload reclamation.
        self.reopen_capture()
        self.assertEqual(self.capture.checkpoint(registration.source_id).proof.prefix_sha256, hashlib.sha256(expected).hexdigest())
        self.assertEqual(self.local_bytes(registration), b"")
        self.assertEqual(len([r for r in self.rows("archive_run_sources") if r["source_id"] == registration.source_id]), 2)
        for invalid in (observation.model_copy(update={"path": self.root / "outside"}),
                        observation.model_copy(update={"ownership": ()})):
            with self.assertRaisesRegex(NativeCaptureError, "^invalid_native_observation$"):
                self.capture.register(invalid, self.binding)

    def test_truncation_prefix_ownership_inode_divergence_and_finite_checkpoint(self):
        for mode in ("truncation", "prefix", "ownership", "replacement"):
            with self.subTest(mode=mode):
                observation = self.native(b"old body\n")
                registration = self.capture.register(observation, self.binding)
                old = self.native_path.read_bytes()
                self.capture.poll(registration.source_id)
                saved = self.capture.checkpoint(registration.source_id)
                if mode == "replacement":
                    # Retained fd drains late data, but cannot hide the lost path.
                    writer = os.open(self.native_path, os.O_WRONLY | os.O_APPEND)
                    try:
                        self.native_path.unlink()
                        os.write(writer, b"late")
                        state = self.capture.poll(registration.source_id, max_bytes=2)
                        self.assertEqual((state.gap, state.local_captured_offset), ("source_missing", len(old) + 2))
                        self.native_path.write_bytes(self.header + b"new body\n")
                        state = self.capture.poll(registration.source_id, max_bytes=2)
                        self.assertEqual((state.gap, state.local_captured_offset), ("identity_changed", len(old) + 4))
                        old += b"late"
                        finite = self.capture.checkpoint(registration.source_id)
                        self.assertEqual(finite.gap, "identity_changed")
                        self.assertEqual(finite.proof.prefix_sha256, hashlib.sha256(old).hexdigest())
                        self.reopen_capture()  # Old inode cannot be reopened by its replaced name.
                    finally:
                        os.close(writer)
                    reason = "identity_changed"
                else:
                    replacement = {"truncation": self.header, "prefix": self.header + b"new body\n",
                                   "ownership": self.header.replace(b'"cli"', b'"cli" ') + b"old body\n"}[mode]
                    self.native_path.write_bytes(replacement)
                    reason = {"truncation": "prefix_shorter", "prefix": "prefix_mismatch", "ownership": "ownership_changed"}[mode]
                state = self.capture.poll(registration.source_id)
                self.assertEqual(state.gap, reason)
                self.assertTrue(state.observation_required)
                self.assertEqual(self.local_bytes(registration), old)
                self.assertEqual(state.source_epoch, registration.source_epoch)
                if mode != "replacement":
                    self.assertEqual(state.checkpoint, saved)
                    with self.assertRaisesRegex(NativeCaptureError, "fresh_native_observation_required"):
                        self.capture.checkpoint(registration.source_id)
                fresh = self.capture.register(self.observation(), self.binding)
                self.assertEqual(fresh.source_id, registration.source_id)
                self.assertNotEqual(fresh.source_epoch, registration.source_epoch)
                self.assertEqual(self.capture.state(fresh.source_id).divergence_reason, reason)
                new = self.native_path.read_bytes()
                self.capture.poll(fresh.source_id, max_bytes=17)
                checkpoint = self.capture.checkpoint(fresh.source_id)
                self.assertEqual(checkpoint.proof.through_offset, 17)
                self.assertEqual(checkpoint.proof.prefix_sha256, hashlib.sha256(new[:17]).hexdigest())
                self.drain(fresh, new)
                self.assertEqual(self.capture.state(fresh.source_id).checkpoint, checkpoint)
                self.transfer_all(fresh)
                self.assertEqual(self.host_bytes(registration), old)
                self.assertEqual(self.host_bytes(fresh), new)
                self.reopen_capture()
                self.assertEqual(self.capture.state(registration.source_id, registration.source_epoch).host_archived_offset, len(old))

    def test_local_archive_lost_receipt_ack_reclaim_and_exact_receipt_fencing(self):
        observation = self.native(b"payload" * (CHUNK_BYTES // 2))
        registration = self.capture.register(observation, self.binding)
        expected = self.native_path.read_bytes()
        self.drain(registration, expected)
        sid, epoch = registration.source_id, registration.source_epoch
        self.fault.point = "archive_after_receipt_commit"
        with self.assertRaises(InjectedCrash):
            self.capture.transfer(sid, self.app.archive, self.context)
        self.assertEqual(self.capture.state(sid).host_archived_offset, 0)
        self.assertTrue(self.capture.state(sid).transfer_pending)
        self.assertEqual(self.app.archive.state(self.context, sid, epoch).host_archived_offset, CHUNK_BYTES)
        self.reopen_capture()
        self.restart()
        receive = self.app.archive.receive
        bad_fields = (
            {"binding": replace(self.binding, run_id=str(uuid4()))},
            {"source_id": str(uuid4())}, {"source_epoch": str(uuid4())},
            {"host_archived_offset": CHUNK_BYTES + 1}, {"recovery_epoch": str(uuid4())},
        )
        for changes in bad_fields:
            with self.subTest(fields=tuple(changes)):
                def bad_receipt(*args):
                    return receive(*args).model_copy(update=changes)
                with patch.object(self.app.archive, "receive", side_effect=bad_receipt), \
                        self.assertRaisesRegex(NativeCaptureError, "^archive_receipt_mismatch$"):
                    self.capture.transfer(sid, self.app.archive, self.context)
                self.assertEqual(self.capture.state(sid).host_archived_offset, 0)
                self.assertEqual(self.local_bytes(registration), expected)
        with patch.object(self.capture.spool, "acknowledge", side_effect=InjectedCrash("before_ack")), self.assertRaises(InjectedCrash):
            self.capture.transfer(sid, self.app.archive, self.context)
        self.reopen_capture()
        acknowledge = self.capture.spool.acknowledge

        def after_ack(*args):
            acknowledge(*args)
            raise InjectedCrash("after_ack")

        with patch.object(self.capture.spool, "acknowledge", side_effect=after_ack), self.assertRaises(InjectedCrash):
            self.capture.transfer(sid, self.app.archive, self.context)
        self.assertEqual(self.capture.state(sid).host_archived_offset, CHUNK_BYTES)
        self.assertEqual(self.local_bytes(registration), expected)
        self.reopen_capture()
        reclaim = self.capture.spool.reclaim

        def after_reclaim(*args):
            reclaim(*args)
            raise InjectedCrash("after_reclaim")

        with patch.object(self.capture.spool, "reclaim", side_effect=after_reclaim), \
                patch.object(self.app.archive, "receive", side_effect=AssertionError("already ACKed")), self.assertRaises(InjectedCrash):
            self.capture.transfer(sid, self.app.archive, self.context)
        self.assertEqual(self.local_bytes(registration), expected[CHUNK_BYTES:])
        self.reopen_capture()
        self.transfer_all(registration)
        self.assertEqual(self.host_bytes(registration), expected)
        self.assertEqual(self.local_bytes(registration), b"")
        self.assertFalse(self.capture.state(sid).transfer_pending)
        self.assertIsNone(self.capture.state(sid).last_error)
        self.assertEqual(self.capture.state(sid).host_archived_offset, len(expected))
        self.assertEqual(self.capture.spool.receipts(sid, epoch).through_offset, len(expected))
        self.assertEqual(len(self.rows("archive_chunks")), (len(expected) + CHUNK_BYTES - 1) // CHUNK_BYTES)
