"""Storage of CRM memberships and the recorded activity observations, plus the Table-discussion scope guards.

``crm_entries`` is a membership, never a second person/customer store: a Table (``table_kind='crm'``) points at an
existing Task by its exact scoped identity (``record_business_id`` NULL is Global, ``record_namespace`` the normalized
``global`` / ``business:<id>``). The pair ``(table, Task)`` is unique, the target never changes, and removal is the soft
``removed`` flag so the same row (and its local cells and development history) comes back when the Task is added again.
``cells_json`` holds only local values; shared fields are read from the Task.

``crm_activity_events`` is the append-only record-owned store of explicitly recorded email, message and development
observations. A row is data: it never triggers a fetch, a send or an approval, and a correction is a new row naming the
earlier one.

A NULL ``business_id`` is the Global scope. Composite foreign keys are not enforced for NULL, so every Global parent
relationship has an explicit partial index or guard trigger, in the same way the Table and Task storage does it.

This DDL needs the discussion, Task and Table schemas (and ``inbox_changes``) created before it: its guards sit on
``thread_messages``, ``thread_follows``, ``tasks`` and ``data_tables``.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

TABLE_NAMES = ("crm_entries", "crm_activity_events")

SOURCE_LIMIT = 8000

SCHEMA = (
    """CREATE TABLE crm_entries (
    business_id TEXT REFERENCES businesses(id),
    id TEXT NOT NULL,
    table_id TEXT NOT NULL,
    record_task_id TEXT NOT NULL,
    record_business_id TEXT REFERENCES businesses(id),
    record_namespace TEXT NOT NULL CHECK((record_business_id IS NULL AND record_namespace='global')
        OR (record_business_id IS NOT NULL AND record_namespace='business:' || record_business_id)),
    position INTEGER NOT NULL CHECK(position>=0),
    stage_id TEXT,
    cells_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(cells_json)),
    removed INTEGER NOT NULL DEFAULT 0 CHECK(removed IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES principals(id),
    updated_by TEXT NOT NULL REFERENCES principals(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(business_id,id),
    FOREIGN KEY(business_id,table_id) REFERENCES data_tables(business_id,id),
    FOREIGN KEY(record_business_id,record_task_id) REFERENCES tasks(business_id,id)
)""",
    """CREATE UNIQUE INDEX crm_entries_global_key ON crm_entries(id) WHERE business_id IS NULL""",
    """CREATE UNIQUE INDEX crm_entries_record ON crm_entries(business_id,table_id,record_namespace,record_task_id)
WHERE business_id IS NOT NULL""",
    """CREATE UNIQUE INDEX crm_entries_global_record ON crm_entries(table_id,record_namespace,record_task_id) WHERE business_id IS NULL""",
    """CREATE UNIQUE INDEX crm_entries_position ON crm_entries(business_id,table_id,position) WHERE business_id IS NOT NULL""",
    """CREATE UNIQUE INDEX crm_entries_global_position ON crm_entries(table_id,position) WHERE business_id IS NULL""",
    """CREATE INDEX crm_entries_by_record ON crm_entries(record_namespace,record_task_id)""",
    # A membership belongs to a CRM table of the same scope (the composite key is not enforced for the NULL Global scope).
    """CREATE TRIGGER crm_entries_parent_insert BEFORE INSERT ON crm_entries
WHEN NOT EXISTS (SELECT 1 FROM data_tables WHERE business_id IS NEW.business_id AND id=NEW.table_id AND table_kind='crm')
BEGIN SELECT RAISE(ABORT,'A membership belongs to a CRM table in the same scope'); END""",
    """CREATE TRIGGER crm_entries_record_insert BEFORE INSERT ON crm_entries
WHEN NOT EXISTS (SELECT 1 FROM tasks WHERE business_id IS NEW.record_business_id AND id=NEW.record_task_id)
BEGIN SELECT RAISE(ABORT,'A membership names an existing Task in its own scope'); END""",
    """CREATE TRIGGER crm_entries_stage_insert BEFORE INSERT ON crm_entries
WHEN NEW.stage_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM data_tables t, json_each(t.crm_config_json,'$.stages') s
    WHERE t.business_id IS NEW.business_id AND t.id=NEW.table_id AND json_extract(s.value,'$.id')=NEW.stage_id)
BEGIN SELECT RAISE(ABORT,'A membership stage must be configured on its CRM'); END""",
    """CREATE TRIGGER crm_entries_stage_update BEFORE UPDATE OF stage_id ON crm_entries
WHEN NEW.stage_id IS NOT NULL AND NEW.stage_id IS NOT OLD.stage_id AND NOT EXISTS (
    SELECT 1 FROM data_tables t, json_each(t.crm_config_json,'$.stages') s
    WHERE t.business_id IS NEW.business_id AND t.id=NEW.table_id AND json_extract(s.value,'$.id')=NEW.stage_id)
BEGIN SELECT RAISE(ABORT,'A membership stage must be configured on its CRM'); END""",
    """CREATE TRIGGER crm_entries_identity BEFORE UPDATE ON crm_entries
WHEN NEW.id IS NOT OLD.id OR NEW.business_id IS NOT OLD.business_id OR NEW.table_id IS NOT OLD.table_id
  OR NEW.record_task_id IS NOT OLD.record_task_id OR NEW.record_business_id IS NOT OLD.record_business_id
  OR NEW.record_namespace IS NOT OLD.record_namespace OR NEW.created_by IS NOT OLD.created_by
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'A membership keeps its table, Task and creator; remove it and add the Task again to replace it'); END""",
    """CREATE TRIGGER crm_entries_keep BEFORE DELETE ON crm_entries
BEGIN SELECT RAISE(ABORT,'A membership is retained; remove it softly'); END""",
    # A stage that a membership (removed ones included) still names cannot leave the CRM's configuration.
    """CREATE TRIGGER data_tables_crm_stage_guard BEFORE UPDATE OF crm_config_json ON data_tables
WHEN OLD.table_kind='crm' AND EXISTS (
    SELECT 1 FROM crm_entries e WHERE e.business_id IS OLD.business_id AND e.table_id=OLD.id AND e.stage_id IS NOT NULL
    AND NOT EXISTS (SELECT 1 FROM json_each(NEW.crm_config_json,'$.stages') s WHERE json_extract(s.value,'$.id')=e.stage_id))
BEGIN SELECT RAISE(ABORT,'A referenced CRM stage cannot be removed'); END""",
    # A CRM with memberships or a discussion is part of the record and is never deleted.
    """CREATE TRIGGER data_tables_keep_crm BEFORE DELETE ON data_tables
WHEN EXISTS (SELECT 1 FROM crm_entries WHERE business_id IS OLD.business_id AND table_id=OLD.id)
  OR EXISTS (SELECT 1 FROM thread_messages WHERE business_id IS OLD.business_id AND item_type='table' AND item_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'A table with memberships or a discussion is retained'); END""",
    """CREATE TRIGGER tasks_keep_crm_entry BEFORE DELETE ON tasks
WHEN EXISTS (SELECT 1 FROM crm_entries WHERE record_business_id IS OLD.business_id AND record_task_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'A Task that belongs to a CRM is retained'); END""",
    """CREATE TRIGGER thread_messages_table_scope BEFORE INSERT ON thread_messages
WHEN NEW.item_type='table' AND NOT EXISTS (SELECT 1 FROM data_tables WHERE business_id IS NEW.business_id AND id=NEW.item_id)
BEGIN SELECT RAISE(ABORT,'A Table discussion belongs to a Table in the same owning scope'); END""",
    """CREATE TRIGGER thread_follows_table_scope BEFORE INSERT ON thread_follows
WHEN NEW.item_type='table' AND NOT EXISTS (SELECT 1 FROM data_tables WHERE business_id IS NEW.business_id AND id=NEW.item_id)
BEGIN SELECT RAISE(ABORT,'A Table follow belongs to a Table in the same owning scope'); END""",
    # The Table discussion finds one record's membership developments by the structured reference it keeps in change_json.
    """CREATE INDEX thread_messages_crm_record ON thread_messages(json_extract(change_json,'$.record.id'),created_at,id)
WHERE item_type='table' AND kind='system'""",
    """CREATE TRIGGER inbox_capture_crm_entries_insert AFTER INSERT ON crm_entries
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('crm_entries','insert',inbox_actor(),NULL,json_object('business_id',NEW.business_id,'id',NEW.id,'table_id',NEW.table_id,'record_task_id',NEW.record_task_id,'record_business_id',NEW.record_business_id,'position',NEW.position,'stage_id',NEW.stage_id,'cells_json',NEW.cells_json,'removed',NEW.removed,'updated_by',NEW.updated_by,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_crm_entries_update AFTER UPDATE ON crm_entries
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('crm_entries','update',inbox_actor(),json_object('business_id',OLD.business_id,'id',OLD.id,'table_id',OLD.table_id,'record_task_id',OLD.record_task_id,'record_business_id',OLD.record_business_id,'position',OLD.position,'stage_id',OLD.stage_id,'cells_json',OLD.cells_json,'removed',OLD.removed,'updated_by',OLD.updated_by,'revision',OLD.revision),json_object('business_id',NEW.business_id,'id',NEW.id,'table_id',NEW.table_id,'record_task_id',NEW.record_task_id,'record_business_id',NEW.record_business_id,'position',NEW.position,'stage_id',NEW.stage_id,'cells_json',NEW.cells_json,'removed',NEW.removed,'updated_by',NEW.updated_by,'revision',NEW.revision)); END""",
    f"""CREATE TABLE crm_activity_events (
    business_id TEXT REFERENCES businesses(id),
    id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    event_key TEXT NOT NULL CHECK(length(event_key) BETWEEN 1 AND 300),
    kind TEXT NOT NULL CHECK(kind IN ('email','message','development')),
    summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 500),
    body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 20000),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES principals(id),
    source_json TEXT NOT NULL CHECK(json_valid(source_json) AND length(source_json)<={SOURCE_LIMIT}),
    corrects_event_id TEXT,
    content_hash TEXT NOT NULL,
    PRIMARY KEY(business_id,id),
    FOREIGN KEY(business_id,task_id) REFERENCES tasks(business_id,id),
    FOREIGN KEY(business_id,corrects_event_id) REFERENCES crm_activity_events(business_id,id)
)""",
    """CREATE UNIQUE INDEX crm_activity_events_global_key ON crm_activity_events(id) WHERE business_id IS NULL""",
    """CREATE UNIQUE INDEX crm_activity_events_event_key ON crm_activity_events(business_id,task_id,event_key)
WHERE business_id IS NOT NULL""",
    """CREATE UNIQUE INDEX crm_activity_events_global_event_key ON crm_activity_events(task_id,event_key) WHERE business_id IS NULL""",
    """CREATE INDEX crm_activity_events_task ON crm_activity_events(business_id,task_id,occurred_at,id)""",
    """CREATE TRIGGER crm_activity_events_task_insert BEFORE INSERT ON crm_activity_events
WHEN NOT EXISTS (SELECT 1 FROM tasks WHERE business_id IS NEW.business_id AND id=NEW.task_id)
BEGIN SELECT RAISE(ABORT,'A recorded observation belongs to a Task in the same owning scope'); END""",
    """CREATE TRIGGER crm_activity_events_correction_insert BEFORE INSERT ON crm_activity_events
WHEN NEW.corrects_event_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM crm_activity_events WHERE business_id IS NEW.business_id AND id=NEW.corrects_event_id AND task_id=NEW.task_id)
BEGIN SELECT RAISE(ABORT,'A correction names an earlier observation of the same Task'); END""",
    """CREATE TRIGGER crm_activity_events_immutable_update BEFORE UPDATE ON crm_activity_events
BEGIN SELECT RAISE(ABORT,'Recorded observations are immutable; record a correction instead'); END""",
    """CREATE TRIGGER crm_activity_events_immutable_delete BEFORE DELETE ON crm_activity_events
BEGIN SELECT RAISE(ABORT,'Recorded observations are immutable'); END""",
    """CREATE TRIGGER tasks_keep_crm_activity BEFORE DELETE ON tasks
WHEN EXISTS (SELECT 1 FROM crm_activity_events WHERE business_id IS OLD.business_id AND task_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'A Task with recorded observations is retained'); END""",
)

TABLES = {
    "crm_entries": TableSpec(("business_id", "id"), "business_id"),
    "crm_activity_events": TableSpec(("business_id", "id"), "business_id", False, None),
}

IMMUTABLE_FIELDS = {
    "crm_entries": frozenset({"table_id", "record_task_id", "record_business_id", "record_namespace", "created_by", "created_at"}),
}

# Host-owned or neighbouring tables these records reference or are written beside.
REQUIRED_TABLES = frozenset({"data_tables", "tasks", "thread_messages", "thread_follows", "inbox_changes"})


def storage_contribution() -> StorageContribution:
    """The membership and recorded-activity storage, for the host's trusted assembly."""
    return StorageContribution("crm", TABLES, IMMUTABLE_FIELDS, SCHEMA, required_tables=REQUIRED_TABLES)
