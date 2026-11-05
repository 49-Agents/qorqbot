"""Host-owned transport cursor in the same Operations store as inbox delivery.

This is receiver metadata, not a copy of the module's change history. Binding is
permanent: replacing the module store requires future explicit reconciliation.
"""

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

OWNER = "agentic-inbox"
TABLE = "agentic_inbox_cursor"
CONSUMER = "overseer"
REQUIRED_TABLES = frozenset({"principals", "agent_profiles", "inbox_events", "inbox_deliveries"})
TABLE_SPECS = {TABLE: TableSpec(("consumer",), revision_column=None)}
IMMUTABLE_FIELDS = {TABLE: frozenset({"consumer", "instance_id"})}
SCHEMA = (
    """CREATE TABLE agentic_inbox_cursor (
        consumer TEXT PRIMARY KEY CHECK(consumer='overseer'),
        instance_id TEXT NOT NULL CHECK(length(instance_id)=37),
        last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(typeof(last_sequence)='integer' AND last_sequence>=0),
        updated_at TEXT NOT NULL
    )""",
    """CREATE TRIGGER agentic_inbox_binding_immutable BEFORE UPDATE ON agentic_inbox_cursor
        WHEN NEW.consumer IS NOT OLD.consumer OR NEW.instance_id IS NOT OLD.instance_id
          OR NEW.last_sequence < OLD.last_sequence
        BEGIN SELECT RAISE(ABORT, 'agentic inbox binding is immutable and cursor cannot regress'); END""",
    """CREATE TRIGGER agentic_inbox_binding_retained BEFORE DELETE ON agentic_inbox_cursor
        BEGIN SELECT RAISE(ABORT, 'agentic inbox binding requires explicit reconciliation'); END""",
)


def storage_contribution() -> StorageContribution:
    return StorageContribution(OWNER, TABLE_SPECS, IMMUTABLE_FIELDS, SCHEMA, required_tables=REQUIRED_TABLES)
