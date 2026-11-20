"""Moving Tables (and project documents) between projects: their move audit and the two guards that admit a move.

The ordered offline upgrade adds the append-only ``table_moves`` and ``document_moves`` audits (empty) and replaces two
triggers so that an armed Table move may change a Table discussion entry's scope and a CRM membership's Table scope, exactly
as a Task move already may for a Task's. Nothing else about either guard changes, and no row is rewritten.

Reverting restores both triggers and drops the audits; it requires explicit permission to discard any recorded moves. This
module is a transform of ``python -m qorqut.migration.upgrade`` and has no command of its own.
"""
from __future__ import annotations

from dataclasses import replace

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.record_moves import OWNER

NAME = 'record_moves'
TABLES = ('table_moves', 'document_moves')
OBJECTS = frozenset({*(('table', table) for table in TABLES), ('index', 'table_moves_table'), ('index', 'document_moves_document'),
                     ('trigger', 'table_moves_immutable_update'), ('trigger', 'table_moves_keep'),
                     ('trigger', 'document_moves_immutable_update'), ('trigger', 'document_moves_keep')})
THREAD_TRIGGER, CRM_TRIGGER = 'thread_messages_immutable_update', 'crm_entries_identity'
CHANGED = frozenset({('trigger', THREAD_TRIGGER), ('trigger', CRM_TRIGGER)})

# The exact text each replaced trigger gained, as (earlier, current) fragments.
THREAD_EDITS = (
    (" AND NOT (OLD.item_type='task' AND NEW.business_id IS NOT OLD.business_id AND NEW.event_id IS OLD.event_id",
     " AND NOT (OLD.item_type IN ('task','table') AND NEW.business_id IS NOT OLD.business_id AND NEW.event_id IS OLD.event_id"),
    (" AND NEW.created_at IS OLD.created_at AND relocation_permits(OLD.business_id,NEW.business_id,OLD.item_id)",
     " AND NEW.created_at IS OLD.created_at\n AND relocation_permits(OLD.business_id,NEW.business_id,CASE OLD.item_type WHEN 'task' "
     "THEN OLD.item_id ELSE 'table:' || OLD.item_id END)"),
)
CRM_EDITS = (
    ("  AND relocation_permits(OLD.record_business_id,NEW.record_business_id,OLD.record_task_id))\nBEGIN",
     "  AND relocation_permits(OLD.record_business_id,NEW.record_business_id,OLD.record_task_id))\n"
     " AND NOT (NEW.id IS OLD.id AND NEW.business_id IS NOT OLD.business_id AND NEW.table_id IS OLD.table_id\n"
     "  AND NEW.record_task_id IS OLD.record_task_id AND NEW.record_business_id IS OLD.record_business_id\n"
     "  AND NEW.record_namespace IS OLD.record_namespace AND NEW.created_by IS OLD.created_by AND NEW.created_at IS OLD.created_at\n"
     "  AND relocation_permits(OLD.business_id,NEW.business_id,'table:' || OLD.table_id))\nBEGIN"),
)


def _earlier(sql, edits):
    for before, after in edits:
        if sql.count(after) != 1:
            raise DomainError('invalid_database', 'A move guard does not have the definition this release expects')
        sql = sql.replace(after, before)
    return sql


def _named(sql, name):
    return sql.lstrip().startswith(f'CREATE TRIGGER {name} ')


def strip(contribution):
    """``contribution`` as it stood before this transform; None when the transform introduced it."""
    if type(contribution) is not StorageContribution:
        return contribution
    if contribution.owner == OWNER:
        return None
    for owner, name, edits in (('discussion', THREAD_TRIGGER, THREAD_EDITS), ('crm', CRM_TRIGGER, CRM_EDITS)):
        if contribution.owner == owner:
            return replace(contribution, schema=tuple(_earlier(sql, edits) if _named(sql, name) else sql for sql in contribution.schema))
    return contribution


def _swap(connection, name, sql):
    connection.execute(f'DROP TRIGGER "{name}"')
    connection.execute(sql)


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    tables = {('table', table) for table in TABLES}
    for kind, name in (*sorted(tables), *sorted(OBJECTS - tables)):
        connection.execute(objects[(kind, name)][1])
    for kind, name in sorted(CHANGED):
        _swap(connection, name, objects[(kind, name)][1])


def backward(connection):
    """The exact inverse of ``forward``. The caller owns the transaction and decides whether recorded moves may go."""
    for name, edits in ((THREAD_TRIGGER, THREAD_EDITS), (CRM_TRIGGER, CRM_EDITS)):
        current = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()[0]
        _swap(connection, name, _earlier(current, edits))
    tables = {('table', table) for table in TABLES}
    for kind, name in sorted(OBJECTS - tables):
        connection.execute(f'DROP {kind.upper()} "{name}"')
    for table in TABLES:
        connection.execute(f'DROP TABLE "{table}"')


def held_rows(connection):
    """How many recorded moves ``backward`` would drop."""
    return sum(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in TABLES)

