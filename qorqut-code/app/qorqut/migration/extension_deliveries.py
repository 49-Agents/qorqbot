"""Addition of the record of extension texts delivered to agents (``extension_deliveries``).

The table starts empty. From this step on, each text of an extension that reaches an agent at one of the hooks and
nudges points is recorded with the extension id and the entry id. Dropping the table forgets those records; nothing else
reads them to decide what to deliver.

This module is a transform of ``python -m qorqut.migration.upgrade``, which applies it after the earlier ones and can
revert it. It has no command of its own. A release that declares this table refuses a store without it, and normal
service startup never upgrades an existing store.
"""
from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution

from qorqut.composition.extension_deliveries import INDEX, OWNER, TABLE

NAME = 'extension_deliveries'
OBJECTS = frozenset({('table', TABLE), ('index', INDEX)})


def strip(contribution):
    """``contribution`` as it stood before this transform; None when the transform introduced it."""
    if type(contribution) is StorageContribution and contribution.owner == OWNER:
        return None
    return contribution


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    connection.execute(objects[('table', TABLE)][1])
    connection.execute(objects[('index', INDEX)][1])


def backward(connection):
    """The exact inverse of ``forward``, dropping whatever rows the table holds. The caller owns the transaction and decides
    whether those rows may go."""
    connection.execute(f'DROP TABLE "{TABLE}"')


def held_rows(connection):
    """How many rows ``backward`` would drop."""
    return connection.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
