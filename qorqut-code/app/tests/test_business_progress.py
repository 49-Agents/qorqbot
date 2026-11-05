from dataclasses import replace
from datetime import datetime, timezone

from bazarlyk.application import Application
from bazarlyk.application.catalog import builtin_registry
from qorqut_operations.contracts import new_id
from bazarlyk.domains.business_progress import BusinessProgress
from foundation_support import BUSINESS, HUMAN, OTHER, FixedPolicy, context, database_at, insert_action
from bazarlyk.storage import Database


def fixture(tmp_path):
    db = database_at(tmp_path)
    registry = builtin_registry()
    clock = lambda: datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
    BusinessProgress(clock=clock).register(registry)
    return db, Application(db, registry, policy=FixedPolicy())


def test_progress_is_owner_only(tmp_path):
    db, app = fixture(tmp_path)
    with db.transaction() as uow:
        member = uow.repository('business_memberships', business_id=BUSINESS).get(HUMAN)
        uow.repository('business_memberships', business_id=BUSINESS).update(HUMAN, {'is_owner': False}, expected_revision=member['revision'])
    assert app.invoke(context(), 'business_progress_get', {}).status == 'error'


def test_migration_does_not_invent_old_completion_dates(tmp_path, monkeypatch):
    migrations = Database.migrations()
    monkeypatch.setattr(Database, 'migrations', staticmethod(lambda: migrations[:49]))
    db = database_at(tmp_path)
    with db.transaction() as uow:
        uow.repository('tasks', business_id=BUSINESS).insert({'id':'old','title':'Previously done','status':'done','created_by':HUMAN})
    monkeypatch.setattr(Database, 'migrations', staticmethod(lambda: migrations))
    db.initialize()
    with db.read() as uow:
        assert uow.connection.execute('SELECT completed_at FROM task_completions WHERE task_id=?', ('old',)).fetchone()[0] is None
        assert not uow.connection.execute('PRAGMA foreign_key_check').fetchall()


def test_progress_deduplicates_task_recompletion(tmp_path):
    db, app = fixture(tmp_path)
    with db.transaction() as uow:
        tasks = uow.repository('tasks', business_id=BUSINESS)
        row = tasks.insert({'id': 'task', 'title': 'Ship', 'status': 'open', 'created_by': HUMAN})
        for status in ['done', 'open', 'done']:
            row = tasks.update('task', {'status': status}, expected_revision=row['revision'])
        tasks.insert({'id': 'dropped', 'title': 'Cancelled', 'status': 'dropped', 'created_by': HUMAN})
        uow.connection.execute('INSERT INTO task_completions VALUES(?,?,NULL)', (BUSINESS, 'legacy'))
        insert_action(uow, BUSINESS, new_id(), summary='Launched', actor_id=HUMAN, occurred_at='2026-09-13T12:00:00Z')
    result = app.invoke(context(), 'business_progress_get', {'days': 30})
    assert result.status == 'ok', result.to_dict()
    data = result.data
    assert data['totals'] == {'tasks_done': 2, 'actions': 1, 'signoffs_approved': 0}
    assert data['undated']['tasks_done'] == 1
    assert 'emails_sent' not in data['undated'] and 'emails_sent' not in data['series'][-1]
    other = replace(context(), business_id=OTHER, allowed_business_ids=frozenset({OTHER}))
    assert not any(app.invoke(other, 'business_progress_get', {}).data['totals'].values())
