"""Record extensions composed with the shared Operations foundation schema."""

from qorqut_operations.context_queue.schema import SCHEMA_SQL as CONTEXT_QUEUE_SQL

SCHEMA_SQL = """
CREATE TABLE tasks (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    status TEXT NOT NULL CHECK (status IN ('open','working','waiting','done','cancelled')),
    description TEXT NOT NULL,
    parent_id TEXT REFERENCES tasks(item_id),
    snoozed_until TEXT,
    CHECK (parent_id IS NULL OR parent_id != item_id)
);
CREATE INDEX tasks_parent ON tasks(parent_id);
CREATE TABLE task_assignees (
    item_id TEXT NOT NULL REFERENCES tasks(item_id),
    principal_id TEXT NOT NULL REFERENCES principals(id),
    PRIMARY KEY (item_id, principal_id)
);
CREATE INDEX task_assignees_principal ON task_assignees(principal_id, item_id);
CREATE TABLE task_dependencies (
    item_id TEXT NOT NULL REFERENCES tasks(item_id),
    dependency_id TEXT NOT NULL REFERENCES tasks(item_id),
    PRIMARY KEY (item_id, dependency_id),
    CHECK (item_id != dependency_id)
);
CREATE TABLE actions (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    text TEXT NOT NULL,
    represented_actor_id TEXT NOT NULL REFERENCES principals(id),
    recorder_id TEXT NOT NULL REFERENCES principals(id),
    related_objects_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    withdrawn INTEGER NOT NULL DEFAULT 0 CHECK (withdrawn IN (0,1))
);
""" + CONTEXT_QUEUE_SQL + """CREATE TABLE signoffs (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    request_kind TEXT NOT NULL CHECK (request_kind IN ('permission','decision','input')),
    requester_id TEXT NOT NULL REFERENCES principals(id),
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','answered')),
    permission_json TEXT,
    response_json TEXT,
    provenance_json TEXT,
    responder_id TEXT REFERENCES principals(id),
    recorder_id TEXT NOT NULL REFERENCES principals(id),
    recorder_native_session_id TEXT,
    CHECK ((request_kind='permission' AND permission_json IS NOT NULL)
        OR (request_kind!='permission' AND permission_json IS NULL))
);
CREATE TABLE approval_grants (
    signoff_id TEXT PRIMARY KEY REFERENCES signoffs(item_id),
    requester_id TEXT NOT NULL REFERENCES principals(id),
    operation TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    expected_revisions_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_by TEXT REFERENCES principals(id),
    consumed_operation TEXT,
    consumed_key TEXT,
    consumed_at TEXT,
    UNIQUE (consumed_by, consumed_operation, consumed_key),
    CHECK ((consumed_by IS NULL AND consumed_operation IS NULL AND consumed_key IS NULL AND consumed_at IS NULL)
        OR (consumed_by IS NOT NULL AND consumed_by=requester_id
            AND consumed_operation IS NOT NULL AND consumed_operation=operation
            AND consumed_key IS NOT NULL AND consumed_at IS NOT NULL))
);
CREATE TRIGGER approval_grant_immutable_binding
BEFORE UPDATE OF signoff_id, requester_id, operation, arguments_json, arguments_hash,
                 expected_revisions_json, created_at ON approval_grants
BEGIN
    SELECT RAISE(ABORT, 'Approval binding is immutable');
END;
CREATE TRIGGER approval_grant_once
BEFORE UPDATE OF consumed_by, consumed_operation, consumed_key, consumed_at ON approval_grants
WHEN OLD.consumed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'Approval is already consumed');
END;
CREATE TRIGGER approval_grant_keep_history
BEFORE DELETE ON approval_grants
BEGIN
    SELECT RAISE(ABORT, 'Approval history is retained');
END;
"""
