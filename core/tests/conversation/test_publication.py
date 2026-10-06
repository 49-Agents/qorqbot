import errno
import hashlib
import json
import os
from contextlib import ExitStack, contextmanager
from unittest.mock import patch
from uuid import UUID, uuid4

from qorqut_core.conversation.ledger import entry_view, transcript_line
from qorqut_core.contracts import CoreError
from qorqut_core.storage.database import assert_external_io_allowed
from test_foundation import InjectedCrash

from messaging.support import ChatCase


class Publication(ChatCase):
    def read_lines(self, path):
        raw = path.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        return [json.loads(line) for line in raw.split(b"\n")[:-1]]

    def until_pending(self, agent=None):
        agent = agent or self.a
        for _ in range(30):
            row = next((r for r in self.rows("conversation_publications") if r["agent_id"] == agent["id"]), None)
            if row and row["pending_receipt_id"]:
                return row
            self.assertIsNotNone(self.app.jobs.run_next(worker_id=uuid4()))
        self.fail("Publisher did not attach a receipt")

    def fail_marker_after_commit(self, key="committed-error"):
        sent = self.send(key)
        required_seq = sent.data["entries"][0]["conversation_seq"]
        date = self.clock()[:10]
        original_marker = self.app.workspaces._marker
        failures = []

        def marker(fd, row, installation, *, complete=False):
            publication = next((p for p in self.rows("conversation_publications")
                if p["agent_id"] == self.a["id"] and p["recording_date"] == date), None)
            if not failures and publication and publication["through_seq"] == required_seq:
                failures.append("marker_read_EIO")
                raise OSError(errno.EIO, "synthetic marker read failure after publication commit")
            return original_marker(fd, row, installation, complete=complete)

        with patch.object(self.app.workspaces, "_marker", side_effect=marker):
            for _ in range(30):
                if self.app.jobs.run_next(worker_id=uuid4()) is None:
                    self.clock.advance(5)
                if failures:
                    break
        self.assertEqual(failures, ["marker_read_EIO"])
        publication = next(p for p in self.rows("conversation_publications")
            if p["agent_id"] == self.a["id"] and p["recording_date"] == date)
        self.assertEqual(publication["through_seq"], required_seq)
        self.assertIsNone(publication["pending_receipt_id"])
        self.assertEqual(publication["error_code"], "storage_blocked")
        job = next(j for j in self.rows("jobs")
            if j["kind"] == "conversation_publish" and j["operation_id"] == str(sent.operation_id))
        self.assertEqual(job["wait_reason"], "retry_delay")
        return publication, UUID(job["job_id"])

    def test_canonical_full_entries_both_agents_onbash_dates_and_saved_observation(self):
        body = "\tExact Unicode 文🙂\nsecond\r\x00\x85"
        self.clock.advance(86400 - 1)
        first = self.send("before", body, sender=self.actor(self.a), recipient=self.onbash)
        date = self.clock()[:10]
        self.clock.advance(2)
        second = self.send("after", "after midnight", sender=self.actor(self.onbash), recipient=self.a)
        current = self.clock()
        # Synthetic delayed platform insertion: occurrence is old, ledger
        # recording is today. This is not a native-capture fixture or claim.
        times = iter(["2026-09-01T12:00:00.000000Z"])
        with patch.object(self.app.db, "clock", side_effect=lambda: next(times, current)):
            self.send("late", "late occurrence", recipient=self.a)
        self.assertFalse(self.path().exists())
        self.pump()
        for agent in (self.a, self.onbash):
            self.assertEqual(self.conversation(agent)["publication"], {"pending_entries": 0, "state": "current", "error": None})
            before = self.read_lines(self.path(agent, date))
            self.assertEqual(len(before), 1)
            self.assertEqual(before[0]["text"], body)
            self.assertEqual(before[0]["source"]["message_id"], first.data["message"]["message_id"])
            today = self.read_lines(self.path(agent))
            self.assertEqual(today[0]["source"]["message_id"], second.data["message"]["message_id"])
            for row in before + today:
                self.assertEqual(row["format_version"], 1)
                self.assertNotIn("delivery", row)
                self.assertNotIn("handling", row)
        today = self.read_lines(self.path())
        self.assertEqual(today[1]["occurred_at"], "2026-09-01T12:00:00.000000Z")
        self.assertEqual(today[1]["recorded_at"][:10], current[:10])
        raw = self.path().read_bytes()
        self.assertTrue(self.ack([second.data["message"]["message_id"]]).ok)
        self.pump()
        self.assertEqual(self.path().read_bytes(), raw)
        self.path().write_bytes(b"externally changed")
        # Read-only publication is the saved observation, not an integrity scan.
        self.assertEqual(self.conversation()["publication"]["state"], "current")
        self.send("detect-change")
        self.pump()
        self.assertEqual(self.path().read_bytes(), b"externally changed")
        self.assertEqual(self.conversation()["publication"]["state"], "blocked")

    def test_streaming_large_day_renews_lease_freezes_snapshot_and_coalesces_jobs(self):
        for i in range(130):
            self.send(str(i), "x" * 65536)
        pages = []
        original_fault = self.app.db.fault

        def page_boundary(point):
            assert_external_io_allowed() if point == "conversation_after_page" else None
            if point == "conversation_after_page":
                pages.append(point)
                self.clock.advance(20)
                if len(pages) == 1:
                    self.send("arrived-during-stage", "later committed entry")
            original_fault(point)

        with patch.object(self.app.db, "fault", side_effect=page_boundary), patch("qorqut_core.conversation.publication.PAGE_SIZE", 16):
            self.pump()
        self.assertGreater(len(pages), 9)
        self.assertGreater(self.path().stat().st_size, 8 * 1024 * 1024)
        rows = self.read_lines(self.path())
        self.assertEqual(len(rows), 131)
        self.assertEqual(len({r["entry_id"] for r in rows}), 131)
        self.assertEqual(rows[-1]["text"], "later committed entry")
        receipts = self.rows("file_receipts")
        self.assertEqual(len(receipts), 2)
        first = next(r for r in receipts if r["expected_sha256"] is None)
        second = next(r for r in receipts if r["expected_sha256"] is not None)
        self.assertEqual(second["expected_sha256"], first["sha256"])
        self.assertEqual(second["sha256"], hashlib.sha256(self.path().read_bytes()).hexdigest())
        self.assertEqual(self.conversation()["publication"]["pending_entries"], 0)

    def test_crash_receipt_boundaries_recover_without_another_message(self):
        points = ("file_before_stage_fsync", "file_after_stage_directory_fsync", "file_after_receipt_insert",
            "conversation_after_attach_commit", "file_after_rename", "file_before_receipt_commit",
            "file_after_receipt_commit", "conversation_before_publication_commit", "conversation_after_publication_commit")
        for index, point in enumerate(points):
            with self.subTest(point=point):
                self.clock.advance(86400)
                sent = self.send(str(index), "exact " + point)
                path = self.path()
                self.fault.point = point
                with self.assertRaises(InjectedCrash):
                    self.pump()
                self.recover()
                self.assertEqual(self.read_lines(path)[0]["source"]["message_id"], sent.data["message"]["message_id"])
                self.assertEqual(len(self.read_lines(path)), 1)
                self.assertEqual(self.conversation()["publication"]["state"], "current")
                self.assertEqual(len(self.rows("messages")), index + 1)
        self.assertTrue(list(path.parent.glob(".qorqut-stage-*")), "Unattached orphans remain under Files semantics")

    def test_transient_failure_survives_restart_and_retries_with_visible_lag(self):
        self.send()
        with patch.object(self.app.files, "stage", side_effect=OSError(errno.ENOSPC, "synthetic full disk")):
            for _ in range(5):
                self.app.jobs.run_next(worker_id=uuid4())
                if self.conversation()["publication"]["state"] == "blocked":
                    break
        publication = self.conversation()["publication"]
        self.assertEqual(publication["pending_entries"], 1)
        self.assertEqual(publication["error"]["code"], "storage_blocked")
        pending = next(r for r in self.rows("jobs") if r["kind"] == "conversation_publish")
        self.assertEqual(pending["wait_reason"], "retry_delay")
        self.assertGreater(pending["next_attempt_at"], self.clock())
        self.recover()
        self.assertEqual(len(self.rows("messages")), 1)
        self.assertEqual(self.conversation()["publication"]["state"], "current")
        self.assertEqual(len(self.read_lines(self.path())), 1)

    def test_committed_error_revalidates_without_new_message_execute_and_inspect(self):
        for index, mode in enumerate(("execute", "inspect")):
            with self.subTest(mode=mode):
                self.clock.advance(86400)
                publication, job_id = self.fail_marker_after_commit(mode)
                original_bytes, original_inode = self.path().read_bytes(), self.path().stat().st_ino
                receipt_count = len(self.rows("file_receipts"))
                self.assertEqual(self.conversation()["publication"]["pending_entries"], 0)
                self.assertEqual(self.conversation()["publication"]["state"], "blocked")
                self.restart()
                with patch.object(self.app.files, "stage", side_effect=AssertionError("Committed recovery must not stage")):
                    if mode == "execute":
                        self.pump()
                    else:
                        self.assertEqual(self.app.jobs.reconcile(job_id).state, "completed")
                self.assertEqual(self.path().read_bytes(), original_bytes)
                self.assertEqual(self.path().stat().st_ino, original_inode)
                self.assertEqual(len(self.rows("file_receipts")), receipt_count)
                saved = next(p for p in self.rows("conversation_publications")
                    if p["recording_date"] == publication["recording_date"])
                self.assertEqual(saved, {**publication, "error_code": None, "error_message": None})
                self.restart()
                self.assertEqual(self.conversation()["publication"], {"pending_entries": 0, "state": "current", "error": None})
                self.assertEqual(len(self.rows("messages")), index + 1)

    def test_committed_error_refuses_file_ownership_mismatches_and_recovery_hold(self):
        publication, job_id = self.fail_marker_after_commit()
        path = self.path()
        original_bytes, original_inode = path.read_bytes(), path.stat().st_ino
        marker = path.parent.parent / ".qorqut-owner.json"
        original_marker = marker.read_bytes()
        retained = path.with_suffix(".retained")
        self.restart()
        original_directory = self.app.publisher._owned_directory

        def exit_marker(*args, **kwargs):
            self.assertIsNotNone(self.rows("conversation_publications")[0]["error_code"])
            raise OSError(errno.EIO, "synthetic marker read failure on committed-inspection exit")

        @contextmanager
        def fail_exit(*args, **kwargs):
            with ExitStack() as contexts:
                yield contexts.enter_context(original_directory(*args, **kwargs))
                with patch.object(self.app.workspaces, "_marker", side_effect=exit_marker):
                    contexts.close()

        with patch.object(self.app.publisher, "_owned_directory", side_effect=fail_exit):
            self.assertEqual(self.app.jobs.reconcile(job_id).reason, "retry_delay")
        self.assertEqual(self.conversation()["publication"]["state"], "blocked")
        for mismatch in ("missing", "changed", "marker", "symlink"):
            with self.subTest(mismatch=mismatch):
                if mismatch in ("missing", "symlink"):
                    path.rename(retained)
                    if mismatch == "symlink":
                        path.symlink_to(retained)
                elif mismatch == "changed":
                    path.write_bytes(b"different committed bytes")
                else:
                    marker.write_bytes(b"{}")
                resolution = self.app.jobs.reconcile(job_id)
                self.assertEqual(resolution.state, "uncertain")
                self.assertEqual(self.conversation()["publication"]["state"], "blocked")
                saved = self.rows("conversation_publications")[0]
                self.assertEqual((saved["through_seq"], saved["sha256"], saved["length"]),
                    (publication["through_seq"], publication["sha256"], publication["length"]))
                self.assertEqual(len(self.rows("file_receipts")), 1)
                if mismatch in ("missing", "symlink"):
                    if mismatch == "symlink":
                        path.unlink()
                    retained.rename(path)
                elif mismatch == "changed":
                    path.write_bytes(original_bytes)
                else:
                    marker.write_bytes(original_marker)
        self.assertEqual(path.read_bytes(), original_bytes)
        self.assertEqual(path.stat().st_ino, original_inode)
        self.recovery()
        with patch("qorqut_core.conversation.publication.file_hash", side_effect=AssertionError("Held recovery must not inspect files")):
            self.assertEqual(self.app.jobs.reconcile(job_id).reason, "recovery_review")
        self.assertEqual(self.conversation()["publication"]["state"], "blocked")
        self.assertEqual(len(self.rows("messages")), 1)

    def test_transcript_parent_sync_is_retried_before_publication_becomes_current(self):
        self.send()
        parent_info = self.path().parent.parent.stat()
        parent_identity = (parent_info.st_dev, parent_info.st_ino)
        original_fsync, original_fault = os.fsync, self.app.db.fault
        events = []

        def fsync(fd):
            assert_external_io_allowed()
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == parent_identity and self.path().parent.exists():
                if not events:
                    events.append("parent_sync_failed")
                    raise OSError(errno.ENOSPC, "synthetic owning-parent sync failure after mkdir")
                original_fsync(fd)
                events.append("parent_synced")
                return
            original_fsync(fd)

        def fault(point):
            if point in ("file_before_stage_fsync", "conversation_after_publication_commit"):
                self.assertIn("parent_synced", events)
                events.append(point)
            original_fault(point)

        with patch("os.fsync", side_effect=fsync), patch.object(self.app.db, "fault", side_effect=fault):
            self.pump()
        self.assertEqual(events[0], "parent_sync_failed")
        self.assertLess(events.index("parent_synced"), events.index("file_before_stage_fsync"))
        self.assertLess(events.index("parent_synced"), events.index("conversation_after_publication_commit"))
        self.restart()
        self.assertEqual(self.conversation()["publication"], {"pending_entries": 0, "state": "current", "error": None})
        self.assertEqual(len(self.rows("messages")), 1)
        self.assertEqual(len(self.read_lines(self.path())), 1)

    def test_ownership_refusal_and_marker_change_after_attach_boundary(self):
        self.send()
        row = self.until_pending()
        marker = self.path().parent.parent / ".qorqut-owner.json"
        original = marker.read_bytes()
        persona = self.path().parent.parent / "workspace" / "persona.md"
        persona.write_bytes(b"Authored content must survive")
        marker.write_text("{}")
        self.pump()
        # Manager amendment: an attached receipt authorizes installation into
        # its pinned parent. A later marker change blocks domain advancement.
        self.assertTrue(self.path().exists())
        publication = self.rows("conversation_publications")[0]
        self.assertEqual(publication["pending_receipt_id"], row["pending_receipt_id"])
        self.assertEqual(publication["through_seq"], 0)
        self.assertEqual(self.conversation()["publication"]["state"], "blocked")
        self.assertEqual(persona.read_bytes(), b"Authored content must survive")
        marker.write_bytes(original)
        self.recover()
        self.assertEqual(self.conversation()["publication"]["state"], "current")
        self.clock.advance(86400)
        marker.write_text("{}")
        self.send("invalid-owner")
        self.pump()
        self.assertFalse(self.path().exists())
        self.assertEqual(self.conversation()["publication"]["state"], "blocked")

    def test_unexpected_target_symlink_special_file_and_parent_replacement(self):
        for agent, kind in ((self.a, "foreign"), (self.b, "fifo"), (self.onbash, "symlink")):
            path = self.path(agent)
            if kind == "symlink":
                outside = self.root / "outside"
                outside.mkdir()
                path.parent.symlink_to(outside, target_is_directory=True)
            else:
                path.parent.mkdir()
                if kind == "foreign":
                    path.write_bytes(b"not our transcript")
                else:
                    os.mkfifo(path)
            self.send(kind, recipient=agent)
        self.pump()
        self.assertEqual(self.path().read_bytes(), b"not our transcript")
        self.assertEqual(list((self.root / "outside").iterdir()), [])
        self.assertTrue(all(self.conversation(agent)["publication"]["state"] == "blocked" for agent in (self.a, self.b, self.onbash)))
        self.clock.advance(3600)
        self.assertIsNone(self.app.jobs.run_next(worker_id=uuid4()))

    def test_parent_replaced_during_stage_is_not_attached_or_overwritten(self):
        self.send()
        moved = self.path().parent.with_name("retained-transcripts")

        def swap(point):
            if point == "file_after_stage_fsync":
                self.path().parent.rename(moved)
                self.path().parent.mkdir()

        with patch.object(self.app.db, "fault", side_effect=swap):
            self.pump()
        self.assertEqual(self.rows("file_receipts"), [])
        self.assertFalse(self.path().exists())
        self.assertTrue(list(moved.glob(".qorqut-stage-*")))
        self.assertEqual(self.conversation()["publication"]["state"], "blocked")

    def test_first_publication_accepts_identical_content_without_rewriting(self):
        self.send()
        self.path().parent.mkdir()
        with self.app.db.read() as tx:
            row = tx.connection.execute("SELECT * FROM conversation_entries WHERE agent_id=?", (self.a["id"],)).fetchone()
            content = transcript_line(entry_view(row))
        self.path().write_bytes(content)
        identity = self.path().stat().st_ino
        self.pump()
        self.assertEqual(self.path().stat().st_ino, identity)
        self.assertEqual(self.path().read_bytes(), content)
        self.assertEqual(self.conversation()["publication"]["state"], "current")

    def test_attached_receipt_refuses_replaced_parent_and_stale_job_cannot_stage(self):
        self.send()
        self.until_pending()
        previous = self.path().parent.with_name("previous-transcripts")
        self.path().parent.rename(previous)
        self.path().parent.mkdir()
        self.pump()
        self.assertFalse(self.path().exists())
        self.assertFalse((previous / self.path().name).exists())
        self.assertEqual(self.conversation()["publication"]["state"], "blocked")
        self.send("other-agent", recipient=self.b)
        for _ in range(10):
            claim = self.app.jobs.claim(worker_id=uuid4())
            self.assertIsNotNone(claim)
            if claim.kind == "conversation_publish":
                break
            self.app.jobs.finish(claim, self.app.jobs.handlers[claim.kind].execute(claim))
        self.recovery()
        with self.assertRaises(CoreError) as error:
            self.app.publisher.execute(claim)
        self.assertEqual(error.exception.code, "target_changed")
        self.assertFalse(self.path(self.b).parent.exists())

    def test_expired_staging_lease_attaches_nothing_and_recovers(self):
        self.send()

        def expire(point):
            if point == "conversation_after_page":
                self.clock.advance(31)

        with patch.object(self.app.db, "fault", side_effect=expire), self.assertRaises(CoreError):
            self.pump()
        self.assertEqual(self.rows("file_receipts"), [])
        self.assertFalse(self.path().exists())
        self.recover()
        self.assertEqual(len(self.read_lines(self.path())), 1)
        self.assertEqual(self.conversation()["publication"]["state"], "current")

    def test_restore_holds_pending_receipt_delivery_and_new_job_for_same_partition(self):
        safe = self.send()
        unsafe = self.send("unsafe", "\x1b").data["message"]
        self.assertTrue(self.ack([safe.data["message"]["message_id"]]).ok)
        old_epoch = self.config()["recovery_epoch"]
        self.until_pending()
        self.recovery()
        for job in self.rows("jobs"):
            if job["wait_reason"] == "recovery_review" and job["kind"] in ("conversation_publish", "message_delivery"):
                self.assertEqual(self.app.jobs.reconcile(UUID(job["job_id"])).reason, "recovery_review")
        self.assertFalse(self.path().exists())
        delivery = self.app.invoke("deliveries_get", self.context, {"message_id": safe.data["message"]["message_id"]}).data["delivery"]
        self.assertEqual((delivery["state"], delivery["reason"]), ("deferred", "recovery_review"))
        self.assertEqual(self.app.invoke("deliveries_get", self.context, {"message_id": unsafe["message_id"]}).data["delivery"]["state"], "failed")
        self.assertEqual(len(self.rows("message_handling")), 1)
        events = [e for e in self.rows("events") if e["kind"] == "message.delivery_changed"]
        self.assertEqual(events[0]["actor_id"], str(self.owner.actor_id))
        for name in ("messages_list", "conversation_list"):
            self.assertEqual(self.app.invoke(name, self.context, {"agent_id": self.a["id"], "after_seq": 0,
                "recovery_epoch": old_epoch}).error.code, "target_changed")
        self.send("post-restore")
        self.pump()
        self.assertFalse(self.path().exists())
        self.assertEqual(self.conversation()["publication"]["pending_entries"], 3)
        self.assertEqual(self.conversation()["publication"]["state"], "blocked")

    def test_restore_before_receipt_cannot_be_bypassed_by_a_new_message(self):
        self.send()
        self.recovery()
        self.send("new-epoch")
        self.pump()
        self.assertFalse(self.path().exists())
        self.assertEqual(self.rows("file_receipts"), [])
        self.assertEqual(self.conversation()["publication"]["state"], "blocked")
