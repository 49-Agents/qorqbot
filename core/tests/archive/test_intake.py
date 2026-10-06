from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from uuid import uuid4

import qorqut_core
from qorqut_core.app import Application, RecoveryActivation
from qorqut_core.contracts import CoreError
from qorqut_core.transcripts.archive import Archive
from test_foundation import InjectedCrash
from .support import ArchiveCase


class IntakeTests(ArchiveCase):
    def test_duplicate_returns_current_watermark_and_old_run_can_drain(self):
        self.register()
        first, second = self.chunk(b"first"), self.chunk(b"second", 5)
        before = self.counts()
        receipt = self.receive(first)
        self.assertEqual(receipt.binding, self.binding)
        self.assertEqual(receipt.host_archived_offset, 5)
        new = replace(self.binding, run_id=str(uuid4()), run_generation=9, environment_generation=4, boot_id=str(uuid4()))
        self.register(binding=new)
        counts = self.counts()
        self.receive(second)  # retired declared run is not a current execution fence
        self.assertEqual(self.receive(first).host_archived_offset, 11)
        self.assertEqual(self.receive(first, new).binding, new)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(counts, (before[0], before[1] + 1, before[2], before[3]))
        self.assertEqual(self.read().payload + self.read(5).payload, b"firstsecond")
        with self.assertRaises(CoreError) as not_start:
            self.read(1)
        self.assertEqual(not_start.exception.code, "not_found")
        before = self.inventory()
        with patch("os.fsync", side_effect=AssertionError("raw read fsync")), \
                patch("os.mkdir", side_effect=AssertionError("raw read mkdir")):
            self.assertEqual(self.read(), first)
            self.assertEqual(self.state().host_archived_offset, 11)
        self.assertEqual(self.inventory(), before)

    def test_conflicts_preserve_bytes_and_deduplicate_overlap_rechunk_and_payload(self):
        for conflicting in (self.chunk(b"evil", 0), self.chunk(b"bcd", 1), self.chunk(b"ab", 0)):
            with self.subTest(offset=conflicting.offset, payload=conflicting.payload):
                self.fresh_source()
                accepted = self.chunk(b"abcd")
                conflict = replace(conflicting, source_id=self.source.source_id, source_epoch=self.epoch)
                self.receive(accepted)
                before = self.counts()
                with self.assertRaises(CoreError) as error:
                    self.receive(conflict)
                self.assertEqual(error.exception.code, "idempotency_conflict")
                self.assertEqual(self.read(), accepted)
                self.assertEqual(self.object_path(conflict, conflict=True).read_bytes(), conflict.payload)
                self.assertTrue(self.state().conflicted)
                self.assertEqual(self.state().host_archived_offset, 4)
                self.assertEqual(self.counts(), (before[0], before[1] + 1, before[2], before[3]))
                saved = self.inventory()
                for attempt in (conflict, accepted, self.chunk(b"more", 4)):
                    with self.assertRaises(CoreError) as closed:
                        self.receive(attempt)
                    self.assertEqual(closed.exception.code, "idempotency_conflict")
                self.assertEqual(self.inventory(), saved)
                epoch = str(uuid4())
                metadata = self.metadata.model_copy(update={"previous_epoch": self.epoch, "divergence_reason": "Preserve divergence"})
                self.register(epoch=epoch, metadata=metadata)
                receipt = self.receive(self.chunk(conflict.payload, epoch=epoch))
                self.assertEqual(receipt.host_archived_offset, len(conflict.payload))
                self.assertTrue(self.state().conflicted)

    def test_conflict_storage_failure_and_uncommitted_orphan_replay(self):
        self.register()
        accepted, conflicting = self.chunk(b"original"), self.chunk(b"diverged")
        self.receive(accepted)
        before = self.inventory()
        self.fault.point = "archive_after_publish"
        with self.assertRaises(InjectedCrash):
            self.receive(conflicting)
        path = self.object_path(conflicting, conflict=True)
        self.assertTrue(path.exists())
        inode = path.stat().st_ino
        self.assertEqual(self.inventory(), before)
        self.assertFalse(self.state().conflicted)
        with self.assertRaises(CoreError) as conflict:
            self.receive(conflicting)
        self.assertEqual(conflict.exception.code, "idempotency_conflict")
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(self.read(), accepted)
        path.unlink()
        before = self.inventory()
        with self.assertRaises(CoreError) as missing:
            self.receive(conflicting)
        self.assertEqual(missing.exception.code, "storage_blocked")
        self.assertEqual(self.inventory(), before)
        self.assertFalse(path.exists())

    def test_each_durability_and_response_boundary_withholds_ack_until_replay(self):
        points = ("archive_before_stage_fsync", "archive_after_stage_fsync", "archive_before_publish",
            "archive_after_publish", "archive_before_parent_fsync", "archive_after_parent_fsync",
            "archive_before_finalize", "archive_after_receipt_insert", "archive_after_watermark",
            "archive_before_receipt_commit", "transaction_before_commit", "transaction_after_commit",
            "archive_after_receipt_commit", "archive_before_return")
        committed = {"transaction_after_commit", "archive_after_receipt_commit", "archive_before_return"}
        for point in points:
            with self.subTest(point=point):
                self.fresh_source()
                before = self.counts()
                self.fault.point = point
                with self.assertRaises(InjectedCrash):
                    self.receive()
                expected = self.chunk().length if point in committed else 0
                self.assertEqual(self.state().host_archived_offset, expected)
                path = self.object_path()
                inode = path.stat().st_ino if path.exists() else None
                receipt = self.receive()
                self.assertEqual(receipt.host_archived_offset, self.chunk().length)
                self.assertEqual(self.read(), self.chunk())
                self.assertEqual(self.counts(), before)
                if inode is not None:
                    self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual(len(self.rows("archive_chunks")), len(points))

    def test_real_fsync_and_publish_errors_never_advance_watermark(self):
        self.register()
        # Establish only reserved archive directories via a failed first stage.
        self.fault.point = "archive_before_stage_fsync"
        with self.assertRaises(InjectedCrash):
            self.receive()
        original = os.fsync
        for point in ("file", "directory"):
            with self.subTest(point=point):
                import stat
                def failing(fd):
                    is_file = stat.S_ISREG(os.fstat(fd).st_mode)
                    if is_file == (point == "file"):
                        raise OSError(errno.EIO, "synthetic fsync failure")
                    original(fd)
                with patch("os.fsync", side_effect=failing), self.assertRaises(CoreError) as error:
                    self.receive()
                self.assertEqual(error.exception.code, "storage_blocked")
                self.assertEqual(self.state().host_archived_offset, 0)
        with patch("qorqut_core.transcripts.archive.rename_noreplace", side_effect=CoreError("unsupported_capability", "synthetic unsupported primitive")), \
                self.assertRaises(CoreError) as error:
            self.receive()
        self.assertEqual(error.exception.code, "unsupported_capability")
        self.assertEqual(self.state().host_archived_offset, 0)
        self.assertEqual(self.receive().host_archived_offset, self.chunk().length)

    def test_missing_or_corrupt_committed_objects_are_never_replaced(self):
        for damage in ("missing", "bytes", "parent"):
            with self.subTest(damage=damage):
                self.fresh_source()
                self.receive()
                path = self.object_path()
                if damage == "missing":
                    path.unlink()
                elif damage == "bytes":
                    path.write_bytes(b"x" * self.chunk().length)
                else:
                    path.parent.rename(path.parent.with_name("owned-detached"))
                before = self.inventory()
                for action in (self.receive, self.read):
                    with self.assertRaises(CoreError) as error:
                        action()
                    self.assertIn(error.exception.code, ("storage_blocked", "uncertain_effect"))
                self.assertEqual(self.inventory(), before)
                if damage == "bytes":
                    self.assertEqual(path.read_bytes(), b"x" * self.chunk().length)
                else:
                    self.assertFalse(path.exists())

    def test_concurrent_intake_uses_shared_gate_without_holding_sql_during_io(self):
        self.register()
        entered, release = threading.Event(), threading.Event()
        def pause(point):
            if point == "archive_after_stage_fsync":
                entered.set()
                self.assertTrue(release.wait(5))
        self.app.db.fault = pause
        another = Archive(self.app.db, self.settings.data_root)
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(self.receive)
            try:
                self.assertTrue(entered.wait(3))
                duplicate = pool.submit(another.receive, self.context, self.binding, self.chunk())
                future = pool.submit(another.receive, self.context, self.binding, self.chunk(offset=2000))
                # This unrelated writer commits while the chunk is paused in I/O.
                updated = self.app.invoke("config_update", self.context, self.update_defaults())
                self.assertTrue(updated.ok, updated)
                self.assertFalse(duplicate.done())
                self.assertEqual(self.state().host_archived_offset, 0)
            finally:
                release.set()
            self.assertEqual(first.result(3), duplicate.result(3))
            with self.assertRaises(CoreError) as gap:
                future.result(3)
            self.assertEqual(gap.exception.code, "revision_conflict")
        self.assertEqual(len(self.rows("archive_chunks")), 1)
        self.assertEqual(self.receive().host_archived_offset, self.chunk().length)

    def test_restore_waits_for_intake_and_fences_ack_links_and_successors(self):
        self.register()
        entered, release = threading.Event(), threading.Event()
        def pause(point):
            if point == "archive_after_stage_fsync":
                entered.set()
                self.assertTrue(release.wait(5))
        self.app.db.fault = pause
        old = self.config()["recovery_epoch"]
        args = RecoveryActivation(idempotency_key="archive-restore", expected_installation_id=self.settings.installation_id,
                                  expected_recovery_epoch=old)
        with ThreadPoolExecutor(max_workers=2) as pool:
            work = pool.submit(self.receive)
            try:
                self.assertTrue(entered.wait(3))
                maintenance = pool.submit(self.app.activate_recovery, self.context, args)
                deadline = time.monotonic() + 3
                while self.app.db.lock._maintenance is None:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.005)
                self.assertFalse(maintenance.done())
            finally:
                release.set()
            receipt = work.result(3)
            result = maintenance.result(3)
        self.assertEqual(receipt.recovery_epoch, old)
        self.assertNotEqual(result.data["recovery_epoch"], old)
        state = self.state()
        self.assertTrue(state.recovery_held)
        self.assertEqual(state.registered_recovery_epoch, old)
        before = self.inventory()
        with patch("qorqut_core.transcripts.archive.directory", side_effect=AssertionError("held filesystem effects")):
            self.assertEqual(self.register(), state)
            for action in (lambda: self.receive(),
                           lambda: self.receive(self.chunk(b"late", self.chunk().length)),
                           lambda: self.register(binding=replace(self.binding, run_id=str(uuid4()))),
                           lambda: self.register(epoch=str(uuid4()), metadata=self.metadata.model_copy(
                               update={"previous_epoch": self.epoch, "divergence_reason": "restore cannot bypass hold"}))):
                with self.assertRaises(CoreError) as held:
                    action()
                self.assertEqual(held.exception.code, "not_ready")
        self.assertEqual(self.inventory(), before)
        self.assertEqual(self.read(), self.chunk())
        self.restart()
        self.assertTrue(self.state().recovery_held)
        self.assertEqual(self.rows("archive_runs")[0]["recovery_epoch"], self.binding.recovery_epoch)

    def test_owned_process_exit_before_and_after_sql_commit_replays_exact_origin(self):
        for point in ("archive_after_publish", "archive_after_receipt_commit"):
            with self.subTest(point=point):
                self.fresh_source()
                root = str(Path(qorqut_core.__file__).resolve().parent.parent)
                spec = {"binding": self.binding.__dict__, "chunk": {**self.chunk().__dict__, "payload": self.chunk().payload.hex()},
                        "actor_id": str(self.owner.actor_id)}
                fixture = self.root / "crash.json"
                fixture.write_text(json.dumps(spec))
                self.app.close()
                script = r'''
import json, os, sys
from pathlib import Path
import qorqut_core
assert Path(qorqut_core.__file__).resolve().parent.parent == Path(sys.argv[1])
print(qorqut_core.__file__, flush=True)
from qorqut_core.app import Application, load_settings
from qorqut_core.contracts import Invocation
from qorqut_core.transcripts.spool import Chunk, RunBinding
spec = json.loads(Path(sys.argv[3]).read_text())
spec["chunk"]["payload"] = bytes.fromhex(spec["chunk"]["payload"])
def crash(point):
    if point == sys.argv[4]: os._exit(73)
app = Application.open(load_settings(Path(sys.argv[2])), fault=crash)
app.archive.receive(Invocation(actor_id=spec["actor_id"]), RunBinding(**spec["binding"]), Chunk(**spec["chunk"]))
raise AssertionError("process did not exit at the owned boundary")
'''
                env = {"PATH": os.defpath, "LC_ALL": "C.UTF-8", "PYTHONPATH": root, "PYTHONDONTWRITEBYTECODE": "1"}
                try:
                    result = subprocess.run([sys.executable, "-c", script, root,
                        str(self.settings.data_root / "installation.json"), str(fixture), point],
                        cwd=self.root, env=env, capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 73, result.stderr)
                    self.assertEqual(result.stdout.strip(), str(Path(qorqut_core.__file__)))
                finally:
                    self.app = Application.open(self.settings, clock=self.clock, fault=self.fault)
                expected = self.chunk().length if point == "archive_after_receipt_commit" else 0
                self.assertEqual(self.state().host_archived_offset, expected)
                inode = self.object_path().stat().st_ino
                self.receive()
                self.assertEqual(self.object_path().stat().st_ino, inode)
                self.assertEqual(self.read(), self.chunk())
