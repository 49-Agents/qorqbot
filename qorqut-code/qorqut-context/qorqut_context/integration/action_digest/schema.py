"""Storage of the daily Action digest, composed once into the operational schema after the queue.

* ``context_action_backlog``: one row per accepted Action revision, written by the composed ``on_revision`` port in the
  Action writer's own transaction. ``sequence`` is the order the revisions committed in (SQLite has one writer), so it
  is the digest's cursor: nothing recorded later can take a lower number. Rows are insert-only.
* ``context_action_digests``: one row per digest part, created with its GENERAL queue input in one transaction.
  ``through_sequence`` is the last backlog sequence the day's digest read; the largest one is the stored cursor. A day has
  at most one row per ``part``, so running the job again never creates a second input. Rows are insert-only.

A GENERAL input under ``foundation/action-digests/`` is refused unless its digest row names it, so no other admission can
place a source there.
"""

from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "context-action-digest"
BACKLOG = "context_action_backlog"
DIGESTS = "context_action_digests"
TRIGGERS = ("context_action_backlog_immutable", "context_action_backlog_keep", "context_action_digests_immutable",
            "context_action_digests_keep", "context_action_digest_inputs")

SCHEMA = (
    f"""CREATE TABLE {BACKLOG} (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    UNIQUE (action_id, revision),
    FOREIGN KEY (action_id, revision) REFERENCES action_revisions(action_id, revision)
)""",
    f"""CREATE TRIGGER context_action_backlog_immutable
BEFORE UPDATE ON {BACKLOG}
BEGIN
    SELECT RAISE(ABORT, 'An Action digest backlog entry is immutable');
END""",
    f"""CREATE TRIGGER context_action_backlog_keep
BEFORE DELETE ON {BACKLOG}
BEGIN
    SELECT RAISE(ABORT, 'Action digest backlog entries are retained');
END""",
    f"""CREATE TABLE {DIGESTS} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36),
    day TEXT NOT NULL CHECK (length(day) = 10 AND day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    part INTEGER NOT NULL CHECK (part >= 1),
    time_zone TEXT NOT NULL CHECK (length(time_zone) BETWEEN 1 AND 64),
    through_sequence INTEGER NOT NULL CHECK (through_sequence >= 1),
    entry_count INTEGER NOT NULL CHECK (entry_count >= 1),
    input_id TEXT NOT NULL UNIQUE REFERENCES context_inputs(id) DEFERRABLE INITIALLY DEFERRED,
    created_at TEXT NOT NULL,
    UNIQUE (day, part)
)""",
    f"""CREATE TRIGGER context_action_digests_immutable
BEFORE UPDATE ON {DIGESTS}
BEGIN
    SELECT RAISE(ABORT, 'An Action digest is immutable');
END""",
    f"""CREATE TRIGGER context_action_digests_keep
BEFORE DELETE ON {DIGESTS}
BEGIN
    SELECT RAISE(ABORT, 'Action digests are retained');
END""",
    f"""CREATE TRIGGER context_action_digest_inputs
BEFORE INSERT ON context_inputs
WHEN NEW.input_kind = 'general' AND json_extract(NEW.payload_json, '$.path') LIKE 'foundation/action-digests/%'
  AND NOT EXISTS (SELECT 1 FROM {DIGESTS} d WHERE d.input_id = NEW.id AND NEW.project_id IS NULL AND NEW.operation = 'add')
BEGIN
    SELECT RAISE(ABORT, 'The Action digest directory holds only the inputs of their own digests');
END""",
)

TABLES = {
    BACKLOG: TableSpec(("action_id", "revision"), None, False, None),
    DIGESTS: TableSpec(("id",), None, False, None),
}
# The backlog names exact Action revisions; a digest part names the GENERAL queue input that carries it.
REQUIRED_TABLES = frozenset({"action_revisions", "context_inputs"})


def storage_contribution() -> StorageContribution:
    return StorageContribution(OWNER, TABLES, {}, SCHEMA, required_tables=REQUIRED_TABLES)
