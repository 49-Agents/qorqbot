"""Private host associations for the Overseer bridge.

Content belongs in the private archive or portable layer; the Operations store
holds only immutable event identities, references, and delivery cursors.
"""
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = 'overseer'
TABLES = {
    'overseer_owner_inputs': TableSpec(('input_id',), mutable=False, revision_column=None),
    'overseer_telegram_associations': TableSpec(('input_id',), mutable=False, revision_column=None),
    'overseer_invocations': TableSpec(('input_id', 'kind'), mutable=False, revision_column=None),
    'overseer_workflow_requests': TableSpec(('id',), mutable=False, revision_column=None),
    'overseer_workflow_request_ops': TableSpec(('operation_id',), mutable=False, revision_column=None),
    'overseer_workflow_interpretations': TableSpec(('request_id', 'response_revision'), mutable=False, revision_column=None),
    'overseer_signoffs': TableSpec(('binding_id',), mutable=False, revision_column=None),
    'overseer_development_tasks': TableSpec(('request_key',), mutable=False, revision_column=None),
    'overseer_development_task_requests': TableSpec(('request_key',), mutable=False, revision_column=None),
    'overseer_development_task_links': TableSpec(('request_key',), mutable=False, revision_column=None),
    'overseer_retention_events': TableSpec(('event_key',), revision_column=None),
    'overseer_retained_materials': TableSpec(('material_key',), mutable=False, revision_column=None),
    'overseer_retained_links': TableSpec(('material_key', 'entry_id', 'target_kind', 'target_id'), mutable=False, revision_column=None),
    # P07 scheduling: one durable occurrence per canonical scheduled routine run, the pending admission request each
    # occurrence is coalesced into, and the append-only occurrence membership of that request.
    'overseer_due_occurrences': TableSpec(('id',), revision_column=None),
    'overseer_due_requests': TableSpec(('id',), revision_column=None),
    'overseer_due_request_occurrences': TableSpec(('request_id', 'occurrence_id'), mutable=False, revision_column=None),
}
IMMUTABLE_FIELDS = {
    # The occurrence's provenance is frozen at record time; only a bounded diagnostic reason may change.
    'overseer_due_occurrences': frozenset({
        'id', 'routine_run_id', 'job_id', 'program', 'program_digest', 'routine_namespace', 'routine_id',
        'routine_revision', 'scheduled_slot', 'missed_count', 'missed_capped', 'target_agent_id',
        'target_conversation_id', 'extraction_arguments_json', 'created_at'}),
    # The canonical source, coalescing identity and target are immutable; only the ordinary state/version columns move.
    'overseer_due_requests': frozenset({
        'id', 'coalescing_key', 'source_json', 'source_hash', 'target_agent_id', 'target_conversation_id',
        'first_occurrence_id'}),
}
SCHEMA = (
    '''CREATE TABLE overseer_owner_inputs (
        input_id TEXT PRIMARY KEY REFERENCES native_inputs(id),
        agent_id TEXT NOT NULL REFERENCES principals(id),
        principal_id TEXT NOT NULL REFERENCES principals(id),
        session_id TEXT NOT NULL REFERENCES native_sessions(id),
        source_json TEXT NOT NULL CHECK(json_valid(source_json)),
        sensitive INTEGER NOT NULL CHECK(sensitive IN (0,1)),
        reference_revision TEXT,
        source_hash TEXT,
        material_ref_json TEXT CHECK(material_ref_json IS NULL OR json_valid(material_ref_json)),
        CHECK((sensitive=1 AND reference_revision IS NULL AND source_hash IS NULL AND material_ref_json IS NULL)
           OR (sensitive=0 AND reference_revision IS NOT NULL AND source_hash IS NOT NULL AND material_ref_json IS NOT NULL))
    ) STRICT''',
    '''CREATE TABLE overseer_invocations (
        input_id TEXT NOT NULL REFERENCES overseer_owner_inputs(input_id),
        kind TEXT NOT NULL CHECK(kind IN ('extraction','review','application')),
        interpretation_json TEXT NOT NULL CHECK(json_valid(interpretation_json)),
        PRIMARY KEY(input_id, kind)
    ) STRICT''',
    '''CREATE TABLE overseer_telegram_associations (
        input_id TEXT PRIMARY KEY REFERENCES overseer_owner_inputs(input_id),
        event_key TEXT NOT NULL,
        entry_id TEXT NOT NULL
    ) STRICT''',
    '''CREATE TABLE overseer_workflow_requests (
        id TEXT PRIMARY KEY,
        request_key TEXT NOT NULL UNIQUE,
        agent_id TEXT NOT NULL REFERENCES principals(id),
        conversation_id TEXT NOT NULL,
        association_json TEXT NOT NULL CHECK(json_valid(association_json)),
        signoff_id TEXT NOT NULL UNIQUE REFERENCES signoffs(id),
        proposal_revision INTEGER NOT NULL,
        payload_hash TEXT NOT NULL
    ) STRICT''',
    '''CREATE TABLE overseer_workflow_interpretations (
        request_id TEXT NOT NULL REFERENCES overseer_workflow_requests(id),
        response_revision TEXT NOT NULL,
        interpretation_json TEXT NOT NULL CHECK(json_valid(interpretation_json)),
        PRIMARY KEY(request_id,response_revision)
    ) STRICT''',
    '''CREATE TABLE overseer_workflow_request_ops (
        operation_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL
    ) STRICT''',
    '''CREATE TABLE overseer_signoffs (
        binding_id TEXT PRIMARY KEY,
        request_key TEXT NOT NULL UNIQUE,
        signoff_id TEXT NOT NULL UNIQUE REFERENCES signoffs(id),
        proposal_revision INTEGER NOT NULL CHECK(proposal_revision > 0),
        payload_hash TEXT NOT NULL,
        review_business_id TEXT REFERENCES businesses(id),
        purpose TEXT NOT NULL,
        run_id TEXT NOT NULL,
        delta_id TEXT,
        delta_revision INTEGER,
        application_id TEXT,
        plan_revision INTEGER,
        batch_id TEXT
    ) STRICT''',
    '''CREATE INDEX overseer_signoffs_delta ON overseer_signoffs(delta_id, signoff_id)''',
    '''CREATE INDEX overseer_signoffs_application ON overseer_signoffs(application_id, signoff_id)''',
    '''CREATE INDEX overseer_signoffs_batch ON overseer_signoffs(batch_id, signoff_id)''',
    '''CREATE TABLE overseer_development_tasks (
        request_key TEXT PRIMARY KEY,
        application_id TEXT NOT NULL,
        plan_revision INTEGER NOT NULL CHECK(plan_revision > 0),
        requirement_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        task_ref_json TEXT NOT NULL CHECK(json_valid(task_ref_json)),
        UNIQUE(application_id, plan_revision, requirement_id)
    ) STRICT''',
    '''CREATE TABLE overseer_development_task_links (
        request_key TEXT PRIMARY KEY REFERENCES overseer_development_tasks(request_key),
        operation_id TEXT NOT NULL,
        linked_at TEXT NOT NULL
    ) STRICT''',
    '''CREATE TABLE overseer_development_task_requests (
        request_key TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL
    ) STRICT''',
    '''CREATE TABLE overseer_retention_events (
        event_key TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        source_json TEXT NOT NULL CHECK(json_valid(source_json)),
        cursor_json TEXT CHECK(cursor_json IS NULL OR json_valid(cursor_json)),
        material_ref_json TEXT CHECK(material_ref_json IS NULL OR json_valid(material_ref_json)),
        state TEXT NOT NULL CHECK(state IN ('pending','complete','unavailable')),
        gap_code TEXT,
        updated_at TEXT NOT NULL
    ) STRICT''',
    '''CREATE TABLE overseer_retained_materials (
        material_key TEXT PRIMARY KEY,
        event_key TEXT NOT NULL REFERENCES overseer_retention_events(event_key),
        material_ref_json TEXT NOT NULL CHECK(json_valid(material_ref_json)),
        record_json TEXT NOT NULL CHECK(json_valid(record_json)),
        targets_json TEXT NOT NULL CHECK(json_valid(targets_json)),
        occurred_at TEXT,
        created_at TEXT NOT NULL
    ) STRICT''',
    '''CREATE INDEX overseer_retained_event ON overseer_retained_materials(event_key,material_key)''',
    '''CREATE TABLE overseer_retained_links (
        material_key TEXT NOT NULL REFERENCES overseer_retained_materials(material_key),
        entry_id TEXT NOT NULL,
        target_kind TEXT NOT NULL,
        target_id TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        PRIMARY KEY(material_key,entry_id,target_kind,target_id)
    ) STRICT''',
    '''CREATE INDEX overseer_retained_links_target ON overseer_retained_links(target_kind,target_id,occurred_at,entry_id)''',
    # P07: scheduled extraction. The occurrence is frozen from the real job/routine metadata; the request carries the
    # immutable canonical source and the one admission; membership is append-only while pending and frozen on reserve.
    '''CREATE TABLE overseer_due_occurrences (
        id TEXT PRIMARY KEY,
        routine_run_id TEXT NOT NULL UNIQUE REFERENCES routine_runs(id),
        job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
        program TEXT NOT NULL,
        program_digest TEXT NOT NULL,
        routine_namespace TEXT NOT NULL,
        routine_id TEXT NOT NULL,
        routine_revision INTEGER NOT NULL CHECK(routine_revision >= 1),
        scheduled_slot TEXT NOT NULL,
        missed_count INTEGER NOT NULL CHECK(missed_count >= 0),
        missed_capped INTEGER NOT NULL CHECK(missed_capped IN (0,1)),
        target_agent_id TEXT NOT NULL REFERENCES principals(id),
        target_conversation_id TEXT NOT NULL,
        extraction_arguments_json TEXT NOT NULL CHECK(json_valid(extraction_arguments_json)),
        created_at TEXT NOT NULL,
        blocked_reason TEXT,
        blocked_at TEXT,
        CHECK((blocked_reason IS NULL AND blocked_at IS NULL) OR (blocked_reason IS NOT NULL AND blocked_at IS NOT NULL))
    ) STRICT''',
    '''CREATE INDEX overseer_due_occurrence_created ON overseer_due_occurrences(created_at,id)''',
    '''CREATE TABLE overseer_due_requests (
        id TEXT PRIMARY KEY,
        coalescing_key TEXT NOT NULL,
        source_json TEXT NOT NULL CHECK(json_valid(source_json)),
        source_hash TEXT NOT NULL,
        target_agent_id TEXT NOT NULL REFERENCES principals(id),
        target_conversation_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','reserved','linked')),
        first_occurrence_id TEXT NOT NULL REFERENCES overseer_due_occurrences(id),
        admission_json TEXT CHECK(admission_json IS NULL OR json_valid(admission_json)),
        admission_hash TEXT,
        linked_run_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK((state='pending' AND admission_json IS NULL AND admission_hash IS NULL AND linked_run_id IS NULL)
           OR (state='reserved' AND admission_json IS NOT NULL AND admission_hash IS NOT NULL AND linked_run_id IS NULL)
           OR (state='linked' AND admission_json IS NOT NULL AND admission_hash IS NOT NULL AND linked_run_id IS NOT NULL))
    ) STRICT''',
    '''CREATE UNIQUE INDEX overseer_due_request_pending ON overseer_due_requests(coalescing_key) WHERE state='pending' ''',
    '''CREATE INDEX overseer_due_request_target ON overseer_due_requests(target_agent_id,created_at,id)''',
    '''CREATE TABLE overseer_due_request_occurrences (
        request_id TEXT NOT NULL REFERENCES overseer_due_requests(id),
        occurrence_id TEXT NOT NULL UNIQUE REFERENCES overseer_due_occurrences(id),
        created_at TEXT NOT NULL,
        PRIMARY KEY(request_id, occurrence_id)
    ) STRICT''',
    '''CREATE INDEX overseer_due_membership ON overseer_due_request_occurrences(request_id,occurrence_id)''',
)


def storage_contribution():
    return StorageContribution(OWNER, TABLES, IMMUTABLE_FIELDS, SCHEMA,
                               required_tables=frozenset({'native_inputs', 'native_sessions', 'principals',
                                                          'signoffs', 'tasks', 'businesses', 'routine_runs', 'jobs'}))
