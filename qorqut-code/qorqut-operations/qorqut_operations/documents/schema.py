"""Storage of operational source documents: the one ``documents`` table and its scope index.

A document row is the registered identity, scope, kind, path, content hash, metadata, source references, availability and
revision of one managed file; the bytes live in the file. ``availability`` is advanced by the file-receipt lifecycle of
``qorqut_operations.document_history``, which this contribution therefore requires.

``SCHEMA`` and ``CHANGE_CAPTURE`` are the exact statements of the committed schema, kept verbatim so the schema fingerprint
is unchanged. ``CHANGE_CAPTURE`` writes the host change feed's ``inbox_changes``, so the integrator splices it where the
committed order places it (after ``SCHEMA``), beside the host's working-memory statements, which stay host-owned.
"""

from __future__ import annotations

from qorqut_operations.document_history.lifecycle import RESOURCE_TYPE
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

SCHEMA = (
    """CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    namespace TEXT NOT NULL,
    owning_agent_id TEXT REFERENCES agent_profiles(agent_id),
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    source_refs_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(source_refs_json)),
    availability TEXT NOT NULL DEFAULT 'ready' CHECK (availability IN ('ready','applying','conflicted')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
    UNIQUE (namespace, path),
    UNIQUE (business_id, id)
)""",
    """CREATE INDEX documents_scope ON documents(business_id, kind, owning_agent_id, id)""",
)

CHANGE_CAPTURE = (
    """CREATE TRIGGER inbox_capture_documents_insert AFTER INSERT ON documents
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('documents','insert',inbox_actor(),NULL,json_object('business_id',NEW.business_id,'id',NEW.id,'owning_agent_id',NEW.owning_agent_id,'kind',NEW.kind,'path',NEW.path,'content_hash',NEW.content_hash,'metadata_json',NEW.metadata_json,'availability',NEW.availability,'archived',NEW.archived,'source_refs_json',NEW.source_refs_json,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_documents_update AFTER UPDATE ON documents
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('documents','update',inbox_actor(),json_object('business_id',OLD.business_id,'id',OLD.id,'owning_agent_id',OLD.owning_agent_id,'kind',OLD.kind,'path',OLD.path,'content_hash',OLD.content_hash,'metadata_json',OLD.metadata_json,'availability',OLD.availability,'archived',OLD.archived,'source_refs_json',OLD.source_refs_json,'revision',OLD.revision),json_object('business_id',NEW.business_id,'id',NEW.id,'owning_agent_id',NEW.owning_agent_id,'kind',NEW.kind,'path',NEW.path,'content_hash',NEW.content_hash,'metadata_json',NEW.metadata_json,'availability',NEW.availability,'archived',NEW.archived,'source_refs_json',NEW.source_refs_json,'revision',NEW.revision)); END""",
    """CREATE TRIGGER inbox_capture_documents_delete AFTER DELETE ON documents
BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)
VALUES('documents','delete',inbox_actor(),json_object('business_id',OLD.business_id,'id',OLD.id,'owning_agent_id',OLD.owning_agent_id,'kind',OLD.kind,'path',OLD.path,'content_hash',OLD.content_hash,'metadata_json',OLD.metadata_json,'availability',OLD.availability,'archived',OLD.archived,'source_refs_json',OLD.source_refs_json,'revision',OLD.revision),NULL); END""",
)

TABLES = {"documents": TableSpec(("id",), "business_id")}
IMMUTABLE_FIELDS: dict[str, frozenset[str]] = {}
REQUIRED_LIFECYCLES = frozenset({RESOURCE_TYPE})


def storage_contribution() -> StorageContribution:
    return StorageContribution("documents", TABLES, immutable_fields=IMMUTABLE_FIELDS, schema=SCHEMA,
                               required_lifecycles=REQUIRED_LIFECYCLES)
