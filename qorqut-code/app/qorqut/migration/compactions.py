"""Addition of the log of an agent's own compactions (``compactions``).

The table starts empty. It holds each handoff as it was filed and when the fresh conversation was opened from it: dropping
it loses that log and which owner messages the last handoff left open, so the next handoff answers only for the messages
of its own period. The notes a handoff wrote on Tasks are Task records and stay.

This module is a transform of ``python -m qorqut.migration.upgrade``, which applies it after the earlier ones and can
revert it. It has no command of its own. A release that declares this table refuses a store without it, and normal
service startup never upgrades an existing store.
"""
from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution

NAME = 'compactions'
TABLE = 'compactions'
INDEX = 'compactions_agent'
OBJECTS = frozenset({('table', TABLE), ('index', INDEX)})


def strip(contribution):
    """``contribution`` as it stood before this transform; None when the transform introduced it."""
    if type(contribution) is StorageContribution and contribution.owner == 'compactions':
        return None
    return contribution


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    connection.execute(objects[('table', TABLE)][1])
    connection.execute(objects[('index', INDEX)][1])


def backward(connection):
    """The exact inverse of ``forward``, dropping whatever rows the table holds. The caller owns the transaction and decides
    whether those rows may go."""
    connection.execute(f'DROP INDEX "{INDEX}"')
    connection.execute(f'DROP TABLE "{TABLE}"')


def held_rows(connection):
    """How many rows ``backward`` would drop."""
    return connection.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
