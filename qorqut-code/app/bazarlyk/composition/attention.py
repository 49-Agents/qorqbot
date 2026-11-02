"""Durable Ordubashy attention associations and reminder occurrences."""
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

TABLE_SPECS = {
    'agent_attention': TableSpec(('agent_id',)),
    'attention_memberships': TableSpec(('id',)),
    'attention_membership_events': TableSpec(('id',), mutable=False, revision_column=None),
    'attention_workflows': TableSpec(('agent_id', 'run_id')),
    'attention_reminders': TableSpec(('id',)),
}
IMMUTABLE = {'attention_memberships': frozenset({'agent_id', 'task_id', 'task_created_at', 'created_at'})}
SCHEMA = (
    """CREATE TABLE agent_attention (
        agent_id TEXT PRIMARY KEY REFERENCES agent_profiles(agent_id),
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
        focus_json TEXT CHECK(focus_json IS NULL OR (json_valid(focus_json) AND json_type(focus_json)='object')),
        work_task_json TEXT CHECK(work_task_json IS NULL OR (json_valid(work_task_json) AND json_type(work_task_json)='object')),
        intent TEXT NOT NULL DEFAULT '', focus_generation INTEGER NOT NULL DEFAULT 0 CHECK(focus_generation >= 0),
        focus_started_at TEXT, last_agent_activity_at TEXT, last_owner_message_at TEXT,
        board_mode TEXT NOT NULL DEFAULT 'empty' CHECK(board_mode IN ('empty','normal','reassess')),
        board_due_at TEXT, focus_due_at TEXT, reminder_sequence INTEGER NOT NULL DEFAULT 0 CHECK(reminder_sequence >= 0),
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE attention_memberships (
        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id),
        task_business_id TEXT, task_id TEXT NOT NULL, task_created_at TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1), created_at TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX attention_memberships_business ON attention_memberships(agent_id,task_business_id,task_id) WHERE task_business_id IS NOT NULL",
    "CREATE UNIQUE INDEX attention_memberships_global ON attention_memberships(agent_id,task_id) WHERE task_business_id IS NULL",
    """CREATE TABLE attention_membership_events (
        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id), membership_id TEXT NOT NULL,
        task_business_id TEXT, task_id TEXT NOT NULL, task_created_at TEXT NOT NULL,
        actor_id TEXT NOT NULL REFERENCES principals(id), event_type TEXT NOT NULL CHECK(event_type IN ('added','removed')),
        owner_requested INTEGER NOT NULL DEFAULT 0 CHECK(owner_requested IN (0,1)), rationale TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    )""",
    "CREATE INDEX attention_membership_events_agent ON attention_membership_events(agent_id,created_at,id)",
    "CREATE TRIGGER attention_membership_events_no_update BEFORE UPDATE ON attention_membership_events BEGIN SELECT RAISE(ABORT,'Attention membership history is immutable'); END",
    "CREATE TRIGGER attention_membership_events_no_delete BEFORE DELETE ON attention_membership_events BEGIN SELECT RAISE(ABORT,'Attention membership history is immutable'); END",
    """CREATE TABLE attention_workflows (
        agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id), run_id TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1), consumer_id TEXT NOT NULL,
        work_task_json TEXT NOT NULL CHECK(json_valid(work_task_json) AND json_type(work_task_json)='object'),
        workflow_ref TEXT NOT NULL, workflow_name TEXT NOT NULL, blocked_since TEXT, blocker_key TEXT,
        blocker_reason TEXT NOT NULL DEFAULT '', resume_context TEXT NOT NULL DEFAULT '',
        related_refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(related_refs_json) AND json_type(related_refs_json)='array'),
        detached_at TEXT, paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
        last_event_ordinal INTEGER NOT NULL DEFAULT 0 CHECK(last_event_ordinal >= 0), updated_at TEXT NOT NULL,
        PRIMARY KEY(agent_id,run_id)
    )""",
    """CREATE TABLE attention_reminders (
        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id),
        kind TEXT NOT NULL CHECK(kind IN ('board_normal','board_reassess','focus','workflow_detach')),
        focus_generation INTEGER NOT NULL CHECK(focus_generation >= 0), sequence INTEGER NOT NULL CHECK(sequence >= 0),
        workflow_run_id TEXT, blocked_since TEXT,
        due_at TEXT NOT NULL, not_before TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','claimed','submitted','uncertain','cancelled')),
        route TEXT CHECK(route IS NULL OR route IN ('hook','chat')), native_input_id TEXT,
        claim_token TEXT UNIQUE, claim_consumer_id TEXT,
        claim_binding_generation INTEGER CHECK(claim_binding_generation IS NULL OR claim_binding_generation >= 1),
        claim_expires_at TEXT, emission_started_at TEXT, delivered_at TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
        CHECK((claim_token IS NULL AND claim_consumer_id IS NULL AND claim_binding_generation IS NULL AND claim_expires_at IS NULL)
           OR (claim_token IS NOT NULL AND claim_consumer_id IS NOT NULL AND claim_binding_generation IS NOT NULL AND claim_expires_at IS NOT NULL)),
        CHECK((kind='workflow_detach' AND workflow_run_id IS NOT NULL AND blocked_since IS NOT NULL)
           OR (kind<>'workflow_detach' AND workflow_run_id IS NULL AND blocked_since IS NULL)),
        UNIQUE(agent_id,kind,focus_generation,sequence)
    )""",
    "CREATE INDEX attention_reminders_pending ON attention_reminders(agent_id,state,due_at)",
    "CREATE INDEX attention_reminders_episode ON attention_reminders(agent_id,workflow_run_id,blocked_since,state)",
)


def storage_contribution():
    return StorageContribution('ordubashy-attention', TABLE_SPECS, IMMUTABLE, SCHEMA)
