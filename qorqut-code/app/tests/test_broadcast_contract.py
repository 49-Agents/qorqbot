"""Exercise Broadcast through the same HTTP/MCP and inbox contracts agents use."""
from datetime import datetime, timezone

from starlette.testclient import TestClient

from qorqut_operations.contracts import new_id
from bazarlyk.inbox.broadcasts import BroadcastDispatcher
from inbox_support import pending_count
from test_service_transports import ORIGIN, ServiceFixture


NOW = datetime(2030, 1, 2, 12, tzinfo=timezone.utc)


def definition(recipient, **changes):
    return {"name": "Review progress", "delivery": "inbox", "message": "Review progress now.",
            "recipients": [recipient], "timing": {"kind": "once", "at": NOW.isoformat()}, **changes}


def setup_service(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    service.runtime.broadcasts.clock = lambda: NOW
    service.dispatcher = BroadcastDispatcher(service.runtime, service.runtime.broadcasts, clock=lambda: NOW)
    return service


def owner_call(client, operation, arguments):
    response = client.post('/api/biz/widget/operations/' + operation,
                           headers=ORIGIN, json={**arguments, "idempotency_key": new_id()})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['status'] == 'ok', value
    return value['data']


def test_owner_scheduled_message_is_consumable_and_replyable(tmp_path):
    s = setup_service(tmp_path)
    client = TestClient(s.web)
    owner_call(client, 'broadcasts_create', {'definition': definition(s.agent.id)})
    s.dispatcher.tick()
    ctx = s.runtime.resolver.invocation_context(s.agent)
    consumer = new_id()
    assert pending_count(s.runtime.inbox, ctx, consumer) == 1
    pending = s.runtime.application.invoke(ctx, 'inbox_list', {'consumer_id': consumer})
    assert pending.status == 'ok', pending.to_dict()
    event = pending.data['items'][0]
    assert event['source_type'] == 'agent_messages'
    history = s.runtime.application.invoke(ctx, 'agents_messages_list', {})
    assert history.status == 'ok', history.to_dict()
    message = history.data['items'][0]
    assert message['sender_id'] == s.fixture.owner.id
    assert message['body'] == 'Review progress now.'
    reply = s.runtime.application.invoke(ctx, 'agents_message_send',
        {'reply_to': message['id'], 'message': 'Review complete.'}, {'idempotency_key': new_id()})
    assert reply.status == 'ok', reply.to_dict()
    assert reply.data['message']['thread_id'] == message['thread_id']
    assert reply.data['message']['recipient_id'] == s.fixture.owner.id


def test_agent_can_discover_self_and_create_self_reminder_over_mcp(tmp_path, monkeypatch):
    s = setup_service(tmp_path)
    monkeypatch.setattr(s.runtime, 'start', lambda: None)
    monkeypatch.setattr(s.runtime, 'stop', lambda: None)
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.agent)
        recipients = s.tool(client, headers, 'broadcast_recipients', {})['decoded']
        assert recipients['status'] == 'ok', recipients
        assert s.agent.id in {row['agent_id'] for row in recipients['data']['items']}
        result = s.tool(client, headers, 'broadcasts_create',
                        {'definition': definition(s.agent.id), 'idempotency_key': new_id()})['decoded']
        assert result['status'] == 'ok', result
    s.dispatcher.tick()
    ctx = s.runtime.resolver.invocation_context(s.agent)
    assert pending_count(s.runtime.inbox, ctx, new_id()) == 1


def test_marketing_cannot_select_input_or_bypass_owner_override(tmp_path):
    s = setup_service(tmp_path)
    ctx = s.runtime.resolver.invocation_context(s.agent)
    denied = s.runtime.application.invoke(ctx, 'broadcasts_create',
        {'definition': definition(s.manager.id, delivery='input')}, {'idempotency_key': new_id()})
    assert denied.status == 'error', denied.to_dict()
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'broadcasts_create': 'deny'}}})
    ctx = s.runtime.resolver.invocation_context(s.agent)
    denied = s.runtime.application.invoke(ctx, 'broadcasts_create',
        {'definition': definition(s.agent.id)}, {'idempotency_key': new_id()})
    assert denied.status == 'error', denied.to_dict()
    with s.fixture.db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM broadcasts').fetchone()[0] == 0


def test_http_create_retry_preserves_one_broadcast(tmp_path):
    s = setup_service(tmp_path)
    client = TestClient(s.web)
    payload = {'definition': definition(s.agent.id), 'idempotency_key': new_id()}
    first = client.post('/api/biz/widget/operations/broadcasts_create', headers=ORIGIN, json=payload)
    second = client.post('/api/biz/widget/operations/broadcasts_create', headers=ORIGIN, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()['status'] == second.json()['status'] == 'ok'
    with s.fixture.db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM broadcasts').fetchone()[0] == 1
    s.dispatcher.tick()
    s.dispatcher.tick()
    with s.fixture.db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM agent_messages').fetchone()[0] == 1


def test_global_http_broadcast_discovery_and_management(tmp_path):
    s = setup_service(tmp_path)
    client = TestClient(s.web)

    def call(name, **args):
        response = client.post('/api/root/operations/' + name, headers=ORIGIN,
            json={'scope': 'global', 'idempotency_key': new_id(), **args})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['status'] == 'ok', result
        return result['data']

    recipients = call('broadcast_recipients', all_businesses=True)['items']
    assert s.agent.id in {r['agent_id'] for r in recipients}
    row = call('broadcasts_create', definition=definition(s.agent.id))['records'][0]
    assert call('broadcasts_list')['items'][0]['id'] == row['id']
    for operation in ('pause', 'resume', 'send_now', 'cancel'):
        current = call('broadcasts_get', broadcast_id=row['id'])['broadcast']
        call('broadcasts_' + operation, broadcast_id=row['id'], expected_revision=current['revision'])
    assert call('broadcasts_get', broadcast_id=row['id'])['broadcast']['status'] == 'cancelled'
