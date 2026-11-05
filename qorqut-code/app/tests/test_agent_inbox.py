from datetime import datetime, timedelta, timezone

import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.inbox.service import NEVER
from inbox_support import pending_count
from test_service_transports import ServiceFixture


@pytest.fixture
def inbox(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    s.consumer = new_id()
    from dataclasses import replace
    s.ctx = lambda: replace(s.runtime.resolver.invocation_context(s.agent),inbox_session_id=s.consumer,inbox_generation=0)
    s.call = lambda op, args: s.runtime.application.invoke(s.ctx(), op, args, {'idempotency_key': new_id()})
    s.now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    s.runtime.inbox.clock = lambda: s.now
    # Pre-migration generation-0 owner fixture. New owners must use verified binding.
    with s.fixture.db.transaction() as uow:
        uow.connection.execute('INSERT INTO inbox_watchers(agent_id,consumer_id,last_check_at,lease_until) VALUES(?,?,?,?)',
            (s.agent.id,s.consumer,NEVER,NEVER))
    return s


def agent_note(s, title, body):
    """Owners create only Global notes now; the agent's own note is a readable document source."""
    created = s.call('notes_create', {'title': title, 'body': body})
    assert created.status == 'ok', created.to_dict()
    return created.data['document']


def story(s):
    result = s.runtime.application.invoke(s.fixture.ctx, 'stories_create',
        {'title': 'Supplier choice', 'purpose': 'Choose a supplier'}, {'idempotency_key': new_id()})
    assert result.status == 'ok', result.to_dict()
    with s.fixture.db.read() as u:
        return dict(u.connection.execute('SELECT * FROM stories WHERE title=?', ('Supplier choice',)).fetchone())


def subscribe(s, row, related=False):
    result = s.call('inbox_subscribe', {'source_type': 'stories', 'source_id': row['id'], 'include_related': related})
    assert result.status == 'ok', result.to_dict()
    return result.data['subscription_id']


def edit(s, row, text='New situation', actor=None):
    with s.fixture.db.transaction() as u:
        u.actor_id = actor or s.fixture.owner.id
        u.connection.execute('UPDATE stories SET situation=?,revision=revision+1 WHERE business_id=? AND id=?',
            (text, row['business_id'], row['id']))


def test_subscribed_edit_is_atomic_and_read_does_not_acknowledge(inbox):
    s = inbox; row = story(s)
    assert subscribe(s, row) == subscribe(s, row)
    edit(s, row)
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1
    result = s.call('inbox_list', {'consumer_id': s.consumer})
    assert result.status == 'ok', result.to_dict()
    events = result.data['items']
    assert len(events) == 1 and events[0]['source_id'] == row['id']
    s.now += timedelta(minutes=10)
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1
    response = s.call('inbox_acknowledge', {'consumer_id': s.consumer, 'event_ids': [events[0]['id']], 'outcome': 'Reviewed; no action needed'})
    assert response.status == 'ok', response.to_dict()
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 0
    with pytest.raises(RuntimeError):
        with s.fixture.db.transaction() as u:
            u.connection.execute('UPDATE stories SET situation=? WHERE business_id=? AND id=?', ('rolled back', row['business_id'], row['id']))
            raise RuntimeError('rollback')
    with s.fixture.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM inbox_changes').fetchone()[0] == 0
        assert u.connection.execute('SELECT count(*) FROM inbox_events').fetchone()[0] == 1


def test_self_changes_and_noop_writes_are_silent(inbox):
    s=inbox; row=story(s); subscribe(s,row)
    edit(s,row,actor=s.agent.id)
    edit(s,row)  # identical values; revision bookkeeping is not an event
    assert pending_count(s.runtime.inbox, s.ctx(),s.consumer)==0




def test_http_owner_snapshot_lists_pending_events_and_subscriptions(inbox):
    from starlette.testclient import TestClient
    s = inbox
    row = story(s); subscribe(s, row); edit(s, row)
    with TestClient(s.web) as client:
        result = client.get('/api/me/agents/' + s.agent.id + '/inbox')
        assert result.status_code == 200, result.text
        data = result.json()['data']
        assert len(data['items']) == len(data['subscriptions']) == 1
        assert 'watcher' not in data and 'watcher_active' not in data
        assert set(data['delivery']) == {'state', 'reason', 'last_attempt_at'} and 'lease_until' not in data['delivery']
        assert client.get('/api/me/agents/' + s.agent.id + '/inbox?after=-1').status_code == 422


def test_pagination_and_exact_acknowledgement_preserve_new_arrivals(inbox):
    s = inbox; row = story(s); subscribe(s, row)
    for index in range(23):
        edit(s, row, str(index))
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 23
    first = s.call('inbox_list', {'consumer_id': s.consumer}).data
    assert len(first['items']) == 20
    last = s.call('inbox_list', {'consumer_id': s.consumer, 'after': first['next_cursor']}).data
    assert len(last['items']) == 3 and last['next_cursor'] is None
    edit(s, row, 'arrived during handling')
    ids = [item['id'] for item in first['items']]
    args = {'consumer_id': s.consumer, 'event_ids': ids, 'outcome': 'Reviewed'}
    assert s.call('inbox_acknowledge', args).status == 'ok'
    assert s.call('inbox_acknowledge', {**args, 'outcome': 'Retry'}).status == 'ok'
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 4
    with s.fixture.db.read() as u:
        outcomes = u.connection.execute('SELECT DISTINCT outcome FROM inbox_deliveries WHERE acknowledged_at IS NOT NULL').fetchall()
        assert [r[0] for r in outcomes] == ['Reviewed']


def test_real_story_notes_and_later_linked_task_changes(inbox):
    s = inbox; row = story(s); subscribe(s, row, related=True)
    # The installation owner's ordinary notes belong to the private channel, so the
    # shared contributions come from Ordubashy working in this business.
    manager = s.runtime.resolver.invocation_context(s.manager, business_id=s.fixture.business_id)
    def manager_call(op, args):
        result = s.runtime.application.invoke(manager, op, args, {'idempotency_key': new_id()})
        assert result.status == 'ok', result.to_dict()
        return result.data
    manager_call('stories_note_add', {'story_id': row['id'], 'body': 'First contribution'})
    manager_call('stories_note_add', {'story_id': row['id'], 'body': 'Second contribution'})
    task = manager_call('tasks_create', {'title': 'Ask supplier'})['task']
    manager_call('stories_link_add', {'story_id': row['id'], 'resource_type': 'tasks', 'resource_id': task['id']})
    with s.fixture.db.transaction() as u:
        u.actor_id = s.fixture.owner.id
        u.connection.execute('UPDATE tasks SET title=?,revision=revision+1 WHERE business_id=? AND id=?',
                             ('Ask supplier about lead time', row['business_id'], task['id']))
    events = s.call('inbox_list', {'consumer_id': s.consumer}).data['items']
    assert len([e for e in events if e['source_type'] == 'stories']) == 3
    assert any(e['source_id'] == task['id'] and e['summary'] == 'Ask supplier about lead time' for e in events)


def test_owner_response_routes_once_even_when_proposer_also_subscribes(inbox):
    from test_signoff_inputs import FORM, binding
    s = inbox
    result = s.call('signoffs_input_create', {'request_key': 'inbox-question', 'form': FORM})
    assert result.status == 'ok', result.to_dict()
    signoff = result.data['signoff']
    result = s.call('inbox_subscribe', {'source_type': 'signoffs', 'source_id': signoff['id']})
    assert result.status == 'ok', result.to_dict()
    response = s.runtime.signoffs.respond(s.fixture.ctx, signoff['id'], notes='Use first', selected_choices=['a'], **binding(signoff))
    assert response.status == 'ok', response.to_dict()
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1
    event = s.call('inbox_list', {'consumer_id': s.consumer}).data['items'][0]
    assert event['source_id'] == signoff['id'] and event['event_type'] == 'response_submitted'
    assert event['actor_id'] == s.fixture.owner.id
    response = s.runtime.signoffs.respond(s.fixture.ctx, signoff['id'], notes='Use first', selected_choices=['a'], **binding(signoff))
    assert response.status == 'ok'
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1


def test_event_finalization_failure_rolls_back_source_change(inbox, monkeypatch):
    s = inbox; row = story(s); subscribe(s, row)
    def fail(_):
        raise RuntimeError('inbox persistence failure')
    monkeypatch.setattr('bazarlyk.inbox.events.finalize_events', fail)
    with pytest.raises(RuntimeError, match='persistence failure'):
        edit(s, row, 'must roll back')
    with s.fixture.db.read() as u:
        saved = u.repository('stories', business_id=row['business_id']).get(row['id'])
        assert saved['situation'] == row['situation']
        assert u.connection.execute('SELECT count(*) FROM inbox_changes').fetchone()[0] == 0


def test_document_notification_follows_durable_file_completion(inbox):
    s = inbox
    app = s.runtime.application
    document = agent_note(s, 'Supplier notes', 'Before')
    sub = s.call('inbox_subscribe', {'source_type': 'documents', 'source_id': document['id']})
    assert sub.status == 'ok', sub.to_dict()
    with s.fixture.db.transaction() as u:
        u.connection.execute("UPDATE documents SET availability='applying' WHERE id=?", (document['id'],))
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 0
    # An unchanged failed file application releases its claim without an edit.
    with s.fixture.db.transaction() as u:
        u.connection.execute("UPDATE documents SET availability='ready' WHERE id=?", (document['id'],))
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 0
    key = new_id()
    args = {'document_id': document['id'], 'body': 'After'}
    options = {'idempotency_key': key, 'expected_revision': document['revision']}
    updated = app.invoke(s.fixture.ctx, 'notes_update', args, options)
    assert updated.status == 'ok', updated.to_dict()
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1
    assert app.invoke(s.fixture.ctx, 'notes_update', args, options).status == 'ok'
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1


def test_task_responsibility_routes_assignment_comment_and_completion(inbox):
    s = inbox
    def owner(op, args, revision=None):
        result = s.runtime.application.invoke(s.fixture.ctx, op, args,
            {'idempotency_key': new_id(), 'expected_revision': revision})
        assert result.status == 'ok', result.to_dict()
        return result.data['task']
    manager_context = s.runtime.resolver.invocation_context(s.manager, business_id=s.fixture.business_id)
    def manager(op, args, revision=None):
        result = s.runtime.application.invoke(manager_context, op, args,
            {'idempotency_key': new_id(), 'expected_revision': revision})
        assert result.status == 'ok', result.to_dict()
        return result.data['task']
    task = owner('tasks_create', {'title': 'Inbox assigned task'})
    task = owner('tasks_assign', {'task_id': task['id'], 'assignee_ids': [s.agent.id]}, task['revision'])
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1
    # A shared task comment comes from Ordubashy; the owner's own comments are private.
    refused = s.runtime.application.invoke(s.fixture.ctx, 'tasks_comment',
        {'task_id': task['id'], 'text': 'Please check this'},
        {'idempotency_key': new_id(), 'expected_revision': task['revision']})
    assert refused.error['code'] == 'channel_required', refused.to_dict()
    task = manager('tasks_comment', {'task_id': task['id'], 'text': 'Please check this'}, task['revision'])
    task = owner('tasks_complete', {'task_id': task['id']}, task['revision'])
    events = s.call('inbox_list', {'consumer_id': s.consumer}).data['items']
    assert len(events) == 3
    assert all(e['source_type'] == 'tasks' and e['source_id'] == task['id'] for e in events)
    assert any('assignment_changed' in e['event_type'] for e in events)
    assert any(e['event_type'] == 'note_added' for e in events)


@pytest.mark.parametrize('failure', [False, True])
def test_approved_action_outcome_reaches_proposer_under_its_execution_identity(inbox, monkeypatch, failure):
    from test_signoff_inputs import binding
    s = inbox
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'tasks_create': 'signoff'}}})
    proposal = s.call('tasks_create', {'title': 'Approved inbox test'})
    assert proposal.status == 'signoff_required', proposal.to_dict()
    signoff = s.runtime.signoffs.get(s.fixture.ctx, proposal.signoff['id']).data['signoff']
    approved = s.runtime.signoffs.review(s.fixture.ctx, signoff['id'], decision='approve', **binding(signoff))
    assert approved.status == 'accepted', approved.to_dict()
    if failure:
        def fail(*args, **kwargs):
            raise DomainError('operation_failed', 'Injected isolated failure')
        monkeypatch.setattr(s.runtime.application, 'apply_prepared', fail)
    s.runtime.signoffs.run_pending()
    events = s.call('inbox_list', {'consumer_id': s.consumer}).data['items']
    assert [e['event_type'] for e in events] == ['response_submitted', 'action_outcome']
    assert events[-1]['actor_id'] is None
    assert s.runtime.signoffs.get(s.fixture.ctx, signoff['id']).data['signoff']['state'] == ('failed' if failure else 'applied')


def test_global_document_subscription_owner_read_and_revocation(inbox):
    s = inbox
    agent = s.fixture.create('inbox-coordinator', 'ordubashy')
    context = s.runtime.resolver.invocation_context(agent).select_business(None)
    document = s.fixture.document('inbox-global', 'Global inbox evidence', 'context/inbox-global.md')
    with pytest.raises(DomainError, match='unavailable'):
        s.runtime.inbox.owner_document(s.fixture.ctx, agent.id, document['id'])
    result = s.runtime.application.invoke(context, 'inbox_subscribe',
        {'scope': 'global', 'source_type': 'documents', 'source_id': document['id']}, {'idempotency_key': new_id()})
    assert result.status == 'ok', result.to_dict()
    source = s.runtime.inbox.owner_document(s.fixture.ctx, agent.id, document['id'])
    assert source['document']['body'] == 'Global inbox evidence'
    from starlette.testclient import TestClient
    with TestClient(s.web) as client:
        response = client.get('/api/me/agents/' + agent.id + '/inbox/documents/' + document['id'])
        assert response.status_code == 200, response.text
        assert response.json()['data']['document']['body'] == 'Global inbox evidence'
    with s.fixture.db.transaction() as u:
        u.actor_id = s.fixture.owner.id
        u.connection.execute("UPDATE documents SET metadata_json=json_set(metadata_json,'$.title','Updated Global source'),revision=revision+1 WHERE id=?", (document['id'],))
    consumer = new_id()
    ready=s.fixture.prepare(agent)
    from test_inbox_native_binding import binding
    from dataclasses import replace
    native=binding(s,consumer)
    native.update(agent_id=agent.id,registration_id=ready['registration_id'],workspace=ready['workspace'])
    s.runtime.inbox.start_session(context,ready['registration_id'],consumer,'startup',binding=native)
    context=replace(context,inbox_session_id=consumer,inbox_generation=1)
    assert pending_count(s.runtime.inbox, context, consumer) == 1
    assert s.runtime.inbox.owner_snapshot(s.fixture.ctx, agent.id)['items'][0]['business_id'] is None
    s.fixture.update(agent, {'tool_policy': {'overrides': {'context_get': 'deny'}}})
    with pytest.raises(DomainError, match='unavailable'):
        s.runtime.inbox.owner_document(s.fixture.ctx, agent.id, document['id'])
    assert pending_count(s.runtime.inbox, context, consumer) == 0


def test_mcp_subscribe_owner_http_change_and_explicit_mcp_acknowledgement(inbox):
    from starlette.testclient import TestClient
    from test_service_transports import ORIGIN
    s = inbox; row = story(s)
    with TestClient(s.web) as client:
        from test_inbox_native_binding import select
        select(s,s.consumer)
        mcp = s.initialize(client, s.agent)
        mcp.update({'X-Qorqut-Native-Session':s.consumer,'X-Qorqut-Binding-Generation':'1'})
        def tool(name, args):
            result = s.tool(client, mcp, name, args)['decoded']
            assert result['status'] == 'ok', result
            return result['data']
        tool('inbox_subscribe', {'source_type': 'stories', 'source_id': row['id'], 'idempotency_key': new_id()})
        changed = client.post('/api/biz/widget/operations/stories_update',
            json={'story_id': row['id'], 'changes': {'situation': 'Owner HTTP update'},
                  'expected_revision': row['revision'], 'idempotency_key': new_id()}, headers=ORIGIN)
        assert changed.status_code == 200, changed.text
        assert pending_count(s.runtime.inbox, s.ctx()) == 1
        event = tool('inbox_list', {'consumer_id': s.consumer})['items'][0]
        assert tool('inbox_list', {'consumer_id': s.consumer})['items'][0]['id'] == event['id']
        tool('inbox_acknowledge', {'consumer_id': s.consumer, 'event_ids': [event['id']], 'outcome': 'Read source', 'idempotency_key': new_id()})
        assert pending_count(s.runtime.inbox, s.ctx()) == 0


def test_deleted_document_notice_can_be_acknowledged_without_reading_deleted_content(inbox):
    s = inbox
    document = agent_note(s, 'Disposable note', 'Not retained in inbox')
    assert s.call('inbox_subscribe', {'source_type': 'documents', 'source_id': document['id']}).status == 'ok'
    deleted = s.runtime.application.invoke(s.fixture.ctx, 'notes_delete', {'document_id': document['id']},
        {'idempotency_key': new_id(), 'expected_revision': document['revision']})
    assert deleted.status == 'ok', deleted.to_dict()
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1
    archived = s.call('inbox_list', {'consumer_id': s.consumer}).data['items'][0]
    assert archived['event_type'] == 'updated'  # Notes use retained archives.
    assert s.call('inbox_acknowledge', {'consumer_id': s.consumer, 'event_ids': [archived['id']]}).status == 'ok'
    with s.fixture.db.transaction() as u:
        u.actor_id = s.fixture.owner.id
        u.connection.execute('DELETE FROM documents WHERE id=?', (document['id'],))
    result = s.call('inbox_list', {'consumer_id': s.consumer})
    event = result.data['items'][0]
    assert event['event_type'] == 'deleted' and 'Not retained' not in str(result.to_dict())
    assert s.call('inbox_acknowledge', {'consumer_id': s.consumer, 'event_ids': [event['id']]}).status == 'ok'


def test_unsubscribe_retains_deliveries_and_other_agent_cannot_consume_them(inbox):
    s = inbox; row = story(s); subscription = subscribe(s, row); edit(s, row)
    event = s.call('inbox_list', {'consumer_id': s.consumer}).data['items'][0]
    other = s.runtime.resolver.invocation_context(s.manager); consumer = new_id()
    assert pending_count(s.runtime.inbox, other, consumer) == 0
    for operation, args in [
        ('inbox_acknowledge', {'consumer_id': consumer, 'event_ids': [event['id']]}),
        ('inbox_unsubscribe', {'subscription_id': subscription}),
    ]:
        result = s.runtime.application.invoke(other, operation, args, {'idempotency_key': new_id()})
        assert result.status == 'error' and result.error['code'] == 'not_found'
    assert s.call('inbox_unsubscribe', {'subscription_id': subscription}).status == 'ok'
    edit(s, row, 'After unsubscribe')
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1
    assert not s.call('inbox_subscriptions_list', {}).data['items']


def test_database_backup_restores_pending_queue_and_subscription(inbox, tmp_path):
    from bazarlyk.storage import Database
    from bazarlyk.service.runtime import build_runtime
    from test_agent_configuration import REPO, PATHS
    s = inbox; row = story(s); subscribe(s, row); edit(s, row)
    snapshot = tmp_path / 'inbox-backup.sqlite3'
    backup = s.fixture.db.backup(snapshot)
    database = Database.restore(snapshot, tmp_path / 'inbox-restored.sqlite3', expected_checksum=backup['checksum'])
    restored = build_runtime(s.fixture.root, REPO, paths=PATHS, database=database)
    restored.inbox.clock = lambda: s.now + timedelta(minutes=16)
    ctx = restored.resolver.invocation_context(s.agent)
    consumer = new_id()
    assert pending_count(restored.inbox, ctx, consumer) == 1
    result = restored.application.invoke(ctx, 'inbox_list', {'consumer_id': consumer})
    assert result.status == 'ok', result.to_dict()
    assert result.data['items'][0]['source_id'] == row['id']
    assert len(restored.application.invoke(ctx, 'inbox_subscriptions_list', {}).data['items']) == 1


def test_document_crash_recovery_delivers_once_after_durable_completion(inbox, monkeypatch):
    s = inbox; app = s.runtime.application
    document = agent_note(s, 'Recoverable note', 'Before')
    assert s.call('inbox_subscribe', {'source_type': 'documents', 'source_id': document['id']}).status == 'ok'
    class Crash(BaseException):
        pass
    def crash(_):
        raise Crash()
    monkeypatch.setattr(app.file_executor.file_applier, 'after_replace', crash)
    args = {'document_id': document['id'], 'body': 'Durably recovered'}
    options = {'idempotency_key': new_id(), 'expected_revision': document['revision']}
    with pytest.raises(Crash):
        app.invoke(s.fixture.ctx, 'notes_update', args, options)
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 0
    monkeypatch.setattr(app.file_executor.file_applier, 'after_replace', None)
    recovered = app.file_executor.recover_pending(app)
    assert len(recovered) == 1 and recovered[0].status == 'ok', [r.to_dict() for r in recovered]
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1
    assert app.invoke(s.fixture.ctx, 'notes_update', args, options).status == 'ok'
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1
