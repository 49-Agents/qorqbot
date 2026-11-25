"""Filing changes on an item's timeline: check them against what holds now, then append them.

``check`` and ``write`` are separate so a caller can validate and pin targets when a filing is prepared (or staged for review)
and write it later: ``check`` returns the rows a change depends on, ``write`` re-checks and appends in the caller's
transaction. A change is one of::

    {action: add,    kind, text, refs?}          an entry that holds until removed (goal and progress: until a later one)
    {action: remove, entry_id, reason?}          an entry that takes out the standing entry ``entry_id``
    {action: ask,    text, blocking, raised_by?}  an open question for the owner (raised_by owner or coordinator)
    {action: answer, question_id, kind, text}    a decision or fact that closes the open question ``question_id``
    {action: drop,   question_id}                closes the open question ``question_id`` without an answer

An add whose kind and text repeat an entry that already stands (for a goal or a progress note: the current one) appends
nothing and is reported under ``skipped``, so the same filing sent twice never leaves the item saying a thing twice.
"""
from __future__ import annotations

import json
import secrets

from qorqut_operations.contracts import DomainError

from .schema import ENTRIES, KINDS, QUESTIONS
from .state import entry_rows, next_sequence, question_rows, removed_ids

MAX_CHANGES = 10
MAX_TEXT = 400
MAX_SOURCES = 10
MAX_QUOTE = 1000


def check(connection, item, changes):
    """Validate ``changes`` against the item's current rows and return what they depend on as
    ``{'entries': [row of every entry a removal takes out], 'questions': [row of every question an answer or drop closes]}``.
    Raises ``DomainError('validation_error')`` for a change that cannot apply; a target that is gone, already removed or already
    closed is reported that way so the filer reads the item again."""
    if not 1 <= len(changes) <= MAX_CHANGES:
        raise DomainError('validation_error', f'File between 1 and {MAX_CHANGES} changes in one call')
    entries = {row['id']: row for row in entry_rows(connection, item)}
    questions = {row['id']: row for row in question_rows(connection, item)}
    gone, closed = removed_ids(list(entries.values())), set()
    pins = {'entries': [], 'questions': []}
    for position, change in enumerate(changes, 1):
        action, label = change['action'], f'Change {position}'
        if action == 'add':
            if change['kind'] not in KINDS:
                raise DomainError('validation_error', f'{label}: kind is goal, decision, fact or progress')
        elif action == 'remove':
            row = entries.get(change['entry_id'])
            if row is None or row['action'] != 'add':
                raise DomainError('validation_error', f'{label}: entry {change["entry_id"]} is not an entry of this item; read timeline_get again')
            if row['id'] in gone:
                raise DomainError('validation_error', f'{label}: entry {row["id"]} is already removed')
            gone.add(row['id'])
            pins['entries'].append(row)
        elif action in ('answer', 'drop'):
            row = questions.get(change['question_id'])
            if row is None or row['state'] != 'open' or row['id'] in closed:
                raise DomainError('validation_error', f'{label}: question {change["question_id"]} is not an open question of this item')
            if action == 'answer' and change['kind'] not in ('decision', 'fact'):
                raise DomainError('validation_error', f'{label}: an answer is filed as a decision or a fact')
            closed.add(row['id'])
            pins['questions'].append(row)
    return pins


def _same(text):
    return ' '.join(text.split()).casefold()


def _standing(connection, item):
    """``{(kind, normalized text): entry id}`` of what an add would repeat: every standing decision and fact, and the current
    goal and progress note."""
    entries = entry_rows(connection, item)
    gone, found, latest = removed_ids(entries), {}, {}
    for row in entries:
        if row['action'] != 'add':
            continue
        if row['kind'] in ('goal', 'progress'):
            latest[row['kind']] = row
        elif row['id'] not in gone:
            found[(row['kind'], _same(row['text']))] = row['id']
    for kind, row in latest.items():
        if row['id'] not in gone:
            found[(kind, _same(row['text']))] = row['id']
    return found


def _short_id(connection, table):
    while True:
        identity = secrets.token_hex(4)
        if connection.execute(f'SELECT 1 FROM {table} WHERE id=?', (identity,)).fetchone() is None:
            return identity


def write(connection, item, changes, sources, *, created_by, at):
    """Re-check and append ``changes`` in the caller's write transaction, in order, each stamped with the next sequence of the
    shared counter. Returns ``{'entries': [ids appended], 'questions': [ids opened], 'closed': [ids answered or dropped],
    'skipped': [{kind, text, stands_as} of every add that repeated a standing entry]}``."""
    pins = check(connection, item, changes)
    kinds = {row['id']: row['kind'] for row in pins['entries']}
    made = {'entries': [], 'questions': [], 'closed': [], 'skipped': []}
    standing = _standing(connection, item)
    attributes = (item['type'], item['id'], item['project_id'])
    quotes = json.dumps(sources)

    def entry(action, kind, text, **more):
        identity = _short_id(connection, ENTRIES)
        sequence = next_sequence(connection)
        connection.execute(
            f'INSERT INTO {ENTRIES}(id,item_type,item_id,project_id,sequence,action,kind,text,removes_id,question_id,sources_json,'
            'refs_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identity, *attributes, sequence, action, kind, text, more.get('removes_id'), more.get('question_id'), quotes,
             json.dumps(more.get('refs', [])), created_by, at))
        made['entries'].append(identity)
        return identity, sequence

    for change in changes:
        action = change['action']
        if action == 'add':
            key = (change['kind'], _same(change['text']))
            if key in standing:
                made['skipped'].append({'kind': change['kind'], 'text': change['text'], 'stands_as': standing[key]})
                continue
            identity, _ = entry('add', change['kind'], change['text'], refs=change.get('refs', []))
            if change['kind'] in ('goal', 'progress'):
                standing = {known: value for known, value in standing.items() if known[0] != change['kind']}
            standing[key] = identity
        elif action == 'remove':
            entry('remove', kinds[change['entry_id']], change.get('reason', ''), removes_id=change['entry_id'])
            standing = {known: value for known, value in standing.items() if value != change['entry_id']}
        elif action == 'ask':
            identity = _short_id(connection, QUESTIONS)
            connection.execute(
                f'INSERT INTO {QUESTIONS}(id,item_type,item_id,project_id,text,blocking,raised_by,sources_json,sequence,created_by,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?)', (identity, *attributes, change['text'], int(change['blocking']),
                 change.get('raised_by', 'coordinator'), quotes, next_sequence(connection), created_by, at))
            made['questions'].append(identity)
        else:
            if action == 'answer':
                answer, sequence = entry('add', change['kind'], change['text'], question_id=change['question_id'])
            else:
                answer, sequence = None, next_sequence(connection)
            connection.execute(
                f'UPDATE {QUESTIONS} SET state=?,answer_entry_id=?,sequence=?,revision=revision+1,closed_at=? WHERE id=? AND state=\'open\'',
                ('answered' if answer else 'dropped', answer, sequence, at, change['question_id']))
            made['closed'].append(change['question_id'])
    return made
