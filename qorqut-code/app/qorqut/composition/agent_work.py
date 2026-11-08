"""The one durable current assignment of a permanent worker.

``agent_activity`` keeps the native observation of every session (and the legacy per-session work list of the other global
agents). A worker's authored status is not a session fact: it survives restarts and is replaced only by an explicit write, so
it has its own row. One row per agent, created by the first write; an absent row reads as revision 0 with no item, and a
cleared assignment is a row whose ``item_json`` is NULL. Reads and restarts never create, clear or replace it.

The item is the existing current-work item ``{business_id, task_id?, summary?, blocked_reason?}``. ``work_updated_at`` moves
only when the item changes; ``work_reviewed_at`` moves on every write, like the session columns of the same names.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "agent-work"

TABLE_SPECS = {"agent_current_work": TableSpec(("agent_id",))}

SCHEMA = (
    """CREATE TABLE agent_current_work (
    agent_id TEXT PRIMARY KEY REFERENCES agent_profiles(agent_id),
    item_json TEXT CHECK(item_json IS NULL OR (json_valid(item_json) AND json_type(item_json)='object')),
    work_updated_at TEXT NOT NULL,
    work_reviewed_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1)
)""",
)


def storage_contribution() -> StorageContribution:
    return StorageContribution(OWNER, TABLE_SPECS, schema=SCHEMA)


__all__ = ["OWNER", "SCHEMA", "TABLE_SPECS", "storage_contribution"]
