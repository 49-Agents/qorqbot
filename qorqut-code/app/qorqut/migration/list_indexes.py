"""Addition of the indexes behind the dashboard's job and routine-run lists (``jobs_business_created``,
``jobs_business_active``, ``jobs_business_queued``, ``routine_runs_namespace_created`` and
``routine_runs_business_created``).

The indexes hold no data of their own: dropping them loses nothing, and the lists they serve return the same rows without
them, only more slowly. Applying builds each over the rows already stored, so the store is offline for that long.

This module is a transform of ``python -m qorqut.migration.upgrade``, which applies it after the earlier ones and can
revert it. It has no command of its own. A release that declares these indexes refuses a store without them, and normal
service startup never upgrades an existing store.
"""
from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.list_indexes import OBJECTS, OWNER  # noqa: F401 - OBJECTS is read by the upgrade tests

NAME = 'list_indexes'


def strip(contribution):
    """``contribution`` as it stood before this transform; None when the transform introduced it."""
    if type(contribution) is StorageContribution and contribution.owner == OWNER:
        return None
    return contribution


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    for key in sorted(OBJECTS):
        connection.execute(objects[key][1])


def backward(connection):
    """The exact inverse of ``forward``. The caller owns the transaction."""
    for _, name in sorted(OBJECTS):
        connection.execute(f'DROP INDEX "{name}"')


def held_rows(connection):
    """How many rows ``backward`` would drop: indexes hold none of their own."""
    return 0
