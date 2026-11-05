"""Owner and Ordubashy private conversations: identity, binding, privacy."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

import pytest

from qorqut_operations.contracts import Principal, new_id, plain
from bazarlyk.transport.mcp_presentation import present
from test_service_transports import ServiceFixture
from test_signoff_inputs import FORM

PRIVATE = 'Only you and I should read this line about the pricing floor.'
REPLY = 'Understood; I will relay only the decision to the team.'


def call(s, ctx, operation, args, *, key=None, revision=None):
    options = {'idempotency_key': key or new_id()}
    if revision is not None:
        options['expected_revision'] = revision
    return s.app.invoke(ctx, operation, args, options)


def ok(result):
    assert result.status == 'ok', result.to_dict()
    return plain(result.data)


def error(result):
    assert result.status == 'error', result.to_dict()
    return result.error['code']


def create_ordubashy(fixture, businesses):
    """The registered Ordubashy must span every business, so build its scope here."""
    from qorqut_operations.contracts import InvocationOptions
    arguments = {'name': 'ordu', 'display_name': 'Ordu', 'role': fixture.refs['ordubashy'], 'runtime': 'codex',
        'assignment_brief': 'Own measurable progress with current evidence.',
        # Grant the private channel explicitly so core tests do not depend on the
        # companion role branch already carrying its default grants.
        'tool_policy': {'overrides': {'owner_conversation_get': 'allow', 'owner_conversation_send': 'allow'}},
        'scope': {'kind': 'ecosystem', 'allowed_business_ids': sorted(businesses),
                  'global_capabilities': sorted(fixture.ctx.global_capabilities)}}
    with fixture.db.read() as uow:
        plan = fixture.ops.prepare('agents_create', fixture.ctx, arguments,
                                   InvocationOptions(idempotency_key=new_id()), uow)
    fixture.apply_files(plan, fixture.ops.resolve_file_target)
    with fixture.db.transaction() as uow:
        result = fixture.ops.apply(fixture.ctx, plan, uow)
    return Principal(result['agent_id'], 'agent', 'Ordu')


@pytest.fixture
def s(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    fixture = service.fixture
    service.app = service.runtime.application
    service.db = fixture.db

    other = new_id()
    (fixture.root / 'second/hq').mkdir(parents=True)
    with fixture.db.transaction() as uow:
        uow.repository('businesses').insert({'id': other, 'slug': 'second', 'name': 'Second',
            'workspace': str(fixture.root / 'second'), 'hq_path': str(fixture.root / 'second/hq'), 'stage': 'validating'})
        uow.repository('business_memberships', business_id=other).insert({'principal_id': fixture.owner.id, 'is_owner': True})
        # Business creation extends every Ordubashy to the new business.
        from bazarlyk.agents.ordubashy import additions_for_business
        for row, updated in additions_for_business(uow.connection, other, fixture.registry):
            uow.repository('agent_profiles', business_id=None).update(row['agent_id'],
                {'profile_json': updated}, expected_revision=row['revision'])
            uow.repository('business_memberships', business_id=other).insert({'principal_id': row['agent_id']})
    service.other_business = other
    fixture.ctx = replace(fixture.ctx, allowed_business_ids=frozenset({fixture.business_id, other}))

    service.ordu = create_ordubashy(fixture, [fixture.business_id, other])
    with fixture.db.transaction() as uow:
        repository = uow.repository('installation')
        row = repository.get('service')
        repository.update('service', {'management_coordinator_id': service.ordu.id}, expected_revision=row['revision'])

    teammate = Principal(new_id(), 'human', 'Teammate')
    with fixture.db.transaction() as uow:
        uow.repository('principals').insert({'id': teammate.id, 'kind': 'human', 'display_name': 'Teammate'})
        uow.repository('business_memberships', business_id=fixture.business_id).insert(
            {'principal_id': teammate.id, 'is_owner': True})
    service.teammate = teammate

    service.owner = fixture.ctx
    service.owner_global = replace(fixture.ctx, business_id=None, review_business_id=None)
    service.owner_other = replace(fixture.ctx, business_id=other, review_business_id=other)
    service.teammate_ctx = replace(fixture.ctx, principal=teammate,
        allowed_business_ids=frozenset({fixture.business_id}), global_capabilities=frozenset())
    service.coordinator = service.runtime.resolver.invocation_context(service.ordu)
    service.coordinator_business = service.runtime.resolver.invocation_context(
        service.ordu, business_id=fixture.business_id)
    service.agent_ctx = service.runtime.resolver.invocation_context(service.agent)
    # A second Ordubashy that is not the configured coordinator.
    service.bystander = service.runtime.resolver.invocation_context(service.manager, business_id=fixture.business_id)
    return service


def task(s, *, global_scope=False, **fields):
    ctx = s.owner_global if global_scope else s.owner
    args = {'title': 'Pricing review', **fields, **({'scope': 'global'} if global_scope else {})}
    row = ok(call(s, ctx, 'tasks_create', args))['task']
    return {'type': 'task', 'id': row['id'], 'business_id': None if global_scope else s.fixture.business_id}, row


def story(s):
    row = ok(call(s, s.owner, 'stories_create', {'title': 'Pilot', 'purpose': 'Learn what customers pay'}))['story']
    return {'type': 'story', 'id': row['id'], 'business_id': s.fixture.business_id}, row


def signoff(s, *, global_scope=False):
    ctx = s.coordinator if global_scope else s.agent_ctx
    args = {'request_key': 'global-q' if global_scope else 'q', 'form': FORM,
            **({'scope': 'global'} if global_scope else {})}
    row = ok(call(s, ctx, 'signoffs_input_create', args))['signoff']
    return {'type': 'signoff', 'id': row['id'], 'business_id': None if global_scope else s.fixture.business_id}, row


def email(s, business_id=None):
    business_id = business_id or s.fixture.business_id
    identity = new_id()
    with s.db.transaction() as uow:
        mailbox = 'mail-' + business_id[:8]
        if not uow.repository('email_mailboxes', business_id=business_id).get(mailbox):
            uow.repository('email_mailboxes', business_id=business_id).insert(
                {'id': mailbox, 'business_id': business_id, 'address': mailbox + '@example.test'})
        uow.repository('email_conversations', business_id=business_id).insert(
            {'id': identity, 'business_id': business_id, 'mailbox': mailbox, 'provider_id': 'thread-' + identity,
             'subject': 'A conversation', 'last_message_at': '2026-09-08T12:00:00Z'})
    return {'type': 'email', 'id': identity, 'business_id': business_id}, identity


def reference(s, key='ref-a', **extra):
    item = ok(call(s, s.owner_global, 'focus_present', {'scope': 'global', 'reference': {'key': key,
        'title': 'Landing page', 'target': {'type': 'url', 'url': 'https://example.com/' + key}}, **extra}))['item']
    return {'type': 'reference', 'id': item['id'], 'business_id': None}, item


def scoped(ctx, args):
    """A Global context must say so; a business context binds its own business."""
    return {**({'scope': 'global'} if ctx.business_id is None else {}), **args}


def send(s, ctx, source, text=PRIVATE, *, key=None, **extra):
    return call(s, ctx, 'owner_conversation_send', scoped(ctx, {'source': source, 'text': text, **extra}), key=key)


def get(s, ctx, source, **extra):
    return call(s, ctx, 'owner_conversation_get', scoped(ctx, {'source': source, **extra}))


def global_args(source):
    """A Global invocation names the owning business inside the source."""
    return {'scope': 'global', 'source': source}


def counts(s):
    with s.db.read() as uow:
        return {name: uow.connection.execute('SELECT count(*) FROM ' + name).fetchone()[0]
                for name in ('owner_conversations', 'owner_conversation_messages', 'owner_conversation_reads',
                             'inbox_events', 'inbox_deliveries', 'inbox_changes')}


# ----- read/write matrix ------------------------------------------------------

def test_every_supported_source_is_private_to_the_owner_and_the_coordinator(s):
    business_task, _ = task(s)
    global_task, _ = task(s, global_scope=True, title='Global follow-up')
    business_story, _ = story(s)
    business_signoff, _ = signoff(s)
    global_signoff, _ = signoff(s, global_scope=True)
    thread, _ = email(s)
    card, _ = reference(s)
    globals_ = {id(global_task), id(global_signoff), id(card)}
    for source in (business_task, global_task, business_story, business_signoff, global_signoff, thread, card):
        owner_ctx = s.owner_global if source['business_id'] is None else s.owner
        args = {'scope': 'global'} if source['business_id'] is None else {}
        assert ok(call(s, owner_ctx, 'owner_conversation_send',
            {**args, 'source': source, 'text': PRIVATE}))['message']['author_role'] == 'owner'
        coordinator = ok(call(s, s.coordinator, 'owner_conversation_send',
            {'scope': 'global', 'source': source, 'text': REPLY}))['message']
        assert coordinator['author_role'] == 'coordinator' and coordinator['is_owner'] is False
        read = ok(call(s, owner_ctx, 'owner_conversation_get', {**args, 'source': source}))
        assert [m['body'] for m in read['messages']] == [PRIVATE, REPLY]
        assert read['conversation']['audience'] == 'owner_ordubashy'
        assert {p['created_by'] for p in read['participants']} == {s.fixture.owner.id, s.ordu.id}
        for outsider in (s.agent_ctx, s.bystander, s.teammate_ctx):
            # The role policy refuses an ungranted tool; the pair ACL refuses another
            # Ordubashy or a human who is merely a business owner. None reads a word.
            assert error(call(s, outsider, 'owner_conversation_get', {'source': source, 'agent_id':s.ordu.id})) in {'forbidden', 'policy_denied'}
            assert error(send(s, outsider, source, agent_id=s.ordu.id)) in {'forbidden', 'policy_denied'}


def test_reference_bound_to_a_context_is_discussed_under_its_source(s):
    source, _ = task(s)
    _, item = reference(s, 'ref-context', context=source)
    card = {'type': 'reference', 'id': item['id'], 'business_id': None}
    assert error(get(s, s.owner_global, card)) == 'validation_error'
    assert error(send(s, s.owner_global, card)) == 'validation_error'
    assert ok(call(s, s.owner, 'owner_conversation_send', {'source': source, 'text': PRIVATE}))['message']['sequence'] == 1


def test_outsiders_cannot_read_write_or_acknowledge(s):
    source, _ = task(s)
    ok(send(s, s.owner, source))
    for outsider in (s.agent_ctx, s.bystander, s.teammate_ctx):
        # Role policy may refuse an unlisted tool first; the pair ACL refuses
        # anyone who reaches the handler.
        assert error(get(s, outsider, source, agent_id=s.ordu.id)) in {'forbidden', 'policy_denied'}
        assert error(send(s, outsider, source, agent_id=s.ordu.id)) in {'forbidden', 'policy_denied'}
    for outsider in (s.coordinator, s.teammate_ctx):
        assert call(s, outsider, 'owner_conversation_mark_read',
            {**scoped(outsider, {}), 'source': source, 'sequence': 1}).status == 'error'


# ----- scope ------------------------------------------------------------------

def test_global_calls_reselect_the_named_business_and_reject_mismatches(s):
    source, _ = task(s)
    assert ok(call(s, s.coordinator, 'owner_conversation_send', global_args(source) | {'text': REPLY}))['message']['sequence'] == 1
    assert ok(call(s, s.owner_global, 'owner_conversation_get', global_args(source)))['conversation']['message_count'] == 1
    mismatched = {**source, 'business_id': s.other_business}
    assert error(call(s, s.owner, 'owner_conversation_get', {'source': mismatched})) == 'validation_error'
    assert error(call(s, s.owner, 'owner_conversation_send', {'source': mismatched, 'text': PRIVATE})) == 'validation_error'
    # The same object reached from its own business scope is one conversation.
    assert ok(call(s, s.owner, 'owner_conversation_get', {'source': {'type': 'task', 'id': source['id']}}))['conversation']['message_count'] == 1


def test_a_business_outside_the_caller_scope_is_unavailable(s):
    source, _ = task(s)
    outside = {**source, 'business_id': s.other_business}
    # The teammate cannot even reach the pair ACL; the coordinator reaches the
    # source check and finds no such task in the named business.
    assert error(call(s, s.coordinator, 'owner_conversation_get', global_args(outside))) == 'not_found'
    restricted = replace(s.owner_global, allowed_business_ids=frozenset({s.other_business}))
    assert error(call(s, restricted, 'owner_conversation_get', global_args(source))) == 'forbidden'
    assert error(call(s, s.owner_global, 'owner_conversation_get',
        global_args({'type': 'story', 'id': 'x', 'business_id': None}))) == 'validation_error'
    assert error(call(s, s.owner, 'owner_conversation_get',
        {'source': {'type': 'reference', 'id': 'x'}})) == 'validation_error'


def test_an_archived_business_closes_its_private_conversations(s):
    source, _ = task(s)
    ok(send(s, s.owner, source))
    with s.db.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at='2026-09-09T00:00:00Z' WHERE id=?",
                               (s.fixture.business_id,))
    assert error(call(s, s.owner_global, 'owner_conversation_get', global_args(source))) in {'forbidden', 'not_found'}
    assert error(call(s, s.coordinator, 'owner_conversation_get', global_args(source))) in {'forbidden', 'not_found'}
    assert error(call(s, s.coordinator, 'owner_conversation_send',
                      global_args(source) | {'text': REPLY})) in {'forbidden', 'not_found'}
    with s.db.read() as uow:
        assert not [item for item in s.runtime.inbox.pending(s.app.current_context(s.coordinator, uow), uow)['items']
                    if item['source_type'] == 'owner_conversations']


def test_a_signoff_is_private_only_inside_its_own_review_scope(s):
    source, _ = signoff(s)
    ok(send(s, s.owner, source))
    for wrong in ({**source, 'business_id': s.other_business}, {**source, 'business_id': None}):
        assert error(call(s, s.owner_global, 'owner_conversation_get', global_args(wrong))) == 'not_found'
        assert error(call(s, s.coordinator, 'owner_conversation_get', global_args(wrong))) == 'not_found'
    assert ok(call(s, s.owner_global, 'owner_conversation_get', global_args(source)))['conversation']['message_count'] == 1


# ----- coordinator identity ---------------------------------------------------

def set_profile_runtime(s, runtime):
    with s.db.transaction() as uow:
        row = uow.connection.execute('SELECT profile_json FROM agent_profiles WHERE agent_id=?', (s.ordu.id,)).fetchone()
        value = json.loads(row['profile_json'])
        value['runtime'] = runtime
        uow.connection.execute('UPDATE agent_profiles SET profile_json=? WHERE agent_id=?',
            (json.dumps(value), s.ordu.id))


def test_one_registered_identity_spans_runtimes_and_sessions(s):
    sources = [task(s)[0], story(s)[0], signoff(s)[0], email(s)[0], reference(s, 'runtime-ref')[0]]
    for source in sources:
        owner_ctx = s.owner_global if source['business_id'] is None else s.owner
        ok(call(s, owner_ctx, 'owner_conversation_send', scoped(owner_ctx, {'source': source, 'text': PRIVATE})))
        ok(call(s, s.coordinator, 'owner_conversation_send', global_args(source) | {'text': REPLY}))
    set_profile_runtime(s, 'claude-code')
    with s.db.transaction() as uow:
        for index, runtime in enumerate(('claude-code', 'codex')):
            registration = new_id()
            uow.repository('machine_registrations').insert({'id': registration, 'principal_id': s.ordu.id,
                'workspace': str(s.fixture.root / ('session-%d' % index)), 'service_url': 'http://127.0.0.1:9988',
                'profile_revision': 1})
            uow.repository('agent_activity').insert({'agent_id': s.ordu.id, 'session_id': new_id(),
                'registration_id': registration, 'runtime': runtime, 'state': 'running',
                'last_seen_at': '2026-09-08T12:00:00Z'})
    coordinator = s.runtime.resolver.invocation_context(s.ordu)
    # Every binding stays readable and writable: the private channel asks who a
    # principal is, never which runtime carries the current session.
    for source in sources:
        read = ok(call(s, coordinator, 'owner_conversation_get', global_args(source)))
        assert [m['body'] for m in read['messages']] == [PRIVATE, REPLY] and read['read_state'] is None
        assert ok(call(s, coordinator, 'owner_conversation_send',
            global_args(source) | {'text': 'Still the same thread.'}))['conversation']['message_count'] == 3
    # The Codex-only management contract is unchanged.
    with s.db.read() as uow:
        assert s.runtime.management.coordinator_dto(uow)['configured'] is False
    assert error(call(s, s.agent_ctx, 'management_message_send', {'message': 'Please look'})) == 'management_unavailable'


def test_email_private_reads_survive_a_non_codex_coordinator_runtime(s):
    """Email keeps its own Codex-only management reader; the channel does not."""
    thread, _ = email(s)
    set_profile_runtime(s, 'claude-code')
    coordinator = s.runtime.resolver.invocation_context(s.ordu)
    ok(call(s, s.owner, 'owner_conversation_send', {'source': thread, 'text': PRIVATE}))
    read = ok(call(s, coordinator, 'owner_conversation_get', global_args(thread)))
    assert [m['body'] for m in read['messages']] == [PRIVATE]
    assert ok(call(s, coordinator, 'owner_conversation_send',
        global_args(thread) | {'text': REPLY}))['message']['author_role'] == 'coordinator'
    with s.db.read() as uow:
        items = s.runtime.inbox.pending(s.app.current_context(coordinator, uow), uow)['items']
        private = [item for item in items if item['source_type'] == 'owner_conversations']
        assert len(private) == 1 and private[0]['source'] == thread
        assert PRIVATE not in json.dumps(private)
        # The management intake keeps its runtime gate.
        assert s.runtime.management.coordinator_dto(uow)['configured'] is False


@pytest.mark.parametrize('breakage', ['inactive', 'missing', 'wrong_role'])
def test_owner_keeps_the_channel_when_the_coordinator_cannot_participate(s, breakage):
    source, _ = task(s)
    ok(send(s, s.owner, source, 'Earlier private note'))
    with s.db.transaction() as uow:
        if breakage == 'inactive':
            uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (s.ordu.id,))
        elif breakage == 'missing':
            uow.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
        else:
            uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'",
                (s.agent.id,))
    read = ok(get(s, s.owner, source))
    assert [m['body'] for m in read['messages']] == ['Earlier private note']
    assert [p['created_by'] for p in read['participants']] == [s.fixture.owner.id]
    before = counts(s)
    assert ok(send(s, s.owner, source, 'Stored with nobody to deliver to'))['message']['sequence'] == 2
    after = counts(s)
    assert after['owner_conversation_messages'] == before['owner_conversation_messages'] + 1
    assert after['inbox_events'] == before['inbox_events']
    if breakage != 'wrong_role':
        assert call(s, s.coordinator, 'owner_conversation_get', global_args(source)).status == 'error'


def test_client_cannot_choose_the_audience_or_forge_an_author(s):
    source, _ = task(s)
    for extra in ({'audience': 'owner_ordubashy'}, {'author_role': 'owner'}, {'created_by': s.agent.id},
                  {'participants': [s.agent.id]}):
        assert error(call(s, s.owner, 'owner_conversation_send',
            {'source': source, 'text': PRIVATE, **extra})) == 'validation_error'
        assert error(call(s, s.owner, 'owner_conversation_get', {'source': source, **extra})) == 'validation_error'
    assert error(call(s, s.owner, 'owner_conversation_send',
        {'source': {**source, 'audience': 'team'}, 'text': PRIVATE})) == 'validation_error'
    message = ok(call(s, s.coordinator, 'owner_conversation_send', global_args(source) | {'text': REPLY}))['message']
    assert message['author_role'] == 'coordinator' and message['created_by'] == s.ordu.id


# ----- binding stability ------------------------------------------------------

def test_reference_binding_survives_target_and_title_edits(s):
    source, item = reference(s, 'stable')
    ok(send(s, s.owner_global, source, 'Before the edit'))
    changed = ok(call(s, s.owner_global, 'focus_edit', {'scope': 'global', 'item_id': item['id'],
        'expected_card_revision': item['revision'], 'changes': {'title': 'Renamed',
        'target': {'type': 'url', 'url': 'https://example.com/elsewhere'}}}))['item']
    assert changed['id'] == item['id']
    read = ok(get(s, s.owner_global, source))
    assert [m['body'] for m in read['messages']] == ['Before the edit']
    with s.db.read() as uow:
        assert uow.connection.execute('SELECT source_key FROM owner_conversations').fetchone()[0] == 'reference:' + item['id']


def test_focus_card_and_native_view_share_one_conversation(s):
    source, row = task(s)
    ok(send(s, s.owner, source, 'From the object view'))
    item = ok(call(s, s.owner_global, 'focus_present', {'scope': 'global', 'source': source}))['item']
    assert item['conversation'] == source
    read = ok(call(s, s.owner_global, 'owner_conversation_get', global_args(item['conversation'])))
    assert [m['body'] for m in read['messages']] == ['From the object view']
    with s.db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM owner_conversations').fetchone()[0] == 1


# ----- storage behaviour ------------------------------------------------------

def test_reading_creates_nothing_and_sending_never_touches_the_source(s):
    business_task, task_row = task(s)
    business_story, story_row = story(s)
    business_signoff, signoff_row = signoff(s)
    thread, thread_id = email(s)
    card_source, card = reference(s, 'untouched')
    empty = ok(get(s, s.owner, business_task))
    assert empty['conversation']['id'] is None and empty['conversation']['revision'] == 0
    assert empty['messages'] == [] and empty['next_before'] is None
    assert empty['read_state'] == {'last_read_sequence': 0, 'unread': False}
    assert counts(s)['owner_conversations'] == 0 and counts(s)['owner_conversation_reads'] == 0
    with s.db.read() as uow:
        board = uow.connection.execute("SELECT value FROM change_sequences WHERE name='focus_board'").fetchone()[0]
        email_revision = uow.repository('email_conversations', business_id=s.fixture.business_id).get(thread_id)['revision']
        card_revision = uow.connection.execute('SELECT revision FROM focus_cards WHERE id=?', (card['id'],)).fetchone()[0]
        signoff_revision = uow.connection.execute('SELECT status_revision FROM signoffs WHERE id=?',
            (signoff_row['id'],)).fetchone()[0]
    for source in (business_task, business_story, business_signoff, thread, card_source):
        ctx = s.owner_global if source['business_id'] is None else s.owner
        args = {'scope': 'global'} if source['business_id'] is None else {}
        ok(call(s, ctx, 'owner_conversation_send', {**args, 'source': source, 'text': PRIVATE}))
    with s.db.read() as uow:
        assert uow.repository('tasks', business_id=s.fixture.business_id).get(task_row['id'])['revision'] == task_row['revision']
        current_story = uow.repository('stories', business_id=s.fixture.business_id).get(story_row['id'])
        assert current_story['revision'] == story_row['revision'] and current_story['sequence'] == story_row['sequence']
        assert uow.connection.execute('SELECT status_revision FROM signoffs WHERE id=?',
            (signoff_row['id'],)).fetchone()[0] == signoff_revision
        assert uow.repository('email_conversations', business_id=s.fixture.business_id).get(thread_id)['revision'] == email_revision
        assert uow.connection.execute('SELECT revision FROM focus_cards WHERE id=?', (card['id'],)).fetchone()[0] == card_revision
        assert uow.connection.execute("SELECT value FROM change_sequences WHERE name='focus_board'").fetchone()[0] == board
        assert uow.connection.execute('SELECT count(*) FROM task_notes').fetchone()[0] == 0
        assert uow.connection.execute('SELECT count(*) FROM signoff_notes').fetchone()[0] == 0
        assert uow.connection.execute('SELECT count(*) FROM email_comments').fetchone()[0] == 0
        assert uow.connection.execute('SELECT count(*) FROM focus_notes').fetchone()[0] == 0


def test_pagination_conflicts_and_idempotent_retries(s):
    source, _ = task(s)
    for index in range(5):
        ok(send(s, s.owner, source, 'Message %d' % index, key='m%d' % index))
    page = ok(get(s, s.owner, source, limit=2))
    assert [m['body'] for m in page['messages']] == ['Message 3', 'Message 4'] and page['next_before'] == 4
    older = ok(get(s, s.owner, source, limit=2, before=page['next_before']))
    assert [m['body'] for m in older['messages']] == ['Message 1', 'Message 2'] and older['next_before'] == 2
    oldest = ok(get(s, s.owner, source, limit=2, before=older['next_before']))
    assert [m['body'] for m in oldest['messages']] == ['Message 0'] and oldest['next_before'] is None
    current = ok(get(s, s.owner, source))['conversation']
    assert current['revision'] == 6 and current['latest_sequence'] == 5 and current['message_count'] == 5
    stale = call(s, s.owner, 'owner_conversation_send',
        {'source': source, 'text': 'Stale', 'expected_conversation_revision': 2})
    assert stale.error['code'] == 'revision_conflict' and stale.error['current_revision'] == 6
    ok(call(s, s.owner, 'owner_conversation_send',
        {'source': source, 'text': 'Fresh', 'expected_conversation_revision': 6}))
    first = ok(send(s, s.owner, source, 'Retried once', key='retry'))['message']
    again = ok(send(s, s.owner, source, 'Retried once', key='retry'))['message']
    assert first == again
    assert ok(get(s, s.owner, source))['conversation']['message_count'] == 7


def test_concurrent_first_sends_converge_on_one_conversation(s):
    source, _ = task(s)
    def attempt(index):
        return send(s, s.owner, source, 'Concurrent %d' % index, key='c%d' % index)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert all(result.status == 'ok' for result in results), [r.to_dict() for r in results]
    with s.db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM owner_conversations').fetchone()[0] == 1
        assert uow.connection.execute('SELECT count(*) FROM owner_conversation_messages').fetchone()[0] == 4
    current = ok(get(s, s.owner, source))['conversation']
    assert current['latest_sequence'] == 4 and current['revision'] == 5


# ----- owner team-write guard on Focus ---------------------------------------

def test_owner_focus_reply_keeps_private_channel_outside_business_tasks_and_stories(s):
    bindings = []
    for source, _ in (task(s, global_scope=True, title='Global one'), signoff(s), email(s)):
        bindings.append(ok(call(s, s.owner_global, 'focus_present', {'scope': 'global', 'source': source}))['item'])
    bindings.append(reference(s, 'reply-ref')[1])
    for item in bindings:
        result = call(s, s.owner_global, 'focus_reply', {'scope': 'global', 'item_id': item['id'], 'text': 'Team note'})
        assert result.error['code'] == 'channel_required', result.to_dict()
        details = result.error['details']
        # `operation` is the private channel to use; `refused_operation` names
        # the team write that was refused, so a client keeps the right draft.
        assert details['channel'] == 'owner_ordubashy' and details['operation'] == 'owner_conversation_send'
        assert details['refused_operation'] == 'focus_reply'
    # The coordinator's team replies are unchanged.
    assert ok(call(s, s.coordinator, 'focus_reply',
        {'scope': 'global', 'item_id': bindings[-1]['id'], 'text': 'Reference note'}))['note']['body'] == 'Reference note'
    with s.db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM focus_notes').fetchone()[0] == 1


# ----- notifications ----------------------------------------------------------

def test_owner_message_delivers_once_to_the_coordinator_without_any_text(s):
    source, row = task(s)
    ok(call(s, s.owner, 'tasks_assign', {'task_id': row['id'], 'assignee_ids': [s.agent.id]}, revision=row['revision']))
    ok(call(s, s.agent_ctx, 'inbox_subscribe', {'source_type': 'tasks', 'source_id': row['id']}))
    before = counts(s)
    summary = 'Pricing floor'
    ok(send(s, s.owner, source, PRIVATE, summary=summary))
    with s.db.read() as uow:
        events = [dict(r) for r in uow.connection.execute(
            "SELECT * FROM inbox_events WHERE source_type='owner_conversations'")]
        assert len(events) == 1
        event = events[0]
        assert event['event_type'] == 'private_message' and event['summary'] == 'Private message from the owner'
        assert event['business_id'] == s.fixture.business_id and event['actor_id'] == s.fixture.owner.id
        assert json.loads(event['visibility_json']) == {'source': source}
        delivered = [r[0] for r in uow.connection.execute(
            'SELECT agent_id FROM inbox_deliveries WHERE event_id=?', (event['id'],))]
        assert delivered == [s.ordu.id]
        assert uow.connection.execute('SELECT count(*) FROM inbox_changes').fetchone()[0] == before['inbox_changes']
        blob = json.dumps([dict(r) for r in uow.connection.execute('SELECT * FROM inbox_events')])
        assert PRIVATE not in blob and summary not in blob
        assert row['title'] not in json.dumps(event)
        items = s.runtime.inbox.pending(s.app.current_context(s.coordinator, uow), uow)['items']
        private = [item for item in items if item['source_type'] == 'owner_conversations']
        assert len(private) == 1 and private[0]['source'] == source
        assert PRIVATE not in json.dumps(private) and summary not in json.dumps(private)


def test_coordinator_reply_notifies_nobody(s):
    source, row = task(s)
    ok(call(s, s.owner, 'tasks_assign', {'task_id': row['id'], 'assignee_ids': [s.agent.id]}, revision=row['revision']))
    ok(call(s, s.agent_ctx, 'inbox_subscribe', {'source_type': 'tasks', 'source_id': row['id']}))
    before = counts(s)
    ok(call(s, s.coordinator, 'owner_conversation_send', global_args(source) | {'text': REPLY}))
    after = counts(s)
    assert after['inbox_events'] == before['inbox_events']
    assert after['inbox_deliveries'] == before['inbox_deliveries']
    assert after['inbox_changes'] == before['inbox_changes']


def test_private_event_follows_the_coordinator_access_to_its_source(s):
    source, row = task(s)
    ok(send(s, s.owner, source))
    listed = ok(call(s, s.coordinator, 'inbox_list', {'consumer_id': claim(s)}))
    assert any(item['source_type'] == 'owner_conversations' for item in listed['items'])
    s.fixture.update(s.ordu, {'tool_policy': {'overrides': {'tasks_get': 'deny'}}})
    coordinator = s.runtime.resolver.invocation_context(s.ordu)
    with s.db.read() as uow:
        assert not [item for item in s.runtime.inbox.pending(s.app.current_context(coordinator, uow), uow)['items']
                    if item['source_type'] == 'owner_conversations']


def test_compact_mcp_projection_keeps_the_coordinators_source_pointer(s):
    """Guards the seam between InboxService.pending() (already correct, see
    bazarlyk/inbox/service.py) and the MCP compact projection: the real
    coordinator inbox result, run through the real present(), must keep the
    bound source object -- including business_id: None for a Global task."""
    source, row = task(s, global_scope=True, title='Global follow-up needing a decision')
    ok(send(s, s.owner_global, source))
    with s.db.read() as uow:
        items = s.runtime.inbox.pending(s.app.current_context(s.coordinator, uow), uow)['items']
    private = [item for item in items if item['source_type'] == 'owner_conversations']
    assert len(private) == 1 and private[0]['source'] == source
    wire = {'request_id': new_id(), 'status': 'ok', 'data': {'items': items}}
    projected = present('inbox_list', wire, 'compact')['data']['items']
    projected_private = [item for item in projected if item['source_type'] == 'owner_conversations']
    assert len(projected_private) == 1 and projected_private[0]['source'] == source


def test_private_conversations_cannot_be_subscribed_to(s):
    source, _ = task(s)
    ok(send(s, s.owner, source))
    with s.db.read() as uow:
        conversation = uow.connection.execute('SELECT id FROM owner_conversations').fetchone()[0]
    result = call(s, s.coordinator, 'inbox_subscribe',
        {'source_type': 'owner_conversations', 'source_id': conversation})
    # The subscribable sources are a closed enum, so this never reaches a handler.
    assert result.error['code'] == 'validation_error', result.to_dict()
    assert 'source_type' in result.error['field_errors']
    listed = ok(call(s, s.coordinator, 'inbox_subscriptions_list', {}))['items']
    assert not [item for item in listed if item['source_type'] == 'owner_conversations']


def claim(s):
    """Register a session inbox watcher for the coordinator."""
    consumer = new_id()
    registration = s.fixture.prepare(s.ordu)['registration_id']
    s.runtime.inbox.start_session(s.coordinator, registration, consumer, 'startup')
    return consumer


# ----- read cursor and Focus indicator ---------------------------------------

def test_read_cursor_is_owner_only_and_monotonic(s):
    source, _ = task(s)
    ok(send(s, s.owner, source))
    assert ok(get(s, s.owner, source))['read_state'] == {'last_read_sequence': 0, 'unread': False}
    ok(call(s, s.coordinator, 'owner_conversation_send', global_args(source) | {'text': REPLY}))
    assert ok(get(s, s.owner, source))['read_state']['unread'] is True
    assert ok(call(s, s.owner, 'owner_conversation_mark_read',
        {'source': source, 'sequence': 2}))['read_state'] == {'last_read_sequence': 2, 'unread': False, 'any_unread': False}
    assert ok(call(s, s.owner, 'owner_conversation_mark_read',
        {'source': source, 'sequence': 1}))['read_state']['last_read_sequence'] == 2
    assert error(call(s, s.owner, 'owner_conversation_mark_read',
        {'source': source, 'sequence': 3})) == 'validation_error'
    ok(call(s, s.coordinator, 'owner_conversation_send', global_args(source) | {'text': 'Another reply'}))
    assert ok(get(s, s.owner, source))['read_state'] == {'last_read_sequence': 2, 'unread': True}
    empty, _ = story(s)
    assert ok(call(s, s.owner, 'owner_conversation_mark_read',
        {'source': empty, 'sequence': 0}))['read_state'] == {'last_read_sequence': 0, 'unread': False, 'any_unread': False}
    assert counts(s)['owner_conversation_reads'] == 1
    assert call(s, s.coordinator, 'owner_conversation_mark_read',
        {'scope': 'global', 'source': source, 'sequence': 1}).status == 'error'


def test_concurrent_marks_keep_one_monotonic_cursor(s):
    source, _ = task(s)
    ok(send(s, s.owner, source))
    for index in range(3):
        ok(call(s, s.coordinator, 'owner_conversation_send',
                global_args(source) | {'text': 'Reply %d' % index}, key='r%d' % index))
    def attempt(sequence):
        return call(s, s.owner, 'owner_conversation_mark_read', {'source': source, 'sequence': sequence},
                    key='mark-%d' % sequence)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, (1, 4, 2, 3)))
    assert all(result.status == 'ok' for result in results), [r.to_dict() for r in results]
    # One cursor row, and an out-of-order acknowledgement never moves it back.
    assert counts(s)['owner_conversation_reads'] == 1
    assert ok(get(s, s.owner, source))['read_state'] == {'last_read_sequence': 4, 'unread': False}


def test_focus_shows_the_private_marker_only_for_the_unread_owner(s):
    source, _ = task(s)
    item = ok(call(s, s.owner_global, 'focus_present', {'scope': 'global', 'source': source}))['item']
    assert item['private_unread'] is False and item['private_message_count'] == 0
    ok(send(s, s.owner, source))
    ok(call(s, s.coordinator, 'owner_conversation_send', global_args(source) | {'text': REPLY}))
    board = ok(call(s, s.owner_global, 'focus_get', {'scope': 'global'}))['items'][0]
    assert board['private_unread'] is True and board['private_message_count'] == 2
    coordinator_board = ok(call(s, s.coordinator, 'focus_get', {'scope': 'global'}))['items'][0]
    assert coordinator_board['private_unread'] is False and coordinator_board['private_message_count'] == 2
    before = counts(s)
    ok(call(s, s.owner_global, 'focus_get', {'scope': 'global', 'item_id': item['id'], 'conversation': True}))
    assert counts(s) == before
    ok(call(s, s.owner, 'owner_conversation_mark_read', {'source': source, 'sequence': 2}))
    assert ok(call(s, s.owner_global, 'focus_get', {'scope': 'global'}))['items'][0]['private_unread'] is False


def test_private_messages_never_create_or_restore_a_card(s):
    source, _ = task(s)
    item = ok(call(s, s.owner_global, 'focus_present', {'scope': 'global', 'source': source}))['item']
    archived = ok(call(s, s.owner_global, 'focus_set_archived', {'scope': 'global', 'item_id': item['id'],
        'expected_card_revision': item['revision'], 'archived': True}))['item']
    ok(send(s, s.owner, source))
    ok(call(s, s.coordinator, 'owner_conversation_send', global_args(source) | {'text': REPLY}))
    assert ok(call(s, s.owner_global, 'focus_get', {'scope': 'global'}))['items'] == []
    with s.db.read() as uow:
        assert uow.connection.execute('SELECT archived,revision FROM focus_cards WHERE id=?',
            (item['id'],)).fetchone()[1] == archived['revision']
    other, _ = story(s)
    ok(send(s, s.owner, other))
    with s.db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM focus_cards').fetchone()[0] == 1


# ----- privacy sinks ----------------------------------------------------------

def test_private_text_stays_out_of_exports_learning_search_and_events(s):
    from bazarlyk.domains.learnings.sources import snapshots
    from bazarlyk.storage.exports import build_exports
    source, _ = task(s)
    ok(send(s, s.owner, source, PRIVATE, summary='Pricing floor'))
    ok(call(s, s.coordinator, 'owner_conversation_send', global_args(source) | {'text': REPLY}))
    exported = build_exports(s.db.path, s.fixture.root)
    body = json.dumps({path: content.decode() if isinstance(content, bytes) else content
                       for path, content in exported.files.items()})
    assert PRIVATE not in body, 'private text reached a shared export'
    assert REPLY not in body and 'Pricing floor' not in body
    # The private tables are not in any export catalog. (Operation names can
    # appear inside an exported agent profile's tool policy; message text cannot.)
    assert not [path for path in exported.files if 'owner_conversation' in path]
    with s.db.read() as uow:
        assert PRIVATE not in json.dumps(snapshots(uow, s.fixture.business_id, set()), default=str)
        rows = json.dumps([dict(r) for r in uow.connection.execute('SELECT * FROM inbox_events')])
        assert PRIVATE not in rows and REPLY not in rows and 'Pricing floor' not in rows
        assert not uow.connection.execute('SELECT count(*) FROM inbox_changes '
            "WHERE after_json LIKE '%pricing floor%'").fetchone()[0]
    found = ok(call(s, s.owner, 'context_search', {'query': 'pricing floor'}))
    assert PRIVATE not in json.dumps(found, default=str)
