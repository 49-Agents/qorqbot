"""Direct Dev input keeps durable inbox attribution and the existing delivery outbox."""
import pytest

from qorqut_operations.contracts import new_id
from test_agent_messages import call, sent
from test_service_transports import ServiceFixture
from test_terminal_control import Backend


@pytest.fixture
def direct(tmp_path):
    s = ServiceFixture(tmp_path, full=True, dev=True)
    s.backend = Backend()
    s.runtime.terminal_control.backend = s.backend
    yield s
    s.runtime.stop()


def send(s, sender=None, **changes):
    return call(s, sender or s.manager, 'agents_message_send',
                {'to_agent_id': s.dev.id, 'message': 'Review the landing task.', 'delivery': 'terminal', **changes},
                business=s.fixture.business_id)


@pytest.mark.parametrize('role', ['ordubashy', 'global-marketing'])
def test_direct_message_is_durable_attributed_and_idempotent(direct, role):
    s = direct
    sender = s.manager if role == 'ordubashy' else s.fixture.create('marketing', role)
    args = {'to_agent_id': s.dev.id, 'message': 'Review the landing task.', 'delivery': 'terminal'}
    key = new_id()
    first = sent(call(s, sender, 'agents_message_send', args, key, s.fixture.business_id))
    assert sent(call(s, sender, 'agents_message_send', args, key, s.fixture.business_id))['id'] == first['id']
    assert first['terminal_delivery']['state'] == 'queued'
    assert not s.backend.calls
    assert s.runtime.terminal_control.tick()
    assert not s.runtime.terminal_control.tick()
    assert len(s.backend.calls) == 1
    target, action, body = s.backend.calls[0]
    assert (target, action) == (s.dev.id, 'ping')
    assert sender.id in body and first['id'] in body and args['message'] in body
    history = call(s, s.dev, 'agents_messages_list', {}, business=s.fixture.business_id)
    assert history.data['items'][0]['terminal_delivery']['state'] == 'completed'
    with s.fixture.db.read() as u:
        assert u.connection.execute('SELECT acknowledged_at FROM inbox_deliveries WHERE agent_id=? AND event_id=?',
            (s.dev.id, first['event_id'])).fetchone()[0] is None
    reply = sent(call(s, s.dev, 'agents_message_send', {'reply_to': first['id'], 'message': 'Received.'}, business=s.fixture.business_id))
    assert reply['thread_id'] == first['id'] and reply['recipient_id'] == sender.id


@pytest.mark.parametrize('change', ['permission', 'marketing_scope', 'session', 'inactive_target', 'uncertain'])
def test_changed_authority_or_session_never_blindly_delivers(direct, change):
    s = direct
    sender = s.fixture.create('marketing', 'global-marketing') if change == 'marketing_scope' else s.manager
    message = sent(send(s, sender=sender))
    if change == 'permission':
        s.fixture.update(s.manager, {'tool_policy': {'overrides': {'agents_message_send': 'deny'}}})
    elif change == 'marketing_scope':
        s.fixture.update(sender, {'scope': {'kind': 'ecosystem', 'allowed_business_ids': [],
                                          'global_capabilities': ['read_global_context']}})
    elif change == 'session':
        s.backend.current['session_id'] = '$2'
    elif change == 'inactive_target':
        with s.fixture.db.transaction() as u:
            u.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (s.dev.id,))
    else:
        s.backend.fail = True
    assert s.runtime.terminal_control.tick()
    assert not s.runtime.terminal_control.tick()
    assert len(s.backend.calls) == (1 if change == 'uncertain' else 0)
    with s.fixture.db.read() as u:
        state = u.repository('terminal_actions').get(message['terminal_delivery']['action_id'])['state']
    assert state == ('uncertain' if change == 'uncertain' else 'failed')


def test_narrow_delivery_scope_and_no_backend(direct):
    s = direct
    assert send(s, sender=s.agent).status == 'error'
    assert send(s, to_agent_id=s.agent.id).status == 'error'
    assert send(s, message='bad\x1binput').status == 'error'
    assert call(s, s.manager, 'agents_message_send',
        {'to_agent_id': s.dev.id, 'message': 'Wrong scope', 'delivery': 'terminal'}).status == 'error'
    s.runtime.terminal_control.backend = None
    result = send(s)
    assert result.status == 'error' and result.error['code'] == 'operation_unavailable'
    assert send(s, delivery='inbox').status == 'ok'


def test_idle_or_unverified_terminal_rejects_without_losing_inbox_fallback(direct):
    s = direct
    from bazarlyk.transport.agent_terminal_control import AgentTerminalBackend
    s.backend.validate = AgentTerminalBackend.validate
    s.backend.current = {'state': 'present', 'command': 'bash', 'client': 'codex'}
    assert send(s).status == 'error'
    assert not s.runtime.terminal_control.tick()
    assert send(s, delivery='inbox').status == 'ok'
