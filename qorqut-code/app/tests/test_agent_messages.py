from datetime import timedelta
import json
import io
import sqlite3

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import DomainError, new_id
from inbox_support import pending_count
from test_agent_inbox import inbox
from test_service_transports import ORIGIN


def call(s, agent, operation, args, key=None, business=None):
    ctx = s.runtime.resolver.invocation_context(agent)
    if business: ctx = ctx.select_business(business)
    return s.runtime.application.invoke(ctx, operation, args, {'idempotency_key': key or new_id()})


def sent(result):
    assert result.status == 'ok', result.to_dict()
    return result.data['message']


def test_direct_delivery_reply_history_and_exact_ack(inbox):
    s = inbox
    key = new_id()
    args = {'to_agent_id': s.agent.id, 'message': 'Please review the supplier update.'}
    business = s.fixture.business_id
    first = sent(call(s, s.manager, 'agents_message_send', args, key, business))
    assert sent(call(s, s.manager, 'agents_message_send', args, key, business))['id'] == first['id']
    assert first['sender_id'] == s.manager.id and first['recipient_id'] == s.agent.id
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1
    batch = s.call('inbox_list', {'consumer_id': s.consumer}).data['items']
    assert len(batch) == 1 and batch[0]['message']['body'] == args['message']
    s.now += timedelta(minutes=10)
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1
    reply = sent(s.call('agents_message_send', {'reply_to': first['id'], 'message': 'Reviewed; one concern.'}))
    assert reply['thread_id'] == first['id'] and reply['recipient_id'] == s.manager.id
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 1  # reply is not acknowledgement
    assert s.call('inbox_acknowledge', {'consumer_id': s.consumer, 'event_ids': [first['event_id']]}).status == 'ok'
    history = s.call('agents_messages_list', {'thread_id': first['id']}).data['items']
    assert len(history) == 2 and history[0]['acknowledged_at'] is not None
    assert pending_count(s.runtime.inbox, s.runtime.resolver.invocation_context(s.manager, business_id=business), new_id()) == 1
    snapshot = s.runtime.inbox.owner_snapshot(s.fixture.ctx, s.manager.id)
    assert snapshot['items'][0]['message']['sender_name'] == s.agent.display_name
    with s.fixture.db.transaction() as u:
        with pytest.raises(sqlite3.IntegrityError):
            u.connection.execute('UPDATE agent_messages SET body=? WHERE id=?', ('changed', first['id']))


def test_private_threads_scope_validation_and_overrides(inbox):
    s = inbox
    business = s.fixture.business_id
    other = s.fixture.create('marketing', 'global-marketing')
    first = sent(call(s, s.manager, 'agents_message_send', {'to_agent_id': s.agent.id, 'message': 'Private'}, business=business))
    assert not call(s, other, 'agents_messages_list', {'thread_id': first['id']}, business=business).data['items']
    for args in [
        {'to_agent_id': s.agent.id, 'message': '   '},
        {'to_agent_id': s.agent.id, 'message': 'x' * 16001},
        {'to_agent_id': s.manager.id, 'message': 'spoof', 'sender_id': other.id},
        {'reply_to': first['id'], 'to_agent_id': other.id, 'message': 'redirect'},
        {'to_agent_id': new_id(), 'message': 'unknown'},
    ]:
        assert s.call('agents_message_send', args).status == 'error'
    assert call(s, other, 'agents_message_send', {'reply_to': first['id'], 'message': 'intrude'}, business=business).status == 'error'
    s.fixture.update(s.manager, {'tool_policy': {'overrides': {'agents_message_send': 'deny'}}})
    assert call(s, s.manager, 'agents_message_send', {'to_agent_id': s.agent.id, 'message': 'denied'}, business=business).status == 'error'
    s.fixture.update(other, {'status': 'inactive'})
    recipients = s.call('agents_message_recipients', {}).data['items']
    assert other.id not in {p['agent_id'] for p in recipients}
    assert s.call('agents_message_send', {'to_agent_id': other.id, 'message': 'inactive'}).status == 'error'


def test_ordubashy_business_global_and_scope_cannot_be_narrowed(inbox):
    s = inbox
    coordinator = s.fixture.create('coordinator', 'ordubashy')
    second = s.fixture.create('second-coordinator', 'ordubashy')
    business = s.fixture.business_id
    first = sent(call(s, coordinator, 'agents_message_send', {'to_agent_id': s.agent.id, 'message': 'Business request'}, business=business))
    assert first['business_id'] == business
    assert call(s, coordinator, 'agents_message_send', {'scope': 'global', 'to_agent_id': s.agent.id, 'message': 'Wrong scope'}).status == 'error'
    global_message = sent(call(s, coordinator, 'agents_message_send', {'scope': 'global', 'to_agent_id': second.id, 'message': 'Global request'}))
    assert global_message['business_id'] is None
    reply = sent(call(s, s.agent, 'agents_message_send', {'reply_to': first['id'], 'message': 'Report'}))
    profile, _ = s.fixture.resolver.profile(coordinator)
    scope = profile.data['scope'].copy(); scope['allowed_business_ids'] = []
    with pytest.raises(DomainError, match='every registered business'):
        s.fixture.update(coordinator, {'scope': scope})
    snapshot = s.runtime.inbox.owner_snapshot(s.fixture.ctx, coordinator.id)
    assert reply['event_id'] in [row['id'] for row in snapshot['items']]
    assert sent(call(s, s.agent, 'agents_message_send', {'to_agent_id': coordinator.id, 'message': 'Still shared'}))


def test_keyless_owner_api_and_mcp_reply(inbox, monkeypatch, capsys):
    s = inbox
    with TestClient(s.web) as client:
        route = '/api/biz/widget/operations/agents_message_send'
        args = {'to_agent_id': s.agent.id, 'message': 'Local coding agent request', 'idempotency_key': new_id()}
        assert client.post(route, json=args).status_code == 403
        assert client.post(route, json=args, headers={'Origin': 'http://evil.invalid'}).status_code == 403
        response = client.post(route, json=args, headers=ORIGIN)
        assert response.status_code == 200, response.text
        first = response.json()['data']['message']
        assert first['sender_id'] == s.fixture.owner.id
        headers = s.initialize(client, s.agent)
        result = s.tool(client, headers, 'agents_message_send', {'reply_to': first['id'], 'message': 'Claude reply', 'idempotency_key': new_id()})
        assert not result.get('isError'), result
        response = client.post('/api/biz/widget/operations/agents_messages_list', json={'thread_id': first['thread_id']}, headers=ORIGIN)
        rows = response.json()['data']['items']
        assert len(rows) == 2 and rows[1]['recipient_id'] == s.fixture.owner.id
        assert rows[1]['body'] == 'Claude reply'
        from bazarlyk.inbox import client as local
        def transport(request, timeout):
            response = client.post(request.full_url, content=request.data, headers=dict(request.header_items()))
            assert response.status_code == 200, response.text
            return io.BytesIO(response.content)
        monkeypatch.setattr(local, 'urlopen', transport)
        monkeypatch.setattr('sys.stdin', io.StringIO('Local helper message'))
        assert local.main(['--url', 'http://testserver', '--business', 'widget', 'send', '--to', s.agent.id,
            '--message-file', '-', '--idempotency-key', new_id()]) == 0
        assert json.loads(capsys.readouterr().out)['data']['message']['sender_id'] == s.fixture.owner.id
        assert local.main(['--url', 'http://testserver', '--business', 'widget', 'history', '--thread-id', first['thread_id']]) == 0
        assert len(json.loads(capsys.readouterr().out)['data']['items']) == 2
        assert local.main(['--url', 'http://testserver', '--business', 'widget', 'history', '--counterpart-id', s.agent.id]) == 0
        items = json.loads(capsys.readouterr().out)['data']['items']
        assert len(items) == 3 and {item['counterpart_id'] for item in items} == {s.agent.id}
        assert client.post('/api/root/operations/agents_message_recipients', json={}, headers=ORIGIN).status_code == 200
        assert client.post('/api/root/operations/agents_message_send', json={**args, 'scope': 'business'}, headers=ORIGIN).status_code == 403


def test_message_transaction_rollback_and_history_pagination(inbox, monkeypatch):
    s = inbox
    original = s.runtime.inbox.messages.view
    def fail(*args): raise RuntimeError('serialization failed')
    monkeypatch.setattr(s.runtime.inbox.messages, 'view', fail)
    with pytest.raises(RuntimeError, match='serialization failed'):
        call(s, s.manager, 'agents_message_send', {'to_agent_id': s.agent.id, 'message': 'rolled back'}, business=s.fixture.business_id)
    monkeypatch.setattr(s.runtime.inbox.messages, 'view', original)
    with s.fixture.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM agent_messages').fetchone()[0] == 0
        assert u.connection.execute("SELECT count(*) FROM inbox_events WHERE source_type='agent_messages'").fetchone()[0] == 0
    for n in range(3):
        sent(call(s, s.manager, 'agents_message_send', {'to_agent_id': s.agent.id, 'message': str(n)}, business=s.fixture.business_id))
    page = s.call('agents_messages_list', {'limit': 2}).data
    assert len(page['items']) == 2 and page['next_cursor']
    last = s.call('agents_messages_list', {'after': page['next_cursor'], 'limit': 2}).data
    assert len(last['items']) == 1 and last['next_cursor'] is None


def test_business_agent_directory_is_in_ordubashy_bootstrap_and_scoped(inbox):
    s = inbox
    coordinator = s.fixture.create('directory-coordinator', 'ordubashy')
    result = call(s, coordinator, 'agents_message_recipients', {'scope': 'global', 'all_businesses': True, 'role_id': 'business-agent'})
    assert result.status == 'ok', result.to_dict()
    rows = result.data['items']
    assert len(rows) == 1
    assert rows[0]['agent_id'] == s.agent.id and rows[0]['business_id'] == s.fixture.business_id
    assert rows[0]['business_slug'] == 'widget' and rows[0]['role_id'] == 'business-agent'
    config = call(s, coordinator, 'agent_config_get', {})
    assert config.status == 'ok', config.to_dict()
    assert 'Business agent directory' in config.data['work_briefing']['rendered']
    assert s.agent.id in config.data['work_briefing']['rendered']
    profile, _ = s.fixture.resolver.profile(coordinator)
    scope = dict(profile.data['scope']); scope['allowed_business_ids'] = []
    with pytest.raises(DomainError, match='every registered business'):
        s.fixture.update(coordinator, {'scope': scope})
    assert call(s, coordinator, 'agents_message_recipients', {
        'scope': 'global', 'all_businesses': True, 'role_id': 'business-agent'}).data['items']
