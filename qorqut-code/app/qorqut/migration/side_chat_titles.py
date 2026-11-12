"""Add editable side-chat names without changing immutable Signoff proposals.

The ordered offline upgrade appends a nullable column. Older stores retain their
original titles until renamed. Reverting requires explicit permission to discard
any saved display names; the original titles and conversations remain intact.
"""
from dataclasses import replace

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import CORE_OWNER, StorageContribution

from .signoff_side_chat import _replace

NAME = 'side_chat_titles'
TABLE = 'signoffs'
OBJECTS = frozenset()
CHANGED = frozenset({('table', TABLE)})
COLUMN = "display_title TEXT CHECK(display_title IS NULL OR (category='side_chat' AND length(trim(display_title)) BETWEEN 1 AND 200))"
DECLARATION = '    ' + COLUMN + ',\n'


def _earlier(definition):
    if definition.count(DECLARATION) != 1:
        raise DomainError('invalid_database', 'The Signoff table does not carry the side-chat display title')
    return definition.replace(DECLARATION, '')


def strip(contribution):
    if type(contribution) is not StorageContribution or contribution.owner != CORE_OWNER:
        return contribution
    return replace(contribution, schema=tuple(
        _earlier(sql) if sql.lstrip().startswith(f'CREATE TABLE "{TABLE}"') else sql for sql in contribution.schema))


def forward(connection, objects):
    connection.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN {COLUMN}')
    # Keep the catalog identical to the compiled target after SQLite appends the column.
    _replace(connection, objects[('table', TABLE)][1])


def backward(connection):
    definition = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
    earlier = _earlier(definition)
    connection.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN display_title')
    _replace(connection, earlier)


def held_rows(connection):
    return connection.execute(f'SELECT count(*) FROM "{TABLE}" WHERE display_title IS NOT NULL').fetchone()[0]
