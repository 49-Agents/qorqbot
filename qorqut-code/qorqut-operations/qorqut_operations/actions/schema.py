"""Storage of Actions: the canonical completed-work log, its immutable revisions and its read links.

An Action lives in one owning namespace: ``business_id`` is the immutable storage scope (NULL is Global) and part of
the canonical ``action_key``. ``action_businesses`` is the v1 read-visibility relation of one shared business-operational
Action; it names who may *read* it and never who owns or may change it. The current row is the Action's present
projection. Every accepted change (record, correction, withdrawal) also appends one ``action_revisions`` row holding the
exact accepted state; these rows are immutable, are what the Context queue's inputs point at, and are never removed.
A withdrawn Action keeps its row and history and accepts no later change: further activity is a new Action.

The shared label catalog and follow state are the tags and discussion contributions' tables, which this DDL needs
created before it."""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

CATEGORIES = ("global-systems", "global-operational", "business-operational")
ORIGINS = ("manual", "agent", "automatic", "legacy")
OPERATIONS = ("add", "edit", "remove")

SCHEMA = (
    """CREATE TABLE action_logs (
    business_id TEXT REFERENCES businesses(id),
    id TEXT NOT NULL,
    action_key TEXT NOT NULL UNIQUE,
    category TEXT CHECK (category IS NULL OR category IN ('global-systems','global-operational','business-operational')),
    action_text TEXT NOT NULL CHECK (length(trim(action_text)) > 0),
    performed_by TEXT REFERENCES principals(id),
    recorded_by TEXT NOT NULL REFERENCES principals(id),
    origin TEXT NOT NULL CHECK (origin IN ('manual','agent','automatic','legacy')),
    occurred_at TEXT NOT NULL,
    occurred_date TEXT,
    occurred_hour INTEGER CHECK (occurred_hour IS NULL OR occurred_hour BETWEEN 0 AND 23),
    time_precision TEXT NOT NULL DEFAULT 'instant' CHECK (time_precision IN ('instant','hour','day')),
    time_zone TEXT NOT NULL DEFAULT 'UTC',
    refs_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(refs_json)),
    related_objects_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(related_objects_json)),
    withdrawn INTEGER NOT NULL DEFAULT 0 CHECK (withdrawn IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (action_key = CASE WHEN business_id IS NULL THEN 'global/' || id ELSE 'business/' || business_id || '/' || id END),
    CHECK (origin <> 'legacy' OR category = 'business-operational'),
    PRIMARY KEY (business_id, id)
)""",
    """CREATE UNIQUE INDEX action_logs_global_key ON action_logs(id) WHERE business_id IS NULL""",
    """CREATE INDEX action_logs_occurrence ON action_logs(occurred_at, action_key)""",
    """CREATE INDEX action_logs_occurred_date ON action_logs(occurred_date, action_key)""",
    """CREATE TABLE action_businesses (
    action_key TEXT NOT NULL REFERENCES action_logs(action_key),
    business_id TEXT NOT NULL REFERENCES businesses(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (action_key, business_id)
)""",
    """CREATE INDEX action_businesses_business ON action_businesses(business_id, action_key)""",
    """CREATE TABLE action_revisions (
    business_id TEXT REFERENCES businesses(id),
    action_key TEXT NOT NULL REFERENCES action_logs(action_key),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    operation TEXT NOT NULL CHECK (operation IN ('add','edit','remove')),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json) AND json_type(snapshot_json) = 'object'),
    recorded_by TEXT NOT NULL REFERENCES principals(id),
    performed_by TEXT REFERENCES principals(id),
    provenance_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(provenance_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (action_key, revision),
    CHECK ((operation = 'add') = (revision = 1))
)""",
    """CREATE INDEX action_revisions_scope ON action_revisions(business_id, action_key, revision)""",
    # Identity, original attribution and the owning scope never change; a withdrawn Action is final.
    """CREATE TRIGGER action_logs_identity_guard BEFORE UPDATE ON action_logs
WHEN NEW.business_id IS NOT OLD.business_id OR NEW.id IS NOT OLD.id OR NEW.action_key IS NOT OLD.action_key
    OR NEW.origin IS NOT OLD.origin OR NEW.created_at IS NOT OLD.created_at OR NEW.recorded_by IS NOT OLD.recorded_by
    OR NEW.performed_by IS NOT OLD.performed_by
BEGIN SELECT RAISE(ABORT,'Action identity, origin, attribution and recording time are immutable'); END""",
    """CREATE TRIGGER action_logs_withdrawn_final BEFORE UPDATE ON action_logs
WHEN OLD.withdrawn = 1
BEGIN SELECT RAISE(ABORT,'A withdrawn Action keeps its history and accepts no later change'); END""",
    """CREATE TRIGGER action_logs_delete_guard BEFORE DELETE ON action_logs
BEGIN SELECT RAISE(ABORT,'Actions are not deleted'); END""",
    """CREATE TRIGGER action_businesses_insert_guard BEFORE INSERT ON action_businesses
WHEN (SELECT category FROM action_logs WHERE action_key=NEW.action_key) IS NOT 'business-operational'
    OR (SELECT withdrawn FROM action_logs WHERE action_key=NEW.action_key) = 1
BEGIN SELECT RAISE(ABORT,'Only a current business-operational Action has business links'); END""",
    """CREATE TRIGGER action_businesses_delete_guard BEFORE DELETE ON action_businesses
WHEN (SELECT withdrawn FROM action_logs WHERE action_key=OLD.action_key) = 1
BEGIN SELECT RAISE(ABORT,'A withdrawn Action keeps its business links'); END""",
    # A revision row records the Action's present revision in the same owning scope, and nothing rewrites it.
    """CREATE TRIGGER action_revisions_current BEFORE INSERT ON action_revisions
WHEN NOT EXISTS (SELECT 1 FROM action_logs WHERE action_key=NEW.action_key AND business_id IS NEW.business_id
    AND revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'An Action revision records the Action''s current revision in its owning scope'); END""",
    """CREATE TRIGGER action_revisions_immutable BEFORE UPDATE ON action_revisions
BEGIN SELECT RAISE(ABORT,'Action revisions are immutable'); END""",
    """CREATE TRIGGER action_revisions_keep BEFORE DELETE ON action_revisions
BEGIN SELECT RAISE(ABORT,'Action revisions are retained'); END""",
    """CREATE TRIGGER item_tags_action_scope BEFORE INSERT ON item_tags
WHEN NEW.item_type='action' AND NOT EXISTS (SELECT 1 FROM action_logs WHERE business_id IS NEW.business_id AND id=NEW.item_id)
BEGIN SELECT RAISE(ABORT,'An Action tag belongs to an Action in the same owning scope'); END""",
    """CREATE TRIGGER thread_follows_action_scope BEFORE INSERT ON thread_follows
WHEN NEW.item_type='action' AND NOT EXISTS (SELECT 1 FROM action_logs WHERE business_id IS NEW.business_id AND id=NEW.item_id)
BEGIN SELECT RAISE(ABORT,'An Action follow belongs to an Action in the same owning scope'); END""",
)

TABLES = {
    "action_logs": TableSpec(("business_id", "id"), "business_id"),
    "action_businesses": TableSpec(("action_key", "business_id"), revision_column=None),
    "action_revisions": TableSpec(("action_key", "revision"), "business_id", False, None),
}
IMMUTABLE_FIELDS = {"action_logs": frozenset({"action_key", "origin", "recorded_by", "performed_by"})}
REQUIRED_TABLES = frozenset({"item_tags", "tasks", "thread_follows"})


def storage_contribution() -> StorageContribution:
    return StorageContribution("actions", TABLES, immutable_fields=IMMUTABLE_FIELDS, schema=SCHEMA, required_tables=REQUIRED_TABLES)
