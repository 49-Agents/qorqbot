"""Stream frozen daily ledger partitions through the foundation file receipts."""

import errno
import os
from contextlib import contextmanager
from uuid import UUID

from ..agents.registry import get_row
from ..agents.workspace import child_directory, same_child
from ..contracts import CoreError, JobProgress, JobResolution
from ..storage.database import assert_external_io_allowed
from ..storage.files import directory
from ..storage.jobs import JobHandler, after
from .ledger import entry_view, transcript_line
from .models import PublishJob

PAGE_SIZE = 32  # At most 32 bounded message bodies, never a whole-day buffer.
RETRY_SECONDS = 5


class Publisher:
    def __init__(self, application):
        self.app, self.db = application, application.db

    def handler(self):
        return JobHandler("conversation_publish", PublishJob, True, self.execute, self.inspect)

    def _coordinates(self, tx, job, claim=None):
        current = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        if claim is not None:
            current = self.app.jobs._current_claim(tx, claim)
        elif current is None or dict(current) != job:
            raise CoreError("target_changed", "Publication job changed during inspection")
        args = PublishJob.model_validate_json(current["args_json"])
        agent = get_row(tx, args.agent_id)
        installation = tx.connection.execute("SELECT * FROM installation").fetchone()
        if current["kind"] != "conversation_publish" or current["target_id"] != agent["id"]:
            raise CoreError("target_changed", "Publication job agent coordinates disagree")
        entry = tx.connection.execute("""SELECT 1 FROM conversation_entries
            WHERE agent_id=? AND conversation_id=? AND recording_date=? AND conversation_seq=?""",
            (agent["id"], agent["conversation_id"], args.date, args.required_seq)).fetchone()
        publication = tx.connection.execute("SELECT * FROM conversation_publications WHERE agent_id=? AND recording_date=?",
            (agent["id"], args.date)).fetchone()
        if entry is None or publication is None:
            raise CoreError("target_changed", "Publication job ledger coordinates disagree")
        held = current["wait_reason"] == "recovery_review" or current["recovery_epoch"] != installation["recovery_epoch"]
        # A new message must not bypass an older held publication of this same
        # daily partition, even if no receipt had yet been attached at restore.
        held = held or tx.connection.execute("""SELECT 1 FROM jobs WHERE kind='conversation_publish'
            AND target_id=? AND wait_reason='recovery_review'
            AND json_extract(args_json,'$.date')=? AND json_extract(args_json,'$.required_seq')>? LIMIT 1""",
            (agent["id"], args.date, publication["through_seq"])).fetchone() is not None
        return args, dict(agent), dict(installation), dict(publication), held

    def hold_for_recovery(self, tx):
        tx.require_write()
        tx.connection.execute("""UPDATE conversation_publications SET error_code='uncertain_effect',
            error_message='Transcript publication is held for recovery review'
            WHERE EXISTS (SELECT 1 FROM conversation_entries e WHERE e.agent_id=conversation_publications.agent_id
                AND e.recording_date=conversation_publications.recording_date AND e.conversation_seq>conversation_publications.through_seq)""")

    def _snapshot(self, job, claim=None):
        with self.db.read() as tx:
            return self._coordinates(tx, job, claim)

    def _guard(self, job, claim=None):
        snapshot = self._snapshot(job, claim)
        if snapshot[-1]:
            raise CoreError("target_changed", "Transcript publication is held for recovery review")
        return snapshot

    @staticmethod
    def _identity(fd):
        info = os.fstat(fd)
        return info.st_dev, info.st_ino

    @contextmanager
    def _owned_directory(self, agent, installation, *, create, guard):
        """No workspace seeding: only transcripts/ below a complete owned agent."""
        root_path = self.app.settings.data_root
        with directory(root_path) as root, child_directory(root, "agents") as parent:
            with child_directory(parent, agent["id"]) as owned:
                guard()
                self.app.workspaces._marker(owned, agent, installation, complete=True)
                same_child(root, "agents", parent)
                same_child(parent, agent["id"], owned)
                with child_directory(owned, "transcripts", create=create) as transcripts:
                    def verify():
                        guard()
                        with directory(root_path) as current:
                            if self._identity(root) != self._identity(current):
                                raise CoreError("target_changed", "Transcript installation parent changed")
                        same_child(root, "agents", parent)
                        same_child(parent, agent["id"], owned)
                        same_child(owned, "transcripts", transcripts)
                        self.app.workspaces._marker(owned, agent, installation, complete=True)
                    verify()
                    yield transcripts, verify
                    verify()

    def _wait(self, phase, *, error=None, held=False):
        return JobResolution(state="waiting", reason="recovery_review" if held else "retry_delay",
            next_attempt_at=after(self.db.clock(), 60 if held else RETRY_SECONDS),
            progress=JobProgress(phase=phase), error=error)

    def _complete(self, sequence):
        return JobResolution(state="completed", progress=JobProgress(phase="published", completed=sequence, total=sequence))

    def _pages(self, job, claim, args, target, verify):
        after_seq = 0
        while after_seq < target:
            verify()
            self.app.jobs.progress(claim, JobProgress(phase="staging", completed=after_seq, total=target))
            with self.db.read() as tx:
                self._coordinates(tx, job, claim)
                rows = tx.connection.execute("""SELECT * FROM conversation_entries WHERE agent_id=? AND recording_date=?
                    AND conversation_seq>? AND conversation_seq<=? ORDER BY conversation_seq LIMIT ?""",
                    (str(args.agent_id), args.date, after_seq, target, PAGE_SIZE)).fetchall()
            if not rows:
                raise CoreError("target_changed", "Frozen transcript partition has a missing ledger suffix")
            # Close the snapshot before every file write; renew between pages.
            for row in rows:
                yield transcript_line(entry_view(row))
            after_seq = rows[-1]["conversation_seq"]
            self.db.fault("conversation_after_page")
        verify()
        self.app.jobs.progress(claim, JobProgress(phase="staged", completed=target, total=target))

    def _pending(self, job, claim, args, publication, verify):
        receipt_id = UUID(publication["pending_receipt_id"])
        receipt = self.app.files.get(receipt_id)
        with self.db.read() as tx:
            self._coordinates(tx, job, claim)
            file_job = tx.connection.execute("SELECT * FROM jobs WHERE job_id=?", (receipt["job_id"],)).fetchone()
            epoch = tx.connection.execute("SELECT recovery_epoch FROM installation").fetchone()[0]
        if (receipt["target_path"] != f"agents/{args.agent_id}/transcripts/{args.date}.jsonl"
                or receipt["recovery_epoch"] != job["recovery_epoch"] or receipt["recovery_epoch"] != epoch
                or file_job is None or file_job["recovery_epoch"] != epoch or file_job["wait_reason"] == "recovery_review"):
            raise CoreError("target_changed", "Pending transcript receipt is held or has different coordinates")
        if file_job["state"] == "uncertain":
            verify()
            resolution = self.app.jobs.reconcile(UUID(file_job["job_id"]))
            if resolution.state != "completed":
                error = resolution.error or CoreError("uncertain_effect", "Transcript file receipt requires inspection").error
                raise CoreError(error.code, error.message, retryable=error.retryable)
            receipt = self.app.files.get(receipt_id)
        if receipt["state"] != "installed":
            if file_job["state"] in ("failed", "cancelled") or receipt["state"] == "conflicted":
                raise CoreError("uncertain_effect", "Transcript installation has not established the saved effect")
            return self._wait("file_install_pending")
        verify()
        # An installed receipt is evidence of a past effect. Verify its bytes
        # again before advancing this separate domain publication watermark.
        installed = self.app.files.install(receipt_id, inspection_only=True)
        verify()
        with self.db.transaction() as tx:
            _, _, _, current, held = self._coordinates(tx, job, claim)
            if held or current != publication:
                raise CoreError("target_changed", "Publication coordinates changed before finalization")
            self.app.files.finalize(tx, installed)
            tx.connection.execute("""UPDATE conversation_publications SET through_seq=?,sha256=?,length=?,
                pending_receipt_id=NULL,pending_target_seq=NULL,error_code=NULL,error_message=NULL
                WHERE agent_id=? AND recording_date=?""",
                (publication["pending_target_seq"], installed.sha256, installed.length, str(args.agent_id), args.date))
            self.db.fault("conversation_before_publication_commit")
        self.db.fault("conversation_after_publication_commit")
        return None

    def _perform(self, job, claim=None):
        args, agent, installation, publication, held = self._snapshot(job, claim)
        if held:
            return self._wait("recovery_held", held=True,
                error=CoreError("uncertain_effect", "Transcript publication is held for recovery review").error)
        if publication["pending_receipt_id"] is None and publication["through_seq"] >= args.required_seq:
            return self._complete(publication["through_seq"])
        if agent["workspace_initialized_at"] is None:
            return self._wait("workspace_required", error=CoreError("not_ready", "Transcript publication awaits the owned workspace", retryable=True).error)
        if claim is None and publication["pending_receipt_id"] is None:
            return self._wait("publication_required")
        guard = lambda: self._guard(job, claim)
        with self._owned_directory(agent, installation,
                create=publication["through_seq"] == 0 and publication["pending_receipt_id"] is None and claim is not None,
                guard=guard) as (parent, verify):
            if publication["pending_receipt_id"] is not None:
                pending = self._pending(job, claim, args, publication, verify)
                if pending is not None:
                    return pending
                _, _, _, publication, _ = guard()
            if publication["through_seq"] >= args.required_seq:
                return self._complete(publication["through_seq"])
            if claim is None:
                # No receipt means no known installed effect to adopt. Keep
                # unknown stage orphans; the next claim can safely stream anew.
                return self._wait("publication_required")
            with self.db.read() as tx:
                self._coordinates(tx, job, claim)
                target = tx.connection.execute("SELECT max(conversation_seq) FROM conversation_entries WHERE agent_id=? AND recording_date=?",
                    (str(args.agent_id), args.date)).fetchone()[0]
            staged = self.app.files.stage(f"agents/{args.agent_id}/transcripts/{args.date}.jsonl",
                self._pages(job, claim, args, target, verify), expected_sha256=publication["sha256"])
            verify()
            if (staged.parent_device, staged.parent_inode) != self._identity(parent):
                raise CoreError("target_changed", "Staging used a different transcript directory")
            with self.db.transaction() as tx:
                _, _, _, current, held = self._coordinates(tx, job, claim)
                if held or current != publication:
                    raise CoreError("target_changed", "Publication coordinates changed before receipt attachment")
                self.app.files.attach(tx, staged, operation_id=UUID(job["operation_id"]))
                tx.connection.execute("""UPDATE conversation_publications SET pending_receipt_id=?,pending_target_seq=?,
                    error_code=NULL,error_message=NULL WHERE agent_id=? AND recording_date=?""",
                    (str(staged.receipt_id), target, str(args.agent_id), args.date))
                self.db.fault("conversation_before_attach_commit")
            self.db.fault("conversation_after_attach_commit")
        return self._wait("file_install_pending")

    def _failure(self, job, claim, exc):
        if isinstance(exc, CoreError):
            error = exc.error
            cause = exc.__cause__
            if isinstance(cause, OSError) and cause.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                error = CoreError("uncertain_effect", "Transcript ownership or path requires inspection").error
        else:
            conflict = exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT)
            error = CoreError("uncertain_effect" if conflict else "storage_blocked",
                "Transcript ownership or path requires inspection" if conflict else "Transcript filesystem operation failed",
                retryable=not conflict).error
        with self.db.transaction() as tx:
            args, _, _, _, held = self._coordinates(tx, job, claim)
            if not held:
                tx.connection.execute("UPDATE conversation_publications SET error_code=?,error_message=? WHERE agent_id=? AND recording_date=?",
                    (error.code, error.message, str(args.agent_id), args.date))
        if held:
            return self._wait("recovery_held", held=True, error=error)
        if error.code in ("storage_blocked", "unavailable"):
            return self._wait("storage_retry", error=error)
        return JobResolution(state="uncertain", reason="reconciliation_required", next_attempt_at=after(self.db.clock(), 60),
            progress=JobProgress(phase="inspection_required"), error=error)

    def execute(self, claim):
        assert_external_io_allowed()
        with self.db.lock.use(), self.db.lock.effects:
            with self.db.read() as tx:
                job = dict(self.app.jobs._current_claim(tx, claim))
            try:
                return self._perform(job, claim)
            except (CoreError, OSError) as exc:
                return self._failure(job, claim, exc)

    def inspect(self, job):
        assert_external_io_allowed()
        with self.db.lock.use(), self.db.lock.effects:
            try:
                return self._perform(job)
            except (CoreError, OSError) as exc:
                return self._failure(job, None, exc)
