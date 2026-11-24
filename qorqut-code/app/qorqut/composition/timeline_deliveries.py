"""What each conversation of an agent was last told of a timeline.

One row per agent, conversation and item: the revision of the item's state that conversation received and when. Delivery
compares revisions, so a level already sent and unchanged since is replaced by a one-line marker. The rows of a conversation
go when its start hook reports a new context (startup, clear, compact, fork) or the session is recovering. Host bookkeeping:
never exported to history, and the item is named by type, id and Project (``project_id`` is the empty string at Global).
"""
from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "timeline-deliveries"
TABLE = "timeline_deliveries"
KEYS = ("agent_id", "conversation_id", "item_type", "item_id", "project_id")

SCHEMA = (
    """CREATE TABLE timeline_deliveries (
    agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id),
    conversation_id TEXT NOT NULL CHECK(length(conversation_id) BETWEEN 1 AND 200),
    item_type TEXT NOT NULL CHECK(item_type IN ('task','project','global')),
    item_id TEXT NOT NULL CHECK(length(item_id) BETWEEN 1 AND 300),
    project_id TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL CHECK(revision >= 0),
    delivered_at TEXT NOT NULL,
    PRIMARY KEY(agent_id, conversation_id, item_type, item_id, project_id)
)""",
)


def storage_contribution() -> StorageContribution:
    return StorageContribution(OWNER, {TABLE: TableSpec(KEYS, mutable=True, revision_column=None)}, schema=SCHEMA)
