"""Storage of Tasks: one record per Task in its single owning namespace.

``business_id`` NULL is the Global namespace; the key stays ``(business_id, id)`` as in v1 so the domain tables that
reference a Task by that pair keep their foreign keys. A Task's state is one of five fixed values. Parent and child
states are independent; ``parent_id`` is an ancestry link inside the same namespace and cannot form a cycle.
Assignees and informational dependencies are ``assigned_to`` / ``blocked_by`` relationship edges (the engine's
relationship store), exactly as before. Labels live in the shared catalog (``item_tags``), the discussion in
``thread_messages``, and a Task's single snooze is the one ``snoozed_until`` time with a generation counter.

This DDL needs the discussion contribution created before it (its guards sit on ``thread_messages`` and
``thread_follows``)."""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

STATES = ("open", "working", "waiting", "done", "cancelled")
CLOSED_STATES = ("done", "cancelled")
PAUSED_STATES = ("waiting",)
KINDS = ("ordinary", "owner_request")

SCHEMA = (
    """CREATE TABLE tasks (
    business_id TEXT REFERENCES businesses(id),
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','working','waiting','done','cancelled')),
    due TEXT,
    fields_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(fields_json)),
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    created_by TEXT NOT NULL REFERENCES principals(id),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    snoozed_until TEXT,
    snooze_generation INTEGER NOT NULL DEFAULT 0 CHECK(snooze_generation >= 0),
    kind TEXT NOT NULL DEFAULT 'ordinary' CHECK(kind IN ('ordinary','owner_request')),
    parent_id TEXT,
    PRIMARY KEY(business_id,id),
    CHECK(parent_id IS NULL OR parent_id <> id)
)""",
    """CREATE UNIQUE INDEX tasks_global_key ON tasks(id) WHERE business_id IS NULL""",
    """CREATE INDEX tasks_queue ON tasks(business_id,archived,status,due,id)""",
    """CREATE INDEX tasks_working_attention ON tasks(business_id,archived,updated_at DESC,id)""",
    """CREATE INDEX tasks_parent ON tasks(business_id,parent_id) WHERE parent_id IS NOT NULL""",
    """CREATE INDEX tasks_snoozed ON tasks(snoozed_until) WHERE snoozed_until IS NOT NULL""",
    # An ancestry link stays inside one namespace and never closes a loop.
    """CREATE TRIGGER tasks_parent_insert BEFORE INSERT ON tasks
WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM tasks p WHERE p.business_id IS NEW.business_id AND p.id=NEW.parent_id)
BEGIN SELECT RAISE(ABORT,'A parent Task belongs to the same owning scope'); END""",
    """CREATE TRIGGER tasks_parent_update BEFORE UPDATE OF parent_id ON tasks
WHEN NEW.parent_id IS NOT NULL AND (
    NOT EXISTS (SELECT 1 FROM tasks p WHERE p.business_id IS NEW.business_id AND p.id=NEW.parent_id)
    OR EXISTS (WITH RECURSIVE up(id) AS (
        SELECT NEW.parent_id
        UNION
        SELECT t.parent_id FROM tasks t JOIN up ON t.id=up.id AND t.business_id IS NEW.business_id WHERE t.parent_id IS NOT NULL)
      SELECT 1 FROM up WHERE id=NEW.id))
BEGIN SELECT RAISE(ABORT,'A Task parent chain cannot contain a cycle or leave its owning scope'); END""",
    # Only an armed relocation (``storage.relocation``) may change the owning scope, and nothing else about the identity.
    """CREATE TRIGGER tasks_scope_identity BEFORE UPDATE OF id,business_id,created_by,created_at ON tasks
WHEN (NEW.id IS NOT OLD.id OR NEW.business_id IS NOT OLD.business_id OR NEW.created_by IS NOT OLD.created_by
  OR NEW.created_at IS NOT OLD.created_at)
 AND NOT (NEW.id IS OLD.id AND NEW.created_by IS OLD.created_by AND NEW.created_at IS OLD.created_at
  AND NEW.business_id IS NOT OLD.business_id AND relocation_permits(OLD.business_id,NEW.business_id,OLD.id))
BEGIN SELECT RAISE(ABORT,'A Task keeps its identity, owning scope and creator'); END""",
    # A Task with a retained discussion is part of the record and is not deleted.
    """CREATE TRIGGER tasks_keep_discussion BEFORE DELETE ON tasks
WHEN EXISTS (SELECT 1 FROM thread_messages WHERE business_id IS OLD.business_id AND item_type='task' AND item_id=OLD.id)
BEGIN SELECT RAISE(ABORT,'A Task with a discussion is retained'); END""",
    """CREATE TRIGGER thread_messages_task_scope BEFORE INSERT ON thread_messages
WHEN NEW.item_type='task' AND NOT EXISTS (SELECT 1 FROM tasks WHERE business_id IS NEW.business_id AND id=NEW.item_id)
BEGIN SELECT RAISE(ABORT,'A Task discussion belongs to a Task in the same owning scope'); END""",
    """CREATE TRIGGER thread_follows_task_scope BEFORE INSERT ON thread_follows
WHEN NEW.item_type='task' AND NOT EXISTS (SELECT 1 FROM tasks WHERE business_id IS NEW.business_id AND id=NEW.item_id)
BEGIN SELECT RAISE(ABORT,'A Task follow belongs to a Task in the same owning scope'); END""",
    # The first time a business Task reaches done (v1 reporting reads this; it is never rewritten).
    """CREATE TABLE task_completions (
    business_id TEXT NOT NULL REFERENCES businesses(id),
    task_id TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY(business_id,task_id)
)""",
    """CREATE TRIGGER task_first_done_insert AFTER INSERT ON tasks
WHEN NEW.business_id IS NOT NULL AND NEW.status='done'
BEGIN
    INSERT OR IGNORE INTO task_completions VALUES(NEW.business_id,NEW.id,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END""",
    """CREATE TRIGGER task_first_done_update AFTER UPDATE OF status ON tasks
WHEN NEW.business_id IS NOT NULL AND NEW.status='done' AND OLD.status!='done'
BEGIN
    INSERT OR IGNORE INTO task_completions VALUES(NEW.business_id,NEW.id,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
END""",
    # One durable due event per Task and snooze generation: the reconciler inserts here with the event it emitted, so
    # a repeated or concurrent tick cannot notify again for the same due state.
    """CREATE TABLE task_snooze_wakes (
    business_id TEXT,
    task_id TEXT NOT NULL,
    snooze_generation INTEGER NOT NULL CHECK(snooze_generation >= 1),
    snoozed_until TEXT NOT NULL,
    event_id INTEGER REFERENCES inbox_events(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(business_id,task_id,snooze_generation),
    FOREIGN KEY(business_id,task_id) REFERENCES tasks(business_id,id)
)""",
    """CREATE UNIQUE INDEX task_snooze_wakes_global_key ON task_snooze_wakes(task_id,snooze_generation) WHERE business_id IS NULL""",
    """CREATE TRIGGER task_snooze_wakes_immutable BEFORE UPDATE ON task_snooze_wakes
WHEN NOT (NEW.business_id IS NOT OLD.business_id AND NEW.task_id IS OLD.task_id AND NEW.snooze_generation IS OLD.snooze_generation
  AND NEW.snoozed_until IS OLD.snoozed_until AND NEW.event_id IS OLD.event_id AND NEW.created_at IS OLD.created_at
  AND relocation_permits(OLD.business_id,NEW.business_id,OLD.task_id))
BEGIN SELECT RAISE(ABORT,'A recorded snooze wake is immutable'); END""",
    """CREATE TRIGGER task_snooze_wakes_keep BEFORE DELETE ON task_snooze_wakes
BEGIN SELECT RAISE(ABORT,'A recorded snooze wake is retained'); END""",
    # The append-only record of every Task relocation: where one Task (identified across moves by its id and its immutable
    # creation time) was, where it went and under whose authority. It is an audit and location aid for stale references,
    # never a second Task store, and it is written only by the relocation helper in the same transaction as the move.
    """CREATE TABLE task_moves (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    move_id TEXT NOT NULL CHECK(length(move_id)=36),
    task_id TEXT NOT NULL,
    source_business_id TEXT REFERENCES businesses(id),
    destination_business_id TEXT NOT NULL REFERENCES businesses(id),
    task_created_at TEXT NOT NULL,
    previous_parent_id TEXT,
    revision_before INTEGER NOT NULL CHECK(revision_before >= 1),
    revision_after INTEGER NOT NULL CHECK(revision_after > revision_before),
    actor_id TEXT NOT NULL REFERENCES principals(id),
    moved_at TEXT NOT NULL,
    UNIQUE(move_id,task_id),
    CHECK(source_business_id IS NOT destination_business_id)
)""",
    """CREATE INDEX task_moves_source ON task_moves(source_business_id,task_id,seq)""",
    """CREATE INDEX task_moves_destination ON task_moves(destination_business_id,task_id,seq)""",
    """CREATE TRIGGER task_moves_immutable_update BEFORE UPDATE ON task_moves
BEGIN SELECT RAISE(ABORT,'A recorded Task move is immutable'); END""",
    """CREATE TRIGGER task_moves_keep BEFORE DELETE ON task_moves
BEGIN SELECT RAISE(ABORT,'A recorded Task move is retained'); END""",
)

TABLES = {
    "tasks": TableSpec(("business_id", "id"), "business_id"),
    "task_completions": TableSpec(("business_id", "task_id"), "business_id", False, None),
    "task_snooze_wakes": TableSpec(("business_id", "task_id", "snooze_generation"), "business_id", False, None),
    "task_moves": TableSpec(("seq",), mutable=False, revision_column=None),
}
# The per-scope configuration row (``business_configs`` domain ``tasks``: typed custom field definitions) stays in the
# host's configuration table; the engine only requires it and the discussion/tag stores it writes beside.
REQUIRED_TABLES = frozenset({"business_configs"})


def storage_contribution() -> StorageContribution:
    return StorageContribution("tasks", TABLES, schema=SCHEMA, required_tables=REQUIRED_TABLES)
