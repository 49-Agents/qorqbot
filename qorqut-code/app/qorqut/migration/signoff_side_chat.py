"""Offline widening of the Signoff category rule to accept ``side_chat``.

An owner side chat is stored as a Signoff of its own category. A store created
before side chats carries the earlier three-value rule, which SQLite never
changes on its own, so it rejects one. This transform is a step of the ordered
upgrade and has no command of its own:

    python -m qorqut.migration.upgrade inspect|apply|revert --instance-root ROOT

Unlike the transforms before it, it adds no object: it changes the stored
definition of ``signoffs``. SQLite cannot alter a CHECK, and the only difference
is one more accepted value, which every stored row already satisfies. The
definition is therefore replaced in place in the schema catalog, as SQLite
documents for a constraint change that leaves stored content alone. No row,
index or trigger is rewritten. The replacement is this build's own compiled
definition; the schema is read again from it and the table's rows are checked
against it before the ordered upgrade verifies the store and commits.

Reverting narrows the rule again. A side-chat Signoff cannot exist under the
earlier rule, so those are the rows this transform holds: the ordered upgrade
refuses to revert while any exist unless told to discard them. A discarded side
chat that other rows still refer to fails the upgrade's reference check, which
leaves the store unchanged.
"""
from __future__ import annotations

from dataclasses import replace

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import CORE_OWNER, StorageContribution

NAME = 'signoff_side_chat'
TABLE = 'signoffs'
CATEGORY = 'side_chat'
OBJECTS = frozenset()
# The one object whose definition differs between the states on either side of this transform.
CHANGED = frozenset({('table', TABLE)})
EARLIER = "CHECK(category IN ('internal_change','platform_action','input_request'))"
CURRENT = "CHECK(category IN ('internal_change','platform_action','input_request','side_chat'))"


def _earlier(definition):
    """The table definition as it stood before this transform."""
    if definition.count(CURRENT) != 1:
        raise DomainError('invalid_database', 'The Signoff table does not carry the side-chat category rule')
    return definition.replace(CURRENT, EARLIER)


def strip(contribution):
    """``contribution`` as it stood before this transform."""
    if type(contribution) is not StorageContribution or contribution.owner != CORE_OWNER:
        return contribution
    return replace(contribution, schema=tuple(
        _earlier(sql) if sql.lstrip().startswith(f'CREATE TABLE "{TABLE}"') else sql for sql in contribution.schema))


def _replace(connection, definition):
    """Store ``definition`` for the table and have SQLite read it. The caller owns the transaction."""
    version = connection.execute('PRAGMA schema_version').fetchone()[0]
    connection.execute('PRAGMA writable_schema=ON')
    try:
        replaced = connection.execute("UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?",
                                      (definition, TABLE)).rowcount
    finally:
        # Off before the schema is read again: while it is on, SQLite tolerates a definition it cannot parse.
        connection.execute('PRAGMA writable_schema=OFF')
    if replaced != 1:
        raise DomainError('invalid_database', 'The Signoff table definition could not be replaced; no changes made')
    # A new schema version makes this connection, and any opened later, parse the stored definition again.
    connection.execute(f'PRAGMA schema_version={version + 1}')
    if connection.execute(f'PRAGMA quick_check("{TABLE}")').fetchall() != [('ok',)]:
        raise DomainError('invalid_database', 'Stored Signoffs do not satisfy the replaced definition; no changes made')


def forward(connection, objects):
    """Widen the rule to the definition of a compiled composition that includes it. The caller owns the transaction."""
    _replace(connection, objects[('table', TABLE)][1])


def backward(connection):
    """The inverse of ``forward``, dropping the side chats the earlier rule cannot hold. The caller owns the transaction
    and decides whether those rows may go."""
    connection.execute(f'DELETE FROM "{TABLE}" WHERE category=?', (CATEGORY,))
    stored = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
    _replace(connection, _earlier(stored))


def held_rows(connection):
    """How many rows ``backward`` would drop."""
    return connection.execute(f'SELECT count(*) FROM "{TABLE}" WHERE category=?', (CATEGORY,)).fetchone()[0]
