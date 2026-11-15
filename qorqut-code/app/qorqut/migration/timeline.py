"""Addition of the timelines of Tasks, Projects and Global (``timeline_entries`` and ``timeline_questions``).

``timeline_entries`` is append-only (its triggers refuse a delete and any edit but the Project a moved Task's entries follow);
``timeline_questions`` holds the open questions and how each was closed. Both start empty; nothing here fills them. Dropping the
tables loses every entry and question filed since the upgrade.

This module is a transform of ``python -m qorqut.migration.upgrade``, which applies it after the earlier ones and can
revert it. It has no command of its own. A release that declares these tables refuses a store without them, and normal
service startup never upgrades an existing store.
"""
from __future__ import annotations

from dataclasses import replace

from qorqut_operations.storage.assembly import StorageContribution

NAME = 'timeline'
TABLES = ('timeline_questions', 'timeline_entries')
OBJECTS = frozenset({*(('table', table) for table in TABLES),
                     ('index', 'timeline_entries_item'), ('index', 'timeline_questions_item'), ('index', 'timeline_questions_sequence'),
                     ('trigger', 'timeline_entries_frozen'), ('trigger', 'timeline_entries_retained'),
                     ('trigger', 'inbox_capture_timeline_entries_insert'),
                     ('trigger', 'inbox_capture_timeline_questions_insert'), ('trigger', 'inbox_capture_timeline_questions_update')})


def strip(contribution):
    """``contribution`` as it stood before this transform; None when the transform introduced it."""
    if type(contribution) is not StorageContribution:
        return contribution
    if contribution.owner == 'timeline':
        return None
    if contribution.owner == 'operational_feed':
        return replace(contribution,
            schema=tuple(sql for sql in contribution.schema if not any('inbox_capture_' + table + '_' in sql for table in TABLES)),
            required_tables=contribution.required_tables - set(TABLES))
    return contribution


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    tables = {('table', table) for table in TABLES}
    for kind, name in (*sorted(tables), *sorted(OBJECTS - tables)):
        connection.execute(objects[(kind, name)][1])


def backward(connection):
    """The exact inverse of ``forward``, dropping whatever entries and questions the tables hold. The caller owns the
    transaction and decides whether those rows may go."""
    tables = {('table', table) for table in TABLES}
    for kind, name in sorted(OBJECTS - tables):
        connection.execute(f'DROP {kind.upper()} "{name}"')
    for table in ('timeline_entries', 'timeline_questions'):
        connection.execute(f'DROP TABLE "{table}"')


def held_rows(connection):
    """How many rows ``backward`` would drop."""
    return sum(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in TABLES)
