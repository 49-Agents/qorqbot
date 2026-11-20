"""Offline widening of the native-session transport rule to accept ``managed``.

A managed agent runs its Claude session headless under a host process of its own, so its binding names a third transport.
A store created before managed agents carries the earlier two-value rule on ``native_sessions.transport``, which SQLite never
changes on its own, so it rejects such a binding. This transform is a step of the ordered upgrade and has no command of its own:

    python -m qorqut.migration.upgrade inspect|apply|revert --instance-root ROOT

It adds no object: it changes the stored definition of ``native_sessions``. SQLite cannot alter a CHECK, and the only
difference is one more accepted value, which every stored row already satisfies. The definition is therefore replaced in place
in the schema catalog, as SQLite documents for a constraint change that leaves stored content alone (the way
``signoff_side_chat`` widens the Signoff category rule). No row, index or trigger is rewritten. The replacement is this build's
own compiled definition; the schema is read again from it and the table's rows are checked against it before the ordered
upgrade verifies the store and commits.

Reverting narrows the rule again. A managed binding cannot exist under the earlier rule, so those are the rows this transform
holds: the ordered upgrade refuses to revert while any exist unless told to discard them. Inputs, captures and replies refer to
a binding, so a managed binding that anything still refers to fails the upgrade's reference check, which leaves the store
unchanged: the owner has to be content to lose that history before the earlier release can open the store.
"""
from __future__ import annotations

from dataclasses import replace

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution

from qorqut.native_chat.schema import OWNER

NAME = 'native_transport_managed'
TABLE = 'native_sessions'
TRANSPORT = 'managed'
NO_DELETE = 'native_sessions_no_delete'
OBJECTS = frozenset()
# The one object whose definition differs between the states on either side of this transform.
CHANGED = frozenset({('table', TABLE)})
EARLIER = "CHECK(transport IN ('terminal','codex_queue'))"
CURRENT = "CHECK(transport IN ('terminal','codex_queue','managed'))"


def _earlier(definition):
    """The table definition as it stood before this transform."""
    if definition.count(CURRENT) != 1:
        raise DomainError('invalid_database', 'The native session table does not carry the managed transport rule')
    return definition.replace(CURRENT, EARLIER)


def strip(contribution):
    """``contribution`` as it stood before this transform: the native-chat tables with the earlier transport rule."""
    if type(contribution) is not StorageContribution or contribution.owner != OWNER:
        return contribution
    return replace(contribution, schema=tuple(
        _earlier(sql) if sql.lstrip().startswith(f'CREATE TABLE {TABLE} ') else sql for sql in contribution.schema))


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
        raise DomainError('invalid_database', 'The native session table definition could not be replaced; no changes made')
    # A new schema version makes this connection, and any opened later, parse the stored definition again.
    connection.execute(f'PRAGMA schema_version={version + 1}')
    if connection.execute(f'PRAGMA quick_check("{TABLE}")').fetchall() != [('ok',)]:
        raise DomainError('invalid_database', 'Stored native sessions do not satisfy the replaced definition; no changes made')


def forward(connection, objects):
    """Widen the rule to the definition of a compiled composition that includes it. The caller owns the transaction."""
    _replace(connection, objects[('table', TABLE)][1])


def backward(connection):
    """The inverse of ``forward``, dropping the managed bindings the earlier rule cannot hold. The caller owns the transaction
    and decides whether those rows may go.

    Bindings are never deleted in normal operation: a trigger refuses it. This is the one deliberate deletion, so the trigger
    is dropped for it and recreated, exactly as stored, before anything else runs."""
    guard = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (NO_DELETE,)).fetchone()
    if guard is None:
        raise DomainError('invalid_database', 'The native session table does not carry its deletion guard')
    connection.execute(f'DROP TRIGGER "{NO_DELETE}"')
    connection.execute(f'DELETE FROM "{TABLE}" WHERE transport=?', (TRANSPORT,))
    connection.execute(guard[0])
    stored = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
    _replace(connection, _earlier(stored))


def held_rows(connection):
    """How many rows ``backward`` would drop."""
    return connection.execute(f'SELECT count(*) FROM "{TABLE}" WHERE transport=?', (TRANSPORT,)).fetchone()[0]
