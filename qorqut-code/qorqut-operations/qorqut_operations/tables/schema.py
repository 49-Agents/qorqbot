"""Storage of the agent-maintained tables: Project and Global tables, their rows and the per-revision history captures.

``SCHEMA`` is the DDL of the three tables (data_tables, data_rows, table_versions) with their indexes, the change-feed
capture triggers, the Global-scope guards and the ``tables`` sequence seed, in exactly the statement order and text of the
installation's ``documents`` schema group. ``TABLES`` and ``IMMUTABLE_FIELDS`` are the repository
specifications. These are declarations only: the host assembles them into that same schema group, so the stored schema
fingerprint is unchanged. They are deliberately not a ``StorageContribution`` of their own.
"""

from __future__ import annotations

from qorqut_operations.storage.specs import TableSpec

TABLE_NAMES = ("data_tables", "data_rows", "table_versions")

SCHEMA = (
    """CREATE TABLE "data_tables" (
    business_id TEXT REFERENCES businesses(id),
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    columns_json TEXT NOT NULL CHECK(json_valid(columns_json)),
    tags_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(tags_json)),
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    created_by TEXT NOT NULL REFERENCES principals(id),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(business_id,id)
)""",
    """CREATE TABLE "data_rows" (
    business_id TEXT,
    id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK(position>=0),
    cells_json TEXT NOT NULL CHECK(json_valid(cells_json)),
    source_upload_id TEXT,
    created_by TEXT NOT NULL REFERENCES principals(id),
    updated_by TEXT NOT NULL REFERENCES principals(id),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(business_id IS NOT NULL OR source_upload_id IS NULL),
    PRIMARY KEY(business_id,id),
    FOREIGN KEY(business_id,table_id) REFERENCES data_tables(business_id,id),
    FOREIGN KEY(business_id,source_upload_id) REFERENCES documents(business_id,id)
)""",
    """CREATE TABLE "table_versions" (
    business_id TEXT,
    id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    table_revision INTEGER NOT NULL,
    sequence INTEGER NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES principals(id),
    operation TEXT NOT NULL,
    path TEXT NOT NULL,
    snapshot_json TEXT CHECK(snapshot_json IS NULL OR json_valid(snapshot_json)),
    snapshot_hash TEXT NOT NULL,
    git_commit TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(business_id,id),
    UNIQUE(business_id,table_id,table_revision),
    FOREIGN KEY(business_id,table_id) REFERENCES data_tables(business_id,id),
    CHECK(snapshot_json IS NOT NULL OR git_commit IS NOT NULL)
)""",
    """CREATE UNIQUE INDEX data_rows_table ON data_rows(business_id,table_id,position)""",
    """CREATE INDEX table_versions_pending ON table_versions(business_id,sequence) WHERE git_commit IS NULL""",
    """CREATE TRIGGER inbox_capture_data_rows_delete AFTER DELETE ON data_rows
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('data_rows','delete',inbox_actor(),json_object('business_id',OLD.business_id,'id',OLD.id,'table_id',OLD.table_id,'position',OLD.position,'cells_json',OLD.cells_json,'source_upload_id',OLD.source_upload_id,'updated_by',OLD.updated_by,'revision',OLD.revision),NULL); END""",
    """CREATE TRIGGER inbox_capture_data_rows_insert AFTER INSERT ON data_rows
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('data_rows','insert',inbox_actor(),NULL,json_object('business_id',NEW.business_id,'id',NEW.id,'table_id',NEW.table_id,'position',NEW.position,'cells_json',NEW.cells_json,'source_upload_id',NEW.source_upload_id,'updated_by',NEW.updated_by,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_data_rows_update AFTER UPDATE ON data_rows
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('data_rows','update',inbox_actor(),json_object('business_id',OLD.business_id,'id',OLD.id,'table_id',OLD.table_id,'position',OLD.position,'cells_json',OLD.cells_json,'source_upload_id',OLD.source_upload_id,'updated_by',OLD.updated_by,'revision',OLD.revision),json_object('business_id',NEW.business_id,'id',NEW.id,'table_id',NEW.table_id,'position',NEW.position,'cells_json',NEW.cells_json,'source_upload_id',NEW.source_upload_id,'updated_by',NEW.updated_by,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_data_tables_delete AFTER DELETE ON data_tables
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('data_tables','delete',inbox_actor(),json_object('business_id',OLD.business_id,'id',OLD.id,'title',OLD.title,'description',OLD.description,'columns_json',OLD.columns_json,'tags_json',OLD.tags_json,'archived',OLD.archived,'revision',OLD.revision),NULL); END""",
    """CREATE TRIGGER inbox_capture_data_tables_insert AFTER INSERT ON data_tables
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('data_tables','insert',inbox_actor(),NULL,json_object('business_id',NEW.business_id,'id',NEW.id,'title',NEW.title,'description',NEW.description,'columns_json',NEW.columns_json,'tags_json',NEW.tags_json,'archived',NEW.archived,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_data_tables_update AFTER UPDATE ON data_tables
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('data_tables','update',inbox_actor(),json_object('business_id',OLD.business_id,'id',OLD.id,'title',OLD.title,'description',OLD.description,'columns_json',OLD.columns_json,'tags_json',OLD.tags_json,'archived',OLD.archived,'revision',OLD.revision),json_object('business_id',NEW.business_id,'id',NEW.id,'title',NEW.title,'description',NEW.description,'columns_json',NEW.columns_json,'tags_json',NEW.tags_json,'archived',NEW.archived,'revision',NEW.revision)); END""",
    """CREATE UNIQUE INDEX data_tables_global_key ON data_tables(id) WHERE business_id IS NULL""",
    """CREATE UNIQUE INDEX data_rows_global_key ON data_rows(id) WHERE business_id IS NULL""",
    """CREATE UNIQUE INDEX data_rows_global_position ON data_rows(table_id,position) WHERE business_id IS NULL""",
    """CREATE UNIQUE INDEX table_versions_global_key ON table_versions(id) WHERE business_id IS NULL""",
    """CREATE UNIQUE INDEX table_versions_global_revision ON table_versions(table_id,table_revision) WHERE business_id IS NULL""",
    """CREATE TRIGGER data_rows_global_parent_insert BEFORE INSERT ON data_rows
WHEN NEW.business_id IS NULL AND NOT EXISTS (SELECT 1 FROM data_tables WHERE business_id IS NULL AND id=NEW.table_id)
BEGIN SELECT RAISE(ABORT,'Global row requires a Global table'); END""",
    """CREATE TRIGGER data_rows_global_parent_update BEFORE UPDATE ON data_rows
WHEN NEW.business_id IS NULL AND NOT EXISTS (SELECT 1 FROM data_tables WHERE business_id IS NULL AND id=NEW.table_id)
BEGIN SELECT RAISE(ABORT,'Global row requires a Global table'); END""",
    """CREATE TRIGGER table_versions_global_parent_insert BEFORE INSERT ON table_versions
WHEN NEW.business_id IS NULL AND NOT EXISTS (SELECT 1 FROM data_tables WHERE business_id IS NULL AND id=NEW.table_id)
BEGIN SELECT RAISE(ABORT,'Global table version requires a Global table'); END""",
    """CREATE TRIGGER table_versions_global_parent_update BEFORE UPDATE ON table_versions
WHEN NEW.business_id IS NULL AND NOT EXISTS (SELECT 1 FROM data_tables WHERE business_id IS NULL AND id=NEW.table_id)
BEGIN SELECT RAISE(ABORT,'Global table version requires a Global table'); END""",
    """CREATE TRIGGER data_tables_global_delete_guard BEFORE DELETE ON data_tables
WHEN OLD.business_id IS NULL AND (EXISTS (SELECT 1 FROM data_rows WHERE business_id IS NULL AND table_id=OLD.id)
    OR EXISTS (SELECT 1 FROM table_versions WHERE business_id IS NULL AND table_id=OLD.id))
BEGIN SELECT RAISE(ABORT,'Global table has retained rows or history'); END""",
    """CREATE TRIGGER data_tables_global_parent_update_guard BEFORE UPDATE OF id,business_id ON data_tables
WHEN OLD.business_id IS NULL AND (NEW.id IS NOT OLD.id OR NEW.business_id IS NOT NULL)
AND (EXISTS (SELECT 1 FROM data_rows WHERE business_id IS NULL AND table_id=OLD.id)
    OR EXISTS (SELECT 1 FROM table_versions WHERE business_id IS NULL AND table_id=OLD.id))
BEGIN SELECT RAISE(ABORT,'Global table has retained rows or history'); END""",
    """INSERT INTO change_sequences(name, value) VALUES ('tables', 0)""",
)

TABLES = {
    "data_tables": TableSpec(("business_id", "id"), "business_id"),
    "data_rows": TableSpec(("business_id", "id"), "business_id"),
    "table_versions": TableSpec(("business_id", "id"), "business_id", True, None),
}

IMMUTABLE_FIELDS = {
    "data_rows": frozenset({"table_id"}),
    "table_versions": frozenset({"table_id", "table_revision", "sequence", "actor_id", "operation", "path", "snapshot_hash"}),
}
