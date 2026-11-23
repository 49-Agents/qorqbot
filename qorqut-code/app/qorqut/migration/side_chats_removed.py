"""Offline removal of owner side chats: every stored one is deleted and the Signoff rule no longer accepts them.

Side chats were Signoffs of category ``side_chat`` whose private owner conversation carried the talk. The feature is
retired, so this transform deletes them on every installation and narrows the stored Signoff definition back to the three
remaining categories without the side-chat ``display_title`` column. It is a step of the ordered upgrade and has no command
of its own:

    python -m qorqut.migration.upgrade inspect|apply|revert --instance-root ROOT

Inside the upgrade's transaction, children first, it deletes: the idempotency receipts of the three side-chat operations
and of the private-conversation send and mark-read whose saved result names a side chat's conversation (they hold copies
of the messages); the inbox events announcing those conversations or the side-chat Signoffs, with their deliveries; the
conversations' read positions, messages and the conversations themselves; and the Signoffs. Rows of any other table that
refer to a deleted row by foreign key go too, found through the schema's own foreign keys, so an extension's table cannot
leave a reference that fails the upgrade's final check. Only the delete guards and the inbox capture triggers of the tables
deleted from are lifted, and each is recreated exactly as stored before anything else runs: the deletion records no inbox
change for a processor that is not running. Receipts of other operations are kept even when they mention a side chat.

The data change is one-way. Reverting restores the earlier definition (the side-chat category and the empty title column),
but the deleted side chats do not come back, so this transform holds no rows. Records made from what was said in a side
chat are deliberately left alone and keep a pointer that now leads nowhere: Task comments whose attribution names a
side-chat message, action revision provenance, Context inputs, learning results, action log lines and files on disk.
Their readers report the missing conversation as unavailable.
"""
from __future__ import annotations

from dataclasses import replace
import re

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import CORE_OWNER, StorageContribution

from . import side_chat_titles, signoff_side_chat
from .signoff_side_chat import _replace

NAME = 'side_chats_removed'
TABLE = 'signoffs'
OBJECTS = frozenset()
CHANGED = frozenset({('table', TABLE)})
# The column before the retired title in the stored definition; the title goes back right after it.
ANCHOR = '    request_key TEXT,\n'
SIDE_CHATS = "SELECT id FROM signoffs WHERE category='side_chat'"
CONVERSATIONS = f"SELECT id FROM owner_conversations WHERE source_type='signoff' AND source_id IN ({SIDE_CHATS})"
# Each selection reads only tables deleted from after it.
ROOTS = (
    ('idempotency_receipts', "operation_name IN ('side_chats_create','side_chats_rename','side_chats_close') OR "
     "(operation_name IN ('owner_conversation_send','owner_conversation_mark_read') "
     f"AND json_extract(result_json,'$.data.conversation.id') IN ({CONVERSATIONS}))"),
    ('inbox_events', f"(source_type='owner_conversations' AND source_id IN ({CONVERSATIONS})) "
     f"OR (source_type='signoffs' AND source_id IN ({SIDE_CHATS}))"),
    ('owner_conversation_reads', f"conversation_id IN ({CONVERSATIONS})"),
    ('owner_conversation_messages', f"conversation_id IN ({CONVERSATIONS})"),
    ('owner_conversations', f"id IN ({CONVERSATIONS})"),
    (TABLE, "category='side_chat'"),
)
_DELETE = re.compile(r'\b(BEFORE|AFTER|INSTEAD\s+OF)\s+DELETE\s+ON\b', re.IGNORECASE)


def _earlier(definition):
    """The table definition as it stood before this transform: the side-chat category and title column back in place."""
    if definition.count(signoff_side_chat.EARLIER) != 1 or definition.count(ANCHOR) != 1 or side_chat_titles.COLUMN in definition:
        raise DomainError('invalid_database', 'The Signoff table does not carry the three-category rule')
    return definition.replace(signoff_side_chat.EARLIER, signoff_side_chat.CURRENT).replace(
        ANCHOR, ANCHOR + side_chat_titles.DECLARATION)


def strip(contribution):
    """``contribution`` as it stood before this transform."""
    if type(contribution) is not StorageContribution or contribution.owner != CORE_OWNER:
        return contribution
    return replace(contribution, schema=tuple(
        _earlier(sql) if sql.lstrip().startswith(f'CREATE TABLE "{TABLE}"') else sql for sql in contribution.schema))


def _key(connection, table):
    keyed = sorted((row[5], row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")') if row[5])
    if len(keyed) != 1:
        raise DomainError('invalid_database', f'{table} is referenced without a single-column key; no changes made')
    return keyed[0][1]


def _deletions(connection):
    """``(table, condition)`` in deletion order: every row that refers to a deleted row by foreign key goes before it."""
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    references = {}
    for child in tables:
        groups = {}
        for row in connection.execute(f'PRAGMA foreign_key_list("{child}")'):
            groups.setdefault(row[0], []).append(row)
        for group in groups.values():
            references.setdefault(group[0][2].lower(), []).append((child, group))
    ordered = []

    def visit(table, condition, path):
        for child, group in references.get(table.lower(), ()):
            if child.lower() == table.lower():
                continue
            if len(group) != 1:
                raise DomainError('invalid_database', f'{child} refers to {table} by several columns; no changes made')
            if child.lower() in path:
                raise DomainError('invalid_database', f'{child} and {table} refer to each other; no changes made')
            target = group[0][4] or _key(connection, table)
            visit(child, f'"{group[0][3]}" IN (SELECT "{target}" FROM "{table}" WHERE {condition})', path | {child.lower()})
        ordered.append((table, condition))

    for table, condition in ROOTS:
        visit(table, condition, {table.lower()})
    return ordered


def forward(connection, objects):
    """Delete every side chat and what holds its conversation, then narrow the definition to the compiled target's. The
    caller owns the transaction."""
    deletions = _deletions(connection)
    touched = {table.lower() for table, _ in deletions}
    triggers = connection.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger' ORDER BY rowid").fetchall()
    # Only delete guards and inbox captures are lifted; any other trigger on these tables runs as it always does.
    lifted = [(name, sql) for name, table, sql in triggers if table.lower() in touched and _DELETE.search(sql)
              and (name.startswith('inbox_capture_') or (re.search(r'\bBEFORE\s+DELETE\s+ON\b', sql, re.IGNORECASE)
                                                         and re.search(r'\bRAISE\s*\(', sql, re.IGNORECASE)))]
    for name, _ in lifted:
        connection.execute(f'DROP TRIGGER "{name}"')
    for table, condition in deletions:
        connection.execute(f'DELETE FROM "{table}" WHERE {condition}')
    for _, sql in lifted:
        connection.execute(sql)
    if connection.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").fetchall() != sorted(triggers):
        raise DomainError('invalid_database', 'The lifted triggers did not come back exactly as stored; no changes made')
    connection.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN display_title')
    _replace(connection, objects[('table', TABLE)][1])


def backward(connection):
    """The earlier definition again: the side-chat category is accepted and the empty title column is back. The side chats
    ``forward`` deleted do not come back. The caller owns the transaction."""
    definition = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()[0]
    earlier = _earlier(definition)
    connection.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN {side_chat_titles.COLUMN}')
    _replace(connection, earlier)


def held_rows(connection):
    """Nothing: the earlier definition holds every row this one can."""
    return 0
