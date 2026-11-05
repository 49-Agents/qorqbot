import pytest
from bazarlyk.attention_queue import queue, viewed, signoff_key
from qorqut_operations.contracts import DomainError
from test_owner_private_conversations import s, task, send, ok, signoff, call, get


def test_queue_exact_versions_persist_and_new_reply_returns(s):
    source, _ = task(s)
    ok(send(s, s.coordinator, source, text='First result'))
    first = queue(s.runtime, s.owner_global)['items']
    assert len(first) == 1 and first[0]['text'] == 'First result'
    assert not queue(s.runtime, s.owner_global, {'viewed': first[0]['viewed_ids']})['items']
    assert not queue(s.runtime, s.owner_global)['items']
    ok(send(s, s.coordinator, source, text='New result'))
    assert queue(s.runtime, s.owner_global)['items'][0]['text'] == 'New result'
    # Late acknowledgment of an older version cannot clear the new reply.
    assert queue(s.runtime, s.owner_global, {'viewed': first[0]['viewed_ids']})['items']
    with pytest.raises(DomainError): queue(s.runtime, s.coordinator)
    with pytest.raises(DomainError): queue(s.runtime, s.owner_global, {'viewed': 'wrong'})


def test_read_elsewhere_clears_thread(s):
    source, _ = task(s)
    ok(send(s, s.coordinator, source, text='Ready'))
    assert queue(s.runtime, s.owner_global)['items']
    conversation = ok(get(s, s.owner, source))['conversation']
    ok(call(s, s.owner, 'owner_conversation_mark_read', {'source': source, 'sequence': conversation['latest_sequence']}))
    assert not queue(s.runtime, s.owner_global)['items']


def test_view_signoff_does_not_resolve_or_repeat_it(s):
    _, row = signoff(s, global_scope=True)
    items = queue(s.runtime, s.owner_global)['items']
    selected = next(item for item in items if item['kind'] == 'signoff')
    assert selected['id'] == signoff_key(row)
    queue(s.runtime, s.owner_global, {'viewed': selected['viewed_ids']})
    assert not [item for item in queue(s.runtime, s.owner_global)['items'] if item['kind'] == 'signoff']
    with s.runtime.database.read() as uow:
        assert uow.connection.execute('SELECT state FROM signoffs WHERE id=?', (row['id'],)).fetchone()[0] == 'pending'


def test_viewed_preferences_merge_without_clobbering_other_browser(tmp_path):
    viewed(tmp_path, 'owner', ['a'])
    viewed(tmp_path, 'owner', ['b'])
    assert viewed(tmp_path, 'owner') == {'a', 'b'}
    assert viewed(tmp_path, 'other') == set()


def test_viewbox_notices_keep_distinct_links_and_acknowledge_exact_placement(s):
    first = ok(call(s, s.agent_ctx, 'viewbox_place', {'link': 'https://example.com/one', 'title': 'First draft'}))
    ok(call(s, s.agent_ctx, 'viewbox_place', {'link': 'https://example.com/two', 'title': 'Second draft'}))
    ok(call(s, s.coordinator, 'viewbox_place', {'link': 'https://example.com/global', 'title': 'Global draft'}))
    board = s.runtime.domains.viewbox.owner_board(s.owner_global, s.agent.id)
    items = queue(s.runtime, s.owner_global)['items']
    assert len(items) == 3 and {item['agent_id'] for item in items} == {s.agent.id, s.ordu.id}
    assert [item['title'] for item in items if item['agent_id'] == s.agent.id] == ['Second draft', 'First draft']
    item = next(i for i in items if i['viewbox_item_id'] == first['id'])
    assert item['id'] == board['items'][1]['attention_id']
    # Listing leaves the unread notice intact; acknowledgment persists without deleting the board entry.
    assert len(queue(s.runtime, s.owner_global)['items']) == 3
    assert len(queue(s.runtime, s.owner_global, {'viewed': item['viewed_ids']})['items']) == 2
    assert len(s.runtime.domains.viewbox.owner_board(s.owner_global, s.agent.id)['items']) == 2
    assert len(queue(s.runtime, s.owner_global)['items']) == 2
    ok(call(s, s.agent_ctx, 'viewbox_place', {'link': 'https://example.com/one', 'title': 'Revised draft'}))
    new = queue(s.runtime, s.owner_global, {'viewed': item['viewed_ids']})['items']
    assert len(new) == 3 and new[0]['viewbox_item_id'] == first['id'] and new[0]['id'] != item['id']


def test_viewbox_attention_is_bounded_per_agent_and_ignores_archived_agents(s):
    for n in range(32):
        ok(call(s, s.agent_ctx, 'viewbox_place', {'link': f'https://example.com/{n}', 'title': f'Draft {n}'}))
    ok(call(s, s.coordinator, 'viewbox_place', {'link': 'https://example.com/global', 'title': 'Global draft'}))
    items = queue(s.runtime, s.owner_global)['items']
    assert len(items) == 31
    assert [item['title'] for item in items if item['agent_id'] == s.agent.id] == [f'Draft {n}' for n in range(31, 1, -1)]
    with s.db.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at='2026-10-03T00:00:00Z' WHERE id=?", (s.fixture.business_id,))
    assert [item['agent_id'] for item in queue(s.runtime, s.owner_global)['items']] == [s.ordu.id]
    with s.db.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (s.ordu.id,))
    assert queue(s.runtime, s.owner_global)['items'] == []
