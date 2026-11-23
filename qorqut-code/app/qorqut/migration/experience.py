"""Addition of the Learning Applier's workflow stores (``experience_runs``, ``experience_clusters``,
``experience_observations``, ``agentic_delta_proposals`` and ``agentic_delta_proposal_links``).

All five start empty. Dropping them loses every filed observation, the clusters they were filed under, every delta
proposal with its state, and the queue of workflow runs. The approval requests (Signoffs) of the proposals are Signoff
records and stay.

This module is a transform of ``python -m qorqut.migration.upgrade``, which applies it after the earlier ones and can
revert it. It has no command of its own. A release that declares these tables refuses a store without them, and normal
service startup never upgrades an existing store.
"""
from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution

from qorqut.experience.schema import INDEXES, OWNER, TABLES  # noqa: F401 - TABLES is read by the upgrade tests

NAME = 'experience'
OBJECTS = frozenset({*(('table', table) for table in TABLES), *(('index', index) for index in INDEXES)})
# Children before the tables they reference.
DROP_ORDER = ('agentic_delta_proposal_links', 'agentic_delta_proposals', 'experience_observations', 'experience_clusters',
              'experience_runs')


def strip(contribution):
    """``contribution`` as it stood before this transform; None when the transform introduced it."""
    if type(contribution) is StorageContribution and contribution.owner == OWNER:
        return None
    return contribution


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    for table in reversed(DROP_ORDER):
        connection.execute(objects[('table', table)][1])
    for index in sorted(INDEXES):
        connection.execute(objects[('index', index)][1])


def backward(connection):
    """The exact inverse of ``forward``, dropping whatever rows the tables hold. The caller owns the transaction and decides
    whether those rows may go."""
    for index in sorted(INDEXES):
        connection.execute(f'DROP INDEX "{index}"')
    for table in DROP_ORDER:
        connection.execute(f'DROP TABLE "{table}"')


def held_rows(connection):
    """How many rows ``backward`` would drop."""
    return sum(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in TABLES)
