"""Source-derived target DDL for the tables the extracted engine owns.

Derived by ``tools/derive_core_schema.py`` from the final state of the deployed v1 migrations (biz-core
e71fd1e527368bfd41b9815126f4bf33452aeebb, migrations 001-113, SQL only). It is a target-schema artifact for
isolated empty instances: nothing here upgrades an existing database, and no historical migration chain is
carried. Retained domains contribute their own tables, indexes, triggers and sequence seeds.
"""

CORE_SCHEMA = (
    """CREATE TABLE "principals" (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('human','agent','system','integration')),
    display_name TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE businesses (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    workspace TEXT NOT NULL UNIQUE,
    hq_path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'UTC',
    config_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, archived_at TEXT, archived_by TEXT REFERENCES principals(id))""",
    """CREATE TABLE business_memberships (
    business_id TEXT NOT NULL REFERENCES businesses(id),
    principal_id TEXT NOT NULL REFERENCES principals(id),
    is_owner INTEGER NOT NULL DEFAULT 0 CHECK (is_owner IN (0,1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, person_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(person_json)),
    PRIMARY KEY (business_id, principal_id)
)""",
    """CREATE TABLE installation (
    id TEXT PRIMARY KEY CHECK (id = 'service'),
    installation_id TEXT NOT NULL UNIQUE CHECK (
        length(installation_id) = 36 AND lower(installation_id) = installation_id
        AND substr(installation_id, 9, 1) = '-' AND substr(installation_id, 14, 1) = '-'
        AND substr(installation_id, 19, 1) = '-' AND substr(installation_id, 24, 1) = '-'
        AND length(replace(installation_id, '-', '')) = 32
        AND replace(installation_id, '-', '') NOT GLOB '*[^0-9a-f]*'
    ),
    owner_principal_id TEXT NOT NULL REFERENCES principals(id),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, management_coordinator_id TEXT REFERENCES principals(id))""",
    """CREATE TABLE agent_profiles (
    agent_id TEXT PRIMARY KEY REFERENCES principals(id),
    principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id),
    home_business_id TEXT REFERENCES businesses(id),
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    workspace TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive','retired')),
    profile_json TEXT NOT NULL CHECK (json_valid(profile_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (agent_id = principal_id),
    CHECK ((home_business_id IS NULL AND namespace = 'ecosystem') OR namespace = home_business_id)
)""",
    """CREATE UNIQUE INDEX agent_profile_active_names ON agent_profiles(namespace, name) WHERE status = 'active'""",
    """CREATE UNIQUE INDEX agent_profiles_one_active_role_per_home ON agent_profiles(home_business_id, json_extract(profile_json, '$.role.id'))
WHERE status = 'active' AND home_business_id IS NOT NULL""",
    """CREATE TABLE "signoffs" (
    id TEXT PRIMARY KEY,
    review_business_id TEXT REFERENCES businesses(id),
    review_scope_kind TEXT NOT NULL DEFAULT 'business' CHECK (review_scope_kind IN ('business','global')),
    proposer_principal_id TEXT NOT NULL REFERENCES principals(id),
    proposer_agent_id TEXT REFERENCES agent_profiles(agent_id),
    initiated_by_principal_id TEXT REFERENCES principals(id),
    operation_name TEXT NOT NULL,
    operation_version INTEGER NOT NULL,
    proposal_revision INTEGER NOT NULL CHECK (proposal_revision >= 1),
    payload_hash TEXT NOT NULL,
    change_set BLOB NOT NULL,
    preview_json TEXT NOT NULL CHECK (json_valid(preview_json)),
    expected_targets_json TEXT NOT NULL CHECK (json_valid(expected_targets_json)),
    policy_binding_json TEXT NOT NULL CHECK (json_valid(policy_binding_json)),
    affected_scope_json TEXT NOT NULL CHECK (json_valid(affected_scope_json)),
    state TEXT NOT NULL CHECK (state IN ('pending','approved','applied','rejected','conflicted','failed','answered','cancelled')),
    status_revision INTEGER NOT NULL DEFAULT 1 CHECK (status_revision >= 1),
    updated_sequence INTEGER NOT NULL UNIQUE,
    decision_json TEXT CHECK (decision_json IS NULL OR json_valid(decision_json)),
    applied_at TEXT,
    apply_result_json TEXT CHECK (apply_result_json IS NULL OR json_valid(apply_result_json)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    supersedes_id TEXT UNIQUE REFERENCES signoffs(id),
    superseded_by_id TEXT UNIQUE REFERENCES signoffs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'internal_change' CHECK(category IN ('internal_change','platform_action','input_request')),
    execution_job_id TEXT,
    execution_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(execution_json) AND json_type(execution_json)='object'),
    legacy_evidence_json TEXT CHECK(legacy_evidence_json IS NULL OR json_valid(legacy_evidence_json)),
    request_key TEXT,
    CHECK ((review_scope_kind = 'business' AND review_business_id IS NOT NULL) OR (review_scope_kind = 'global' AND review_business_id IS NULL))
)""",
    """CREATE INDEX signoffs_category_queue ON signoffs(review_business_id, category, state, created_at, id)""",
    """CREATE UNIQUE INDEX signoffs_execution_job ON signoffs(execution_job_id) WHERE execution_job_id IS NOT NULL""",
    """CREATE UNIQUE INDEX signoffs_open_request_key ON signoffs(proposer_principal_id, COALESCE(review_business_id, ''), request_key)
WHERE request_key IS NOT NULL AND state='pending' AND superseded_by_id IS NULL""",
    """CREATE INDEX signoffs_queue ON signoffs(review_scope_kind, review_business_id, state, created_at, id)""",
    """CREATE INDEX signoffs_updates ON signoffs(review_scope_kind, review_business_id, updated_sequence)""",
    """CREATE INDEX signoffs_working_attention ON signoffs(proposer_principal_id,review_business_id,updated_sequence DESC)""",
    """CREATE TRIGGER signoffs_immutable_payload
BEFORE UPDATE OF id, review_scope_kind, review_business_id, proposer_principal_id, proposer_agent_id,
    initiated_by_principal_id, operation_name, operation_version, proposal_revision,
    payload_hash, change_set, preview_json, expected_targets_json, policy_binding_json,
    affected_scope_json, supersedes_id, created_at ON signoffs
BEGIN
    SELECT RAISE(ABORT, 'signoff proposal bytes and identity are immutable');
END""",
    """CREATE TRIGGER signoffs_immutable_platform_binding
BEFORE UPDATE OF category, execution_job_id, legacy_evidence_json ON signoffs
BEGIN
    SELECT RAISE(ABORT, 'signoff category, execution identity and historical evidence are immutable');
END""",
    """CREATE TRIGGER signoffs_immutable_request_key BEFORE UPDATE OF request_key ON signoffs
BEGIN SELECT RAISE(ABORT, 'signoff request key is immutable'); END""",
    """CREATE TRIGGER signoffs_immutable_response BEFORE UPDATE OF decision_json ON signoffs
WHEN OLD.decision_json IS NOT NULL AND NEW.decision_json IS NOT OLD.decision_json
BEGIN SELECT RAISE(ABORT, 'submitted owner response is immutable'); END""",
    """CREATE TABLE idempotency_receipts (
    principal_id TEXT NOT NULL REFERENCES principals(id),
    target_namespace TEXT NOT NULL,
    operation_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, target_namespace, operation_name, idempotency_key)
)""",
    """CREATE TRIGGER idempotency_receipts_no_update BEFORE UPDATE ON idempotency_receipts
BEGIN
    SELECT RAISE(ABORT, 'idempotency receipts are immutable');
END""",
    """CREATE TABLE apply_receipts (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(id),
    target_namespace TEXT NOT NULL,
    operation_name TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    change_set BLOB NOT NULL,
    signoff_id TEXT UNIQUE REFERENCES signoffs(id),
    state TEXT NOT NULL CHECK (state IN ('prepared','applying','applied','failed','conflicted')),
    progress_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(progress_json)),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TRIGGER apply_receipts_immutable_payload
BEFORE UPDATE OF id, principal_id, target_namespace, operation_name, payload_hash,
    change_set, signoff_id, created_at ON apply_receipts
BEGIN
    SELECT RAISE(ABORT, 'application receipt bindings are immutable');
END""",
    """CREATE TABLE resource_claims (
    target_namespace TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    apply_id TEXT NOT NULL REFERENCES apply_receipts(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (target_namespace, resource_type, resource_id)
)""",
    """CREATE TABLE maintenance_state (
    id TEXT PRIMARY KEY CHECK (id = 'service'),
    state TEXT NOT NULL CHECK (state IN ('open','draining','closed','retired')),
    message TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE change_sequences (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0 CHECK (value >= 0)
)""",
    """INSERT INTO change_sequences(name, value) VALUES ('signoffs', 0)""",
)
