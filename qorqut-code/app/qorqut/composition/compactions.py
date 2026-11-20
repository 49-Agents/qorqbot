"""An agent's own compactions: one row per compaction, which is also its log entry.

A row is made when a handoff is asked for (``reason`` ``context``: the conversation neared its limit) or when the agent
files one unasked (``agent``). ``request_json`` holds what was known when it was asked for (how full the context was, which
conversation). ``listed_from`` and ``listed_until`` bound the owner's messages the handoff answers for.
``filing_json`` is the handoff as filed, with the Task each note was written to. ``opened_at`` is when the fresh
conversation was started from it, and ``opened_json`` what that conversation was given that the filing did not settle:
the ids of the listed messages it gave no status.

The owner's words are not stored here: a filing names each message by its id, and the text is read from its own place
whenever it is shown. Host bookkeeping: never exported to history.
"""
from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "compactions"
TABLE = "compactions"
INDEX = "compactions_agent"

SCHEMA = (
    """CREATE TABLE compactions (
    id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 100),
    agent_id TEXT NOT NULL REFERENCES agent_profiles(agent_id),
    reason TEXT NOT NULL CHECK(length(reason) BETWEEN 1 AND 40),
    requested_at TEXT NOT NULL,
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    listed_from TEXT NOT NULL,
    listed_until TEXT NOT NULL,
    filed_at TEXT,
    filing_json TEXT CHECK(filing_json IS NULL OR json_valid(filing_json)),
    opened_at TEXT,
    opened_json TEXT CHECK(opened_json IS NULL OR json_valid(opened_json))
)""",
    "CREATE INDEX compactions_agent ON compactions(agent_id, requested_at)",
)


def storage_contribution() -> StorageContribution:
    return StorageContribution(OWNER, {TABLE: TableSpec(("id",), mutable=True, revision_column=None)}, schema=SCHEMA)
