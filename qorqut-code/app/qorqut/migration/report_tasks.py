"""Addition of the Task a read-and-acknowledge report belongs to (``signoff_report_tasks``).

The table holds one row per report: the Signoff and the Task it reports on. It is not a response dependency (that is
``signoff_source_links``) and changes no Signoff or Task. The table starts empty; reports posted before it existed have
no row and read as belonging to no Task, and nothing here links them. Dropping the table loses only these associations.

This module is a transform of ``python -m qorqut.migration.upgrade``, which applies it after the earlier ones and can
revert it. It has no command of its own. A release that declares this table refuses a store without it, and normal
service startup never upgrades an existing store.
"""
from __future__ import annotations

from dataclasses import replace

from qorqut_operations.storage.assembly import StorageContribution

NAME = 'report_tasks'
TABLE = 'signoff_report_tasks'
OBJECTS = frozenset({('table', TABLE), ('index', TABLE + '_task'),
                     *(('trigger', TABLE + '_' + event) for event in ('insert', 'update', 'delete')),
                     *(('trigger', 'inbox_capture_' + TABLE + '_' + event) for event in ('insert', 'update', 'delete'))})


def strip(contribution):
    """``contribution`` as it stood before this transform; None when the transform introduced it."""
    if type(contribution) is not StorageContribution:
        return contribution
    if contribution.owner == 'signoff-report-tasks':
        return None
    if contribution.owner == 'operational_feed':
        return replace(contribution,
            schema=tuple(sql for sql in contribution.schema if 'inbox_capture_' + TABLE + '_' not in sql),
            required_tables=contribution.required_tables - {TABLE})
    return contribution


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    connection.execute(objects[('table', TABLE)][1])
    for kind, name in sorted(OBJECTS - {('table', TABLE)}):
        connection.execute(objects[(kind, name)][1])


def backward(connection):
    """The exact inverse of ``forward``, dropping whatever associations the table holds. The caller owns the transaction
    and decides whether those rows may go."""
    for kind, name in sorted(OBJECTS - {('table', TABLE)}):
        connection.execute(f'DROP {kind.upper()} "{name}"')
    connection.execute(f'DROP TABLE "{TABLE}"')


def held_rows(connection):
    """How many rows ``backward`` would drop."""
    return connection.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
