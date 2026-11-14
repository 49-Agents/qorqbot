"""Target DDL for the retained host domains, one tuple of statements per owner.

This committed file is the authority for the host's storage contribution, together with the extension-owned
contributions of the explicitly selected extensions and the Operations engine's own schema. It is a target-schema artifact
for an empty store: nothing here upgrades an existing database and no migration chain, Python hook or schema_migrations
bookkeeping exists. Edit it directly and review the diff; no generator rewrites it.
"""

from qorqut_operations import tables as operations_tables
from qorqut_operations.document_history import schema as history_schema
from qorqut_operations.documents import schema as documents_schema
from qorqut_operations.inbox import schema as inbox_schema
from qorqut_operations.records import schema as records_schema

OWNER_TABLES = {
    'access': ('credentials', 'machine_registrations', 'session_registrations', 'session_deliveries', 'artifacts'),
    'business': ('business_configs', 'workspace_presentations', 'management_routing'),
    'documents': ('documents', 'working_memory_versions', 'working_memory_provenance', 'qbdv_document_versions', 'qbdv_business_events', *operations_tables.TABLE_NAMES),
    'ledger': ('agent_activity',),
    'focus': ('agent_focus', 'focus_cards', 'focus_notes', 'agent_viewbox_boards', 'agent_viewbox_items'),
    'inbox': ('inbox_changes', 'native_session_owners', 'broadcasts', 'broadcast_deliveries', 'agent_messages', 'owner_conversations', 'owner_conversation_messages', 'owner_conversation_reads'),
    'delegation': ('operational_assignments', 'operational_assignment_events', 'operational_delegation_bindings', 'operational_action_receipts', 'operational_event_claims'),
    'browser': ('browser_turns', 'browser_turn_actions', 'browser_handles'),
    'learning': ('learning_candidates', 'learning_decisions', 'learning_runs', 'learning_scan_state', 'learning_history'),
    'incidents': ('system_incidents', 'system_incident_observations', 'system_incident_recoveries', 'system_incident_history', 'system_incident_ingested', 'system_incident_impacts', 'system_incident_fault_runs', 'agent_checkpoint_findings', 'dev_tickets', 'dev_ticket_events'),
    'global-chat': ('global_chat_bindings', 'global_chat_runner', 'global_chat_messages', 'global_chat_turns', 'global_chat_actions', 'global_chat_events', 'global_chat_reads'),
    'maintenance': ('imports', 'backup_runs', 'journal_reviews', 'journal_reconciliations', 'terminal_commands', 'terminal_actions'),
    'legacy-context': tuple(records_schema.TABLES),
}

OWNER_SCHEMA = {
    'access': (
        """CREATE TABLE artifacts (
    kind TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    manifest_schema_version INTEGER NOT NULL DEFAULT 1,
    stored_path TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (kind, artifact_id, version),
    UNIQUE (kind, artifact_id, digest)
)""",
        """CREATE TABLE credentials (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(id),
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT,
    revoked_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
        """CREATE TABLE machine_registrations (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(id),
    workspace TEXT NOT NULL UNIQUE,
    service_url TEXT NOT NULL,
    credential_id TEXT REFERENCES credentials(id),
    profile_revision INTEGER NOT NULL,
    settings_digest TEXT,
    skills_digest TEXT,
    projection_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(projection_json)),
    compatibility_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(compatibility_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
        """CREATE TABLE session_registrations (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL REFERENCES principals(id),
    registration_id TEXT REFERENCES machine_registrations(id),
    session_id TEXT,
    workspace TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (registration_id, session_id)
)""",
        """CREATE TABLE session_deliveries (
    id TEXT PRIMARY KEY,
    registration_id TEXT NOT NULL REFERENCES machine_registrations(id),
    session_id TEXT NOT NULL,
    effective_config_id TEXT NOT NULL,
    bundle_hash TEXT NOT NULL,
    lifecycle_event TEXT NOT NULL,
    emitted_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (registration_id, session_id)
)""",
        """CREATE TRIGGER artifacts_no_update BEFORE UPDATE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'published artifact records are immutable');
END""",
        """CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'published artifact records are immutable');
END""",
    ),
    'business': (
        """CREATE TABLE "business_configs" (
    business_id TEXT REFERENCES businesses(id),
    domain TEXT NOT NULL,
    config_json TEXT NOT NULL CHECK (json_valid(config_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (business_id IS NOT NULL OR domain IN ('tasks','task_customer_states')),
    PRIMARY KEY (business_id, domain)
)""",
        """CREATE UNIQUE INDEX business_configs_global_key ON business_configs(domain) WHERE business_id IS NULL""",
        """CREATE TABLE management_routing (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK(source_type IN ('signoffs','agent_messages')),
    source_id TEXT NOT NULL,
    business_id TEXT REFERENCES businesses(id),
    coordinator_id TEXT NOT NULL REFERENCES principals(id),
    attention_destination TEXT NOT NULL CHECK(attention_destination IN ('management','owner')),
    escalation_reason TEXT NOT NULL DEFAULT '' CHECK(length(escalation_reason)<=4000),
    recommendation TEXT NOT NULL DEFAULT '' CHECK(length(recommendation)<=8000),
    deadline TEXT,
    management_allowed INTEGER NOT NULL DEFAULT 0 CHECK(management_allowed IN (0,1)),
    disposition TEXT NOT NULL DEFAULT 'pending' CHECK(disposition IN ('pending','resolved','dismissed')),
    created_by TEXT NOT NULL REFERENCES principals(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT REFERENCES principals(id),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
    UNIQUE(source_type, source_id)
)""",
        """CREATE INDEX management_routing_scope ON management_routing(business_id,attention_destination,disposition,updated_at)""",
        """CREATE TABLE workspace_presentations (
    business_id TEXT NOT NULL REFERENCES businesses(id),
    id TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES principals(id),
    workspace_key TEXT NOT NULL,
    presentation_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(presentation_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(business_id, id),
    UNIQUE(business_id, agent_id, workspace_key)
)""",
        """CREATE INDEX workspace_presentations_agent ON workspace_presentations(agent_id, business_id, workspace_key)""",
    ),
    # One serialized group, in its committed order: Operations' documents, the host's working memory and jobs index, Operations'
    # document history, then Operations' tables. Splitting it into several groups would change the schema fingerprint.
    'documents': (
        *documents_schema.SCHEMA,
        *documents_schema.CHANGE_CAPTURE,
        """CREATE UNIQUE INDEX working_memory_scope ON documents(owning_agent_id, coalesce(business_id,''))
WHERE kind='working_memory'""",
        """CREATE TRIGGER working_memory_owner_insert BEFORE INSERT ON documents
WHEN NEW.kind='working_memory' AND NEW.owning_agent_id IS NULL BEGIN
 SELECT RAISE(ABORT,'Working memory requires an agent owner'); END""",
        """CREATE TRIGGER working_memory_owner_update BEFORE UPDATE ON documents
WHEN NEW.kind='working_memory' AND NEW.owning_agent_id IS NULL BEGIN
 SELECT RAISE(ABORT,'Working memory requires an agent owner'); END""",
        """CREATE TABLE working_memory_versions (
 document_id TEXT NOT NULL REFERENCES documents(id),
 version INTEGER NOT NULL CHECK(version > 0),
 business_id TEXT REFERENCES businesses(id),
 file_bytes TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 source_refs_json TEXT NOT NULL CHECK(json_valid(source_refs_json)),
 legacy_plan_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(legacy_plan_json)),
 author_id TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL,
 PRIMARY KEY(document_id,version)
)""",
        """CREATE TRIGGER working_memory_history_update BEFORE UPDATE ON working_memory_versions BEGIN
 SELECT RAISE(ABORT,'Working memory history is immutable'); END""",
        """CREATE TRIGGER working_memory_history_delete BEFORE DELETE ON working_memory_versions BEGIN
 SELECT RAISE(ABORT,'Working memory history is immutable'); END""",
        """CREATE INDEX jobs_business_kind_upload ON jobs(business_id, kind, json_extract(arguments_json, '$.upload_id'))""",
        """CREATE TABLE working_memory_provenance (
 document_id TEXT NOT NULL REFERENCES documents(id),
 version INTEGER NOT NULL CHECK(version > 0),
 business_id TEXT REFERENCES businesses(id),
 content_hash TEXT NOT NULL,
 source_refs_json TEXT NOT NULL CHECK(json_valid(source_refs_json)),
 legacy_plan_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(legacy_plan_json)),
 author_id TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL,
 PRIMARY KEY(document_id,version)
)""",
        """CREATE TRIGGER working_memory_provenance_update BEFORE UPDATE ON working_memory_provenance BEGIN
 SELECT RAISE(ABORT,'Working memory provenance is immutable'); END""",
        """CREATE TRIGGER working_memory_provenance_delete BEFORE DELETE ON working_memory_provenance BEGIN
 SELECT RAISE(ABORT,'Working memory provenance is immutable'); END""",
        *history_schema.SCHEMA,
        *operations_tables.SCHEMA,
    ),
    'ledger': (
        """CREATE TABLE agent_activity (
 agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id),
 session_id TEXT NOT NULL,
 registration_id TEXT NOT NULL REFERENCES machine_registrations(id),
 runtime TEXT NOT NULL CHECK(runtime IN ('claude-code','codex')),
 state TEXT NOT NULL DEFAULT 'unknown' CHECK(state IN ('running','idle','offline','unknown')),
 sequence INTEGER NOT NULL DEFAULT 0,
 last_seen_at TEXT,
 activity_at TEXT,
 items_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(items_json)),
 work_updated_at TEXT,
 revision INTEGER NOT NULL DEFAULT 1, work_reviewed_at TEXT, work_reviewed_turn TEXT, work_reviewed_sequence INTEGER NOT NULL DEFAULT 0, checkpoint_state TEXT NOT NULL DEFAULT 'unobserved', checkpoint_checked_at TEXT,
 PRIMARY KEY(agent_id,session_id)
)""",
        """CREATE INDEX agent_activity_seen ON agent_activity(agent_id,last_seen_at)""",
    ),
    'focus': (
        """CREATE TABLE agent_focus (
 agent_id TEXT PRIMARY KEY REFERENCES agent_profiles(agent_id),
 revision INTEGER NOT NULL DEFAULT 0,
 consumer_id TEXT,
 status TEXT NOT NULL DEFAULT '',
 items_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(items_json)),
 browser_json TEXT CHECK(browser_json IS NULL OR json_valid(browser_json)),
 activity_json TEXT CHECK(activity_json IS NULL OR json_valid(activity_json)),
 updated_at TEXT
)""",
        """CREATE TABLE focus_cards (
 id TEXT PRIMARY KEY,
 source_key TEXT NOT NULL UNIQUE,
 source_json TEXT CHECK(source_json IS NULL OR json_valid(source_json)),
 reference_json TEXT CHECK(reference_json IS NULL OR json_valid(reference_json)),
 context_json TEXT CHECK(context_json IS NULL OR json_valid(context_json)),
 reason TEXT NOT NULL DEFAULT '',
 requested_response TEXT NOT NULL DEFAULT '',
 recommendation TEXT NOT NULL DEFAULT '',
 references_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(references_json)),
 position INTEGER NOT NULL,
 deferred_until TEXT,
 archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
 revision INTEGER NOT NULL DEFAULT 1,
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
, display_name TEXT NOT NULL DEFAULT '', blocker INTEGER NOT NULL DEFAULT 0 CHECK(blocker IN (0,1)), urgent INTEGER NOT NULL DEFAULT 0 CHECK(urgent IN (0,1)))""",
        """CREATE INDEX focus_cards_order ON focus_cards(archived,position,id)""",
        """CREATE TABLE focus_notes (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE,
 card_id TEXT NOT NULL REFERENCES focus_cards(id),
 refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(refs_json)),
 body TEXT NOT NULL CHECK(length(trim(body))>0 AND length(body)<=20000),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL
, summary TEXT)""",
        """CREATE INDEX focus_notes_thread ON focus_notes(card_id,sequence)""",
        """CREATE TRIGGER focus_notes_no_update BEFORE UPDATE ON focus_notes BEGIN SELECT RAISE(ABORT,'Notes are immutable'); END""",
        """CREATE TRIGGER focus_notes_no_delete BEFORE DELETE ON focus_notes BEGIN SELECT RAISE(ABORT,'Notes are immutable'); END""",
        """CREATE TABLE agent_viewbox_boards (
    agent_id TEXT PRIMARY KEY REFERENCES principals(id),
    sequence INTEGER NOT NULL DEFAULT 0 CHECK (sequence >= 0),
    updated_at TEXT NOT NULL
)""",
        """CREATE TABLE agent_viewbox_items (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES principals(id),
    canonical_link TEXT NOT NULL CHECK (length(canonical_link) BETWEEN 1 AND 4096),
    link TEXT NOT NULL CHECK (length(link) BETWEEN 1 AND 4096),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 80),
    created_at TEXT NOT NULL,
    placed_at TEXT NOT NULL,
    placement_sequence INTEGER NOT NULL CHECK (placement_sequence >= 1),
    UNIQUE (agent_id, canonical_link),
    UNIQUE (agent_id, placement_sequence)
)""",
        """INSERT INTO change_sequences(name, value) VALUES ('focus_board', 0)""",
    ),
    'inbox': (
        *inbox_schema.CHANGE_SCHEMA,
        """CREATE TABLE native_session_owners (
 agent_id TEXT PRIMARY KEY REFERENCES agent_profiles(agent_id),
 consumer_id TEXT NOT NULL,
 binding_json TEXT NOT NULL CHECK(json_valid(binding_json) AND json_type(binding_json)='object' AND binding_json<>'{}'),
 generation INTEGER NOT NULL CHECK(generation>=1),
 desired_state TEXT NOT NULL DEFAULT 'running' CHECK(desired_state IN ('running','paused','stopped'))
)""",
        *inbox_schema.CAPTURE_SCHEMA,
        *inbox_schema.BROADCAST_SCHEMA,
        """CREATE TABLE owner_conversations (
 id TEXT PRIMARY KEY,
 source_key TEXT NOT NULL UNIQUE,
 business_id TEXT REFERENCES businesses(id),
 source_type TEXT NOT NULL CHECK(source_type IN ('task','signoff','reference','project','table')),
 source_id TEXT NOT NULL,
 audience TEXT NOT NULL CHECK(audience='owner_ordubashy'),
 revision INTEGER NOT NULL DEFAULT 1,
 latest_sequence INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
, peer_agent_id TEXT REFERENCES principals(id))""",
        """CREATE TABLE owner_conversation_messages (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 id TEXT NOT NULL UNIQUE,
 conversation_id TEXT NOT NULL REFERENCES owner_conversations(id),
 created_by TEXT NOT NULL REFERENCES principals(id),
 author_role TEXT NOT NULL CHECK(author_role IN ('owner','coordinator')),
 body TEXT NOT NULL CHECK(length(trim(body))>0 AND length(body)<=20000),
 summary TEXT CHECK(summary IS NULL OR (length(trim(summary))>0 AND length(summary)<=500)),
 created_at TEXT NOT NULL,
 refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(refs_json))
)""",
        """CREATE INDEX owner_conversation_messages_thread ON owner_conversation_messages(conversation_id,sequence)""",
        """CREATE TRIGGER owner_conversation_messages_no_update BEFORE UPDATE ON owner_conversation_messages BEGIN SELECT RAISE(ABORT,'Private messages are immutable'); END""",
        """CREATE TRIGGER owner_conversation_messages_no_delete BEFORE DELETE ON owner_conversation_messages BEGIN SELECT RAISE(ABORT,'Private messages are immutable'); END""",
        """CREATE TABLE owner_conversation_reads (
 conversation_id TEXT NOT NULL REFERENCES owner_conversations(id),
 principal_id TEXT NOT NULL REFERENCES principals(id),
 last_read_sequence INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(conversation_id,principal_id)
)""",
        *inbox_schema.PRINCIPAL_CAPTURE_SCHEMA,
        *inbox_schema.MESSAGE_SCHEMA,
        """CREATE INDEX owner_conversation_peer ON owner_conversations(peer_agent_id,source_type,source_id)""",
        *inbox_schema.RELATIONSHIP_CAPTURE_SCHEMA,
    ),
    'delegation': (
        """CREATE TABLE operational_assignments (
 id TEXT PRIMARY KEY,
 parent_principal_id TEXT NOT NULL REFERENCES principals(id),
 business_id TEXT REFERENCES businesses(id),
 scope_json TEXT NOT NULL CHECK(json_valid(scope_json)),
 objective TEXT NOT NULL CHECK(length(objective) BETWEEN 1 AND 10000),
 plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
 plan_revision INTEGER NOT NULL CHECK(plan_revision >= 1),
 links_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(links_json)),
 allowed_operations_json TEXT NOT NULL CHECK(json_valid(allowed_operations_json)),
 constraints_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(constraints_json)),
 internal_recipients_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(internal_recipients_json)),
 completion_evidence_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(completion_evidence_json)),
 escalation_rules_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(escalation_rules_json)),
 resource_bounds_json TEXT NOT NULL CHECK(json_valid(resource_bounds_json)),
 selected_model TEXT NOT NULL,
 reasoning_effort TEXT NOT NULL CHECK(reasoning_effort IN ('low','medium','high','xhigh')),
 state TEXT NOT NULL CHECK(state IN ('queued','running','waiting','blocked','paused','completed','cancelled')),
 dispatch_reason TEXT,
 checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(checkpoint_json)),
 next_step TEXT,
 wake_at TEXT,
 wake_condition_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(wake_condition_json)),
 result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
 execution_generation INTEGER NOT NULL DEFAULT 0 CHECK(execution_generation >= 0),
 lease_id TEXT,
 lease_until TEXT,
 attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
 max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 20),
 runtime_thread_id TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1), execution_started_at TEXT, runtime_used_seconds INTEGER NOT NULL DEFAULT 0
    CHECK(runtime_used_seconds >= 0), retry_count INTEGER NOT NULL DEFAULT 0
    CHECK(retry_count >= 0),
 CHECK((state='running' AND lease_id IS NOT NULL AND lease_until IS NOT NULL)
       OR (state<>'running' AND lease_id IS NULL AND lease_until IS NULL))
)""",
        """CREATE INDEX operational_assignments_parent ON operational_assignments(parent_principal_id,state,updated_at)""",
        """CREATE INDEX operational_assignments_due ON operational_assignments(state,wake_at,lease_until)""",
        """CREATE TABLE operational_assignment_events (
 id TEXT PRIMARY KEY,
 assignment_id TEXT NOT NULL REFERENCES operational_assignments(id),
 sequence INTEGER NOT NULL CHECK(sequence >= 1),
 execution_generation INTEGER NOT NULL CHECK(execution_generation >= 0),
 actor_principal_id TEXT NOT NULL REFERENCES principals(id),
 event_type TEXT NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 created_at TEXT NOT NULL,
 UNIQUE(assignment_id,sequence)
)""",
        """CREATE INDEX operational_assignment_events_history ON operational_assignment_events(assignment_id,sequence)""",
        """CREATE TABLE operational_delegation_bindings (
 id TEXT PRIMARY KEY,
 assignment_id TEXT NOT NULL REFERENCES operational_assignments(id),
 parent_principal_id TEXT NOT NULL REFERENCES principals(id),
 execution_generation INTEGER NOT NULL CHECK(execution_generation >= 1),
 credential_hash TEXT NOT NULL UNIQUE,
 registration_id TEXT NOT NULL REFERENCES machine_registrations(id),
 runtime_thread_id TEXT,
 expires_at TEXT NOT NULL,
 revoked_at TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
 UNIQUE(assignment_id,execution_generation)
)""",
        """CREATE INDEX operational_delegation_bindings_assignment ON operational_delegation_bindings(assignment_id,revoked_at)""",
        """CREATE TABLE operational_action_receipts (
 id TEXT PRIMARY KEY,
 assignment_id TEXT NOT NULL REFERENCES operational_assignments(id),
 action_key TEXT NOT NULL CHECK(length(action_key) BETWEEN 1 AND 200),
 execution_generation INTEGER NOT NULL CHECK(execution_generation >= 1),
 operation_name TEXT NOT NULL,
 payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
 state TEXT NOT NULL CHECK(state IN ('started','uncertain','applied')),
 effect_ref_json TEXT CHECK(effect_ref_json IS NULL OR json_valid(effect_ref_json)),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1), parent_principal_id TEXT REFERENCES principals(id), delegation_binding_id TEXT REFERENCES operational_delegation_bindings(id), plan_revision INTEGER CHECK(plan_revision IS NULL OR plan_revision >= 1),
 UNIQUE(assignment_id,action_key)
)""",
        """CREATE INDEX operational_action_receipts_state ON operational_action_receipts(assignment_id,state)""",
        """CREATE TABLE operational_event_claims (
 event_id INTEGER PRIMARY KEY REFERENCES inbox_events(id),
 assignment_id TEXT NOT NULL REFERENCES operational_assignments(id),
 execution_generation INTEGER NOT NULL CHECK(execution_generation >= 1),
 state TEXT NOT NULL CHECK(state IN ('claimed','handled','released')),
 lease_until TEXT,
 outcome TEXT,
 handled_at TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
 CHECK((state='claimed' AND lease_until IS NOT NULL AND handled_at IS NULL)
       OR (state='handled' AND lease_until IS NULL AND handled_at IS NOT NULL)
       OR (state='released' AND lease_until IS NULL AND handled_at IS NULL))
)""",
        """CREATE INDEX operational_event_claims_assignment ON operational_event_claims(assignment_id,state,event_id)""",
    ),
    'browser': (
        """CREATE UNIQUE INDEX businesses_browser_cdp_port
ON businesses(json_extract(config_json, '$.browser.cdp_port'))
WHERE json_extract(config_json, '$.browser.cdp_port') IS NOT NULL""",
        """CREATE TABLE browser_turns (
 id TEXT PRIMARY KEY,
 browser_key TEXT NOT NULL,
 business_id TEXT REFERENCES businesses(id),
 assignment_id TEXT REFERENCES operational_assignments(id),
 parent_principal_id TEXT NOT NULL REFERENCES principals(id),
 execution_generation INTEGER,
 requester_kind TEXT NOT NULL CHECK(requester_kind IN ('assignment','agent','owner')),
 requester_id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('queued','owning','released','blocked')),
 lease_id TEXT,
 lease_until TEXT,
 checkpoint_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(checkpoint_json)),
 last_action TEXT,
 pending_outcome_json TEXT CHECK(pending_outcome_json IS NULL OR json_valid(pending_outcome_json)),
 reason TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 released_at TEXT,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1), command_id TEXT, command_until TEXT,
 CHECK((state='owning' AND lease_id IS NOT NULL AND lease_until IS NOT NULL)
       OR (state<>'owning' AND lease_id IS NULL AND lease_until IS NULL))
)""",
        """CREATE UNIQUE INDEX browser_turns_one_owner ON browser_turns(browser_key) WHERE state='owning'""",
        """CREATE INDEX browser_turns_queue ON browser_turns(browser_key,state,created_at,id)""",
        """CREATE INDEX browser_turns_assignment ON browser_turns(assignment_id,state,updated_at)""",
        """CREATE TABLE browser_turn_actions (
 id TEXT PRIMARY KEY,
 turn_id TEXT NOT NULL REFERENCES browser_turns(id),
 action_id TEXT NOT NULL UNIQUE,
 command_json TEXT NOT NULL CHECK(json_valid(command_json)),
 state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed','uncertain','reconciled')),
 outcome_json TEXT CHECK(outcome_json IS NULL OR json_valid(outcome_json)),
 error TEXT,
 created_at TEXT NOT NULL,
 started_at TEXT,
 updated_at TEXT NOT NULL,
 finished_at TEXT,
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)
)""",
        """CREATE INDEX browser_turn_actions_turn ON browser_turn_actions(turn_id,created_at,id)""",
        """CREATE INDEX browser_turns_command_fence ON browser_turns(browser_key,state,command_until)""",
        """CREATE TABLE browser_handles (
 id TEXT PRIMARY KEY,
 turn_id TEXT NOT NULL REFERENCES browser_turns(id),
 binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
 arguments_json TEXT NOT NULL CHECK(json_valid(arguments_json)),
 superseded INTEGER NOT NULL DEFAULT 0 CHECK(superseded IN (0,1))
)""",
        """CREATE INDEX browser_handles_turn ON browser_handles(turn_id)""",
    ),
    'learning': (
        """CREATE TABLE "learning_candidates" (
    business_id TEXT REFERENCES businesses(id),
    id TEXT NOT NULL,
    candidate_json TEXT NOT NULL CHECK (json_valid(candidate_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    work_state TEXT CHECK(work_state IN ('in-progress', 'implemented', 'rejected-in-work')),
    agent_note TEXT NOT NULL DEFAULT '',
    work_targets_json TEXT NOT NULL DEFAULT '[]',
    work_outcome_json TEXT NOT NULL DEFAULT '{}',
    filing_json TEXT NOT NULL DEFAULT '{}',
    scope_key TEXT GENERATED ALWAYS AS (CASE WHEN business_id IS NULL THEN 'global' ELSE 'business:' || business_id END) STORED,
    PRIMARY KEY (business_id, id)
)""",
        """CREATE UNIQUE INDEX learning_candidates_workspace ON learning_candidates(scope_key, id)""",
        """CREATE TABLE "learning_decisions" (
    business_id TEXT,
    candidate_id TEXT NOT NULL,
    state TEXT NOT NULL,
    feedback TEXT,
    changes_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(changes_json)),
    decided_by TEXT NOT NULL REFERENCES principals(id),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (CASE WHEN business_id IS NULL THEN 'global' ELSE 'business:' || business_id END) STORED,
    PRIMARY KEY (business_id, candidate_id),
    FOREIGN KEY (scope_key, candidate_id) REFERENCES learning_candidates(scope_key, id)
)""",
        """CREATE UNIQUE INDEX learning_decisions_workspace ON learning_decisions(scope_key, candidate_id)""",
        """CREATE TABLE "learning_runs" (
    business_id TEXT REFERENCES businesses(id),
    id TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    created_at TEXT NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (CASE WHEN business_id IS NULL THEN 'global' ELSE 'business:' || business_id END) STORED,
    PRIMARY KEY (business_id, id)
)""",
        """CREATE UNIQUE INDEX learning_runs_workspace ON learning_runs(scope_key, id)""",
        """CREATE TABLE "learning_scan_state" (
    business_id TEXT REFERENCES businesses(id),
    session_id TEXT NOT NULL,
    transcript_hash TEXT NOT NULL,
    state_json TEXT NOT NULL CHECK (json_valid(state_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (CASE WHEN business_id IS NULL THEN 'global' ELSE 'business:' || business_id END) STORED,
    PRIMARY KEY (business_id, session_id)
)""",
        """CREATE UNIQUE INDEX learning_scan_state_workspace ON learning_scan_state(scope_key, session_id)""",
        """CREATE TABLE "learning_history" (
    business_id TEXT,
    id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    event TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES principals(id),
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    scope_key TEXT GENERATED ALWAYS AS (CASE WHEN business_id IS NULL THEN 'global' ELSE 'business:' || business_id END) STORED,
    PRIMARY KEY (business_id, id),
    FOREIGN KEY (scope_key, candidate_id) REFERENCES learning_candidates(scope_key, id)
)""",
        """CREATE UNIQUE INDEX learning_history_workspace ON learning_history(scope_key, id)""",
        """CREATE INDEX learning_history_candidate ON learning_history(business_id, candidate_id, created_at, id)""",
    ),
    'incidents': (
        """CREATE TABLE dev_tickets (
 id TEXT PRIMARY KEY,
 title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200),
 details TEXT NOT NULL DEFAULT '' CHECK(length(details)<=12000),
 kind TEXT NOT NULL DEFAULT 'bug' CHECK(kind IN ('bug','feature','tooling','workflow')),
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
 reporter_id TEXT NOT NULL REFERENCES principals(id),
 business_id TEXT REFERENCES businesses(id),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1
, resolved_at TEXT, resolved_by TEXT REFERENCES principals(id), resolution TEXT NOT NULL DEFAULT '' CHECK(length(resolution)<=12000))""",
        """CREATE INDEX dev_tickets_queue ON dev_tickets(status,created_at,id)""",
        """CREATE TABLE dev_ticket_events (
 id TEXT PRIMARY KEY,
 ticket_id TEXT NOT NULL REFERENCES dev_tickets(id),
 event_kind TEXT NOT NULL CHECK(event_kind IN ('follow_up','resolved','status_changed')),
 actor_id TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL,
 details TEXT NOT NULL DEFAULT '' CHECK(length(details)<=12000),
 resolution TEXT NOT NULL DEFAULT '' CHECK(length(resolution)<=12000),
 verification TEXT NOT NULL DEFAULT '' CHECK(length(verification)<=12000),
 deployment_ref TEXT NOT NULL DEFAULT '' CHECK(length(deployment_ref)<=200)
)""",
        """CREATE INDEX dev_ticket_events_ticket ON dev_ticket_events(ticket_id,created_at,id)""",
        """CREATE TABLE agent_checkpoint_findings (
 agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id),
 check_key TEXT NOT NULL CHECK(length(check_key)<=200),
 code TEXT NOT NULL,
 business_id TEXT REFERENCES businesses(id),
 task_id TEXT,
 session_id TEXT NOT NULL,
 episode INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL CHECK(state IN ('open','resolved')),
 message TEXT NOT NULL CHECK(length(message)<=1000),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 resolved_at TEXT,
 claimed_at TEXT,
 claim_id TEXT,
 PRIMARY KEY(agent_id,check_key)
)""",
        """CREATE INDEX agent_checkpoint_open ON agent_checkpoint_findings(agent_id,state)""",
        """CREATE TABLE system_incidents (
 id TEXT PRIMARY KEY,
 fingerprint TEXT NOT NULL UNIQUE,
 ticket_id TEXT NOT NULL UNIQUE REFERENCES dev_tickets(id),
 title TEXT NOT NULL CHECK(length(title)<=200),
 status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','recovering','closed')),
 classification TEXT NOT NULL CHECK(length(classification)<=60),
 first_seen TEXT NOT NULL,
 last_seen TEXT NOT NULL,
 occurrence_count INTEGER NOT NULL DEFAULT 0 CHECK(occurrence_count>=0),
 recovery_count INTEGER NOT NULL DEFAULT 0 CHECK(recovery_count>=0),
 business_id TEXT REFERENCES businesses(id),
 representative_observation_id TEXT
)""",
        """CREATE INDEX system_incidents_scope ON system_incidents(business_id,status,last_seen)""",
        """CREATE TABLE system_incident_observations (
 id TEXT PRIMARY KEY,
 fingerprint TEXT NOT NULL REFERENCES system_incidents(fingerprint),
 observed_at TEXT NOT NULL,
 payload_json TEXT NOT NULL CHECK(length(payload_json)<=4096)
)""",
        """CREATE INDEX system_incident_observations_fingerprint ON system_incident_observations(fingerprint,observed_at)""",
        """CREATE TABLE system_incident_recoveries (
 incident_id TEXT NOT NULL REFERENCES system_incidents(id),
 observation_id TEXT NOT NULL UNIQUE REFERENCES system_incident_observations(id),
 observed_at TEXT NOT NULL,
 evidence TEXT NOT NULL CHECK(length(evidence)<=500)
, agent_id TEXT REFERENCES principals(id), episode INTEGER NOT NULL DEFAULT 1)""",
        """CREATE UNIQUE INDEX system_incident_recovery_agent ON system_incident_recoveries(incident_id,coalesce(agent_id,''),episode)""",
        """CREATE TABLE system_incident_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 incident_id TEXT NOT NULL REFERENCES system_incidents(id),
 event_kind TEXT NOT NULL CHECK(event_kind IN ('opened','observed','recovered')),
 observed_at TEXT NOT NULL,
 observation_id TEXT,
 details TEXT NOT NULL DEFAULT '' CHECK(length(details)<=500)
)""",
        """CREATE INDEX system_incident_history_incident ON system_incident_history(incident_id,observed_at,id)""",
        """CREATE TABLE system_incident_ingested (
 observation_id TEXT PRIMARY KEY,
 created_at TEXT NOT NULL
)""",
        """CREATE INDEX system_incident_ingested_retention ON system_incident_ingested(created_at)""",
        """CREATE TABLE system_incident_impacts (
 incident_id TEXT NOT NULL REFERENCES system_incidents(id),
 business_id TEXT REFERENCES businesses(id),
 agent_id TEXT REFERENCES principals(id),
 session_id TEXT,
 task_id TEXT,
 operation TEXT,
 job_id TEXT,
 receipt_id TEXT,
 episode INTEGER NOT NULL DEFAULT 1,
 recovered_at TEXT,
 first_seen TEXT NOT NULL,
 last_seen TEXT NOT NULL,
 PRIMARY KEY(incident_id,business_id,agent_id,session_id,task_id,operation,job_id,receipt_id)
)""",
        """CREATE INDEX system_incident_impacts_agent ON system_incident_impacts(agent_id,business_id,session_id,incident_id)""",
        """CREATE INDEX system_incident_impacts_task ON system_incident_impacts(task_id,incident_id)""",
        """CREATE UNIQUE INDEX system_incident_impacts_identity ON system_incident_impacts(
 incident_id,coalesce(business_id,''),coalesce(agent_id,''),coalesce(session_id,''),coalesce(task_id,''),coalesce(operation,''),coalesce(job_id,''),coalesce(receipt_id,''))""",
        """CREATE TABLE system_incident_fault_runs (
 fingerprint TEXT NOT NULL,
 agent_key TEXT NOT NULL,
 session_id TEXT,
 business_id TEXT,
 operation TEXT NOT NULL,
 component TEXT NOT NULL,
 call_site TEXT NOT NULL,
 first_seen TEXT NOT NULL,
 last_seen TEXT NOT NULL,
 failures INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(fingerprint,agent_key,session_id)
)""",
    ),
    'global-chat': (
        """CREATE TABLE global_chat_bindings (
    agent_id TEXT PRIMARY KEY REFERENCES principals(id),
    runtime TEXT NOT NULL CHECK(runtime IN ('claude-code','codex')),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    desired_state TEXT NOT NULL DEFAULT 'stopped' CHECK(desired_state IN ('stopped','running')),
    native_session_id TEXT CHECK(native_session_id IS NULL OR length(native_session_id) BETWEEN 1 AND 200),
    native_runtime TEXT CHECK(native_runtime IS NULL OR native_runtime IN ('claude-code','codex')),
    native_state TEXT CHECK(native_state IS NULL OR native_state IN ('reserved','confirmed')),
    runner_state TEXT NOT NULL DEFAULT 'offline' CHECK(runner_state IN ('offline','starting','ready','error')),
    runner_detail TEXT CHECK(runner_detail IS NULL OR length(runner_detail) <= 500),
    next_sequence INTEGER NOT NULL DEFAULT 1 CHECK(next_sequence >= 1),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    enabled_by TEXT REFERENCES principals(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT""",
        """CREATE TABLE global_chat_runner (
    id TEXT PRIMARY KEY CHECK(id = 'runner'),
    token TEXT NOT NULL CHECK(length(token) >= 16),
    pid INTEGER NOT NULL CHECK(pid > 0),
    process_start TEXT,
    host TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    lease_until TEXT NOT NULL
) STRICT""",
        """CREATE TABLE global_chat_messages (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES global_chat_bindings(agent_id),
    sequence INTEGER NOT NULL CHECK(sequence >= 1),
    role TEXT NOT NULL CHECK(role IN ('owner','agent','notice')),
    body TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','interrupted','uncertain')),
    origin TEXT NOT NULL CHECK(origin IN ('chat','telegram','inbox','broadcast','ping','system')),
    turn_id TEXT,
    caller_id TEXT REFERENCES principals(id),
    idempotency_key TEXT CHECK(idempotency_key IS NULL OR length(idempotency_key) BETWEEN 8 AND 120),
    request_hash TEXT,
    correlation_id TEXT,
    error_code TEXT,
    error_message TEXT CHECK(error_message IS NULL OR length(error_message) <= 500),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(agent_id, sequence)
) STRICT""",
        """CREATE UNIQUE INDEX global_chat_message_idempotency ON global_chat_messages(agent_id, caller_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL""",
        """CREATE UNIQUE INDEX global_chat_message_correlation ON global_chat_messages(agent_id, origin, correlation_id)
    WHERE correlation_id IS NOT NULL""",
        """CREATE INDEX global_chat_message_turn ON global_chat_messages(turn_id)""",
        """CREATE TABLE global_chat_turns (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES global_chat_bindings(agent_id),
    origin TEXT NOT NULL CHECK(origin IN ('chat','telegram','inbox','broadcast','ping','system')),
    input_message_id TEXT REFERENCES global_chat_messages(id),
    input_text TEXT NOT NULL CHECK(length(input_text) BETWEEN 1 AND 70000),
    reply_message_id TEXT REFERENCES global_chat_messages(id),
    state TEXT NOT NULL CHECK(state IN ('queued','dispatching','running','completed','failed','interrupted','uncertain','cancelled')),
    correlation_json TEXT CHECK(correlation_json IS NULL OR json_valid(correlation_json)),
    native_session_id TEXT,
    native_turn_id TEXT,
    stop_requested INTEGER NOT NULL DEFAULT 0 CHECK(stop_requested IN (0,1)),
    error_code TEXT,
    error_message TEXT CHECK(error_message IS NULL OR length(error_message) <= 500),
    created_at TEXT NOT NULL,
    dispatch_intent_at TEXT,
    started_at TEXT,
    finished_at TEXT
) STRICT""",
        """CREATE INDEX global_chat_turn_queue ON global_chat_turns(agent_id, state)""",
        """CREATE TABLE global_chat_actions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES global_chat_bindings(agent_id),
    turn_id TEXT NOT NULL REFERENCES global_chat_turns(id),
    kind TEXT NOT NULL CHECK(kind IN ('permission','question')),
    state TEXT NOT NULL CHECK(state IN ('pending','answered','expired','cancelled')),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    response_json TEXT CHECK(response_json IS NULL OR json_valid(response_json)),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT REFERENCES principals(id)
) STRICT""",
        """CREATE INDEX global_chat_action_pending ON global_chat_actions(agent_id, state)""",
        """CREATE TABLE global_chat_events (
    cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES global_chat_bindings(agent_id),
    kind TEXT NOT NULL CHECK(kind IN ('message','delta','turn','action','runtime')),
    message_id TEXT,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    created_at TEXT NOT NULL
) STRICT""",
        """CREATE INDEX global_chat_event_agent ON global_chat_events(agent_id, cursor)""",
        """CREATE INDEX global_chat_event_message ON global_chat_events(message_id, kind)""",
        """CREATE TABLE global_chat_reads (
    agent_id TEXT NOT NULL REFERENCES global_chat_bindings(agent_id),
    principal_id TEXT NOT NULL REFERENCES principals(id),
    last_read_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_read_sequence >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, principal_id)
) STRICT""",
        """CREATE TRIGGER global_chat_message_immutable BEFORE UPDATE ON global_chat_messages
WHEN NEW.id IS NOT OLD.id OR NEW.agent_id IS NOT OLD.agent_id OR NEW.sequence IS NOT OLD.sequence OR NEW.role IS NOT OLD.role
  OR NEW.origin IS NOT OLD.origin OR NEW.created_at IS NOT OLD.created_at OR NEW.caller_id IS NOT OLD.caller_id
  OR NEW.idempotency_key IS NOT OLD.idempotency_key
BEGIN
    SELECT RAISE(ABORT, 'Chat message identity, order and authorship never change');
END""",
        """CREATE TRIGGER global_chat_message_no_delete BEFORE DELETE ON global_chat_messages
BEGIN
    SELECT RAISE(ABORT, 'Chat history is never deleted');
END""",
    ),
    'maintenance': (
        """CREATE TABLE imports (
    id TEXT PRIMARY KEY,
    source_fingerprint TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json)),
    report_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(report_json)),
    identity_map_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(identity_map_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
        """CREATE TABLE backup_runs (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    state TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    checksum TEXT,
    manifest_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(manifest_json)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
        """CREATE TABLE terminal_commands (
 id TEXT PRIMARY KEY,
 issuer_id TEXT NOT NULL REFERENCES principals(id),
 targets_json TEXT NOT NULL CHECK(json_valid(targets_json)),
 action TEXT NOT NULL CHECK(action IN ('start','restart','kill','ping')),
 message TEXT,
 expires_at TEXT NOT NULL,
 consumed_by TEXT,
 revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
)""",
        """CREATE TABLE terminal_actions (
 id TEXT PRIMARY KEY,
 batch_id TEXT NOT NULL,
 proposer_id TEXT NOT NULL REFERENCES principals(id),
 target_id TEXT NOT NULL REFERENCES principals(id),
 action TEXT NOT NULL CHECK(action IN ('start','restart','kill','ping')),
 message TEXT,
 binding_json TEXT NOT NULL CHECK(json_valid(binding_json)),
 state TEXT NOT NULL CHECK(state IN ('queued','uncertain','completed','failed')),
 detail TEXT,
 revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
)""",
        """CREATE INDEX terminal_actions_queue ON terminal_actions(state,created_at)""",
        """CREATE INDEX terminal_actions_batch ON terminal_actions(batch_id,created_at,id)""",
        """CREATE TABLE journal_reviews (
 id TEXT PRIMARY KEY,
 document_id TEXT NOT NULL REFERENCES documents(id),
 reviewer_id TEXT NOT NULL REFERENCES principals(id),
 document_revision INTEGER NOT NULL,
 content_hash TEXT NOT NULL,
 reviewed_at TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,
 UNIQUE(document_id,reviewer_id)
)""",
        """CREATE TABLE journal_reconciliations (
 id TEXT PRIMARY KEY,
 document_id TEXT NOT NULL REFERENCES documents(id),
 reviewer_id TEXT NOT NULL REFERENCES principals(id),
 document_revision INTEGER NOT NULL,
 content_hash TEXT NOT NULL,
 assignment_id TEXT NOT NULL UNIQUE,
 outcome TEXT CHECK(outcome IN ('accounted_for','partial','missing','uncertain','nothing_to_file')),
 report_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(report_json)),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1
)""",
        """CREATE INDEX journal_reconciliations_entry ON journal_reconciliations(document_id,created_at)""",
    ),
    'legacy-context': (*records_schema.SCHEMA, *records_schema.CHANGE_CAPTURE),
}
