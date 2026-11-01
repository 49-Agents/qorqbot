"""Document version history and business activity on a caller-owned SQLite connection."""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MAX_PAGE_LIMIT = 200

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS qbdv_document_versions (
        namespace TEXT NOT NULL,
        document_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        content TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        author_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        reason TEXT NOT NULL,
        restored_from_version INTEGER,
        PRIMARY KEY (namespace, document_id, version)
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS qbdv_document_versions_no_update
    BEFORE UPDATE ON qbdv_document_versions
    BEGIN
        SELECT RAISE(ABORT, 'qbdv_document_versions rows are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS qbdv_document_versions_no_delete
    BEFORE DELETE ON qbdv_document_versions
    BEGIN
        SELECT RAISE(ABORT, 'qbdv_document_versions rows are immutable');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS qbdv_business_events (
        sequence INTEGER PRIMARY KEY,
        business_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        summary TEXT NOT NULL,
        subject_type TEXT NOT NULL,
        subject_id TEXT NOT NULL,
        data_json TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS qbdv_business_events_identity
    ON qbdv_business_events (business_id, event_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS qbdv_business_events_business_sequence
    ON qbdv_business_events (business_id, sequence)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS qbdv_business_events_no_update
    BEFORE UPDATE ON qbdv_business_events
    BEGIN
        SELECT RAISE(ABORT, 'qbdv_business_events rows are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS qbdv_business_events_no_delete
    BEFORE DELETE ON qbdv_business_events
    BEGIN
        SELECT RAISE(ABORT, 'qbdv_business_events rows are immutable');
    END
    """,
)


class HistoryError(Exception):
    """Base class for errors raised by this package."""


class TransactionRequired(HistoryError):
    """A schema or mutation call was made without an active caller-owned transaction."""


class NotFound(HistoryError, LookupError):
    """The requested document, version or event does not exist in its scope."""


class VersionConflict(HistoryError):
    """expected_version did not match the document's current version."""

    def __init__(self, namespace: str, document_id: str, expected_version: int, current_version: int):
        self.namespace = namespace
        self.document_id = document_id
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"document {document_id!r} in namespace {namespace!r} is at version "
            f"{current_version}, not expected version {expected_version}"
        )


class EventConflict(HistoryError):
    """An event with this (business_id, event_id) already exists with different contents."""

    def __init__(self, business_id: str, event_id: str, existing: BusinessEvent):
        self.business_id = business_id
        self.event_id = event_id
        self.existing = existing
        super().__init__(
            f"event {event_id!r} in business {business_id!r} already exists with different contents"
        )


@dataclass(frozen=True)
class DocumentVersionInfo:
    """Metadata of one saved document version (no content)."""

    namespace: str
    document_id: str
    version: int
    content_sha256: str
    author_id: str
    created_at: str
    reason: str
    restored_from_version: int | None


@dataclass(frozen=True)
class DocumentVersion:
    """One immutable full snapshot of a document."""

    namespace: str
    document_id: str
    version: int
    content: str
    content_sha256: str
    author_id: str
    created_at: str
    reason: str
    restored_from_version: int | None


@dataclass(frozen=True)
class VersionPage:
    """Newest-first page of version metadata; pass next_before_version to continue."""

    versions: tuple[DocumentVersionInfo, ...]
    next_before_version: int | None


@dataclass(frozen=True)
class BusinessEvent:
    """One immutable business activity fact."""

    sequence: int
    business_id: str
    event_id: str
    kind: str
    actor_id: str
    summary: str
    subject_type: str
    subject_id: str
    data: dict[str, Any]
    occurred_at: str
    recorded_at: str


@dataclass(frozen=True)
class EventPage:
    """Newest-appended-first page of events; pass next_before_sequence to continue."""

    events: tuple[BusinessEvent, ...]
    next_before_sequence: int | None


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create the package tables, indexes and triggers if absent.

    Repeatable. Must run inside the caller's active transaction and is committed
    or rolled back with it.
    """
    _require_transaction(connection)
    cursor = connection.cursor()
    try:
        for statement in _SCHEMA:
            cursor.execute(statement)
    finally:
        cursor.close()


class HistoryStore:
    """Document versions and business events stored through a caller-owned connection.

    Construction performs no database access. Mutations require an active
    transaction that the caller commits or rolls back.
    """

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a sqlite3.Connection")
        self._connection = connection

    # Documents

    def save_document(
        self,
        namespace: str,
        document_id: str,
        content: str,
        author_id: str,
        *,
        expected_version: int,
        reason: str = "",
    ) -> DocumentVersion:
        """Append a full snapshot as the next version of the document."""
        _require_transaction(self._connection)
        _require_name("namespace", namespace)
        _require_name("document_id", document_id)
        _require_text("content", content)
        _require_name("author_id", author_id)
        _require_version_number("expected_version", expected_version, minimum=0)
        _require_text("reason", reason)
        current = self._current_version(namespace, document_id)
        if expected_version != current:
            raise VersionConflict(namespace, document_id, expected_version, current)
        return self._append_version(namespace, document_id, current + 1, content, author_id, reason, None)

    def restore_document(
        self,
        namespace: str,
        document_id: str,
        source_version: int,
        author_id: str,
        *,
        expected_version: int,
        reason: str = "",
    ) -> DocumentVersion:
        """Append the content of source_version as the next version."""
        _require_transaction(self._connection)
        _require_name("namespace", namespace)
        _require_name("document_id", document_id)
        _require_version_number("source_version", source_version, minimum=1)
        _require_name("author_id", author_id)
        _require_version_number("expected_version", expected_version, minimum=0)
        _require_text("reason", reason)
        current = self._current_version(namespace, document_id)
        if expected_version != current:
            raise VersionConflict(namespace, document_id, expected_version, current)
        source = self.get_document(namespace, document_id, version=source_version)
        return self._append_version(
            namespace, document_id, current + 1, source.content, author_id, reason, source_version
        )

    def get_document(self, namespace: str, document_id: str, *, version: int | None = None) -> DocumentVersion:
        """Return the latest version, or the selected version when given."""
        _require_name("namespace", namespace)
        _require_name("document_id", document_id)
        columns = (
            "namespace, document_id, version, content, content_sha256, author_id, "
            "created_at, reason, restored_from_version"
        )
        if version is None:
            row = self._fetchone(
                f"SELECT {columns} FROM qbdv_document_versions WHERE namespace = ? AND document_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (namespace, document_id),
            )
        else:
            _require_version_number("version", version, minimum=1)
            row = self._fetchone(
                f"SELECT {columns} FROM qbdv_document_versions "
                "WHERE namespace = ? AND document_id = ? AND version = ?",
                (namespace, document_id, version),
            )
        if row is None:
            target = "document" if version is None else f"version {version} of document"
            raise NotFound(f"{target} {document_id!r} in namespace {namespace!r} not found")
        return DocumentVersion(*row)

    def list_versions(
        self,
        namespace: str,
        document_id: str,
        *,
        before_version: int | None = None,
        limit: int = 50,
    ) -> VersionPage:
        """Return newest-first version metadata, optionally below before_version (exclusive)."""
        _require_name("namespace", namespace)
        _require_name("document_id", document_id)
        _require_limit(limit)
        columns = (
            "namespace, document_id, version, content_sha256, author_id, "
            "created_at, reason, restored_from_version"
        )
        if before_version is None:
            rows = self._fetchall(
                f"SELECT {columns} FROM qbdv_document_versions WHERE namespace = ? AND document_id = ? "
                "ORDER BY version DESC LIMIT ?",
                (namespace, document_id, limit + 1),
            )
        else:
            _require_version_number("before_version", before_version, minimum=1)
            rows = self._fetchall(
                f"SELECT {columns} FROM qbdv_document_versions "
                "WHERE namespace = ? AND document_id = ? AND version < ? "
                "ORDER BY version DESC LIMIT ?",
                (namespace, document_id, before_version, limit + 1),
            )
        versions = tuple(DocumentVersionInfo(*row) for row in rows[:limit])
        next_before = versions[-1].version if len(rows) > limit else None
        return VersionPage(versions, next_before)

    def diff_document(self, namespace: str, document_id: str, from_version: int, to_version: int) -> str:
        """Return a unified diff between two saved versions; empty when contents are equal."""
        _require_version_number("from_version", from_version, minimum=1)
        _require_version_number("to_version", to_version, minimum=1)
        old = self.get_document(namespace, document_id, version=from_version)
        new = self.get_document(namespace, document_id, version=to_version)
        lines = difflib.unified_diff(
            _split_lines(old.content),
            _split_lines(new.content),
            fromfile=f"{namespace} {document_id} version {from_version}",
            tofile=f"{namespace} {document_id} version {to_version}",
        )
        output = []
        for line in lines:
            output.append(line)
            if not line.endswith("\n"):
                output.append("\n\\ No newline at end of file\n")
        return "".join(output)

    # Business events

    def append_event(
        self,
        business_id: str,
        event_id: str,
        kind: str,
        actor_id: str,
        summary: str,
        *,
        subject_type: str,
        subject_id: str,
        occurred_at: str,
        data: dict[str, Any] | None = None,
    ) -> BusinessEvent:
        """Record one business activity fact, or return the identical earlier record."""
        _require_transaction(self._connection)
        _require_name("business_id", business_id)
        _require_name("event_id", event_id)
        _require_name("kind", kind)
        _require_name("actor_id", actor_id)
        _require_text("summary", summary)
        _require_name("subject_type", subject_type)
        _require_name("subject_id", subject_id)
        occurred = _normalize_timestamp(occurred_at)
        data_json = _canonical_json(data)
        try:
            existing = self.get_event(business_id, event_id)
        except NotFound:
            existing = None
        if existing is not None:
            same = (
                existing.kind == kind
                and existing.actor_id == actor_id
                and existing.summary == summary
                and existing.subject_type == subject_type
                and existing.subject_id == subject_id
                and existing.occurred_at == occurred
                and _canonical_json(existing.data) == data_json
            )
            if not same:
                raise EventConflict(business_id, event_id, existing)
            return existing
        recorded = _utc_now()
        cursor = self._connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO qbdv_business_events (business_id, event_id, kind, actor_id, summary, "
                "subject_type, subject_id, data_json, occurred_at, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (business_id, event_id, kind, actor_id, summary, subject_type, subject_id,
                 data_json, occurred, recorded),
            )
            sequence = cursor.lastrowid
        finally:
            cursor.close()
        return BusinessEvent(
            sequence, business_id, event_id, kind, actor_id, summary, subject_type, subject_id,
            json.loads(data_json), occurred, recorded,
        )

    def get_event(self, business_id: str, event_id: str) -> BusinessEvent:
        """Return the event with this identity in this business."""
        _require_name("business_id", business_id)
        _require_name("event_id", event_id)
        row = self._fetchone(
            f"SELECT {_EVENT_COLUMNS} FROM qbdv_business_events WHERE business_id = ? AND event_id = ?",
            (business_id, event_id),
        )
        if row is None:
            raise NotFound(f"event {event_id!r} in business {business_id!r} not found")
        return _event_from_row(row)

    def list_events(self, business_id: str, *, before_sequence: int | None = None, limit: int = 50) -> EventPage:
        """Return the business's events newest-appended first, below before_sequence (exclusive)."""
        _require_name("business_id", business_id)
        _require_limit(limit)
        if before_sequence is None:
            rows = self._fetchall(
                f"SELECT {_EVENT_COLUMNS} FROM qbdv_business_events WHERE business_id = ? "
                "ORDER BY sequence DESC LIMIT ?",
                (business_id, limit + 1),
            )
        else:
            _require_version_number("before_sequence", before_sequence, minimum=1)
            rows = self._fetchall(
                f"SELECT {_EVENT_COLUMNS} FROM qbdv_business_events WHERE business_id = ? AND sequence < ? "
                "ORDER BY sequence DESC LIMIT ?",
                (business_id, before_sequence, limit + 1),
            )
        events = tuple(_event_from_row(row) for row in rows[:limit])
        next_before = events[-1].sequence if len(rows) > limit else None
        return EventPage(events, next_before)

    # Internals

    def _current_version(self, namespace: str, document_id: str) -> int:
        row = self._fetchone(
            "SELECT COALESCE(MAX(version), 0) FROM qbdv_document_versions WHERE namespace = ? AND document_id = ?",
            (namespace, document_id),
        )
        return row[0]

    def _append_version(
        self,
        namespace: str,
        document_id: str,
        version: int,
        content: str,
        author_id: str,
        reason: str,
        restored_from_version: int | None,
    ) -> DocumentVersion:
        record = DocumentVersion(
            namespace=namespace,
            document_id=document_id,
            version=version,
            content=content,
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            author_id=author_id,
            created_at=_utc_now(),
            reason=reason,
            restored_from_version=restored_from_version,
        )
        cursor = self._connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO qbdv_document_versions (namespace, document_id, version, content, content_sha256, "
                "author_id, created_at, reason, restored_from_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.namespace, record.document_id, record.version, record.content, record.content_sha256,
                 record.author_id, record.created_at, record.reason, record.restored_from_version),
            )
        finally:
            cursor.close()
        return record

    def _cursor(self) -> sqlite3.Cursor:
        # A per-cursor row_factory keeps plain tuples without touching the caller's connection setting.
        cursor = self._connection.cursor()
        cursor.row_factory = None
        return cursor

    def _fetchone(self, sql: str, params: tuple) -> tuple | None:
        cursor = self._cursor()
        try:
            return cursor.execute(sql, params).fetchone()
        finally:
            cursor.close()

    def _fetchall(self, sql: str, params: tuple) -> list[tuple]:
        cursor = self._cursor()
        try:
            return cursor.execute(sql, params).fetchall()
        finally:
            cursor.close()


_EVENT_COLUMNS = (
    "sequence, business_id, event_id, kind, actor_id, summary, subject_type, subject_id, "
    "data_json, occurred_at, recorded_at"
)


def _event_from_row(row: tuple) -> BusinessEvent:
    values = list(row)
    values[8] = json.loads(values[8])
    return BusinessEvent(*values)


def _require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise TransactionRequired(
            "an active caller-owned transaction is required; run BEGIN (BEGIN IMMEDIATE for writers) first"
        )


def _require_text(field: str, value: object) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    value.encode("utf-8")


def _require_name(field: str, value: object) -> None:
    _require_text(field, value)
    if not value.strip():
        raise ValueError(f"{field} must be a nonblank string")


def _require_version_number(field: str, value: object, *, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")


def _require_limit(limit: object) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if not 1 <= limit <= MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _normalize_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("occurred_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"occurred_at is not an ISO-8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("occurred_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _canonical_json(data: object) -> str:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TypeError("data must be a JSON object (dict) or None")
    _check_json_value(data, "data")
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    text.encode("utf-8")
    return text


def _check_json_value(value: object, path: str) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} has a non-string object key {key!r}")
            _check_json_value(item, f"{path}.{key}")
        return
    raise TypeError(f"{path} contains a non-JSON value of type {type(value).__name__}")


def _split_lines(text: str) -> list[str]:
    # Split only on "\n" so Unicode line separators stay inside their line.
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines
