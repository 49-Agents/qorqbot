"""Change-feed capture over the engine's operational-record tables.

The dashboard's change feed learns about writes from ``inbox_changes`` rows that same-transaction triggers leave behind (see
``service/changes.py``). In v1 those triggers sat on the host-owned ``tasks``, ``task_notes``, ``action_logs`` and
``action_log_labels`` tables. The engine now owns the Task, thread, tag and Action tables, so the host attaches the same kind of
capture here, after the engine's storage and as its own owner, instead of the engine knowing the host's feed. Each trigger
records only the scope (``business_id``) and the identity the feed groups by; no row text or private payload is copied.
Immutable tables (thread messages, Action revisions) capture inserts only."""

from __future__ import annotations

from qorqut_operations import crm
from qorqut_operations.storage.assembly import StorageContribution

OWNER = "operational_feed"


def _crm(table: str):
    """The CRM tables' identity columns come from the Domain's own repository specification, never from a guess here."""
    spec = crm.TABLES[table]
    return tuple(dict.fromkeys((spec.scope_column, *spec.keys))) if spec.scope_column else spec.keys


# table -> (columns captured as the row identity, events captured)
CAPTURED = {
    "tasks": (("business_id", "id"), ("insert", "update", "delete")),
    "thread_messages": (("business_id", "item_type", "item_id"), ("insert",)),
    "thread_follows": (("business_id", "item_type", "item_id"), ("insert", "update")),
    "item_tags": (("business_id", "item_type", "item_id"), ("insert", "delete")),
    "action_logs": (("business_id", "id"), ("insert", "update")),
    "action_businesses": (("business_id", "action_key"), ("insert", "delete")),
    # CRM memberships carry their own capture triggers in the Domain's approved DDL (``qorqut_operations.crm``), beside
    # the guards that read the same rows; declaring them again here would build the same trigger twice and publish two
    # feed rows per change. Recorded observations are immutable and have no trigger there, so the feed attaches this one.
    "crm_activity_events": (_crm("crm_activity_events"), ("insert",)),
}


def _object(prefix: str, columns) -> str:
    return "json_object(" + ",".join(f"'{column}',{prefix}.{column}" for column in columns) + ")"


def triggers() -> tuple[str, ...]:
    statements = []
    for table, (columns, events) in CAPTURED.items():
        for event in events:
            before = _object("OLD", columns) if event in ("update", "delete") else "NULL"
            after = _object("NEW", columns) if event in ("insert", "update") else "NULL"
            statements.append(
                f"CREATE TRIGGER inbox_capture_{table}_{event} AFTER {event.upper()} ON {table}\n"
                f"BEGIN INSERT INTO inbox_changes(source_type,action,actor_id,before_json,after_json)\n"
                f"VALUES('{table}','{event}',inbox_actor(),{before},{after}); END")
    return tuple(statements)


def capture_contribution() -> StorageContribution:
    return StorageContribution(OWNER, schema=triggers(), required_tables=frozenset({"inbox_changes", *CAPTURED}))


__all__ = ["CAPTURED", "OWNER", "capture_contribution", "triggers"]
