"""Version 2 Overseer tables, installed only by initialization or offline migration."""

DDL = """
CREATE TABLE proposed_deltas (id TEXT PRIMARY KEY, extraction_run_id TEXT NOT NULL, record TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL);
CREATE TABLE deltas (id TEXT PRIMARY KEY, current_revision INTEGER NOT NULL, decision TEXT NOT NULL, accepted_revision INTEGER, acceptance_ref TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE delta_applications (id TEXT PRIMARY KEY, delta_id TEXT NOT NULL REFERENCES deltas(id), accepted_revision INTEGER NOT NULL, acceptance_ref TEXT NOT NULL, run_id TEXT, invocation_ref TEXT, phase TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, plan_revision INTEGER, agreement_ref TEXT, waiting_input_ref TEXT, external_tasks TEXT NOT NULL, merge TEXT, checks TEXT NOT NULL, outcome TEXT, satisfaction_evidence TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX overseer_one_active_application ON delta_applications(delta_id) WHERE phase != 'complete';
CREATE TABLE overseer_delta_revisions (delta_id TEXT NOT NULL REFERENCES deltas(id), revision INTEGER NOT NULL, record TEXT NOT NULL, PRIMARY KEY (delta_id, revision));
CREATE TABLE overseer_plan_revisions (application_id TEXT NOT NULL REFERENCES delta_applications(id), revision INTEGER NOT NULL, record TEXT NOT NULL, PRIMARY KEY (application_id, revision));
CREATE TABLE overseer_memberships (delta_id TEXT NOT NULL, revision INTEGER NOT NULL, proposal_id TEXT NOT NULL REFERENCES proposed_deltas(id), PRIMARY KEY (delta_id, revision, proposal_id), FOREIGN KEY (delta_id, revision) REFERENCES overseer_delta_revisions(delta_id, revision));
CREATE TABLE overseer_materials (id TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE overseer_coverage (id TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE overseer_coverage_sources (coverage_id TEXT NOT NULL REFERENCES overseer_coverage(id), provider TEXT NOT NULL, kind TEXT NOT NULL, namespace TEXT NOT NULL, record_id TEXT NOT NULL, revision TEXT NOT NULL, content_hash TEXT NOT NULL, PRIMARY KEY (coverage_id, provider, kind, namespace, record_id, revision, content_hash));
CREATE INDEX overseer_coverage_source_lookup ON overseer_coverage_sources(provider, kind, namespace, record_id, revision, content_hash);
CREATE TABLE overseer_batches (id TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE overseer_inputs (id TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE overseer_owner_responses (response_key TEXT PRIMARY KEY, binding_id TEXT NOT NULL REFERENCES overseer_inputs(id), digest TEXT NOT NULL, record TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE overseer_input_resolutions (input_id TEXT PRIMARY KEY REFERENCES overseer_inputs(id), resolving_input_id TEXT NOT NULL REFERENCES overseer_inputs(id), response_key TEXT NOT NULL REFERENCES overseer_owner_responses(response_key));
CREATE TABLE overseer_operations (operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE overseer_application_events (id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES delta_applications(id), record TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE overseer_publications (application_id TEXT NOT NULL REFERENCES delta_applications(id), operation_id TEXT NOT NULL PRIMARY KEY, change_id TEXT NOT NULL, expected_base TEXT NOT NULL, candidate_commit TEXT, candidate_tree TEXT, branch_ref TEXT, worktree_ref TEXT, state_ref TEXT, file_hashes TEXT NOT NULL CHECK(json_valid(file_hashes) AND json_type(file_hashes)='array'), stage TEXT NOT NULL CHECK(length(stage)>0), observed_errors TEXT NOT NULL CHECK(json_valid(observed_errors) AND json_type(observed_errors)='array'));
CREATE TABLE overseer_migrations (from_version INTEGER NOT NULL, to_version INTEGER NOT NULL, completed_at TEXT NOT NULL, PRIMARY KEY (from_version, to_version));
"""


def install(conn):
    # sqlite3.executescript commits implicitly; execute each statement in the caller's transaction.
    for statement in DDL.split(';'):
        if statement.strip():
            conn.execute(statement)


OUTSIDE_DDL = "CREATE TABLE outside_deltas (id TEXT PRIMARY KEY, record TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL)"

def install_v3(conn):
    conn.execute(OUTSIDE_DDL)
