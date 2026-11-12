"""Addition of the native user turn index (``native_owner_turns``).

The table holds one pointer per native user turn of an agent's conversation: where the turn starts in the transcript
archive, when it was written and how it arrived. It holds no text. The indexer fills it from the day the table exists;
turns written before that are not indexed, and dropping the table loses only pointers that a re-index could rebuild.

This module is a transform of ``python -m qorqut.migration.upgrade``, which applies it after the earlier ones and can
revert it. It has no command of its own. A release that declares this table refuses a store without it, and normal
service startup never upgrades an existing store.
"""
from __future__ import annotations

from dataclasses import replace

from qorqut_operations.storage.assembly import StorageContribution

from qorqut.native_chat.schema import OWNER

NAME = 'owner_turns'
TABLE = 'native_owner_turns'
OBJECTS = frozenset({('table', TABLE),
                     *(('index', TABLE + '_' + suffix) for suffix in ('time', 'agent', 'input')),
                     *(('trigger', TABLE + '_' + suffix) for suffix in ('no_update', 'no_delete'))})


def strip(contribution):
    """``contribution`` as it stood before this transform: the native-chat tables without the turn index."""
    if type(contribution) is not StorageContribution or contribution.owner != OWNER:
        return contribution
    return replace(contribution,
                   tables={name: spec for name, spec in contribution.tables.items() if name != TABLE},
                   immutable_fields={name: fields for name, fields in contribution.immutable_fields.items() if name != TABLE},
                   schema=tuple(sql for sql in contribution.schema if TABLE not in sql))


def forward(connection, objects):
    """This transform's DDL, taken from a compiled composition that includes it. The caller owns the transaction."""
    connection.execute(objects[('table', TABLE)][1])
    for key in sorted(OBJECTS - {('table', TABLE)}):
        connection.execute(objects[key][1])


def backward(connection):
    """The exact inverse of ``forward``, dropping whatever pointers the table holds. The caller owns the transaction and
    decides whether those rows may go."""
    for kind, name in sorted(OBJECTS - {('table', TABLE)}):
        connection.execute(f'DROP {kind.upper()} "{name}"')
    connection.execute(f'DROP TABLE "{TABLE}"')


def held_rows(connection):
    """How many rows ``backward`` would drop."""
    return connection.execute(f'SELECT count(*) FROM "{TABLE}"').fetchone()[0]
