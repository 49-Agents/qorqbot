"""The append-only archive lifecycle: capture a bound session's transcript and index its replies, with Chat closed.

This module reuses ``qorqut.native_archive`` as it is. It never discovers agents, sessions, providers or transcripts: the
caller hands it a registered profile and a verified durable session binding, and it archives exactly that binding's source.

One ``step`` is bounded. It captures the source only when the file changed (or a final capture is due), then indexes a bounded
batch of records from the saved cursor in one transaction with the completions it found. A larger backlog continues on the next
step from the same cursor, so a restart, a crash between the archive and the database, or a rebuild resumes without
re-notifying or skipping a reply.

A step that finds nothing to record writes nothing. The existing capture row is read without a write transaction, the proposed
cursor state is computed outside any write unit, and a write unit opens only when that state differs from the stored row (a new
cursor, receipt, binding, error repair, completion or finalization); it then writes only the columns that actually differ.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import hashlib
import logging
import os
from pathlib import Path
import stat

from qorqut_operations.contracts import utc_now

from qorqut.native_archive import CaptureBinding, CaptureError, FinalCaptureEvidence, TranscriptArchive

from . import store
from .projection import Lineage, Projector, ProjectorState, PROVENANCE_VERSION
from .reader import proven_type
from .delivery_reconciliation import DeliveryReconciler

LOG = logging.getLogger(__name__)
BATCH_RECORDS = 20_000
BATCH_BYTES = 16 * 1024 * 1024
FINAL_ATTEMPTS = 5
# A failure that retrying cannot repair for this binding; the final capture is then reported unavailable rather than retried.
PERMANENT = frozenset({"source_unavailable", "source_replaced", "source_shrunk", "source_prefix_changed", "source_path_changed",
                       "session_mismatch", "workspace_mismatch", "agent_mismatch", "no_identity_evidence", "archive_integrity",
                       "unsafe_path", "archive_root_missing", "receipt_too_large", "final_evidence_conflict",
                       "final_evidence_mismatch", "not_found"})


@dataclass
class StepResult:
    captured: bool = False
    completions: int = 0
    more: bool = False
    error: str | None = None
    retryable: bool = False
    finalized: bool = False
    receipt_id: str | None = None


def _stamp(path: Path):
    """The source's identity and size right now (None when it is not a regular file); no content is read."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) if stat.S_ISREG(info.st_mode) else None


def _aware(text: str | None) -> datetime:
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
    except ValueError:
        value = None
    return value.astimezone(timezone.utc) if value and value.tzinfo else datetime.now(timezone.utc)


def _record_time(text: str | None, fallback: str) -> str:
    if isinstance(text, str):
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            return text
        except ValueError:
            pass
    return fallback


class Indexer:
    def __init__(self, runtime, *, archive_factory=None):
        self.runtime = runtime
        self._archive_factory = archive_factory
        self._seen: dict[tuple, tuple] = {}
        self._attempts: dict[tuple, int] = {}
        self._deliveries = DeliveryReconciler(runtime)

    @property
    def database(self):
        return self.runtime.database

    def archive(self, profile: dict) -> TranscriptArchive:
        """The agent's own archive root, selected by its registered identity and home scope, never by a stored path."""
        folder = profile.get("business_slug") if profile.get("home_business_id") else None
        root = str(self.runtime.paths.agent_transcripts(profile["agent_id"], business_folder=folder))
        return self._archive_factory(root) if self._archive_factory else TranscriptArchive(root)

    @staticmethod
    def binding(profile: dict, session: dict) -> CaptureBinding:
        return CaptureBinding(session["agent_id"], session["provider"], session["conversation_id"], session["generation"],
                              profile["workspace"], session["transcript_path"])

    def step(self, profile: dict, session: dict, *, final: bool = False) -> StepResult:
        key = (session["agent_id"], session["provider"], session["conversation_id"])
        archive = self.archive(profile)
        capture = self._cursor(session)
        result = StepResult()
        receipt = None
        if capture["receipt_id"]:
            try:
                receipt = archive.load_receipt(capture["receipt_id"])
            except (CaptureError, ValueError) as error:
                LOG.warning("Native capture receipt for %s is unavailable: %s", key[0], getattr(error, "code", error))
        stamp = _stamp(Path(session["transcript_path"]))
        if final or receipt is None or stamp != self._seen.get(key):
            try:
                receipt = self._capture(archive, profile, session, final)
            except CaptureError as error:
                if error.code == "no_identity_evidence":
                    # Content-dependent: nothing to retry until the file changes.
                    self._seen[key] = stamp
                return self._failed(capture, error.code, error.retryable, result)
            except ValueError:
                return self._failed(capture, "invalid_binding", False, result)
            self._seen[key] = stamp
            result.captured = True
        result.receipt_id = receipt.receipt_id
        try:
            self._index(archive, capture, session, receipt, final, result)
            self._deliveries.step(archive, session, receipt)
        except CaptureError as error:
            return self._failed(capture, error.code, error.retryable, result)
        retention = getattr(self.runtime, 'overseer_retention', None)
        if retention is not None:
            try:
                retention.queue_capture(profile, session, receipt)
            except Exception:
                # The source is already durable; the next observer pass retries the metadata event.
                LOG.warning('Overseer capture observation remains pending')
        return result

    # ----- archive ---------------------------------------------------------------------------------------------------

    def _capture(self, archive, profile, session, final):
        binding = self.binding(profile, session)
        evidence = None
        if final:
            # Exit evidence for exactly this generation: the process token is the verified native process, the time is when
            # the host recorded the end.
            evidence = FinalCaptureEvidence(session["agent_id"], session["provider"], session["conversation_id"],
                                            session["generation"], f"{session['process_pid']}:{session['process_started']}",
                                            _aware(session["ended_at"]))
        return archive.capture(binding, final_evidence=evidence)

    def _cursor(self, session: dict) -> dict:
        """The capture row of this binding's conversation. An existing row is read without a write transaction; a missing one is
        created by the guarded, idempotent write, which checks again that no other step created it in the meantime."""
        identity = (session["agent_id"], session["provider"], session["conversation_id"])
        with self.database.read() as uow:
            # Closed writes (maintenance) stop the step here, before anything is archived.
            self.runtime.application.ensure_writes_open(uow)
            capture = store.get_capture(uow.connection, *identity)
        if capture is None:
            with self.database.transaction(operation="native_chat.capture_cursor") as uow:
                self.runtime.application.ensure_writes_open(uow)
                capture = store.get_capture(uow.connection, *identity) or store.ensure_capture(uow.connection, session)
        return capture

    def _failed(self, capture, code, retryable, result):
        result.error, result.retryable = code, retryable
        if capture["error_code"] != code:
            with self.database.transaction(operation="native_chat.capture_error") as uow:
                fresh = store.get_capture(uow.connection, capture["agent_id"], capture["provider"], capture["conversation_id"])
                changes = store.capture_changes(fresh, error_code=code)
                if changes:
                    store.update_capture(uow.connection, fresh, **changes)
        return result

    # ----- reply index -----------------------------------------------------------------------------------------------

    def _index(self, archive, capture, session, receipt, final, result):
        start = capture["indexed_offset"]
        saved = json.loads(capture["index_state_json"])
        if start and saved.get("provenance_version") != PROVENANCE_VERSION:
            # An old cursor may already call a compaction summary user work.
            # Repair the in-flight state before admitting any new completion.
            self._refresh_provenance(archive, capture, session, receipt, saved, result)
            return
        projector = Projector(session["provider"], session["conversation_id"],
                              ProjectorState.load(saved), self._resolver(session))
        completions, consumed, count = [], start, 0
        if start < receipt.record_boundary:
            with archive.iter_records(receipt, offset=start) as records:
                for record in records:
                    if record.kind == "partial" or record.end > receipt.record_boundary:
                        break
                    outcome = projector.feed(record)
                    if outcome.completion:
                        completions.append(outcome.completion)
                    consumed = record.end
                    count += 1
                    if count >= BATCH_RECORDS or consumed - start >= BATCH_BYTES:
                        break
        result.more = consumed < receipt.record_boundary
        finalized = bool(final and receipt.final is not None and not result.more)
        state = json.dumps(projector.state.dump(), separators=(",", ":"))
        # A conversation resumed by a later binding is already finalized on its capture row; the binding's own end still has to be
        # recorded once.
        unmarked = finalized and session.get("final_captured_at") is None
        if not completions and not unmarked and not store.capture_changes(
                capture, **self._proposal(capture, session, receipt, consumed, state, finalized)):
            # Nothing to record: the row already holds exactly the state this step would write.
            result.finalized = finalized
            return
        with self.database.transaction(operation="native_chat.index") as uow:
            self.runtime.application.ensure_writes_open(uow)
            conn, now = uow.connection, utc_now()
            fresh = store.get_capture(conn, session["agent_id"], session["provider"], session["conversation_id"])
            if fresh["indexed_offset"] != start:
                # Another step advanced this cursor; nothing here is applied twice.
                result.more = True
                return
            lineage = Lineage(session["agent_id"], session["provider"], session["conversation_id"], receipt.archive_id)
            for completion in completions:
                row = store.insert_reply(conn, lineage=lineage, kind=completion.identity_kind, key=completion.key,
                                         session_id=session["id"], receipt_id=receipt.receipt_id, offset=completion.offset,
                                         length=completion.length, completed_at=_record_time(completion.timestamp, now),
                                         preview=completion.preview, origin=completion.origin, user_work=completion.user_work)
                result.completions += 1 if row else 0
                # Only the original timestamp on a newly indexed real reply is
                # activity. Observer time and system-only reminder turns are not.
                if row and completion.timestamp and (completion.origin == 'user' or completion.user_work):
                    try:
                        stamp = datetime.fromisoformat(completion.timestamp.replace('Z', '+00:00'))
                    except (ValueError, TypeError):
                        stamp = None
                    if stamp is not None and stamp.tzinfo is not None:
                        from qorqut.agents.attention import enrolled_board
                        if enrolled_board(uow, session['agent_id']):
                            from qorqut.agents.attention_delivery import record_attention_activity
                            key = hashlib.sha256((session['conversation_id'] + ':' + completion.identity_kind + ':'
                                                  + completion.key).encode()).hexdigest()
                            record_attention_activity(uow, session['agent_id'], 'agent_activity', stamp.isoformat(),
                                'native-reply:' + key, attention=self.runtime.application.ordubashy_attention)
            grew = receipt.sequence != fresh["receipt_sequence"] or consumed != start
            changes = store.capture_changes(fresh, **self._proposal(fresh, session, receipt, consumed, state, finalized))
            if changes:
                store.update_capture(conn, fresh, **changes)
            if finalized:
                store.mark_final_captured(conn, session["id"], now)
            if grew and (changes or result.completions):
                store.bump(conn, session["agent_id"])
            result.finalized = finalized

    def _refresh_provenance(self, archive, capture, session, receipt, saved, result):
        """Bounded, resumable replay of an old cursor's state, without replaying effects.

        The archive and immutable reply ledger stay intact. Replayed completions never
        send notifications, increment counters or record fresh owner activity.
        """
        replay = saved.get("compaction_replay") or {}
        start = replay.get("offset", 0)
        target = capture["indexed_offset"]
        projector = Projector(session["provider"], session["conversation_id"],
                              ProjectorState.load(replay.get("state")), self._resolver(session))
        consumed = start
        with archive.iter_records(receipt, offset=start) as records:
            for count, record in enumerate(records, 1):
                if record.kind == "partial" or record.end > min(target, receipt.record_boundary):
                    break
                projector.feed(record)
                consumed = record.end
                if count >= BATCH_RECORDS or consumed - start >= BATCH_BYTES or consumed == target:
                    break
        state = projector.state.dump()
        if consumed != target:
            state = {**saved, "compaction_replay": {"offset": consumed, "state": state}}
        with self.database.transaction(operation="native_chat.compaction_provenance") as uow:
            self.runtime.application.ensure_writes_open(uow)
            fresh = store.get_capture(uow.connection, session["agent_id"], session["provider"], session["conversation_id"])
            if fresh["indexed_offset"] == target and fresh["index_state_json"] == capture["index_state_json"]:
                changes = store.capture_changes(fresh, index_state_json=json.dumps(state, separators=(",", ":")))
                if changes:
                    store.update_capture(uow.connection, fresh, **changes)
        result.more = True

    @staticmethod
    def _proposal(row, session, receipt, consumed, state, finalized) -> dict:
        """The capture state this step would leave, given the stored ``row`` (a cursor and a length only move forward)."""
        return {"session_id": session["id"], "receipt_id": receipt.receipt_id,
                "receipt_sequence": max(receipt.sequence, row["receipt_sequence"]),
                "captured_length": max(receipt.length, row["captured_length"]), "indexed_offset": consumed,
                "index_state_json": state, "complete": 1 if receipt.complete else 0, "error_code": None,
                "finalized": 1 if finalized or row["finalized"] else 0}

    def _resolver(self, session: dict):
        """What proves a leading marker was typed by the application: an input receipt this very conversation holds, of the
        declared type, that reached delivery. A string that merely starts like a marker is whatever its author typed."""
        def resolve(marker: str, declared: str | None) -> str | None:
            with self.database.read() as uow:
                found = store.conversation_markers(uow.connection, session["agent_id"], session["provider"],
                                                   session["conversation_id"], [marker], session["transcript_path"])
            return proven_type(found.get(marker), declared)
        return resolve

    # ----- final capture ---------------------------------------------------------------------------------------------

    def give_up_final(self, session: dict, code: str) -> None:
        """A final capture that cannot succeed is reported on the capture row and the session stops being retried; the archive
        keeps the last committed prefix and the lineage is explicitly not finalized."""
        with self.database.transaction(operation="native_chat.final_unavailable") as uow:
            conn = uow.connection
            capture = store.ensure_capture(conn, session)
            store.update_capture(conn, capture, error_code=f"final_unavailable:{code}"[:120])
            store.mark_final_captured(conn, session["id"], utc_now())
            store.bump(conn, session["agent_id"])
        self._attempts.pop((session["id"],), None)

    def final_failed(self, session: dict, result: StepResult) -> bool:
        """Count a failed final attempt; ``True`` once it should be given up (a condition retrying cannot repair, or too many
        attempts at a transient one)."""
        key = (session["id"],)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        return result.error in PERMANENT or not result.retryable or self._attempts[key] >= FINAL_ATTEMPTS


__all__ = ["Indexer", "StepResult"]
