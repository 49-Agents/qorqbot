"""Storage of shared document history and business activity: the ``qorqut-business-docs-versioning`` tables.

The library owns these two tables and their immutability triggers; the statements are kept exactly as committed so the
schema fingerprint is unchanged, and nothing here creates or repairs them at run time (``validate_schema`` is the
read-only startup check). They have no repository specification: they are read and written only through the library's
``HistoryStore`` on the caller's unit-of-work connection.
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution

HISTORY_TABLES = ("qbdv_document_versions", "qbdv_business_events")

SCHEMA = (
    """CREATE TABLE qbdv_document_versions (
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
    )""",
    """CREATE TRIGGER qbdv_document_versions_no_update
    BEFORE UPDATE ON qbdv_document_versions
    BEGIN
        SELECT RAISE(ABORT, 'qbdv_document_versions rows are immutable');
    END""",
    """CREATE TRIGGER qbdv_document_versions_no_delete
    BEFORE DELETE ON qbdv_document_versions
    BEGIN
        SELECT RAISE(ABORT, 'qbdv_document_versions rows are immutable');
    END""",
    """CREATE TABLE qbdv_business_events (
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
    )""",
    """CREATE UNIQUE INDEX qbdv_business_events_identity
    ON qbdv_business_events (business_id, event_id)""",
    """CREATE INDEX qbdv_business_events_business_sequence
    ON qbdv_business_events (business_id, sequence)""",
    """CREATE TRIGGER qbdv_business_events_no_update
    BEFORE UPDATE ON qbdv_business_events
    BEGIN
        SELECT RAISE(ABORT, 'qbdv_business_events rows are immutable');
    END""",
    """CREATE TRIGGER qbdv_business_events_no_delete
    BEFORE DELETE ON qbdv_business_events
    BEGIN
        SELECT RAISE(ABORT, 'qbdv_business_events rows are immutable');
    END""",
)


def validate_schema(connection):
    """Read-only startup check; never creates or changes history tables."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not set(HISTORY_TABLES) <= tables:
        raise DomainError("invalid_database", "Existing database is missing shared history tables")


def storage_contribution() -> StorageContribution:
    return StorageContribution("document_history", schema=SCHEMA, validators=(validate_schema,))
