"""The two stores of a timeline: ``timeline_entries`` (append-only) and ``timeline_questions`` (changeable).

An item is a Task (``item_id`` is the Task id, ``project_id`` the Project it is in or null at Global) or a Project (``item_id``
and ``project_id`` are the Project id). The stores also accept Global (``item_id`` is ``global``, ``project_id`` null) because
rows were filed on it before Global lost its timeline; the host files and reads nothing there now, and the rows stay as
they are. An entry adds or removes one thing and is never edited or deleted; the one permitted update is ``project_id``, which follows a Task that moves to another
Project. One sequence orders every entry and every question change of the store: the next value is one more than the highest
stored in either table, taken inside the writing transaction, so no separate counter row exists.
"""
from qorqut_operations.storage.assembly import StorageContribution
from qorqut_operations.storage.specs import TableSpec

ENTRIES = 'timeline_entries'
QUESTIONS = 'timeline_questions'
ITEM_TYPES = ('task', 'project', 'global')
KINDS = ('goal', 'decision', 'fact', 'progress')
ENTRY_COLUMNS = ('id', 'item_type', 'item_id', 'project_id', 'sequence', 'action', 'kind', 'text', 'removes_id', 'question_id',
                 'sources_json', 'refs_json', 'created_by', 'created_at')
_FROZEN = ' OR '.join(f'NEW.{name} IS NOT OLD.{name}' for name in ENTRY_COLUMNS if name != 'project_id')
SCHEMA = (
    '''CREATE TABLE timeline_questions (
 id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 100),
 item_type TEXT NOT NULL CHECK(item_type IN ('task','project','global')),
 item_id TEXT NOT NULL CHECK(length(item_id) BETWEEN 1 AND 300),
 project_id TEXT REFERENCES projects(id),
 text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 400),
 blocking INTEGER NOT NULL CHECK(blocking IN (0,1)),
 raised_by TEXT NOT NULL CHECK(raised_by IN ('owner','coordinator')),
 state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','answered','dropped')),
 answer_entry_id TEXT,
 sources_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(sources_json)),
 sequence INTEGER NOT NULL CHECK(sequence>=1),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL,
 closed_at TEXT
)''',
    '''CREATE TABLE timeline_entries (
 id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 100),
 item_type TEXT NOT NULL CHECK(item_type IN ('task','project','global')),
 item_id TEXT NOT NULL CHECK(length(item_id) BETWEEN 1 AND 300),
 project_id TEXT REFERENCES projects(id),
 sequence INTEGER NOT NULL UNIQUE CHECK(sequence>=1),
 action TEXT NOT NULL CHECK(action IN ('add','remove')),
 kind TEXT NOT NULL CHECK(kind IN ('goal','decision','fact','progress')),
 text TEXT NOT NULL CHECK(length(text)<=400 AND (action='remove' OR length(text)>=1)),
 removes_id TEXT REFERENCES timeline_entries(id),
 question_id TEXT,
 sources_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(sources_json)),
 refs_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(refs_json)),
 created_by TEXT NOT NULL REFERENCES principals(id),
 created_at TEXT NOT NULL,
 CHECK((action='remove')=(removes_id IS NOT NULL))
)''',
    'CREATE INDEX timeline_entries_item ON timeline_entries(item_type,item_id,project_id)',
    'CREATE INDEX timeline_questions_item ON timeline_questions(item_type,item_id,project_id,state)',
    'CREATE INDEX timeline_questions_sequence ON timeline_questions(sequence)',
    f'''CREATE TRIGGER timeline_entries_frozen BEFORE UPDATE ON timeline_entries
 WHEN {_FROZEN}
 BEGIN SELECT RAISE(ABORT,'A timeline entry is never edited; only its Project follows a moved Task'); END''',
    '''CREATE TRIGGER timeline_entries_retained BEFORE DELETE ON timeline_entries
 BEGIN SELECT RAISE(ABORT,'A timeline entry is never deleted'); END''',
)


def storage_contribution():
    return StorageContribution('timeline', {ENTRIES: TableSpec(('id',), mutable=False, revision_column=None),
                                            QUESTIONS: TableSpec(('id',))}, schema=SCHEMA)
