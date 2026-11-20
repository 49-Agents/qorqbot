"""The append-only audit of Tables and project documents moved between projects, beside ``task_moves``.

A Table or a document is identified across moves by its own id (a UUID, unique in every scope). Each row records where one
record was, where it went and under whose authority; it is written only by its move, in the same transaction, and is the
only input of the location resolvers that let a stale scoped reference find the record where it is now. It is never a
second store of the record. A document row also keeps both paths, since a document's file moves with it."""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "record-moves"
TABLE_MOVES = "table_moves"
DOCUMENT_MOVES = "document_moves"

SCHEMA = (
    """CREATE TABLE table_moves (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    move_id TEXT NOT NULL CHECK(length(move_id)=36),
    table_id TEXT NOT NULL,
    source_business_id TEXT NOT NULL REFERENCES businesses(id),
    destination_business_id TEXT NOT NULL REFERENCES businesses(id),
    revision_before INTEGER NOT NULL CHECK(revision_before >= 1),
    actor_id TEXT NOT NULL REFERENCES principals(id),
    moved_at TEXT NOT NULL,
    UNIQUE(move_id,table_id),
    CHECK(source_business_id IS NOT destination_business_id)
)""",
    """CREATE INDEX table_moves_table ON table_moves(table_id,seq)""",
    """CREATE TRIGGER table_moves_immutable_update BEFORE UPDATE ON table_moves
BEGIN SELECT RAISE(ABORT,'A recorded table move is immutable'); END""",
    """CREATE TRIGGER table_moves_keep BEFORE DELETE ON table_moves
BEGIN SELECT RAISE(ABORT,'A recorded table move is retained'); END""",
    """CREATE TABLE document_moves (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    move_id TEXT NOT NULL CHECK(length(move_id)=36),
    document_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_business_id TEXT NOT NULL REFERENCES businesses(id),
    destination_business_id TEXT NOT NULL REFERENCES businesses(id),
    source_path TEXT NOT NULL,
    destination_path TEXT NOT NULL,
    revision_before INTEGER NOT NULL CHECK(revision_before >= 1),
    actor_id TEXT NOT NULL REFERENCES principals(id),
    moved_at TEXT NOT NULL,
    UNIQUE(move_id,document_id),
    CHECK(source_business_id IS NOT destination_business_id)
)""",
    """CREATE INDEX document_moves_document ON document_moves(document_id,seq)""",
    """CREATE TRIGGER document_moves_immutable_update BEFORE UPDATE ON document_moves
BEGIN SELECT RAISE(ABORT,'A recorded document move is immutable'); END""",
    """CREATE TRIGGER document_moves_keep BEFORE DELETE ON document_moves
BEGIN SELECT RAISE(ABORT,'A recorded document move is retained'); END""",
)

TABLES = {TABLE_MOVES: TableSpec(("seq",), None, False, None), DOCUMENT_MOVES: TableSpec(("seq",), None, False, None)}


def storage_contribution() -> StorageContribution:
    return StorageContribution(OWNER, TABLES, schema=SCHEMA)


def moved_location(uow, kind, business_id, record_id):
    """The project that now holds the Table or document a stale ``(business_id, record_id)`` reference named, or ``None``.

    ``kind`` is ``table`` or ``document``. A record still in that scope is never redirected. Otherwise the recorded moves of
    that id are replayed from that scope; the last place must still hold the record (ids are UUIDs, so one id is one record)."""
    store, table, column = (("data_tables", TABLE_MOVES, "table_id") if kind == "table" else ("documents", DOCUMENT_MOVES, "document_id"))
    connection = uow.connection
    if connection.execute(f"SELECT 1 FROM {store} WHERE business_id IS ? AND id=?", (business_id, record_id)).fetchone():
        return None
    location, seen = business_id, False
    for move in connection.execute(f"SELECT source_business_id, destination_business_id FROM {table} WHERE {column}=? ORDER BY seq LIMIT 1001",
                                   (record_id,)):
        if move["source_business_id"] == location:
            location, seen = move["destination_business_id"], True
    if not seen or location == business_id:
        return None
    found = connection.execute(f"SELECT 1 FROM {store} WHERE business_id IS ? AND id=?", (location, record_id)).fetchone()
    return location if found else None


__all__ = ["DOCUMENT_MOVES", "OWNER", "SCHEMA", "TABLE_MOVES", "moved_location", "storage_contribution"]
