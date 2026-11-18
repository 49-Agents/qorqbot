"""What holds now on an item, worked out from its entries and questions, and the text form agents are given.

The state is read off the timeline, never stored: the latest goal; every decision and fact added and not removed, oldest first;
the latest progress note; the open questions. A removal names the entry it takes out. A later goal or progress note replaces
the earlier one for good: removing the latest leaves none and never brings an older one back. The revision of an item is the
highest sequence among its entries and its questions (0 when it has none)."""
from __future__ import annotations

import json

from .schema import ENTRIES, QUESTIONS

# Above this many characters the standing state asks its filer to remove what no longer holds. A warning, never a limit.
WARNING_CHARACTERS = 3000
LABELS = {'task': 'Task', 'project': 'Project', 'global': 'Global'}
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def item_key(item):
    """``(item_type, item_id, business_id)`` of an ``{type, id, business_id}`` item."""
    return item['type'], item['id'], item['business_id']


def next_sequence(connection):
    """The next value of the one sequence shared by entries and question changes. Call inside the writing transaction."""
    return 1 + max(connection.execute(f'SELECT COALESCE(MAX(sequence),0) FROM {table}').fetchone()[0] for table in (ENTRIES, QUESTIONS))


def entry_rows(connection, item):
    return [dict(row) for row in connection.execute(
        f'SELECT * FROM {ENTRIES} WHERE item_type=? AND item_id=? AND business_id IS ? ORDER BY sequence', item_key(item))]


def question_rows(connection, item):
    return [dict(row) for row in connection.execute(
        f'SELECT * FROM {QUESTIONS} WHERE item_type=? AND item_id=? AND business_id IS ? ORDER BY sequence', item_key(item))]


def removed_ids(entries):
    return {row['removes_id'] for row in entries if row['action'] == 'remove'}


def revision_of(entries, questions):
    return max((row['sequence'] for row in (*entries, *questions)), default=0)


def revision(connection, item):
    return revision_of(entry_rows(connection, item), question_rows(connection, item))


def _entry(row):
    return {'id': row['id'], 'text': row['text'], 'created_at': row['created_at'], 'sequence': row['sequence']}


def _question(row):
    return {'id': row['id'], 'text': row['text'], 'blocking': bool(row['blocking']), 'raised_by': row['raised_by'],
            'created_at': row['created_at'], 'sequence': row['sequence']}


def state(connection, item):
    """What holds now on ``item``: ``{item, revision, goal, decisions, facts, progress, questions}``, entries and questions
    carrying their ids. ``goal`` and ``progress`` are the latest standing one or None."""
    entries, questions = entry_rows(connection, item), question_rows(connection, item)
    gone = removed_ids(entries)
    added = [row for row in entries if row['action'] == 'add']
    standing = [row for row in added if row['id'] not in gone]

    def latest(kind):
        row = next((row for row in reversed(added) if row['kind'] == kind), None)
        return _entry(row) if row and row['id'] not in gone else None
    return {'item': {'type': item['type'], 'id': item['id'], 'business_id': item['business_id']},
            'revision': revision_of(entries, questions), 'goal': latest('goal'),
            'decisions': [_entry(row) for row in standing if row['kind'] == 'decision'],
            'facts': [_entry(row) for row in standing if row['kind'] == 'fact'], 'progress': latest('progress'),
            'questions': [_question(row) for row in questions if row['state'] == 'open']}


def empty(held):
    """Whether a state has no entries and no open questions."""
    return not (held['goal'] or held['decisions'] or held['facts'] or held['progress'] or held['questions'])


def day(stamp):
    return f'{int(stamp[8:10])} {MONTHS[int(stamp[5:7]) - 1]}'


def heading(held, title):
    label = LABELS[held['item']['type']]
    return f'{label} "{title}"' if title else label


def render(held, title=None, further=None):
    """The state as text, the form agents receive. A level with no entries and no questions is its title only. ``further`` is
    an optional ``{'subtasks': n, 'approvals': n, 'reports': n}``; zero counts are left out and none at all omits the line."""
    if empty(held):
        return heading(held, title)
    lines = [f'{heading(held, title)} — what holds now (revision {held["revision"]})']
    if held['goal']:
        lines.append(f'Goal: {held["goal"]["text"]}')
    for name, rows in (('Decisions', held['decisions']), ('Facts', held['facts'])):
        if rows:
            lines.append(f'{name}:')
            lines.extend(f'- [{row["id"]}] {row["text"]} ({day(row["created_at"])})' for row in rows)
    if held['progress']:
        lines.append(f'Progress: {held["progress"]["text"]} ({day(held["progress"]["created_at"])})')
    if held['questions']:
        lines.append('Open questions for the owner:')
        lines.extend(f'- [{row["id"]}{", blocks" if row["blocking"] else ""}] {row["text"]}' for row in held['questions'])
    counts = [f'{n} {noun if n == 1 else noun + "s"}' for noun, n in
              (('subtask', (further or {}).get('subtasks', 0)), ('approval', (further or {}).get('approvals', 0)),
               ('report', (further or {}).get('reports', 0))) if n]
    if counts:
        lines.append(f'Further reading: {", ".join(counts)} (timeline_get with history)')
    return '\n'.join(lines)


def standing_size(held, title=None):
    """The length of the state text without its further-reading line: what the warning threshold measures."""
    return len(render(held, title))


def closed_questions(connection, item, limit):
    """The answered and dropped questions of ``item``, latest closed first: the state lists only open ones, so this is where a
    closed question and what became of it can still be read."""
    rows = [row for row in question_rows(connection, item) if row['state'] != 'open']
    return [{'id': row['id'], 'text': row['text'], 'state': row['state'], 'blocking': bool(row['blocking']),
             'raised_by': row['raised_by'], 'answer_entry_id': row['answer_entry_id'], 'closed_at': row['closed_at']}
            for row in sorted(rows, key=lambda row: row['sequence'], reverse=True)[:limit]]


def history(connection, item, limit, after=0):
    """One page of every entry of ``item``, oldest first, removed ones marked with the entry that removed them. ``after`` is the
    last sequence already read; the page returns ``next_after`` when more follow."""
    entries = entry_rows(connection, item)
    removers = {row['removes_id']: row for row in entries if row['action'] == 'remove'}
    page = [row for row in entries if row['sequence'] > after][:limit + 1]
    more, page = len(page) > limit, page[:limit]
    items = []
    for row in page:
        value = {'id': row['id'], 'sequence': row['sequence'], 'action': row['action'], 'kind': row['kind'], 'text': row['text'],
                 'created_at': row['created_at'], 'created_by': row['created_by'],
                 'sources': json.loads(row['sources_json']), 'refs': json.loads(row['refs_json'])}
        if row['removes_id']:
            value['removes_id'] = row['removes_id']
        if row['question_id']:
            value['question_id'] = row['question_id']
        if row['action'] == 'add' and row['id'] in removers:
            value['removed_by'] = removers[row['id']]['id']
        items.append(value)
    return items, (page[-1]['sequence'] if more else None)
