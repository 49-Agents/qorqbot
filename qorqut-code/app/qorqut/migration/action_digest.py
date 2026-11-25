"""Addition of the daily Action digest's two tables (``context_action_backlog`` and ``context_action_digests``).

Both tables start empty. From this step on, a recorded, corrected or withdrawn Action is noted in the backlog instead of
being queued as its own Context input, and once a local day is over its Actions become one GENERAL Context input. The
Action inputs already in the queue stay as they are and are filed as before. Dropping the tables loses which revisions
are still to be digested and the digest cursor: revisions recorded since this step would then reach no digest.

This module is a transform of ``python -m qorqut.migration.upgrade``, which applies it after the earlier ones and can
revert it. It has no command of its own. A release that declares these tables refuses a store without them, and normal
service startup never upgrades an existing store.
"""
from __future__ import annotations

from qorqut_operations.storage.assembly import StorageContribution

from qorqut_context.integration.action_digest.schema import BACKLOG, DIGESTS, OWNER, TRIGGERS

NAME = 'action_digest'
OBJECTS = frozenset({('table', BACKLOG), ('table', DIGESTS), *(('trigger', name) for name in TRIGGERS)})
# Creation order: each trigger after the table it is on or reads.
_ORDER = (('table', BACKLOG), ('trigger', 'context_action_backlog_immutable'), ('trigger', 'context_action_backlog_keep'),
          ('table', DIGESTS), ('trigger', 'context_action_digests_immutable'), ('trigger', 'context_action_digests_keep'),
          ('trigger', 'context_action_digest_inputs'))


def strip(contribution):
    """``contribution`` as it stood before this transform; None when the transform introduced it."""
    if type(contribution) is StorageContribution and contribution.owner == OWNER:
        return None
    return contribution


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    for key in _ORDER:
        connection.execute(objects[key][1])


def backward(connection):
    """The exact inverse of ``forward``, dropping whatever rows the tables hold. The caller owns the transaction and decides
    whether those rows may go."""
    connection.execute('DROP TRIGGER "context_action_digest_inputs"')
    connection.execute(f'DROP TABLE "{DIGESTS}"')
    connection.execute(f'DROP TABLE "{BACKLOG}"')


def held_rows(connection):
    """How many rows ``backward`` would drop."""
    return sum(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in (BACKLOG, DIGESTS))
