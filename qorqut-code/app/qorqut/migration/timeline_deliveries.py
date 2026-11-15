"""Addition of the record of what each conversation was told of a timeline (``timeline_deliveries``).

The table starts empty and holds only delivery bookkeeping: dropping it loses nothing but which levels a conversation already
received, so the next Task read of every open conversation sends them in full again.

This module is a transform of ``python -m qorqut.migration.upgrade``, which applies it after the earlier ones and can
revert it. It has no command of its own. A release that declares this table refuses a store without it, and normal
service startup never upgrades an existing store.
"""
from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution

NAME = 'timeline_deliveries'
TABLE = 'timeline_deliveries'
OBJECTS = frozenset({('table', TABLE)})


def strip(contribution):
    """``contribution`` as it stood before this transform; None when the transform introduced it."""
    if type(contribution) is StorageContribution and contribution.owner == 'timeline-deliveries':
        return None
    return contribution


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    connection.execute(objects[('table', TABLE)][1])


def backward(connection):
    """The exact inverse of ``forward``, dropping whatever rows the table holds. The caller owns the transaction and decides
    whether those rows may go."""
    connection.execute(f'DROP TABLE "{TABLE}"')


def held_rows(connection):
    """How many rows ``backward`` would drop."""
    return connection.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
