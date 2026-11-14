"""Add record references to private owner–Ordubashy messages, as Signoff notes already carry them.

The ordered offline upgrade appends a column with an empty default, so every earlier message reads as having no
references. Reverting requires explicit permission to discard any saved references; the messages themselves remain.
"""
from dataclasses import replace

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import StorageContribution

NAME = 'private_message_refs'
OWNER = 'inbox'
TABLE = 'owner_conversation_messages'
OBJECTS = frozenset()
CHANGED = frozenset({('table', TABLE)})
COLUMN = "refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(refs_json))"
DECLARATION = ',\n ' + COLUMN


def _earlier(definition):
    if definition.count(DECLARATION) != 1:
        raise DomainError('invalid_database', 'The private message table does not carry message references')
    return definition.replace(DECLARATION, '')


def strip(contribution):
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
        raise DomainError('invalid_database', 'The private message table definition could not be replaced; no changes made')
    connection.execute(f'PRAGMA schema_version={version + 1}')
    if connection.execute(f'PRAGMA quick_check("{TABLE}")').fetchall() != [('ok',)]:
        raise DomainError('invalid_database', 'Stored private messages do not satisfy the replaced definition; no changes made')


def forward(connection, objects):
    connection.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN {COLUMN}')
    # Keep the catalog identical to the compiled target after SQLite appends the column.
    _replace(connection, objects[('table', TABLE)][1])


def backward(connection):
    definition = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
    earlier = _earlier(definition)
    connection.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN refs_json')
    _replace(connection, earlier)


def held_rows(connection):
    return connection.execute(f"SELECT count(*) FROM \"{TABLE}\" WHERE refs_json<>'[]'").fetchone()[0]
