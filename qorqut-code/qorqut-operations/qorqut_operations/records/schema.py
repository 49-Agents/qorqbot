"""Storage of the Context entities: the one ``context_entities`` table of named external references (a label and a URL).

A Context entity is the ``entity`` item kind (``records.vocabulary``): resolved, related and archived through the
neutral item resolver, with no operation of its own. ``SCHEMA`` and ``CHANGE_CAPTURE`` are the statements of the schema
(owner ``legacy-context``); one unique index on ``id`` makes an entity id name one entity in the whole store. ``CHANGE_CAPTURE``
writes the host change feed's ``inbox_changes``, so the host composition places it where the committed order puts it (after
``SCHEMA``, as the last owner's statements).
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

SCHEMA = (
    """CREATE TABLE context_entities (
    project_id TEXT NOT NULL REFERENCES projects(id),
    id TEXT NOT NULL,
    label TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,id)
)""",
    """CREATE UNIQUE INDEX context_entities_id_unique ON context_entities(id)""",
)

CHANGE_CAPTURE = (
    """CREATE TRIGGER inbox_capture_context_entities_insert AFTER INSERT ON context_entities
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('context_entities','insert',inbox_actor(),NULL,json_object('project_id',NEW.project_id,'id',NEW.id,'label',NEW.label,'url',NEW.url,'archived',NEW.archived,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_context_entities_update AFTER UPDATE ON context_entities
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('context_entities','update',inbox_actor(),json_object('project_id',OLD.project_id,'id',OLD.id,'label',OLD.label,'url',OLD.url,'archived',OLD.archived,'revision',OLD.revision),json_object('project_id',NEW.project_id,'id',NEW.id,'label',NEW.label,'url',NEW.url,'archived',NEW.archived,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_context_entities_delete AFTER DELETE ON context_entities
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('context_entities','delete',inbox_actor(),json_object('project_id',OLD.project_id,'id',OLD.id,'label',OLD.label,'url',OLD.url,'archived',OLD.archived,'revision',OLD.revision),NULL); END""",
)

TABLES = {"context_entities": TableSpec(("project_id", "id"), "project_id")}
IMMUTABLE_FIELDS: dict[str, frozenset[str]] = {}


def storage_contribution() -> StorageContribution:
    return StorageContribution("records", TABLES, immutable_fields=IMMUTABLE_FIELDS, schema=SCHEMA)
