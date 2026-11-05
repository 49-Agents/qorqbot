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
    'business': ('business_configs', 'business_sandboxes', 'business_previews', 'workspace_presentations', 'management_routing'),
    'documents': ('documents', 'working_memory_versions', 'working_memory_provenance', 'qbdv_document_versions', 'qbdv_business_events', *operations_tables.TABLE_NAMES),
    'ledger': ('agent_activity',),
    'focus': ('agent_focus', 'focus_cards', 'focus_notes', 'agent_viewbox_boards', 'agent_viewbox_items'),
    'inbox': ('inbox_changes', 'native_session_owners', 'broadcasts', 'broadcast_deliveries', 'agent_messages', 'owner_conversations', 'owner_conversation_messages', 'owner_conversation_reads'),
    'delegation': ('operational_assignments', 'operational_assignment_events', 'operational_delegation_bindings', 'operational_action_receipts', 'operational_event_claims'),
    'browser': ('browser_turns', 'browser_turn_actions', 'browser_handles'),
    'learning': ('learning_candidates', 'learning_decisions', 'learning_runs', 'learning_scan_state', 'learning_history'),
    'incidents': ('system_incidents', 'system_incident_observations', 'system_incident_recoveries', 'system_incident_history', 'system_incident_ingested', 'system_incident_impacts', 'system_incident_fault_runs', 'agent_checkpoint_findings', 'dev_tickets', 'dev_ticket_events'),
    'telegram': ('telegram_state', 'telegram_updates', 'telegram_messages', 'telegram_outbox', 'telegram_events', 'telegram_signoff_posts', 'telegram_owner_inputs', 'telegram_cleanup_jobs'),
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
        """CREATE TABLE business_sandboxes (
    business_id TEXT PRIMARY KEY REFERENCES businesses(id),
    desired_generation INTEGER NOT NULL CHECK (desired_generation >= 1),
    desired_state TEXT NOT NULL DEFAULT 'running' CHECK (desired_state IN ('running', 'stopped')),
    observed_generation INTEGER CHECK (observed_generation >= 1),
    environment_id TEXT CHECK (environment_id IS NULL OR (
        length(environment_id) BETWEEN 1 AND 256
        AND environment_id NOT GLOB '*[^ -~]*'
    )),
    state TEXT NOT NULL CHECK (state IN ('pending', 'ready', 'stopped', 'failed')),
    error_code TEXT CHECK (error_code IS NULL OR error_code IN (
        'configuration_missing',
        'environment_conflict',
        'environment_unavailable',
        'environment_unhealthy',
        'provisioning_failed'
    )),
    lease_fence INTEGER NOT NULL DEFAULT 0 CHECK (lease_fence >= 0),
    lease_until TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, disk_used_bytes INTEGER CHECK (disk_used_bytes IS NULL OR disk_used_bytes >= 0), disk_budget_bytes INTEGER CHECK (disk_budget_bytes IS NULL OR disk_budget_bytes >= 0), disk_reserved_bytes INTEGER CHECK (disk_reserved_bytes IS NULL OR disk_reserved_bytes >= 0), disk_available_bytes INTEGER CHECK (disk_available_bytes IS NULL OR disk_available_bytes >= 0), disk_host_floor_bytes INTEGER CHECK (disk_host_floor_bytes IS NULL OR disk_host_floor_bytes >= 0), disk_checked_at TEXT, disk_error_code TEXT CHECK (disk_error_code IS NULL OR disk_error_code IN ('environment_conflict', 'environment_unavailable')),
    CHECK ((observed_generation IS NULL) = (environment_id IS NULL)),
    CHECK (observed_generation IS NULL OR observed_generation <= desired_generation),
    CHECK (state <> 'ready' OR (
        desired_state = 'running' AND observed_generation = desired_generation
        AND environment_id IS NOT NULL
        AND error_code IS NULL
    )),
    CHECK (state <> 'stopped' OR (
        desired_state = 'stopped' AND error_code IS NULL
    )),
    CHECK (
        (state = 'pending' AND (error_code IS NULL OR error_code = 'configuration_missing'))
        OR (state = 'failed' AND error_code IS NOT NULL)
        OR (state IN ('ready', 'stopped') AND error_code IS NULL)
    ),
    CHECK (lease_until IS NULL OR length(lease_until) BETWEEN 20 AND 40)
)""",
        """CREATE TABLE business_previews (
    id TEXT PRIMARY KEY,
    business_id TEXT NOT NULL REFERENCES businesses(id),
    workload_port INTEGER NOT NULL CHECK (workload_port BETWEEN 1024 AND 65535),
    public_port INTEGER NOT NULL UNIQUE CHECK (public_port BETWEEN 1024 AND 65535),
    origin TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (business_id, workload_port)
)""",
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
 source_type TEXT NOT NULL CHECK(source_type IN ('task','signoff','reference','project','table','automation')),
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
 created_at TEXT NOT NULL
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
    'telegram': (
        """CREATE TABLE telegram_state (
    id TEXT PRIMARY KEY CHECK(id = 'telegram'),
    bot_id INTEGER NOT NULL CHECK(bot_id > 0),
    installation_id TEXT NOT NULL CHECK(length(installation_id) = 36),
    owner_principal_id TEXT NOT NULL REFERENCES principals(id),
    owner_telegram_user_id INTEGER NOT NULL CHECK(owner_telegram_user_id > 0),
    config_generation INTEGER NOT NULL CHECK(config_generation >= 1),
    binding_fingerprint TEXT NOT NULL CHECK(length(binding_fingerprint) = 64),
    binding_effective_at TEXT NOT NULL,
    activation_at TEXT NOT NULL,
    poll_offset INTEGER NOT NULL DEFAULT 0 CHECK(poll_offset >= 0),
    last_poll_at TEXT,
    last_update_received_at TEXT,
    last_error_json TEXT CHECK(last_error_json IS NULL OR json_valid(last_error_json)),
    outage_json TEXT CHECK(outage_json IS NULL OR json_valid(outage_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT""",
        """CREATE TRIGGER telegram_state_identity BEFORE UPDATE ON telegram_state
WHEN NEW.id IS NOT OLD.id OR NEW.bot_id IS NOT OLD.bot_id OR NEW.installation_id IS NOT OLD.installation_id
  OR NEW.activation_at IS NOT OLD.activation_at
  OR NEW.created_at IS NOT OLD.created_at OR NEW.poll_offset < OLD.poll_offset
  OR NEW.config_generation < OLD.config_generation OR NEW.binding_effective_at < OLD.binding_effective_at
BEGIN
    SELECT RAISE(ABORT, 'Telegram bot and installation identity, activation cutoff, binding cutoff and poll cursor never move backwards');
END""",
        """CREATE TRIGGER telegram_state_no_delete BEFORE DELETE ON telegram_state
BEGIN
    SELECT RAISE(ABORT, 'Telegram integration state keeps its cursor and cutoff');
END""",
        """CREATE TABLE telegram_updates (
    bot_id INTEGER NOT NULL CHECK(bot_id > 0),
    update_id INTEGER NOT NULL CHECK(update_id >= 0),
    kind TEXT NOT NULL CHECK(kind IN ('message','message_reaction','ignored')),
    chat_id INTEGER,
    message_id INTEGER,
    actor_id INTEGER,
    sent_at TEXT,
    normalized_json TEXT NOT NULL CHECK(json_valid(normalized_json)),
    state TEXT NOT NULL DEFAULT 'received' CHECK(state IN ('received','done','ignored','failed')),
    reason TEXT,
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    received_at TEXT NOT NULL,
    processed_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (bot_id, update_id),
    CHECK(state NOT IN ('done','ignored') OR processed_at IS NOT NULL)
) STRICT""",
        """CREATE INDEX telegram_updates_pending ON telegram_updates(bot_id, state, update_id)""",
        """CREATE INDEX telegram_updates_message ON telegram_updates(chat_id, message_id)""",
        """CREATE TRIGGER telegram_updates_identity BEFORE UPDATE ON telegram_updates
WHEN NEW.bot_id IS NOT OLD.bot_id OR NEW.update_id IS NOT OLD.update_id OR NEW.kind IS NOT OLD.kind
  OR (NEW.normalized_json IS NOT OLD.normalized_json AND NOT (OLD.state = 'received' AND NEW.state = 'ignored'))
  OR NEW.received_at IS NOT OLD.received_at
  OR (OLD.state IN ('done','ignored') AND NEW.state IS NOT OLD.state)
BEGIN
    SELECT RAISE(ABORT, 'A stored Telegram update keeps its normalized content (an ignored one loses only its text) and a finished update stays finished');
END""",
        """CREATE TABLE telegram_messages (
    id TEXT PRIMARY KEY,
    direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
    chat_name TEXT NOT NULL CHECK(chat_name IN ('ordubashy','signoffs','urgent')),
    chat_id INTEGER NOT NULL,
    telegram_message_id INTEGER CHECK(telegram_message_id IS NULL OR telegram_message_id > 0),
    agent_id TEXT REFERENCES principals(id),
    purpose TEXT NOT NULL CHECK(purpose IN ('reply','blocking_input','update','owner','notice','signoff_post','signoff_reply','urgent')),
    body TEXT,
    body_redacted INTEGER NOT NULL DEFAULT 0 CHECK(body_redacted IN (0,1)),
    sensitive INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0,1)),
    reply_to_id TEXT REFERENCES telegram_messages(id),
    reply_to_telegram_id INTEGER,
    request_key TEXT CHECK(request_key IS NULL OR length(request_key) BETWEEN 1 AND 200),
    input_mode TEXT CHECK(input_mode IS NULL OR input_mode IN ('text','secret','screen_action')),
    request_reason TEXT,
    request_state TEXT CHECK(request_state IS NULL OR request_state IN ('pending','answered','expired','cancelled')),
    request_expires_at TEXT,
    request_closed_at TEXT,
    update_id INTEGER,
    outbox_id TEXT,
    occurred_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, message_thread_id INTEGER CHECK(message_thread_id IS NULL OR message_thread_id > 1),
    CHECK(body_redacted = 1 OR body IS NOT NULL OR direction = 'outbound'),
    CHECK(sensitive = 0 OR body_redacted = 1),
    CHECK(sensitive = 0 OR body IS NULL),
    CHECK((request_key IS NULL) = (input_mode IS NULL) AND (request_key IS NULL) = (request_state IS NULL)),
    CHECK(purpose = 'blocking_input' OR request_key IS NULL),
    CHECK(request_state IS NULL OR request_state = 'pending' OR request_closed_at IS NOT NULL)
) STRICT""",
        """CREATE UNIQUE INDEX telegram_messages_provider ON telegram_messages(chat_id, telegram_message_id)
    WHERE telegram_message_id IS NOT NULL""",
        """CREATE UNIQUE INDEX telegram_messages_update ON telegram_messages(update_id) WHERE update_id IS NOT NULL""",
        """CREATE INDEX telegram_messages_history ON telegram_messages(chat_name, occurred_at, id)""",
        """CREATE UNIQUE INDEX telegram_messages_open_request ON telegram_messages(chat_name, request_key)
    WHERE request_state = 'pending'""",
        """CREATE UNIQUE INDEX telegram_messages_open_secret ON telegram_messages(chat_name)
    WHERE request_state = 'pending' AND input_mode = 'secret'""",
        """CREATE TRIGGER telegram_messages_no_delete BEFORE DELETE ON telegram_messages
BEGIN
    SELECT RAISE(ABORT, 'Telegram conversation records are retained');
END""",
        """CREATE TABLE telegram_outbox (
    id TEXT PRIMARY KEY,
    chat_name TEXT NOT NULL CHECK(chat_name IN ('ordubashy','signoffs','urgent')),
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('text','document','photo','edit')),
    source_kind TEXT NOT NULL CHECK(length(source_kind) BETWEEN 1 AND 64),
    source_key TEXT NOT NULL CHECK(length(source_key) BETWEEN 1 AND 300),
    part TEXT NOT NULL DEFAULT 'main' CHECK(length(part) BETWEEN 1 AND 64),
    message_id TEXT REFERENCES telegram_messages(id),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','dispatching','uncertain','sent','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at TEXT,
    dispatch_started_at TEXT,
    provider_message_id INTEGER CHECK(provider_message_id IS NULL OR provider_message_id > 0),
    error_code TEXT,
    error_message TEXT CHECK(error_message IS NULL OR length(error_message) <= 500),
    binding_fingerprint TEXT NOT NULL CHECK(length(binding_fingerprint) = 64),
    config_generation INTEGER NOT NULL CHECK(config_generation >= 1),
    agent_id TEXT REFERENCES principals(id),
    producer_id TEXT,
    event_producer TEXT,
    event_key TEXT,
    event_sequence INTEGER CHECK(event_sequence IS NULL OR event_sequence >= 1),
    resolved_by TEXT REFERENCES principals(id),
    resolved_at TEXT,
    sent_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, message_thread_id INTEGER CHECK(message_thread_id IS NULL OR message_thread_id > 1),
    UNIQUE (source_kind, source_key, part),
    CHECK((event_producer IS NULL) = (event_key IS NULL) AND (event_key IS NULL) = (event_sequence IS NULL)),
    CHECK(state != 'sent' OR (provider_message_id IS NOT NULL AND sent_at IS NOT NULL)),
    CHECK(state NOT IN ('dispatching','uncertain') OR dispatch_started_at IS NOT NULL)
) STRICT""",
        """CREATE INDEX telegram_outbox_due ON telegram_outbox(state, next_attempt_at, created_at)""",
        """CREATE INDEX telegram_outbox_chat ON telegram_outbox(chat_name, state, created_at)""",
        """CREATE INDEX telegram_outbox_message ON telegram_outbox(message_id)""",
        """CREATE INDEX telegram_outbox_event ON telegram_outbox(event_producer, event_key, event_sequence)""",
        """CREATE TRIGGER telegram_outbox_state BEFORE UPDATE OF state ON telegram_outbox
WHEN NEW.state != OLD.state AND NOT (
       (OLD.state = 'queued' AND NEW.state IN ('dispatching','cancelled','failed'))
    OR (OLD.state = 'dispatching' AND NEW.state IN ('sent','queued','failed','uncertain'))
    OR (OLD.state = 'uncertain' AND NEW.state IN ('sent','queued','failed'))
    OR (OLD.state = 'failed' AND NEW.state = 'queued'))
BEGIN
    SELECT RAISE(ABORT, 'Invalid Telegram outbox state transition');
END""",
        """CREATE TRIGGER telegram_outbox_no_delete BEFORE DELETE ON telegram_outbox
BEGIN
    SELECT RAISE(ABORT, 'Telegram delivery receipts are retained');
END""",
        """CREATE TABLE telegram_events (
    producer_id TEXT NOT NULL CHECK(length(producer_id) BETWEEN 1 AND 100),
    event_key TEXT NOT NULL CHECK(length(event_key) BETWEEN 1 AND 200),
    state TEXT NOT NULL CHECK(state IN ('firing','resolved')),
    content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
    message_id TEXT REFERENCES telegram_messages(id),
    last_outbox_id TEXT REFERENCES telegram_outbox(id),
    occurrences INTEGER NOT NULL DEFAULT 1 CHECK(occurrences >= 1),
    first_seen_at TEXT NOT NULL,
    last_changed_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (producer_id, event_key)
) STRICT""",
        """CREATE TABLE telegram_signoff_posts (
    id TEXT PRIMARY KEY,
    signoff_id TEXT NOT NULL REFERENCES signoffs(id),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    notification_id TEXT,
    category TEXT NOT NULL,
    proposal_revision INTEGER NOT NULL CHECK(proposal_revision >= 1),
    payload_hash TEXT NOT NULL CHECK(length(payload_hash) = 64),
    expected_status_revision INTEGER NOT NULL CHECK(expected_status_revision >= 1),
    input_form TEXT NOT NULL CHECK(input_form IN ('action','binary_input','input','status')),
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','active','retired')),
    retire_reason TEXT,
    message_id TEXT REFERENCES telegram_messages(id),
    outbox_id TEXT REFERENCES telegram_outbox(id),
    activation_at TEXT NOT NULL,
    refresh_at TEXT,
    retired_at TEXT,
    decision_json TEXT CHECK(decision_json IS NULL OR json_valid(decision_json)),
    handoff_json TEXT CHECK(handoff_json IS NULL OR json_valid(handoff_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (signoff_id, generation),
    CHECK((state = 'retired') = (retired_at IS NOT NULL)),
    CHECK(state != 'retired' OR retire_reason IS NOT NULL)
) STRICT""",
        """CREATE UNIQUE INDEX telegram_signoff_posts_current ON telegram_signoff_posts(signoff_id)
    WHERE state IN ('queued','active') AND input_form != 'status'""",
        """CREATE INDEX telegram_signoff_posts_message ON telegram_signoff_posts(message_id)""",
        """CREATE INDEX telegram_signoff_posts_open ON telegram_signoff_posts(state, refresh_at)""",
        """CREATE TRIGGER telegram_signoff_posts_identity BEFORE UPDATE ON telegram_signoff_posts
WHEN NEW.id IS NOT OLD.id OR NEW.signoff_id IS NOT OLD.signoff_id OR NEW.generation IS NOT OLD.generation
  OR NEW.category IS NOT OLD.category OR NEW.proposal_revision IS NOT OLD.proposal_revision
  OR NEW.payload_hash IS NOT OLD.payload_hash OR NEW.expected_status_revision IS NOT OLD.expected_status_revision
  OR NEW.input_form IS NOT OLD.input_form OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.state = 'retired' AND NEW.state IS NOT 'retired')
BEGIN
    SELECT RAISE(ABORT, 'A posted Signoff source binding is immutable and a retired post stays retired');
END""",
        """CREATE TABLE telegram_owner_inputs (
    id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL CHECK(purpose IN ('owner_message','signoff_reply')),
    correlation_id TEXT NOT NULL CHECK(length(correlation_id) BETWEEN 1 AND 100),
    message_id TEXT NOT NULL REFERENCES telegram_messages(id),
    agent_id TEXT NOT NULL REFERENCES principals(id),
    signoff_id TEXT REFERENCES signoffs(id),
    inbox_event_id TEXT,
    -- The identity this delivery was accepted under. It is never upgraded: a delivery whose generation, owner, binding or
    -- target registration no longer matches is failed, not retargeted or reattributed.
    owner_principal_id TEXT NOT NULL REFERENCES principals(id),
    config_generation INTEGER NOT NULL CHECK(config_generation >= 1),
    binding_fingerprint TEXT NOT NULL CHECK(length(binding_fingerprint) = 64),
    target_fingerprint TEXT NOT NULL CHECK(length(target_fingerprint) = 64),
    sensitive INTEGER NOT NULL DEFAULT 0 CHECK(sensitive IN (0,1)),
    spool_id TEXT CHECK(spool_id IS NULL OR length(spool_id) BETWEEN 16 AND 100),
    expires_at TEXT,
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','submitting','uncertain','submitted','failed','expired','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at TEXT,
    deferral_reason TEXT,
    notice_sent_at TEXT,
    submitted_at TEXT,
    error_code TEXT,
    receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (correlation_id, agent_id),
    CHECK(sensitive = 0 OR (expires_at IS NOT NULL AND (spool_id IS NOT NULL OR state != 'queued'))),
    CHECK(sensitive = 1 OR spool_id IS NULL),
    CHECK(purpose != 'signoff_reply' OR signoff_id IS NOT NULL)
) STRICT""",
        """CREATE INDEX telegram_owner_inputs_due ON telegram_owner_inputs(agent_id, state, created_at)""",
        """CREATE INDEX telegram_owner_inputs_message ON telegram_owner_inputs(message_id)""",
        """CREATE TRIGGER telegram_owner_inputs_identity BEFORE UPDATE ON telegram_owner_inputs
WHEN NEW.id IS NOT OLD.id OR NEW.purpose IS NOT OLD.purpose OR NEW.correlation_id IS NOT OLD.correlation_id
  OR NEW.message_id IS NOT OLD.message_id OR NEW.agent_id IS NOT OLD.agent_id OR NEW.sensitive IS NOT OLD.sensitive
  OR NEW.owner_principal_id IS NOT OLD.owner_principal_id OR NEW.config_generation IS NOT OLD.config_generation
  OR NEW.binding_fingerprint IS NOT OLD.binding_fingerprint OR NEW.target_fingerprint IS NOT OLD.target_fingerprint
  OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.state IN ('submitted','expired','cancelled') AND NEW.state IS NOT OLD.state)
  OR (OLD.state = 'uncertain' AND NEW.state NOT IN ('uncertain','failed','submitted','expired','cancelled'))
BEGIN
    SELECT RAISE(ABORT, 'Owner input identity is immutable and terminal input is never replayed');
END""",
        """CREATE TABLE telegram_cleanup_jobs (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL UNIQUE REFERENCES telegram_messages(id),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 100),
    state TEXT NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','deleting','deleted','failed')),
    not_before TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at TEXT,
    error_code TEXT,
    error_message TEXT CHECK(error_message IS NULL OR length(error_message) <= 500),
    completed_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(state != 'deleted' OR completed_at IS NOT NULL)
) STRICT""",
        """CREATE INDEX telegram_cleanup_due ON telegram_cleanup_jobs(state, next_attempt_at, created_at)""",
        """CREATE TRIGGER telegram_cleanup_identity BEFORE UPDATE ON telegram_cleanup_jobs
WHEN NEW.id IS NOT OLD.id OR NEW.message_id IS NOT OLD.message_id OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.state = 'deleted' AND NEW.state IS NOT 'deleted')
BEGIN
    SELECT RAISE(ABORT, 'Cleanup identity is immutable and a completed deletion stays complete');
END""",
        """CREATE INDEX telegram_messages_destination ON telegram_messages(chat_id, message_thread_id, occurred_at)""",
        """CREATE INDEX telegram_outbox_destination ON telegram_outbox(chat_id, message_thread_id, state)""",
        """CREATE TRIGGER telegram_messages_identity BEFORE UPDATE ON telegram_messages
WHEN NEW.id IS NOT OLD.id OR NEW.direction IS NOT OLD.direction OR NEW.chat_name IS NOT OLD.chat_name
  OR NEW.chat_id IS NOT OLD.chat_id OR NEW.message_thread_id IS NOT OLD.message_thread_id
  OR NEW.agent_id IS NOT OLD.agent_id OR NEW.purpose IS NOT OLD.purpose
  OR NEW.occurred_at IS NOT OLD.occurred_at OR NEW.request_key IS NOT OLD.request_key
  OR NEW.input_mode IS NOT OLD.input_mode OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.telegram_message_id IS NOT NULL AND NEW.telegram_message_id IS NOT OLD.telegram_message_id)
  OR (OLD.sensitive = 1 AND NEW.sensitive = 0)
  OR (OLD.body_redacted = 1 AND NEW.body IS NOT NULL)
BEGIN
    SELECT RAISE(ABORT, 'Telegram conversation identity and destination are immutable and a redacted body is never restored');
END""",
        """CREATE TRIGGER telegram_outbox_identity BEFORE UPDATE ON telegram_outbox
WHEN NEW.id IS NOT OLD.id OR NEW.chat_name IS NOT OLD.chat_name OR NEW.chat_id IS NOT OLD.chat_id
  OR NEW.message_thread_id IS NOT OLD.message_thread_id
  OR NEW.kind IS NOT OLD.kind OR NEW.source_kind IS NOT OLD.source_kind OR NEW.source_key IS NOT OLD.source_key
  OR NEW.part IS NOT OLD.part OR NEW.created_at IS NOT OLD.created_at OR NEW.payload_json IS NOT OLD.payload_json
  OR NEW.message_id IS NOT OLD.message_id OR NEW.event_producer IS NOT OLD.event_producer
  OR NEW.event_key IS NOT OLD.event_key OR NEW.event_sequence IS NOT OLD.event_sequence
BEGIN
    SELECT RAISE(ABORT, 'A saved Telegram outbox intent and its destination are immutable');
END""",
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
