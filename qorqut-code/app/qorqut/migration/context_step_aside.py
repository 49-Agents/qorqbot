"""A blocked Context input steps aside: several inputs may be blocked, one is processing.

The Context queue used to allow one open input at a time (``processing``, ``blocked`` or ``discard_requested``), so one
blocked input held every later one. A blocked input now steps aside for later inputs of other sources, so only one
``processing`` input remains a stored rule. See ``qorqut_context.integration.driver``.

This transform is a step of the ordered upgrade and has no command of its own:

    python -m qorqut.migration.upgrade inspect|apply|revert --instance-root ROOT

It drops the unique index ``context_inputs_single_open_head`` and creates ``context_inputs_single_processing``; no row
changes. Every store that satisfies the earlier index satisfies the new one. Reverting restores the earlier index, which
holds only while at most one input is open: the inputs beyond one are the rows this transform holds, and reverting refuses
while any exist, with or without ``--discard-rows`` (queue history is never dropped; retry or discard them first).
"""
from __future__ import annotations

from dataclasses import replace

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution

NAME = 'context_step_aside'
OWNER = 'context-queue'
NEW_INDEX = 'context_inputs_single_processing'
OLD_INDEX = 'context_inputs_single_open_head'
OBJECTS = frozenset({('index', NEW_INDEX)})
# The earlier index is gone in the state after this transform.
CHANGED = frozenset({('index', OLD_INDEX)})
CURRENT = f"""CREATE UNIQUE INDEX {NEW_INDEX} ON context_inputs((1))
    WHERE state = 'processing'"""
EARLIER = f"""CREATE UNIQUE INDEX {OLD_INDEX} ON context_inputs((1))
    WHERE state IN ('processing','blocked','discard_requested')"""


def strip(contribution):
    """``contribution`` as it stood before this transform."""
    if type(contribution) is not StorageContribution or contribution.owner != OWNER:
        return contribution
    if contribution.schema.count(CURRENT) != 1:
        raise DomainError('invalid_database', 'The Context queue schema does not carry the single processing index')
    return replace(contribution, schema=tuple(EARLIER if sql == CURRENT else sql for sql in contribution.schema))


def forward(connection, objects):
    """Replace the earlier index with the compiled one. The caller owns the transaction."""
    connection.execute(f'DROP INDEX {OLD_INDEX}')
    connection.execute(objects[('index', NEW_INDEX)][1])


def backward(connection):
    """The inverse of ``forward``. The caller owns the transaction; a store with more than one open input is refused."""
    if held_rows(connection):
        raise DomainError('validation_error', 'More than one Context input is open (processing, blocked or discard requested); '
                          'the earlier index cannot hold. Retry or discard them first. No changes made')
    connection.execute(f'DROP INDEX {NEW_INDEX}')
    connection.execute(EARLIER)


def held_rows(connection):
    """How many open inputs beyond one the earlier index would refuse."""
    count = connection.execute("SELECT count(*) FROM context_inputs WHERE state IN ('processing','blocked','discard_requested')"
                               ).fetchone()[0]
    return max(0, count - 1)
