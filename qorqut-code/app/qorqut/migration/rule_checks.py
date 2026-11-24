"""The owner's check of a rule an agent filed: three columns on ``experience_observations``.

A rule an agent files from the owner's words gets a card for the owner (``experience_rule_check``); the observation keeps
the owner's mark (``owner_check``), the card's Signoff (``check_signoff_id``) and when it was checked (``checked_at``). The
mark also makes the table's rows editable: an approval may replace the rule's text, and a rejection deletes the rule.

This transform is a step of the ordered upgrade and has no command of its own:

    python -m qorqut.migration.upgrade inspect|apply|revert --instance-root ROOT

It adds no object: it changes the stored definition of ``experience_observations``. The three nullable columns are
added with ``ALTER TABLE``, which leaves every stored row as it was (no rule filed before this step has a mark), and the
stored definition is then replaced with this build's own compiled one, which differs from what SQLite wrote only in
layout. Reverting drops the three columns and restores the earlier definition. The marks are the rows this transform
holds: the ordered upgrade refuses to revert while any exist unless told to discard them.
"""
from __future__ import annotations

from dataclasses import replace

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

from qorqut.experience.schema import OBSERVATIONS, OWNER

NAME = 'rule_checks'
TABLE = OBSERVATIONS
OBJECTS = frozenset()
# The one object whose definition differs between the states on either side of this transform.
CHANGED = frozenset({('table', TABLE)})
COLUMNS = ("owner_check TEXT CHECK(owner_check IS NULL OR owner_check IN ('unchecked','approved','rejected'))",
           'check_signoff_id TEXT', 'checked_at TEXT')
EARLIER_END = ' created_at TEXT NOT NULL\n)'
CURRENT_END = ' created_at TEXT NOT NULL,\n' + ''.join(f' {column},\n' for column in COLUMNS[:-1]) + f' {COLUMNS[-1]}\n)'
# Before this step the table's rows were append-only.
EARLIER_SPEC = TableSpec(('id',), mutable=False, revision_column=None)


def _earlier(definition):
    """The table definition as it stood before this transform."""
    if not definition.endswith(CURRENT_END) or definition.count(CURRENT_END) != 1:
        raise DomainError('invalid_database', 'The observations table does not carry the owner\'s rule check')
    return definition[:-len(CURRENT_END)] + EARLIER_END


def _is_table(sql):
    return sql.lstrip().startswith(f'CREATE TABLE {TABLE} (')


def strip(contribution):
    """``contribution`` as it stood before this transform."""
    if type(contribution) is not StorageContribution or contribution.owner != OWNER:
        return contribution
    return replace(contribution, tables={**contribution.tables, TABLE: EARLIER_SPEC},
                   schema=tuple(_earlier(sql) if _is_table(sql) else sql for sql in contribution.schema))


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
        raise DomainError('invalid_database', 'The observations table definition could not be replaced; no changes made')
    # A new schema version makes this connection, and any opened later, parse the stored definition again.
    connection.execute(f'PRAGMA schema_version={version + 1}')
    if connection.execute(f'PRAGMA quick_check("{TABLE}")').fetchall() != [('ok',)]:
        raise DomainError('invalid_database', 'Stored observations do not satisfy the replaced definition; no changes made')


def forward(connection, objects):
    """Add the three columns, then store the compiled definition. The caller owns the transaction."""
    for column in COLUMNS:
        connection.execute(f'ALTER TABLE {TABLE} ADD COLUMN {column}')
    _replace(connection, objects[('table', TABLE)][1])


def backward(connection):
    """The inverse of ``forward``: the marks go with their columns. The caller owns the transaction and decides whether
    they may go."""
    stored = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
    earlier = _earlier(stored)
    for column in reversed(COLUMNS):
        connection.execute(f'ALTER TABLE {TABLE} DROP COLUMN {column.split()[0]}')
    _replace(connection, earlier)


def held_rows(connection):
    """How many observations carry a mark of the owner's check that ``backward`` would drop."""
    return connection.execute(f'SELECT count(*) FROM {TABLE} WHERE owner_check IS NOT NULL OR check_signoff_id IS NOT NULL '
                              'OR checked_at IS NOT NULL').fetchone()[0]
