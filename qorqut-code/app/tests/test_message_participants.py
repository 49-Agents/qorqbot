"""Current message participants route replies and delivery; the original envelope keeps authorship."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3

import pytest

from qorqut_operations.contracts import DomainError, new_id, plain
from bazarlyk.storage import Database
from bazarlyk.transport.mcp_presentation import present
from inbox_support import pending_count, pending_ids
from test_delegated_event_routing import assignment
from test_operational_delegation_store import setup_store
from test_service_transports import ServiceFixture

AT = '2026-09-01T12:00:00.000000Z'


def retired(u, business_id, name):
    """An inactive local identity kept only as an attribution record."""
    identity = new_id()
    u.repository('principals').insert({'id': identity, 'kind': 'agent', 'display_name': name, 'active': 0})
    u.connection.execute('INSERT INTO agent_profiles(agent_id,principal_id,home_business_id,namespace,name,workspace,status,'
        'profile_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', (identity, identity, business_id, business_id,
        name.lower(), '/retired/' + identity, 'retired', json.dumps({'role': {'id': 'strategist'}}), AT, AT))
    return identity


def insert(u, business_id, sender, recipient, body, *, reply_to=None, acknowledged=False):
    """A message recorded before conversion, with its original delivery."""
    identity = new_id()
    thread = u.connection.execute('SELECT thread_id FROM agent_messages WHERE id=?', (reply_to,)).fetchone()[0] if reply_to else identity
    event = u.connection.execute('INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) '
        'VALUES(?,?,?,?,?,?,?)', (business_id, 'agent_messages', identity, sender, 'direct_message', body, AT)).lastrowid
    u.connection.execute('INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,'
        'current_sender_id,current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (identity, event, business_id, sender, recipient, reply_to, thread, body, AT, sender, recipient))
    u.connection.execute('INSERT INTO inbox_deliveries(agent_id,event_id,acknowledged_at,outcome) VALUES(?,?,?,?)',
        (recipient, event, AT if acknowledged else None, 'Handled' if acknowledged else None))
    return identity


def remap(u, mapping):
    """What reviewed maintenance does: move current participants and pending deliveries."""
    for old, new in mapping.items():
        u.connection.execute('UPDATE agent_messages SET current_sender_id=? WHERE current_sender_id=?', (new, old))
        u.connection.execute('UPDATE agent_messages SET current_recipient_id=? WHERE current_recipient_id=?', (new, old))
        u.connection.execute('UPDATE inbox_deliveries SET agent_id=? WHERE agent_id=?', (new, old))


@pytest.fixture
def s(tmp_path):
    s = ServiceFixture(tmp_path, full=True)
    s.canonical = s.agent  # the business's one execution agent
    a, m = s.fixture.business_id, {}
    with s.fixture.db.transaction() as u:
        s.name = u.repository('principals').get(s.canonical.id)['display_name']
        s.other = u.repository('businesses').insert({'id': new_id(), 'slug': 'elsewhere', 'workspace': str(tmp_path / 'b'),
            'hq_path': str(tmp_path / 'b/hq'), 'name': 'Elsewhere'})['id']
        u.repository('business_memberships', business_id=s.other).insert({'principal_id': s.fixture.owner.id, 'is_owner': True})
    # Ordubashy covers every registered business; the business agent never gains the second one.
    s.fixture.ctx = replace(s.fixture.ctx, allowed_business_ids=frozenset({a, s.other}))
    s.coordinator = s.fixture.create('coordinator', 'ordubashy', runtime='codex', scope={'kind': 'ecosystem',
        'allowed_business_ids': [a, s.other], 'global_capabilities': sorted(s.fixture.ctx.global_capabilities)})
    s.founder = s.fixture.create('founder', 'founder')
    s.now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    s.runtime.inbox.clock = lambda: s.now
    s.consumer = new_id()
    with s.fixture.db.transaction() as u:
        planner, promoter, stray = (retired(u, a, name) for name in ('Planner', 'Promoter', 'Stray'))
        remote, remote_peer = retired(u, s.other, 'Remote'), retired(u, s.other, 'Remote peer')
        c, f = s.coordinator.id, s.founder.id
        m[1] = insert(u, a, c, planner, 'Which supplier is next?')
        m[2] = insert(u, a, planner, c, 'Supplier B, pending terms.', reply_to=m[1], acknowledged=True)
        m[3] = insert(u, a, promoter, c, 'Campaign draft is ready.', acknowledged=True)
        m[4] = insert(u, a, c, promoter, 'Send the draft for review.', reply_to=m[3])
        m[5] = insert(u, a, planner, promoter, 'Internal handoff.')
        m[6] = insert(u, a, promoter, planner, 'Handoff received.', reply_to=m[5], acknowledged=True)
        m[7] = insert(u, a, f, planner, 'Strategy note.', acknowledged=True)
        m[8] = insert(u, a, c, stray, 'Unmapped identity.')
        m['b'] = insert(u, s.other, remote, remote_peer, 'Other business.')
        remap(u, {planner: s.canonical.id, promoter: s.canonical.id, remote_peer: s.canonical.id})
        s.events = {k: u.connection.execute('SELECT event_id FROM agent_messages WHERE id=?', (v,)).fetchone()[0] for k, v in m.items()}
    s.m, s.planner, s.promoter, s.stray = m, planner, promoter, stray
    try:
        yield s
    finally:
        s.close()


def call(s, principal, operation, args, business=None):
    business = business or (s.fixture.business_id if principal in (s.coordinator, s.founder) else None)
    ctx = s.runtime.resolver.invocation_context(principal)
    if business: ctx = ctx.select_business(business)
    return s.runtime.application.invoke(ctx, operation, args, {'idempotency_key': new_id()})


def ok(result):
    assert result.status == 'ok', result.to_dict()
    return result.data


def history(s, principal, business=None, **args):
    items, after = [], 0
    while True:
        page = ok(call(s, principal, 'agents_messages_list', {**args, 'after': after, 'limit': 3}, business))
        items += page['items']
        if page['next_cursor'] is None:
            return items
        after = page['next_cursor']


def test_canonical_agent_sees_acknowledges_and_pages_remapped_history(s):
    m, e, me = s.m, s.events, s.canonical.id
    ctx = s.runtime.resolver.invocation_context(s.canonical)
    # Moved pending deliveries only: collapsed history and another business never appear.
    assert pending_ids(s.runtime.inbox, ctx) == [e[1], e[4]]
    items = ok(call(s, s.canonical, 'inbox_list', {'consumer_id': s.consumer}))['items']
    first = items[0]['message']
    assert [item['source_id'] for item in items] == [m[1], m[4]]
    assert (first['sender_name'], first['recipient_name'], first['recipient_id']) == ('Coordinator', 'Planner', s.planner)
    assert first['current_recipient_id'] == me and first['current_recipient_name'] == s.name
    assert first['current_sender_name'] == 'Coordinator' and first['counterpart_id'] == s.coordinator.id
    assert call(s, s.canonical, 'inbox_acknowledge', {'consumer_id': s.consumer, 'event_ids': [e[5]]}).error['code'] == 'not_found'
    ok(call(s, s.canonical, 'inbox_acknowledge', {'consumer_id': s.consumer, 'event_ids': [e[1]], 'outcome': 'Answered'}))
    assert pending_count(s.runtime.inbox, ctx, s.consumer) == 1
    snapshot = s.runtime.inbox.owner_snapshot(s.fixture.ctx, me)['items']
    assert [(i['source_id'], i['message']['sender_name'], i['message']['recipient_name']) for i in snapshot] == [
        (m[4], 'Coordinator', 'Promoter')]

    rows = history(s, s.canonical)
    assert [row['id'] for row in rows] == [m[i] for i in range(1, 8)]  # unmapped and other-business rows stay out
    by_id = {row['id']: row for row in rows}
    assert by_id[m[1]]['acknowledged_at'] and by_id[m[1]]['outcome'] == 'Answered'
    assert by_id[m[2]]['acknowledged_at'] and by_id[m[4]]['acknowledged_at'] is None
    assert (by_id[m[3]]['sender_name'], by_id[m[3]]['current_sender_name']) == ('Promoter', s.name)
    assert by_id[m[5]]['counterpart_id'] is None and by_id[m[5]]['current_sender_id'] == by_id[m[5]]['current_recipient_id'] == me
    assert (by_id[m[5]]['sender_name'], by_id[m[5]]['recipient_name']) == ('Planner', 'Promoter')
    with_coordinator = history(s, s.canonical, counterpart_id=s.coordinator.id)
    assert [row['id'] for row in with_coordinator] == [m[1], m[2], m[3], m[4]]
    assert {row['thread_id'] for row in with_coordinator} == {m[1], m[3]}
    assert [row['id'] for row in history(s, s.canonical, counterpart_id=s.founder.id)] == [m[7]]
    assert [row['id'] for row in history(s, s.canonical, thread_id=m[5])] == [m[5], m[6]]
    # A thread preview or deep link by message ID resolves the whole original thread.
    thread = history(s, s.canonical, thread_id=items[1]['message']['thread_id'])
    assert [(row['id'], row['reply_to'], row['sender_name']) for row in thread] == [(m[3], None, 'Promoter'), (m[4], m[3], 'Coordinator')]
    theirs = history(s, s.coordinator, s.fixture.business_id, counterpart_id=me)
    assert [row['id'] for row in theirs] == [m[1], m[2], m[3], m[4]] and theirs[0]['counterpart_id'] == me
    assert m[8] in {row['id'] for row in history(s, s.coordinator, s.fixture.business_id)}

    compact = present('agents_messages_list', {'status': 'ok', 'data': {'items': plain(rows), 'next_cursor': None}})['data']['items']
    for full, item in zip(rows, compact):
        assert item.get('counterpart_id') == full.get('counterpart_id') and 'sender_name' in item and 'sender_kind' not in item
        for side in ('sender', 'recipient'):
            changed = full[f'current_{side}_id'] != full[f'{side}_id']
            assert (f'current_{side}_id' in item, f'current_{side}_name' in item) == (changed, changed)
    assert any('current_recipient_id' in item for item in compact)
    with pytest.raises(DomainError, match='outside'):
        call(s, s.canonical, 'agents_messages_list', {}, business=s.other)
    with s.fixture.db.transaction() as u, pytest.raises(sqlite3.IntegrityError):
        u.connection.execute('UPDATE agent_messages SET sender_id=? WHERE id=?', (me, m[3]))


def test_replies_reach_current_counterpart_and_collapsed_history_is_read_only(s):
    m, me, c = s.m, s.canonical.id, s.coordinator.id
    with s.fixture.db.read() as u:
        before = u.connection.execute('SELECT count(*) FROM agent_messages').fetchone()[0]
    reply = ok(call(s, s.canonical, 'agents_message_send', {'reply_to': m[2], 'message': 'Terms confirmed.'}))['message']
    assert (reply['recipient_id'], reply['thread_id'], reply['reply_to'], reply['sender_id']) == (c, m[1], m[2], me)
    assert reply['counterpart_id'] == c and reply['current_recipient_id'] == c
    answer = ok(call(s, s.coordinator, 'agents_message_send', {'reply_to': m[3], 'message': 'Approved.'}))['message']
    assert (answer['recipient_id'], answer['thread_id'], answer['recipient_name']) == (me, m[3], s.name)
    ctx = s.runtime.resolver.invocation_context(s.canonical)
    assert answer['event_id'] in pending_ids(s.runtime.inbox, ctx, s.consumer)
    coordinator = s.runtime.resolver.invocation_context(s.coordinator)
    assert reply['event_id'] in pending_ids(s.runtime.inbox, coordinator, new_id())

    refused = {
        'retired original': (s.canonical, {'reply_to': m[2], 'to_agent_id': s.planner}, 'validation_error'),
        'collapsed history': (s.canonical, {'reply_to': m[5]}, 'invalid_transition'),
        'owner-directed founder': (s.canonical, {'reply_to': m[7]}, 'not_found'),
        'unmapped counterpart': (s.coordinator, {'reply_to': m[8]}, 'not_found'),
        'not a participant': (s.canonical, {'reply_to': m[8]}, 'not_found'),
        'retired recipient': (s.canonical, {'to_agent_id': s.promoter}, 'not_found'),
    }
    for case, (principal, args, code) in refused.items():
        result = call(s, principal, 'agents_message_send', {**args, 'message': 'Follow-up'})
        assert result.status == 'error' and result.error['code'] == code, case
    with s.fixture.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM agent_messages').fetchone()[0] == before + 2
        # No runtime self-delivery: the only collapsed deliveries are the ones maintenance moved.
        assert u.connection.execute('SELECT count(*) FROM inbox_deliveries d JOIN agent_messages a ON a.event_id=d.event_id '
            'WHERE a.current_sender_id=a.current_recipient_id').fetchone()[0] == 2
        assert u.connection.execute("SELECT * FROM pragma_foreign_key_check('agent_messages')").fetchall() == []
        orphans = u.connection.execute('SELECT m.id FROM agent_messages m LEFT JOIN agent_messages p ON p.id=m.reply_to '
            'LEFT JOIN agent_messages t ON t.id=m.thread_id WHERE t.id IS NULL OR t.thread_id!=t.id '
            'OR (m.reply_to IS NOT NULL AND (p.id IS NULL OR p.thread_id!=m.thread_id)) '
            'OR (m.reply_to IS NULL AND m.thread_id!=m.id)').fetchall()
        assert orphans == []
    with s.fixture.db.transaction() as u:
        reminder = insert(u, s.fixture.business_id, me, me, 'Self-reminder.')
        event = u.connection.execute('SELECT event_id FROM agent_messages WHERE id=?', (reminder,)).fetchone()[0]
    # One original participant is a self-reminder, not collapsed history: delivered, not repliable.
    assert event in pending_ids(s.runtime.inbox, ctx, s.consumer)
    result = call(s, s.canonical, 'agents_message_send', {'reply_to': reminder, 'message': 'Done'})
    assert result.status == 'error' and result.error['code'] == 'validation_error'


def test_management_routing_follows_current_sender(s):
    m, me = s.m, s.canonical.id
    with s.fixture.db.transaction() as u:
        u.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (s.coordinator.id,))
        routed = new_id()
        u.connection.execute('INSERT INTO management_routing(id,source_type,source_id,business_id,coordinator_id,attention_destination,'
            'management_allowed,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (routed, 'agent_messages', m[2], s.fixture.business_id, s.coordinator.id, 'management', 1, s.planner, AT, AT))
    s.fixture.update(s.canonical, {'tool_policy': {'overrides': {
        'management_list': 'allow', 'management_get': 'allow', 'management_route': 'allow'}}})
    listed = ok(call(s, s.canonical, 'management_list', {}))['items']
    assert [item['id'] for item in listed] == [routed]
    assert ok(call(s, s.canonical, 'management_get', {'routing_id': routed}))['routing']['source']['id'] == m[2]
    # The current sender is the requester, whoever originally wrote the message.
    created = ok(call(s, s.canonical, 'management_route', {'source_type': 'agent_messages', 'source_id': m[3],
        'attention_destination': 'management'}))['routing']
    assert created['coordinator_id'] == s.coordinator.id and created['created_by'] == me
    refused = call(s, s.canonical, 'management_route', {'source_type': 'agent_messages', 'source_id': m[4],
        'attention_destination': 'management'})
    assert refused.status == 'error' and refused.error['code'] == 'forbidden'
    assert {item['id'] for item in ok(call(s, s.canonical, 'management_list', {}))['items']} == {routed, created['id']}


def test_delegated_routing_uses_current_recipient(tmp_path):
    database, store, parent, business, _ = setup_store(tmp_path)
    with database.transaction() as u:
        old, second, peer, other = (retired(u, business, name) for name in ('Planner', 'Promoter', 'Peer', 'Other'))
        root = insert(u, business, peer, old, 'Please continue this thread.')
        reply = insert(u, business, peer, old, 'Second note.', reply_to=root)
        away = insert(u, business, peer, parent, 'Now handled elsewhere.', reply_to=root)
        internal = insert(u, business, old, second, 'Internal exchange.', reply_to=root)
        saved = assignment(store, u, parent, business, root)
        remap(u, {old: parent, second: parent})
        u.connection.execute('UPDATE agent_messages SET current_recipient_id=? WHERE id=?', (other, away))
        events = {k: dict(u.connection.execute('SELECT * FROM inbox_events WHERE source_id=?', (v,)).fetchone())
                  for k, v in {'reply': reply, 'away': away, 'internal': internal}.items()}
        routed = {k: [row['id'] for row in store.routed_assignments(u, parent, event)] for k, event in events.items()}
    # Current recipient routes; an original recipient or a collapsed pair never does.
    assert routed == {'reply': [saved['id']], 'away': [], 'internal': []}


def test_message_participants_backfill_and_original_envelope_stays_immutable(tmp_path):
    database = Database(tmp_path / "state.sqlite3")
    database.initialize(through=92)
    conn = database._connect()
    conn.execute("INSERT INTO principals(id,kind,display_name,created_at,updated_at) VALUES ('a','agent','A','t','t'),('b','agent','B','t','t'),('c','agent','C','t','t')")
    conn.execute("INSERT INTO inbox_events(id,source_type,source_id,event_type,summary,created_at) VALUES (1,'agent_messages','m','message','s','t')")
    conn.execute("INSERT INTO agent_messages(id,event_id,sender_id,recipient_id,thread_id,body,created_at) VALUES ('m',1,'a','b','m','hello','t')")
    conn.close()
    database.initialize()
    conn = database._connect()
    row = conn.execute("SELECT * FROM agent_messages").fetchone()
    assert (row["current_sender_id"], row["current_recipient_id"]) == ("a", "b")
    conn.execute("UPDATE agent_messages SET current_recipient_id='c' WHERE id='m'")
    for statement in ("UPDATE agent_messages SET body='x'", "UPDATE agent_messages SET sender_id='c'", "DELETE FROM agent_messages"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)
    conn.close()
