"""The record of every text of an extension delivered to an agent (docs/EXTENSION-CONTRACT.md section 8, rule 6).

One row per delivered text: the agent, the extension and its entry, the point (``start``, ``owner_message``,
``after_tool``, ``stop``, ``timed``), whether the text was shown at that point or written to the point's rest file that
the agent was told to read (``place``; ``in_effect`` marks instead when a ``timed`` entry came into effect), where it went (``session_id``, ``turn_id``, ``request_id`` of a tool call,
``native_input_id`` of a timed nudge; NULL where it does not apply), the digest of the text and when. Rows are only
added; they are kept. Written only through ``qorqut.extension_hooks``.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "extension-deliveries"
TABLE = "extension_deliveries"
INDEX = "extension_deliveries_entry"
POINTS = ("start", "owner_message", "after_tool", "stop", "timed")
# ``in_effect`` is not a delivery: it marks when a ``timed`` entry came into effect for the agent, the anchor of its first
# nudge one ``every`` later (each later one is anchored at the last delivery).
PLACES = ("shown", "rest_file", "in_effect")

SCHEMA = (
    """CREATE TABLE extension_deliveries (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    extension_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    point TEXT NOT NULL CHECK(point IN ('start','owner_message','after_tool','stop','timed')),
    place TEXT NOT NULL CHECK(place IN ('shown','rest_file','in_effect')),
    session_id TEXT,
    turn_id TEXT,
    request_id TEXT,
    native_input_id TEXT,
    text_sha256 TEXT NOT NULL CHECK(length(text_sha256) = 64),
    delivered_at TEXT NOT NULL
)""",
    """CREATE INDEX extension_deliveries_entry ON extension_deliveries(agent_id, extension_id, entry_id, point, delivered_at)""",
)


def storage_contribution() -> StorageContribution:
    return StorageContribution(OWNER, {TABLE: TableSpec(("id",), mutable=False, revision_column=None)}, schema=SCHEMA)
