"""Owner Focus edits and snoozes notify the bound source's recipients, atomically and without prose."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import InvocationOptions, new_id, plain
from test_ordubashy_owner_inbox import inbox, task

UNTIL = '2026-10-01T08:00:00.000000Z'
SECRET = 'Private curator prose'


def board(s):
    """Install Focus around the inbox fixture; Ordubashy becomes the configured coordinator."""
    from bazarlyk.domains.relationships.items import ContextItems
    from bazarlyk.inbox.focus import FocusService
    from bazarlyk.transport.auth import human_global_capabilities
    s.policy.human_global_capabilities = human_global_capabilities
    with s.db.transaction() as u:
        u.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (s.ordubashy.id,))
        profile = json.loads(u.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?', (s.ordubashy.id,)).fetchone()[0])
        profile['runtime'] = 'codex'
        u.connection.execute('UPDATE agent_profiles SET profile_json=? WHERE agent_id=?', (json.dumps(profile), s.ordubashy.id))
    s.inbox.domains.stories.relationships = SimpleNamespace(items=ContextItems(s.inbox.domains.stories, s.app))
    FocusService(s.inbox, s.service).register()
    s.owner_global = replace(s.owner_context, business_id=None, review_business_id=None)
    s.coordinator_global = replace(s.ctx(), business_id=None, review_business_id=None)
    s.change_override('focus_edit', 'allow', actor=s.ordubashy)
    s.change_override('focus_present', 'allow', actor=s.ordubashy)
    s.change_override('focus_set_archived', 'allow', actor=s.ordubashy)
    return s


def call(s, ctx, op, args, key=None):
    return s.app.invoke(ctx, op, {'scope': 'global', **args}, {'idempotency_key': key or new_id()})


def ok(result):
    assert result.status == 'ok', result.to_dict()
    return plain(result.data)


def present(s, source=None, **args):
    return ok(call(s, s.owner_global, 'focus_present', {'source': source, **args} if source else args))['item']


def edit(s, card, changes, *, ctx=None, key=None):
    current = ok(call(s, s.owner_global, 'focus_get', {'item_id': card['id']}))['item']
    return call(s, ctx or s.owner_global, 'focus_edit',
        {'item_id': card['id'], 'expected_card_revision': current['revision'], 'changes': changes}, key)


def last_event(s):
    with s.db.read() as u:
        return u.connection.execute('SELECT coalesce(max(id),0) FROM inbox_events').fetchone()[0]


def sent(s, after):
    """Raw addressed events since `after`: one (type, source, id, summary, recipients) per event."""
    with s.db.read() as u:
        return [(e['event_type'], e['source_type'], e['source_id'], e['summary'],
                 {r[0] for r in u.connection.execute('SELECT agent_id FROM inbox_deliveries WHERE event_id=?', (e['id'],))})
                for e in u.connection.execute('SELECT * FROM inbox_events WHERE id>? ORDER BY id', (after,))]


def visible(s, agent, after=0):
    with s.db.read() as u:
        return [e for e in s.inbox.pending(s.agent_context(agent), u, after=after)['items']]


HOME = object()  # Omitted means the test business; an explicit None means Global.


def assign_agent(s, kind, identity, agent, business=HOME):
    with s.db.transaction() as u:
        u.actor_id = s.owner.id
        u.repository('relationships', business_id=s.business if business is HOME else business).insert({'id': new_id(), 'from_type': kind, 'from_id': identity,
            'to_type': 'agent', 'to_id': agent.id, 'role': 'assigned_to'})


def outsider(s):
    """A business agent homed elsewhere, with no membership in the test business."""
    from qorqut_operations.contracts import Principal
    actor = Principal(new_id(), 'agent', 'Outsider')
    with s.db.transaction() as u:
        profile = json.loads(u.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?', (s.agent.id,)).fetchone()[0])
        profile.update(agent_id=actor.id, name='agent-' + actor.id, display_name='Outsider', scope={'kind': 'business', 'business_id': s.other_business})
        u.repository('principals').insert({'id': actor.id, 'kind': 'agent', 'display_name': 'Outsider'})
        u.repository('business_memberships', business_id=s.other_business).insert({'principal_id': actor.id})
        u.repository('agent_profiles', business_id=s.other_business).insert({'agent_id': actor.id, 'principal_id': actor.id,
            'namespace': s.other_business, 'name': profile['name'], 'workspace': str(s.root / 'agents' / actor.id), 'profile_json': profile})
    return actor


def set_global_capabilities(s, agent, capabilities):
    with s.db.transaction() as u:
        repo = u.repository('agent_profiles', business_id=None)
        row = repo.get(agent.id)
        profile = row['profile_json']
        profile['scope']['global_capabilities'] = capabilities
        profile['profile_revision'] += 1
        repo.update(agent.id, {'profile_json': profile}, expected_revision=row['revision'])


def follow(s, agent, source_type, identity):
    with s.db.transaction() as u:
        u.connection.execute('INSERT INTO inbox_subscriptions(id,agent_id,business_id,source_type,source_id,created_at) VALUES(?,?,?,?,?,?)',
            (new_id(), agent.id, s.business, source_type, identity, '2026-09-30T00:00:00Z'))


def test_owner_snooze_resume_and_edit_notify_each_recipient_once(inbox):
    s = board(inbox)
    task(s)
    assignee, subscriber, stranger = s.agent, s.add_agent('global-marketing'), outsider(s)
    assign_agent(s, 'task', 'owner-task', assignee)
    follow(s, subscriber, 'tasks', 'owner-task')
    # An agent homed in another business is assigned but has no access here.
    assign_agent(s, 'task', 'owner-task', stranger)
    card = present(s, {'type': 'task', 'id': 'owner-task', 'business_id': s.business}, display_name=SECRET, reason=SECRET)
    with s.db.read() as u:
        source = dict(u.connection.execute("SELECT * FROM tasks WHERE id='owner-task'").fetchone())
        changes = u.connection.execute('SELECT count(*) FROM task_changes').fetchone()[0]
    recipients = {s.ordubashy.id, assignee.id, subscriber.id}

    after = last_event(s)
    request = {'item_id': card['id'], 'expected_card_revision': card['revision'], 'changes': {'deferred_until': '2026-10-01T10:00:00+02:00'}}
    snoozed = call(s, s.owner_global, 'focus_edit', request, 'snooze')
    assert ok(snoozed)['item']['deferred_until'] == UNTIL
    events = sent(s, after)
    assert events == [('focus_snoozed', 'tasks', 'owner-task', 'Owner snoozed Focus until ' + UNTIL + ': Owner work', recipients)]
    with s.db.read() as u:
        row = u.connection.execute('SELECT * FROM inbox_events WHERE id>? ', (after,)).fetchone()
        assert (row['actor_id'], row['source_revision'], row['business_id']) == (s.owner.id, source['revision'], s.business)
        # Presentation never mutates the task or fabricates task history.
        assert dict(u.connection.execute("SELECT * FROM tasks WHERE id='owner-task'").fetchone()) == source
        assert u.connection.execute('SELECT count(*) FROM task_changes').fetchone()[0] == changes
    # An exact retry replays the stored result; an equivalent instant and a blank edit are no-ops.
    assert call(s, s.owner_global, 'focus_edit', request, 'snooze').status == 'ok'
    edit(s, card, {'deferred_until': UNTIL.replace('.000000', '')})
    edit(s, card, {})
    assert len(sent(s, after)) == 1
    # Delivery is authorized again on read, without copying prose.
    for agent in (s.ordubashy, assignee, subscriber):
        item, = [e for e in visible(s, agent, after) if e['event_type'] == 'focus_snoozed']
        assert item['summary'] == events[0][3] and SECRET not in json.dumps(item)
    s.change_override('tasks_get', 'deny', actor=assignee)
    assert not visible(s, assignee, after)
    # An inactive agent is skipped when the next event is written.
    with s.db.transaction() as u:
        u.connection.execute('UPDATE principals SET active=0 WHERE id=?', (subscriber.id,))
    recipients = {s.ordubashy.id, assignee.id}

    resumed = last_event(s)
    ok(edit(s, card, {'deferred_until': None}))
    assert sent(s, resumed) == [('focus_resumed', 'tasks', 'owner-task', 'Owner resumed Focus: Owner work', recipients)]

    edited = last_event(s)
    ok(edit(s, card, {'display_name': 'Another name', 'urgent': True, 'blocker': False, 'reason': SECRET + ' changed'}))
    events = sent(s, edited)
    assert events == [('focus_edited', 'tasks', 'owner-task', 'Owner edited Focus name, reason, urgent flag: Owner work', recipients)]
    assert 'Another name' not in events[0][3] and SECRET not in events[0][3]

    combined = last_event(s)
    ok(edit(s, card, {'deferred_until': '2026-11-01T00:00:00Z', 'requested_response': 'Reply today'}))
    assert sent(s, combined) == [('focus_snoozed,focus_edited', 'tasks', 'owner-task',
        'Owner snoozed Focus until 2026-11-01T00:00:00.000000Z and edited Focus requested action: Owner work', recipients)]

    # JSON-valued fields compare by content: rewriting the same references or flags is silent.
    task(s, identity='ref-task')
    linked = last_event(s)
    references = {'references': [{'type': 'task', 'id': 'ref-task', 'business_id': s.business}]}
    ok(edit(s, card, references))
    ok(edit(s, card, references))
    ok(edit(s, card, {'blocker': False, 'urgent': True, 'display_name': 'Another name'}))
    assert sent(s, linked) == [('focus_edited', 'tasks', 'owner-task', 'Owner edited Focus references: Owner work', recipients)]


def test_agent_edits_reorders_archives_failures_and_rollback_stay_silent(inbox):
    s = board(inbox)
    task(s)
    task(s, identity='second-task')
    assign_agent(s, 'task', 'owner-task', s.agent)
    first = present(s, {'type': 'task', 'id': 'owner-task', 'business_id': s.business})
    second = present(s, {'type': 'task', 'id': 'second-task', 'business_id': s.business})
    third = present(s, reference={'key': 'r', 'title': 'Preview', 'target': {'type': 'url', 'url': 'https://example.com/r'}})
    after = last_event(s)
    # The configured coordinator's own curation is not an owner action.
    ok(edit(s, first, {'deferred_until': UNTIL, 'display_name': 'Agent name'}, ctx=s.coordinator_global))
    agent_edit = ok(call(s, s.owner_global, 'focus_get', {'item_id': first['id']}))['item']
    assert agent_edit['deferred_until'] == UNTIL and agent_edit['display_name'] == 'Agent name'
    # Reordering, including the neighbours whose positions shift, archiving and re-presenting.
    for card, move in ((third, {'position': 'top'}), (second, {'before_id': third['id']}), (first, {'position': 'bottom'})):
        current = ok(call(s, s.owner_global, 'focus_get', {'item_id': card['id']}))['item']
        ok(call(s, s.owner_global, 'focus_move', {'item_id': card['id'], 'expected_card_revision': current['revision'], **move}))
    current = ok(call(s, s.owner_global, 'focus_get', {'item_id': second['id']}))['item']
    archived = ok(call(s, s.owner_global, 'focus_set_archived', {'item_id': second['id'], 'expected_card_revision': current['revision'], 'archived': True}))['item']
    ok(call(s, s.owner_global, 'focus_set_archived', {'item_id': second['id'], 'expected_card_revision': archived['revision'], 'archived': False}))
    present(s, {'type': 'task', 'id': 'owner-task', 'business_id': s.business}, urgent=True)
    assert sent(s, after) == []
    # A stale write fails without an event or a change.
    stale = call(s, s.owner_global, 'focus_edit', {'item_id': first['id'], 'expected_card_revision': 1, 'changes': {'deferred_until': UNTIL}})
    assert stale.error['code'] == 'revision_conflict'
    # The capture belongs to the write: an unwound transaction leaves neither.
    current = ok(call(s, s.owner_global, 'focus_get', {'item_id': first['id']}))['item']
    args = {'scope': 'global', 'item_id': first['id'], 'expected_card_revision': current['revision'], 'changes': {'deferred_until': '2026-12-01T00:00:00Z'}}
    with pytest.raises(RuntimeError):
        with s.db.transaction() as u:
            plan = s.app.attention.prepare('focus_edit', s.owner_global, args, InvocationOptions(), u)
            s.app.attention.apply(s.owner_global, plan, u)
            assert u.connection.execute('SELECT count(*) FROM inbox_changes').fetchone()[0] == 1
            raise RuntimeError('rollback')
    assert sent(s, after) == []
    assert ok(call(s, s.owner_global, 'focus_get', {'item_id': first['id']}))['item']['deferred_until'] == UNTIL


def test_standalone_reference_reaches_only_the_current_focus_reader(inbox):
    s = board(inbox)
    card = present(s, reference={'key': 'preview', 'title': 'Landing preview', 'target': {'type': 'url', 'url': 'https://example.com/private'}},
        display_name=SECRET)
    with s.db.transaction() as u:
        u.connection.execute('UPDATE focus_cards SET created_by=? WHERE id=?', (s.ordubashy.id, card['id']))
    after = last_event(s)
    ok(edit(s, card, {'deferred_until': UNTIL, 'title': 'Renamed preview'}))
    events = sent(s, after)
    assert events == [('focus_snoozed,focus_edited', 'focus_cards', card['id'],
        'Owner snoozed Focus until ' + UNTIL + ' and edited Focus title: Renamed preview', {s.ordubashy.id})]
    item, = visible(s, s.ordubashy, after)
    assert item['source_id'] == card['id'] and SECRET not in json.dumps(item) and 'example.com' not in json.dumps(item)
    # A former coordinator that created the card has no Focus access now and gets nothing.
    replacement = s.add_agent('ordubashy')
    with s.db.transaction() as u:
        profile = json.loads(u.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?', (replacement.id,)).fetchone()[0])
        profile['runtime'] = 'codex'
        u.connection.execute('UPDATE agent_profiles SET profile_json=? WHERE agent_id=?', (json.dumps(profile), replacement.id))
        u.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (replacement.id,))
    after = last_event(s)
    ok(edit(s, card, {'deferred_until': None}))
    assert sent(s, after) == [('focus_resumed', 'focus_cards', card['id'], 'Owner resumed Focus: Renamed preview', {replacement.id})]
    assert not visible(s, s.ordubashy, after)


PEER_CAPABILITIES = ['read_global_context', 'coordinate_businesses']


def marketing_peer(s, capabilities=PEER_CAPABILITIES):
    """An ecosystem Global Marketing agent; the peer predicate also needs coordinate_businesses and an active principal."""
    agent = s.add_agent('global-marketing')
    set_global_capabilities(s, agent, list(capabilities))
    return agent


def peer_present(s, agent, key='peer-note'):
    """The peer creates its own standalone Reference through the real operation."""
    ctx = replace(s.agent_context(agent), business_id=None, review_business_id=None)
    return ok(call(s, ctx, 'focus_present', {'reference': {'key': key, 'title': 'Peer reference',
        'target': {'type': 'url', 'url': 'https://example.com/peer-private'}}}))['item'], ctx


def test_marketing_peer_own_reference_reaches_only_its_author_and_the_coordinator(inbox):
    s = board(inbox)
    peer, other, ungranted = marketing_peer(s), marketing_peer(s), marketing_peer(s, ['read_global_context'])
    card, peer_ctx = peer_present(s, peer)
    with s.db.read() as u:
        assert u.connection.execute('SELECT created_by FROM focus_cards WHERE id=?', (card['id'],)).fetchone()[0] == peer.id
    after = last_event(s)
    ok(edit(s, card, {'deferred_until': UNTIL, 'display_name': SECRET}))
    summary = 'Owner snoozed Focus until ' + UNTIL + ' and edited Focus name: Peer reference'
    assert sent(s, after) == [('focus_snoozed,focus_edited', 'focus_cards', card['id'], summary, {s.ordubashy.id, peer.id})]
    # The author reads a metadata-only event; neither owner prose nor the reference target is in it.
    item, = visible(s, peer, after)
    assert item['source_id'] == card['id'] and item['summary'] == summary
    assert SECRET not in json.dumps(item) and 'peer-private' not in json.dumps(item)
    assert item['source_id'] == card['id'] and item['event_type'] == 'focus_snoozed,focus_edited'
    # An unrelated peer with the same authority, an ungranted Marketing agent and a business agent get nothing.
    for agent in (other, ungranted, s.agent):
        assert not visible(s, agent, after)
    # The peer's own edits are agent curation, not an owner action, so they stay silent; so does a no-op owner edit.
    after = last_event(s)
    current = ok(call(s, s.owner_global, 'focus_get', {'item_id': card['id']}))['item']
    ok(call(s, peer_ctx, 'focus_edit', {'item_id': card['id'], 'expected_card_revision': current['revision'], 'changes': {'blocker': True}}))
    assert sent(s, after) == []
    # Another peer's reference never reaches this one.
    foreign, _ = peer_present(s, other, key='other-note')
    after = last_event(s)
    ok(edit(s, foreign, {'urgent': True}))
    assert sent(s, after) == [('focus_edited', 'focus_cards', foreign['id'], 'Owner edited Focus urgent flag: Peer reference', {s.ordubashy.id, other.id})]
    assert not visible(s, peer, after) and [e['source_id'] for e in visible(s, other, after)] == [foreign['id']]


def test_marketing_peer_reference_notifies_its_author_without_a_configured_coordinator(inbox):
    s = board(inbox)
    peer = marketing_peer(s)
    card, _ = peer_present(s, peer)
    with s.db.transaction() as u:
        u.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
    after = last_event(s)
    ok(edit(s, card, {'deferred_until': UNTIL}))
    assert sent(s, after) == [('focus_snoozed', 'focus_cards', card['id'], 'Owner snoozed Focus until ' + UNTIL + ': Peer reference', {peer.id})]
    item, = visible(s, peer, after)
    assert item['source_id'] == card['id']
    assert not visible(s, s.ordubashy, after)
    after = last_event(s)
    ok(edit(s, card, {'deferred_until': None}))
    assert sent(s, after) == [('focus_resumed', 'focus_cards', card['id'], 'Owner resumed Focus: Peer reference', {peer.id})]


def test_marketing_peer_authors_without_current_peer_authority_get_nothing(inbox):
    s = board(inbox)
    card, _ = peer_present(s, marketing_peer(s))
    ungranted, business = marketing_peer(s, ['read_global_context']), s.agent
    for author in (ungranted, business):
        with s.db.transaction() as u:
            u.connection.execute('UPDATE focus_cards SET created_by=? WHERE id=?', (author.id, card['id']))
        after = last_event(s)
        ok(edit(s, card, {'urgent': True}))
        assert sent(s, after) == [('focus_edited', 'focus_cards', card['id'], 'Owner edited Focus urgent flag: Peer reference', {s.ordubashy.id})]
        assert not visible(s, author, after)
        ok(edit(s, card, {'urgent': False}))
    # Authority lost before the edit: the author is not a recipient any more.
    peer = marketing_peer(s)
    with s.db.transaction() as u:
        u.connection.execute('UPDATE focus_cards SET created_by=? WHERE id=?', (peer.id, card['id']))
    set_global_capabilities(s, peer, ['read_global_context'])
    after = last_event(s)
    ok(edit(s, card, {'urgent': True}))
    assert sent(s, after) == [('focus_edited', 'focus_cards', card['id'], 'Owner edited Focus urgent flag: Peer reference', {s.ordubashy.id})]


def test_marketing_peer_queued_events_are_reauthorized_on_every_read(inbox):
    s = board(inbox)
    peer = marketing_peer(s)
    card, _ = peer_present(s, peer)
    after = last_event(s)
    ok(edit(s, card, {'deferred_until': UNTIL}))
    assert sent(s, after) == [('focus_snoozed', 'focus_cards', card['id'], 'Owner snoozed Focus until ' + UNTIL + ': Peer reference', {s.ordubashy.id, peer.id})]
    assert [e['source_id'] for e in visible(s, peer, after)] == [card['id']]
    # Capability revocation hides the queued event, and regranting restores it.
    set_global_capabilities(s, peer, ['read_global_context'])
    assert not visible(s, peer, after)
    set_global_capabilities(s, peer, PEER_CAPABILITIES)
    assert [e['source_id'] for e in visible(s, peer, after)] == [card['id']]
    # A denied Focus read hides it without touching the coordinator's copy.
    s.change_override('focus_get', 'deny', actor=peer)
    assert not visible(s, peer, after)
    assert [e['source_id'] for e in visible(s, s.ordubashy, after)] == [card['id']]
    s.change_override('focus_get', 'allow', actor=peer)
    assert [e['source_id'] for e in visible(s, peer, after)] == [card['id']]
    # Deactivation hides it too, even for a context that was built while the peer was still active.
    held = s.agent_context(peer)
    with s.db.transaction() as u:
        u.connection.execute('UPDATE principals SET active=0 WHERE id=?', (peer.id,))
    with s.db.read() as u:
        assert not s.inbox.pending(held, u, after=after)['items']


def test_source_bound_reference_routes_to_its_source_without_reference_content(inbox):
    s = board(inbox)
    task(s)
    assign_agent(s, 'task', 'owner-task', s.agent)
    card = present(s, reference={'key': 'preview', 'title': 'Reference headline', 'target': {'type': 'url', 'url': 'https://example.com/private'}},
        context={'type': 'task', 'id': 'owner-task', 'business_id': s.business}, display_name=SECRET)
    after = last_event(s)
    ok(edit(s, card, {'deferred_until': UNTIL, 'target': {'type': 'url', 'url': 'https://example.com/other'}}))
    events = sent(s, after)
    assert events == [('focus_snoozed,focus_edited', 'tasks', 'owner-task',
        'Owner snoozed Focus until ' + UNTIL + ' and edited Focus opening link: Owner work', {s.ordubashy.id, s.agent.id})]
    assert not [e for e in sent(s, after) if e[1] == 'focus_cards']


def test_global_task_reaches_only_global_readers(inbox):
    s = board(inbox)
    with s.db.transaction() as u:
        u.actor_id = s.owner.id
        u.repository('tasks', business_id=None).insert({'id': 'global-task', 'title': 'Global work', 'status': 'open', 'created_by': s.owner.id})
    # One Global assignee holds Global read; the business-scoped agent is assigned but cannot read Global.
    reader = s.add_agent('global-marketing')
    set_global_capabilities(s, reader, ['read_global_context'])
    assign_agent(s, 'task', 'global-task', reader, business=None)
    assign_agent(s, 'task', 'global-task', s.agent, business=None)
    with s.db.read() as u:
        edges = u.connection.execute("SELECT business_id,to_id FROM relationships WHERE from_type='task' AND from_id='global-task'").fetchall()
    assert {(e[0], e[1]) for e in edges} == {(None, reader.id), (None, s.agent.id)}
    card = present(s, {'type': 'task', 'id': 'global-task', 'business_id': None})
    after = last_event(s)
    ok(edit(s, card, {'urgent': True}))
    # The coordinator and the Global reader each get the one event; the business agent gets none.
    assert sent(s, after) == [('focus_edited', 'tasks', 'global-task', 'Owner edited Focus urgent flag: Global work', {s.ordubashy.id, reader.id})]
    for agent in (s.ordubashy, reader):
        item, = [e for e in visible(s, agent, after) if e['event_type'] == 'focus_edited']
        assert item['business_id'] is None and item['source_id'] == 'global-task'
    # Without Global capabilities the assignment no longer delivers, and the pending event is hidden on read.
    set_global_capabilities(s, reader, [])
    after = last_event(s)
    ok(edit(s, card, {'urgent': False}))
    assert sent(s, after) == [('focus_edited', 'tasks', 'global-task', 'Owner edited Focus urgent flag: Global work', {s.ordubashy.id})]
    assert not [e for e in visible(s, reader) if e['event_type'] == 'focus_edited']


def test_story_and_signoff_route_to_their_canonical_rows(inbox):
    s = board(inbox)
    created = ok(s.app.invoke(s.owner_context, 'stories_create', {'title': 'Campaign', 'purpose': 'Review assets'}, {'idempotency_key': new_id()}))
    story = created['story']
    assign_agent(s, 'story', story['id'], s.agent)
    card = present(s, {'type': 'story', 'id': story['id'], 'business_id': s.business})
    after = last_event(s)
    ok(edit(s, card, {'deferred_until': UNTIL}))
    assert sent(s, after) == [('focus_snoozed', 'stories', story['id'], 'Owner snoozed Focus until ' + UNTIL + ': Campaign', {s.ordubashy.id, s.agent.id})]
    with s.db.read() as u:
        revision = u.connection.execute('SELECT revision FROM stories WHERE id=?', (story['id'],)).fetchone()[0]
        assert u.connection.execute('SELECT source_revision FROM inbox_events WHERE id>?', (after,)).fetchone()[0] == revision

    proposal = s.propose('Approve this')
    card = present(s, {'type': 'signoff', 'id': proposal['id'], 'business_id': s.business})
    after = last_event(s)
    ok(edit(s, card, {'urgent': True}))
    events = sent(s, after)
    assert len(events) == 1 and events[0][:3] == ('focus_edited', 'signoffs', proposal['id'])
    assert events[0][4] == {s.ordubashy.id, s.agent.id}  # The coordinator and the proposer.
    assert events[0][3].startswith('Owner edited Focus urgent flag: ')


def test_missing_source_falls_back_to_a_coordinator_only_card_event(inbox):
    s = board(inbox)
    task(s)
    assign_agent(s, 'task', 'owner-task', s.agent)
    card = present(s, {'type': 'task', 'id': 'owner-task', 'business_id': s.business}, display_name=SECRET)
    with s.db.transaction() as u:
        for table in ('relationships', 'task_changes'):
            u.connection.execute(f"DELETE FROM {table} WHERE {'from_id' if table == 'relationships' else 'task_id'}='owner-task'")
        u.connection.execute("DELETE FROM tasks WHERE id='owner-task'")
    after = last_event(s)
    ok(edit(s, card, {'deferred_until': UNTIL}))
    events = sent(s, after)
    assert events == [('focus_snoozed', 'focus_cards', card['id'], 'Owner snoozed Focus until ' + UNTIL + ': Focus card', {s.ordubashy.id})]
    # The Focus reader still refuses a card whose source is unavailable.
    assert not visible(s, s.ordubashy, after)
