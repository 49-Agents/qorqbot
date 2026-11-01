"""The append-only archive lifecycle: capture a bound session's transcript and index its replies, with Chat closed.

This module reuses ``bazarlyk.native_archive`` as it is. It never discovers agents, sessions, providers or transcripts: the
caller hands it a registered profile and a verified durable session binding, and it archives exactly that binding's source.

One ``step`` is bounded. It captures the source only when the file changed (or a final capture is due), then indexes a bounded
batch of records from the saved cursor in one transaction with the completions it found. A larger backlog continues on the next
step from the same cursor, so a restart, a crash between the archive and the database, or a rebuild resumes without
re-notifying or skipping a reply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import stat

from qorqut_operations.contracts import utc_now

from bazarlyk.native_archive import CaptureBinding, CaptureError, FinalCaptureEvidence, TranscriptArchive

from . import store
from .projection import Lineage, Projector, ProjectorState
from .reader import proven_type

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
        with self.database.transaction(operation="native_chat.capture_cursor") as uow:
            self.runtime.application.ensure_writes_open(uow)
            capture = store.ensure_capture(uow.connection, session)
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
        except CaptureError as error:
            return self._failed(capture, error.code, error.retryable, result)
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

    def _failed(self, capture, code, retryable, result):
        result.error, result.retryable = code, retryable
        if capture["error_code"] != code:
            with self.database.transaction(operation="native_chat.capture_error") as uow:
                store.update_capture(uow.connection, capture, error_code=code)
        return result

    # ----- reply index -----------------------------------------------------------------------------------------------

    def _index(self, archive, capture, session, receipt, final, result):
        start = capture["indexed_offset"]
        projector = Projector(session["provider"], session["conversation_id"],
                              ProjectorState.load(json.loads(capture["index_state_json"])), self._resolver(session))
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
            grew = receipt.sequence != fresh["receipt_sequence"] or consumed != start
            store.update_capture(conn, fresh, session_id=session["id"], receipt_id=receipt.receipt_id,
                                 receipt_sequence=max(receipt.sequence, fresh["receipt_sequence"]),
                                 captured_length=max(receipt.length, fresh["captured_length"]), indexed_offset=consumed,
                                 index_state_json=json.dumps(projector.state.dump(), separators=(",", ":")),
                                 complete=1 if receipt.complete else 0, error_code=None,
                                 finalized=1 if finalized or fresh["finalized"] else 0)
            if finalized:
                store.mark_final_captured(conn, session["id"], now)
            if grew:
                store.bump(conn, session["agent_id"])
            result.finalized = finalized

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
