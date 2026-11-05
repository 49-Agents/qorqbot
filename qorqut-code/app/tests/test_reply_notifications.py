import pytest
from qorqut_operations.contracts import DomainError
from bazarlyk.reply_notifications import replies
from test_owner_private_conversations import s, task, send, ok, get, call


def test_reply_feed_tracks_exact_coordinator_read_cursor_and_owner_boundary(s):
    source, _ = task(s)
    ok(send(s, s.coordinator, source, text='Ordu reply'))
    data = replies(s.runtime, s.owner_global)['items']
    dms = [item for item in data if item['kind'] == 'thread']
    assert {item['agent_id'] for item in dms} == {s.ordu.id}
    assert all(item['source'] == source for item in dms)
    read = ok(get(s, s.owner, source, agent_id=s.ordu.id))
    ok(call(s, s.owner, 'owner_conversation_mark_read', {'source': source, 'agent_id': s.ordu.id,
        'sequence': read['conversation']['latest_sequence']}))
    assert [item['agent_id'] for item in replies(s.runtime, s.owner_global)['items'] if item['kind'] == 'thread'] == []
    with pytest.raises(DomainError):
        replies(s.runtime, s.coordinator)
