"""Subprojects: three columns on ``projects``.

A subproject is an ordinary Project with a parent Project. The row keeps its parent (``parent_id``), the Project whose
archiving also archived it (``archived_with``) and, for a subproject, its folder relative to the projects directory
(``folder``, inside the parent's folder). See ``qorqut_operations.projects.hierarchy``.

This transform is a step of the ordered upgrade and has no command of its own:

    python -m qorqut.migration.upgrade inspect|apply|revert --instance-root ROOT

It adds no object: it changes the stored definition of ``projects``. The three nullable columns are added with
``ALTER TABLE``, which leaves every stored row as it was (every existing Project stays top level, with no parent and its
folder unchanged), and the stored definition is then replaced with this build's own compiled one. Reverting drops the
three columns and restores the earlier definition. Subprojects and cascade marks are the rows this transform holds: the
ordered upgrade refuses to revert while any exist unless told to discard them (a reverted subproject's folder stays where
it is on disk, and the earlier build no longer finds it).
"""
from __future__ import annotations

from dataclasses import replace

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import CORE_OWNER, StorageContribution

NAME = 'project_hierarchy'
TABLE = 'projects'
OBJECTS = frozenset()
CHANGED = frozenset({('table', TABLE)})
COLUMNS = ('parent_id TEXT', 'archived_with TEXT', 'folder TEXT')
EARLIER_END = ', archived_at TEXT, archived_by TEXT REFERENCES principals(id))'
CURRENT_END = ', archived_at TEXT, archived_by TEXT REFERENCES principals(id), ' + ', '.join(COLUMNS) + ')'


def _earlier(definition):
    """The table definition as it stood before this transform."""
    if not definition.endswith(CURRENT_END) or definition.count(CURRENT_END) != 1:
        raise DomainError('invalid_database', 'The projects table does not carry the project hierarchy')
    return definition[:-len(CURRENT_END)] + EARLIER_END


def _is_table(sql):
    return sql.lstrip().startswith(f'CREATE TABLE {TABLE} (')


def strip(contribution):
    """``contribution`` as it stood before this transform."""
    if type(contribution) is not StorageContribution or contribution.owner != CORE_OWNER:
        return contribution
    return replace(contribution, schema=tuple(_earlier(sql) if _is_table(sql) else sql for sql in contribution.schema))


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
        raise DomainError('invalid_database', 'The projects table definition could not be replaced; no changes made')
    connection.execute(f'PRAGMA schema_version={version + 1}')
    if connection.execute(f'PRAGMA quick_check("{TABLE}")').fetchall() != [('ok',)]:
        raise DomainError('invalid_database', 'Stored projects do not satisfy the replaced definition; no changes made')


def forward(connection, objects):
    """Add the three columns, then store the compiled definition. The caller owns the transaction."""
    for column in COLUMNS:
        connection.execute(f'ALTER TABLE {TABLE} ADD COLUMN {column}')
    _replace(connection, objects[('table', TABLE)][1])


def backward(connection):
    """The inverse of ``forward``: the hierarchy goes with its columns. The caller owns the transaction and decides whether
    it may go."""
    stored = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
    earlier = _earlier(stored)
    for column in reversed(COLUMNS):
        connection.execute(f'ALTER TABLE {TABLE} DROP COLUMN {column.split()[0]}')
    _replace(connection, earlier)


def held_rows(connection):
    """How many Projects carry a parent, a cascade mark or a subproject folder that ``backward`` would drop."""
    return connection.execute(f'SELECT count(*) FROM {TABLE} WHERE parent_id IS NOT NULL OR archived_with IS NOT NULL '
                              'OR folder IS NOT NULL').fetchone()[0]
