"""Storage of Actions: the canonical completed-work log, its immutable revisions and its read links.

An Action lives in one owning namespace: ``project_id`` is the immutable storage scope (NULL is Global) and ``id`` is
unique across all Actions. ``action_projects`` is the v1 read-visibility relation of one shared project-operational
Action; it names who may *read* it and never who owns or may change it. The current row is the Action's present
projection. Every accepted change (record, correction, withdrawal) also appends one ``action_revisions`` row holding the
exact accepted state; these rows are immutable, are what the Context queue's inputs point at, and are never removed.
A withdrawn Action keeps its row and history and accepts no later change: further activity is a new Action.

The shared label catalog and follow state are the tags and discussion contributions' tables, which this DDL needs
created before it."""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

CATEGORIES = ("global-systems", "global-operational", "project-operational")
ORIGINS = ("manual", "agent", "automatic", "legacy")
OPERATIONS = ("add", "edit", "remove")

SCHEMA = (
    """CREATE TABLE action_logs (
    project_id TEXT REFERENCES projects(id),
    id TEXT NOT NULL,
    category TEXT CHECK (category IS NULL OR category IN ('global-systems','global-operational','project-operational')),
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
    CHECK (origin <> 'legacy' OR category = 'project-operational'),
    PRIMARY KEY (project_id, id)
)""",
    """CREATE UNIQUE INDEX action_logs_id ON action_logs(id)""",
    """CREATE INDEX action_logs_occurrence ON action_logs(occurred_at, id)""",
    """CREATE INDEX action_logs_occurred_date ON action_logs(occurred_date, id)""",
    """CREATE TABLE action_projects (
    action_id TEXT NOT NULL REFERENCES action_logs(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (action_id, project_id)
)""",
    """CREATE INDEX action_projects_project ON action_projects(project_id, action_id)""",
    """CREATE TABLE action_revisions (
    project_id TEXT REFERENCES projects(id),
    action_id TEXT NOT NULL REFERENCES action_logs(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    operation TEXT NOT NULL CHECK (operation IN ('add','edit','remove')),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json) AND json_type(snapshot_json) = 'object'),
    recorded_by TEXT NOT NULL REFERENCES principals(id),
    performed_by TEXT REFERENCES principals(id),
    provenance_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(provenance_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (action_id, revision),
    CHECK ((operation = 'add') = (revision = 1))
)""",
    """CREATE INDEX action_revisions_scope ON action_revisions(project_id, action_id, revision)""",
    # Identity, original attribution and the owning scope never change; a withdrawn Action is final.
    """CREATE TRIGGER action_logs_identity_guard BEFORE UPDATE ON action_logs
WHEN NEW.project_id IS NOT OLD.project_id OR NEW.id IS NOT OLD.id
    OR NEW.origin IS NOT OLD.origin OR NEW.created_at IS NOT OLD.created_at OR NEW.recorded_by IS NOT OLD.recorded_by
    OR NEW.performed_by IS NOT OLD.performed_by
BEGIN SELECT RAISE(ABORT,'Action identity, origin, attribution and recording time are immutable'); END""",
    """CREATE TRIGGER action_logs_withdrawn_final BEFORE UPDATE ON action_logs
WHEN OLD.withdrawn = 1
BEGIN SELECT RAISE(ABORT,'A withdrawn Action keeps its history and accepts no later change'); END""",
    """CREATE TRIGGER action_logs_delete_guard BEFORE DELETE ON action_logs
BEGIN SELECT RAISE(ABORT,'Actions are not deleted'); END""",
    """CREATE TRIGGER action_projects_insert_guard BEFORE INSERT ON action_projects
WHEN (SELECT category FROM action_logs WHERE id=NEW.action_id) IS NOT 'project-operational'
    OR (SELECT withdrawn FROM action_logs WHERE id=NEW.action_id) = 1
BEGIN SELECT RAISE(ABORT,'Only a current project-operational Action has project links'); END""",
    """CREATE TRIGGER action_projects_delete_guard BEFORE DELETE ON action_projects
WHEN (SELECT withdrawn FROM action_logs WHERE id=OLD.action_id) = 1
BEGIN SELECT RAISE(ABORT,'A withdrawn Action keeps its project links'); END""",
    # A revision row records the Action's present revision in the same owning scope, and nothing rewrites it.
    """CREATE TRIGGER action_revisions_current BEFORE INSERT ON action_revisions
WHEN NOT EXISTS (SELECT 1 FROM action_logs WHERE id=NEW.action_id AND project_id IS NEW.project_id
    AND revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'An Action revision records the Action''s current revision in its owning scope'); END""",
    """CREATE TRIGGER action_revisions_immutable BEFORE UPDATE ON action_revisions
BEGIN SELECT RAISE(ABORT,'Action revisions are immutable'); END""",
    """CREATE TRIGGER action_revisions_keep BEFORE DELETE ON action_revisions
BEGIN SELECT RAISE(ABORT,'Action revisions are retained'); END""",
    """CREATE TRIGGER item_tags_action_scope BEFORE INSERT ON item_tags
WHEN NEW.item_type='action' AND NOT EXISTS (SELECT 1 FROM action_logs WHERE project_id IS NEW.project_id AND id=NEW.item_id)
BEGIN SELECT RAISE(ABORT,'An Action tag belongs to an Action in the same owning scope'); END""",
    """CREATE TRIGGER thread_follows_action_scope BEFORE INSERT ON thread_follows
WHEN NEW.item_type='action' AND NOT EXISTS (SELECT 1 FROM action_logs WHERE project_id IS NEW.project_id AND id=NEW.item_id)
BEGIN SELECT RAISE(ABORT,'An Action follow belongs to an Action in the same owning scope'); END""",
)

TABLES = {
    "action_logs": TableSpec(("project_id", "id"), "project_id"),
    "action_projects": TableSpec(("action_id", "project_id"), revision_column=None),
    "action_revisions": TableSpec(("action_id", "revision"), "project_id", False, None),
}
IMMUTABLE_FIELDS = {"action_logs": frozenset({"origin", "recorded_by", "performed_by"})}
REQUIRED_TABLES = frozenset({"item_tags", "tasks", "thread_follows"})


def storage_contribution() -> StorageContribution:
    return StorageContribution("actions", TABLES, immutable_fields=IMMUTABLE_FIELDS, schema=SCHEMA, required_tables=REQUIRED_TABLES)
