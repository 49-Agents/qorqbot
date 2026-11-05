"""Owner-only agent-to-agent communication hints on the reply feed: cursor on agent_messages.event_id, no bodies, no mutation.
Synthetic tmp instance; message rows are inserted directly because only the stored envelope is under test."""
import pytest

from qorqut_operations.contracts import DomainError, new_id

from bazarlyk.reply_notifications import replies
from test_reply_notification_batching import host, agent_ctx  # noqa: F401  (fixture reuse)

SECRET = 'SECRET BODY MUST NEVER LEAVE THE DATABASE'


def message(host, sender, recipient, body=SECRET):
    message_id = new_id()
    with host.instance.database.transaction() as uow:
        event = uow.connection.execute("INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) "
            "VALUES(NULL,'agent_message',?,?,'agent_message','x','2026-10-18T10:00:00+00:00')", (message_id, sender)).lastrowid
        uow.connection.execute("INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,thread_id,body,created_at,"
            "current_sender_id,current_recipient_id) VALUES(?,?,NULL,?,?,?,?,'2026-10-18T10:00:00+00:00',?,?)",
            (message_id, event, sender, recipient, message_id, body, sender, recipient))
    return message_id, event


def test_baseline_cursor_filters_privacy_and_idempotence(host):
    a, b, owner_id = host.coordinator, host.marketing, host.instance.owner
    old, _ = message(host, b, a)
    with pytest.raises(DomainError):
        replies(host.runtime, agent_ctx(host, host.coordinator, host.first), True, None)
    assert 'communications' not in replies(host.runtime, host.owner)
    baseline = replies(host.runtime, host.owner, True, None)['communications']
    assert baseline['items'] == [] and baseline['has_more'] is False and baseline['next_cursor'] > 0
    for bad in (-1, 'x', True, 1.5):
        with pytest.raises(DomainError) as raised:
            replies(host.runtime, host.owner, True, bad)
        assert raised.value.code == 'validation_error'
    cursor = baseline['next_cursor']
    first, _ = message(host, b, a)          # worker -> primary
    message(host, owner_id, b)              # owner-origin
    message(host, b, owner_id)              # to the owner
    message(host, b, b)                     # self
    second, last = message(host, a, b)
    feed = replies(host.runtime, host.owner, True, cursor)
    data = feed['communications']
    assert [item['id'] for item in data['items']] == [first, second]
    assert set(data['items'][0]) == {'event_id', 'id', 'sender_id', 'recipient_id', 'sender_name', 'recipient_name', 'created_at'}
    assert (data['items'][0]['sender_id'], data['items'][0]['recipient_id']) == (b, a)
    assert data['items'][0]['sender_name'] and data['items'][0]['recipient_name']
    assert 'SECRET' not in str(feed) and old not in str(data)
    assert data['next_cursor'] == last and data['has_more'] is False
    assert replies(host.runtime, host.owner, True, cursor)['communications'] == data
    assert replies(host.runtime, host.owner, True, data['next_cursor'])['communications']['items'] == []
    assert feed['items'] == replies(host.runtime, host.owner)['items']


def test_pages_continue_without_loss_or_repeat(host):
    a, b = host.coordinator, host.marketing
    cursor = replies(host.runtime, host.owner, True, None)['communications']['next_cursor']
    ids = [message(host, b, a)[0] for _ in range(201)]
    one = replies(host.runtime, host.owner, True, cursor)['communications']
    assert len(one['items']) == 200 and one['has_more'] is True and one['next_cursor'] == one['items'][-1]['event_id']
    two = replies(host.runtime, host.owner, True, one['next_cursor'])['communications']
    assert [item['id'] for item in one['items'] + two['items']] == ids and two['has_more'] is False
