"""The extension's two tables: the stable automation record and its durable run steps.

A run is an ordinary ``jobs`` row of the ``automation.run`` program (its identity, state and snapshot checkpoint belong to the
background engine); this module adds only what the engine cannot hold: the authored record, its typed grants and
restrictions, the frozen admission of the next run, and the per-step ledger that makes a restarted run replay, wait and retry
safely. Both tables are scoped like every other record: ``business_id`` is NULL for a Global automation and names the Project
otherwise. Nothing here migrates or deletes an existing table.
"""

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "automation"

SCHEMA = (
    """CREATE TABLE automations (
    id TEXT PRIMARY KEY,
    business_id TEXT REFERENCES businesses(id),
    slug TEXT NOT NULL CHECK (length(slug) BETWEEN 1 AND 80),
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 8000),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','paused','archived')),
    pause_reason TEXT,
    script_ref TEXT NOT NULL CHECK (length(script_ref) BETWEEN 3 AND 200),
    arguments_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(arguments_json)),
    phases_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(phases_json)),
    routine_id TEXT,
    grants_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(grants_json)),
    restrictions_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(restrictions_json)),
    saved_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(saved_json)),
    admitted_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(admitted_json)),
    created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE UNIQUE INDEX automations_slug ON automations(COALESCE(business_id, ''), slug)""",
    """CREATE UNIQUE INDEX automations_script_ref ON automations(script_ref)""",       # a script entry has exactly one owner record
    """CREATE INDEX automations_state ON automations(state, updated_at)""",
    """CREATE TABLE automation_steps (
    id TEXT PRIMARY KEY,
    automation_id TEXT NOT NULL REFERENCES automations(id),
    business_id TEXT REFERENCES businesses(id),
    run_id TEXT NOT NULL,
    step_key TEXT NOT NULL CHECK (length(step_key) BETWEEN 1 AND 200),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    parent_step_id TEXT REFERENCES automation_steps(id),
    kind TEXT NOT NULL CHECK (kind IN ('call','ask','publish','phase','grant')),
    operation TEXT,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    state TEXT NOT NULL CHECK (state IN ('running','waiting','succeeded','failed','unknown','cancelled')),
    effect TEXT NOT NULL DEFAULT 'none' CHECK (effect IN ('none','possible','confirmed')),
    wait_json TEXT CHECK (wait_json IS NULL OR json_valid(wait_json)),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
    debug_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(debug_json)),
    log_expired_at TEXT,
    closed_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, step_key, attempt)
)""",
    """CREATE INDEX automation_steps_run ON automation_steps(run_id, created_at)""",
    """CREATE INDEX automation_steps_open ON automation_steps(state, updated_at)""",
    """CREATE INDEX automation_steps_history ON automation_steps(automation_id, kind, created_at)""",
    """CREATE INDEX automation_steps_logs ON automation_steps(closed_at) WHERE closed_at IS NOT NULL AND log_expired_at IS NULL""",
)

TABLES = {
    "automations": TableSpec(("id",), "business_id"),
    "automation_steps": TableSpec(("id",), "business_id"),
}

# Host tables this package reads or writes without owning them (checked when the store is composed and validated).
REQUIRED_TABLES = frozenset({"principals", "businesses", "jobs", "routines", "signoffs", "signoff_notes", "management_routing",
                             "inbox_events", "inbox_deliveries", "inbox_subscriptions", "agent_profiles", "installation",
                             "idempotency_receipts"})


def storage_contribution() -> StorageContribution:
    return StorageContribution(OWNER, TABLES, {}, SCHEMA, required_tables=REQUIRED_TABLES)
