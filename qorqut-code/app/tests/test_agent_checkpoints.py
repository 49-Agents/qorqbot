"""Completion checks cannot generate their own unbounded continuation chain."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from bazarlyk.bootstrap import activity_hook as hook
from qorqut_operations.contracts import DomainError, new_id
from inbox_support import claim
from test_service_transports import ServiceFixture
from test_agent_activity import ok


def local_turn(tmp_path):
    session, turn = new_id(), new_id()
    path = tmp_path / (session + '.json')
    value = {'checkpoint':hook.checkpoint_activity(
        {'hook_event_name':'UserPromptSubmit','turn_id':turn}, {})}
    native = {'session_id':session,'turn_id':turn,'stop_hook_active':False}
    return path, value, native, {'registration_id':new_id()}


def test_durable_receipt_survives_lost_observation_and_continuation(tmp_path):
    path, value, native, registration = local_turn(tmp_path)
    original = deepcopy(value)
    allowances = []
    def post(reg, body):
        allowances.append(body['allow_correction'])
        return {'state':'checked','correction':'Confirm current work.'}
    first = hook.checkpoint_stop(native,registration,path,value,post=post)
    assert first['decision'] == 'block'
    # Simulate an old non-durable activity snapshot after a restart. Its
    # separate fsynced turn receipt still prohibits another continuation.
    assert hook.checkpoint_stop(native,registration,path,original,post=post) == {}
    assert allowances == [True,False]
    continued = hook.checkpoint_activity({'hook_event_name':'UserPromptSubmit',
        'turn_id':new_id(),'prompt':first['reason']}, value['checkpoint'])
    assert continued['root_turn_id'] == native['turn_id'] and continued['spent']
    assert path.with_suffix('.checkpoint.json').stat().st_mode & 0o077 == 0


def test_unavailable_check_spends_once_and_native_flag_always_wins(tmp_path):
    path, value, native, registration = local_turn(tmp_path)
    def failed(*args):
        raise OSError('private transport detail')
    assert hook.checkpoint_stop(native,registration,path,value,post=failed) == {}
    assert value['checkpoint']['state'] == 'unavailable'
    assert hook.checkpoint_stop(native,registration,path,value,
        post=lambda r,b: {'correction':'Must not launch'}) == {}
    path, value, native, registration = local_turn(tmp_path)
    native['stop_hook_active'] = True
    seen = []
    assert hook.checkpoint_stop(native,registration,path,value,
        post=lambda r,b: seen.append(b['allow_correction']) or {'correction':'Never'}) == {}
    assert seen == [False]


def test_uncertain_lineage_or_durability_never_blocks(tmp_path, monkeypatch):
    path, value, native, registration = local_turn(tmp_path)
    native.pop('stop_hook_active')
    assert hook.checkpoint_stop(native,registration,path,value,
        post=lambda *a: pytest.fail('No inspection without native lineage')) == {}
    native['stop_hook_active'] = False
    def fail_write(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(hook,'write_state',fail_write)
    assert hook.checkpoint_stop(native,registration,path,value,
        post=lambda *a: pytest.fail('No claim before durable local receipt')) == {}


def test_action_watermark_excludes_bookkeeping_and_reads():
    turn = new_id()
    state = hook.checkpoint_activity({'hook_event_name':'UserPromptSubmit','turn_id':turn},{})
    for name in ('mcp__bazarlyk__agent_work_update','bazarlyk.tasks_comment',
                 'mcp__bazarlyk__inbox_acknowledge','tasks_get','mcp__bazarlyk__actions_record',
                 'bazarlyk.logs_add','mcp__bazarlyk__actions_list'):
        state = hook.checkpoint_activity({'hook_event_name':'PostToolUse','turn_id':turn,'tool_name':name}, state)
    assert state['dirty_sequence'] == 0
    state = hook.checkpoint_activity({'hook_event_name':'PostToolUse','turn_id':turn,'tool_name':'exec_command'},state)
    assert state['dirty_sequence'] == 1
    batch = {'hook_event_name':'PreToolUse','turn_id':turn,'tool_use_id':'batch1',
             'tool_name':'mcp__bazarlyk__operations_batch','tool_input':{'operations':[
                 {'operation':'tasks_complete'},{'operation':'agent_work_update'}]}}
    state = hook.checkpoint_activity(batch,state)
    reviewed_inside_batch = state['dirty_sequence']
    state = hook.checkpoint_activity({**batch,'hook_event_name':'PostToolUse'},state)
    assert state['dirty_sequence'] == reviewed_inside_batch == 2
    batch['tool_input']['operations'] = [{'operation':'agent_work_update'},{'operation':'inbox_acknowledge'}]
    for event in ('PreToolUse','PostToolUse'):
        state = hook.checkpoint_activity({**batch,'hook_event_name':event},state)
    assert state['dirty_sequence'] == 2
    # Saves answering a reminder, own notes, prepared reads and Claude's native
    # read/planning tools stay clean; a note for another agent is real work.
    for name, arguments in (('mcp__bazarlyk__working_memory_update',{}),('mcp__bazarlyk__tasks_update',{}),
            ('mcp__bazarlyk__notes_create',{'title':'Launch','body':'Shipped'}),('mcp__bazarlyk__notes_update',{}),
            ('mcp__bazarlyk__tasks_overview',{}),('mcp__bazarlyk__context_search',{}),
            ('Read',{}),('Grep',{}),('TodoWrite',{}),('Monitor',{}),('Agent',{})):
        state = hook.checkpoint_activity({'hook_event_name':'PostToolUse','turn_id':turn,'tool_name':name,
            'tool_input':arguments}, state, reads=frozenset({'tasks_overview','context_search'}))
    assert state['dirty_sequence'] == 2
    batch['tool_input']['operations'] = [{'operation':'working_memory_update'},
        {'operation':'notes_create','arguments':{'title':'Brief','body':'Hi','target_agent_id':new_id()}}]
    state = hook.checkpoint_activity({**batch,'tool_use_id':'batch2'},state)
    assert state['dirty_sequence'] == 3
    for name in ('Bash','Edit','mcp__outreach__campaign_send'):
        state = hook.checkpoint_activity({'hook_event_name':'PostToolUse','turn_id':turn,'tool_name':name},state)
    assert state['dirty_sequence'] == 6


def test_authoritative_episode_claim_review_and_recurrence(tmp_path, monkeypatch):
    s = ServiceFixture(tmp_path,full=True)
    ctx = s.runtime.resolver.invocation_context(s.agent)
    activity, checks = s.runtime.activity, s.runtime.checkpoints
    session, turn = new_id(), new_id()
    registration = s.registrations[s.agent.id]
    activity.register_session(ctx,registration,session,'codex')
    workspace = Path(s.fixture.resolver.resolve_agent_config(s.agent).data['workspace'])
    directory = workspace / '.bazarlyk/activity'
    directory.mkdir(parents=True,exist_ok=True)
    path = directory / (session + '.json')
    native = {'agent_id':s.agent.id,'registration_id':registration,'session_id':session,'runtime':'codex',
              'checkpoint':{'protocol':1,'turn_id':turn,'root_turn_id':turn,'dirty_sequence':1}}
    def save(): hook.write_state(path,native)
    def inspect(allow=True):
        return checks.check(ctx,registration,session,native['checkpoint']['turn_id'],
                            allow_correction=allow,claim_id=new_id() if allow else None)
    def call(name,args,revision=None):
        return ok(s.runtime.application.invoke(ctx,name,args,{'idempotency_key':new_id(),
                     'expected_revision':revision}))
    now = datetime(2026,9,15,12,tzinfo=timezone.utc)
    activity.clock = checks.clock = lambda:now
    task = call('tasks_create',{'title':'Pixel integration','assignee_ids':[s.agent.id]})['task']
    items = [{'business_id':s.fixture.business_id,'task_id':task['id'],'summary':'Verify pixel events'}]
    save()
    work = call('agent_work_update',{'session_id':session,'items':items,'expected_work_revision':1})
    assert inspect()['correction'] is None
    native['checkpoint']['dirty_sequence'] += 1; save()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:inspect(),range(2)))
    assert sum(bool(r['correction']) for r in results) == 1
    assert inspect()['correction'] is None
    # A continuation of the same root turn never re-presents it; a later
    # substantive root turn gets its own reminder, which replaces the ignored one.
    native['checkpoint']['turn_id'] = new_id(); save()
    assert inspect()['correction'] is None
    native['checkpoint'].update(turn_id=new_id(),root_turn_id=new_id()); save()
    assert inspect()['correction'] and inspect()['correction'] is None
    assert inspect(False)['outstanding_count'] == 1
    with s.fixture.db.read() as u:
        events_before = u.connection.execute('SELECT count(*) FROM inbox_events').fetchone()[0]
    now += timedelta(minutes=3)
    review = call('agent_work_update',{'session_id':session,'items':items,'expected_work_revision':work['work_revision']})
    assert review['work_updated_at'] == work['work_updated_at']
    assert review['work_reviewed_at'] != work['work_reviewed_at']
    assert inspect()['outstanding_count'] == 0
    with s.fixture.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM inbox_events').fetchone()[0] == events_before
        assert u.connection.execute('SELECT count(*) FROM task_notes').fetchone()[0] == 0
    call('tasks_complete',{'task_id':task['id']},1)
    native['checkpoint']['dirty_sequence'] += 1; save()
    result = inspect()
    assert {i['code'] for i in result['issues']} == {'finished_work_listed','work_review_missing'}
    assert result['correction']
    call('agent_work_update',{'session_id':session,'items':[],'expected_work_revision':review['work_revision']})
    assert inspect()['outstanding_count'] == 0
    with pytest.raises(DomainError,match='identity'):
        checks.check(ctx,registration,session,'not-a-turn')
    assert checks.check(ctx,s.registrations[s.manager.id],session,native['checkpoint']['turn_id'])['state']=='unobserved'


def configured_ordu_checkpoint(tmp_path):
    """Build the configured coordinator and one exact native inbox session."""
    from test_owner_private_conversations import create_ordubashy
    from types import SimpleNamespace
    from bazarlyk.agents.activity import AgentActivity
    from bazarlyk.agents.checkpoints import AgentCheckpoints
    from bazarlyk.inbox.private_conversations import PrivateConversations
    from bazarlyk.inbox.service import InboxService

    service = ServiceFixture(tmp_path)
    fixture = service.fixture
    inbox = InboxService(service.runtime.application, SimpleNamespace(), resolver=fixture.resolver).register()
    description = service.runtime.application.registry.get('inbox_list').description
    assert 'fresh reconciliation omit after' in description and 'notification watermark' in description
    PrivateConversations(inbox).register()
    service.runtime.inbox = inbox
    service.runtime.private_conversations = service.runtime.application.private_conversations
    service.runtime.activity = AgentActivity(service.runtime)
    service.runtime.activity.register()
    service.runtime.checkpoints = AgentCheckpoints(service.runtime)
    ordu = create_ordubashy(fixture, [fixture.business_id])
    with fixture.db.transaction() as uow:
        row = uow.repository('installation').get('service')
        uow.repository('installation').update('service',
            {'management_coordinator_id': ordu.id}, expected_revision=row['revision'])
    ready = fixture.prepare(ordu)
    registration = ready['registration_id']
    session, turn = new_id(), new_id()
    ctx = fixture.resolver.invocation_context(ordu)
    service.runtime.activity.register_session(ctx, registration, session, 'codex')
    claim(service.runtime.inbox, ctx, session)
    workspace = Path(fixture.resolver.resolve_agent_config(ordu).data['workspace'])
    path = workspace / '.bazarlyk/activity' / (session + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    native = {'agent_id': ordu.id, 'registration_id': registration, 'session_id': session,
              'runtime': 'codex', 'checkpoint': {'protocol': 1, 'turn_id': turn,
              'root_turn_id': turn, 'dirty_sequence': 0}}
    hook.write_state(path, native)
    return service, fixture, ordu, ctx, registration, session, turn, path, native


def invoke(app, ctx, operation, arguments):
    result = app.invoke(ctx, operation, arguments, {'idempotency_key': new_id()})
    assert result.status == 'ok', result.to_dict()
    return result.data


def test_owner_private_checkpoint_warns_on_current_pending_state_and_hides_shared_summary(tmp_path):
    (service, fixture, ordu, ctx, registration, session, turn, path,
     native) = configured_ordu_checkpoint(tmp_path)
    try:
        agent_ctx = fixture.resolver.invocation_context(service.agent)
        task = invoke(service.runtime.application, agent_ctx, 'tasks_create',
            {'title': 'Private source'})['task']
        source = {'type': 'task', 'id': task['id'], 'business_id': fixture.business_id}
        owner_ctx = fixture.ctx
        for index in range(4):
            invoke(service.runtime.application, owner_ctx, 'owner_conversation_send',
                {'source': source, 'text': f'Owner note {index}', 'summary': 'Owner update'})
        invoke(service.runtime.application, agent_ctx, 'agents_message_send',
            {'to_agent_id': ordu.id, 'scope': 'business',
             'message': 'Unrelated newer event'})
        with fixture.db.read() as uow:
            fresh = service.runtime.inbox.pending(ctx, uow, limit=20)['items']
        owner_events = [row for row in fresh if row['source_type'] == 'owner_conversations']
        unrelated = [row for row in fresh if row['source_type'] != 'owner_conversations']
        assert len(owner_events) == 4 and unrelated
        event_id = unrelated[-1]['id']
        invoke(service.runtime.application, ctx, 'inbox_acknowledge',
            {'consumer_id': session, 'event_ids': [event_id], 'outcome': 'Handled test update'})

        # A fresh bounded pass with pagination starts from the oldest event even
        # though a later unrelated delivery was already acknowledged.
        first = invoke(service.runtime.application, ctx, 'inbox_list',
                       {'consumer_id': session, 'limit': 2})
        second = invoke(service.runtime.application, ctx, 'inbox_list',
                        {'consumer_id': session, 'after': first['next_cursor'], 'limit': 2})
        assert len(first['items']) == len(second['items']) == 2
        assert all(row['source_type'] == 'owner_conversations'
                   for row in first['items'] + second['items'])
        assert second['next_cursor'] is None

        checks = service.runtime.checkpoints
        result = checks.check(ctx, registration, session, turn, allow_correction=True, claim_id=new_id())
        assert result['correction'] == 'There are pending owner messages; reconcile and handle or explicitly defer them.'
        assert {item['code'] for item in result['issues']} == {'pending_owner_messages'}
        assert all('Owner update' not in item['message'] and task['id'] not in item['message']
                   for item in result['issues'])
        hard_stop = hook.checkpoint_stop(
            {'session_id': session, 'turn_id': turn, 'stop_hook_active': True},
            {'registration_id': registration}, path, native,
            post=lambda _registration, body: checks.check(ctx, registration, session, body['turn_id'],
                allow_correction=body['allow_correction'], claim_id=body['claim_id']))
        assert hard_stop == {}
        with fixture.db.read() as uow:
            summary = checks.summary(ctx, ordu.id,
                {'checkpoint_state': 'checked', 'checkpoint_checked_at': None}, uow)
        assert summary['issues'] == [] and summary['outstanding_count'] == 0
        with fixture.db.read() as uow:
            owner_summary = checks.summary(fixture.ctx, ordu.id,
                {'checkpoint_state': 'checked', 'checkpoint_checked_at': None}, uow)
        assert owner_summary['outstanding_count'] == 1
        assert owner_summary['issues'][0]['code'] == 'pending_owner_messages'

        # Reading the private thread is silent and leaves all four events pending.
        business_ctx = fixture.resolver.invocation_context(ordu, business_id=fixture.business_id)
        invoke(service.runtime.application, business_ctx, 'owner_conversation_get', {'source': source})
        native['checkpoint'].update(turn_id=new_id(), root_turn_id=new_id())
        hook.write_state(path, native)
        repeated = checks.check(ctx, registration, session, native['checkpoint']['turn_id'],
                                allow_correction=True, claim_id=new_id())
        assert repeated['correction'] is None
        assert {item['code'] for item in repeated['issues']} == {'pending_owner_messages'}

        # Revoking the canonical source read hides the event and resolves the
        # generic warning without disclosing why it disappeared.
        fixture.update(ordu, {'tool_policy': {'overrides': {
            'owner_conversation_get': 'allow', 'owner_conversation_send': 'allow', 'tasks_get': 'deny'}}})
        native['checkpoint'].update(turn_id=new_id(), root_turn_id=new_id())
        hook.write_state(path, native)
        revoked = checks.check(ctx, registration, session, native['checkpoint']['turn_id'])
        assert revoked['issues'] == [] and revoked['outstanding_count'] == 0
        fixture.update(ordu, {'tool_policy': {'overrides': {
            'owner_conversation_get': 'allow', 'owner_conversation_send': 'allow', 'tasks_get': 'allow'}}})
        native['checkpoint'].update(turn_id=new_id(), root_turn_id=new_id())
        hook.write_state(path, native)
        restored = checks.check(ctx, registration, session, native['checkpoint']['turn_id'],
                                allow_correction=True, claim_id=new_id())
        assert restored['correction'] == 'There are pending owner messages; reconcile and handle or explicitly defer them.'

        invoke(service.runtime.application, ctx, 'inbox_acknowledge',
            {'consumer_id': session, 'event_ids': [row['id'] for row in owner_events],
             'outcome': 'Handled test owner updates'})
        native['checkpoint'].update(turn_id=new_id(), root_turn_id=new_id())
        hook.write_state(path, native)
        cleared = checks.check(ctx, registration, session, native['checkpoint']['turn_id'])
        assert cleared['outstanding_count'] == 0 and cleared['issues'] == []
    finally:
        service.close()


def test_owner_private_checkpoint_fails_closed_when_coordinator_or_session_is_not_current(tmp_path):
    (service, fixture, ordu, ctx, registration, session, turn, path,
     native) = configured_ordu_checkpoint(tmp_path)
    try:
        agent_ctx = fixture.resolver.invocation_context(service.agent)
        task = invoke(service.runtime.application, agent_ctx, 'tasks_create',
            {'title': 'Private source'})['task']
        source = {'type': 'task', 'id': task['id'], 'business_id': fixture.business_id}
        invoke(service.runtime.application, fixture.ctx, 'owner_conversation_send',
            {'source': source, 'text': 'Private pending note'})
        noncoordinator = service.manager
        other_ctx = fixture.resolver.invocation_context(noncoordinator)
        other_registration = service.registrations[noncoordinator.id]
        other_session, other_turn = new_id(), new_id()
        service.runtime.activity.register_session(other_ctx, other_registration, other_session, 'codex')
        claim(service.runtime.inbox, other_ctx, other_session)
        other_workspace = Path(fixture.resolver.resolve_agent_config(noncoordinator).data['workspace'])
        other_path = other_workspace / '.bazarlyk/activity' / (other_session + '.json')
        other_path.parent.mkdir(parents=True, exist_ok=True)
        hook.write_state(other_path, {'agent_id': noncoordinator.id,
            'registration_id': other_registration, 'session_id': other_session, 'runtime': 'codex',
            'checkpoint': {'protocol': 1, 'turn_id': other_turn, 'root_turn_id': other_turn,
                           'dirty_sequence': 0}})
        other_result = service.runtime.checkpoints.check(other_ctx, other_registration,
            other_session, other_turn, allow_correction=True, claim_id=new_id())
        assert all(item['code'] != 'pending_owner_messages' for item in other_result['issues'])

        with fixture.db.transaction() as uow:
            row = uow.repository('installation').get('service')
            uow.repository('installation').update('service', {'management_coordinator_id': None},
                                                   expected_revision=row['revision'])
        hidden = service.runtime.checkpoints.check(ctx, registration, session, turn,
                                                   allow_correction=True, claim_id=new_id())
        assert hidden['correction'] is None and hidden['issues'] == []

        # Restore the exact trusted coordinator, then make the session stale.
        with fixture.db.transaction() as uow:
            row = uow.repository('installation').get('service')
            uow.repository('installation').update('service', {'management_coordinator_id': ordu.id},
                                                   expected_revision=row['revision'])
            uow.connection.execute('UPDATE inbox_watchers SET consumer_id=? WHERE agent_id=?',
                                    (new_id(), ordu.id))
        with pytest.raises(DomainError, match='newer session'):
            service.runtime.checkpoints.check(ctx, registration, session, turn,
                                               allow_correction=True, claim_id=new_id())
    finally:
        service.close()


def test_owner_private_checkpoint_requires_effective_read_inbox_source_and_session_ownership(tmp_path):
    (service, fixture, ordu, ctx, registration, session, turn, path,
     native) = configured_ordu_checkpoint(tmp_path)
    try:
        agent_ctx = fixture.resolver.invocation_context(service.agent)
        task = invoke(service.runtime.application, agent_ctx, 'tasks_create',
            {'title': 'Private source'})['task']
        source = {'type': 'task', 'id': task['id'], 'business_id': fixture.business_id}
        invoke(service.runtime.application, fixture.ctx, 'owner_conversation_send',
            {'source': source, 'text': 'Private pending note'})
        checks = service.runtime.checkpoints

        def checkpoint():
            native['checkpoint'].update(turn_id=new_id(), root_turn_id=new_id())
            hook.write_state(path, native)
            return checks.check(ctx, registration, session, native['checkpoint']['turn_id'],
                                allow_correction=True, claim_id=new_id())

        def assert_hidden():
            result = checkpoint()
            assert result['correction'] is None
            assert all(item['code'] != 'pending_owner_messages' for item in result['issues'])

        def assert_restored():
            result = checkpoint()
            assert result['correction'] == (
                'There are pending owner messages; reconcile and handle or explicitly defer them.')
            assert {item['code'] for item in result['issues']} == {'pending_owner_messages'}

        def set_override(operation, mode):
            profile, _ = fixture.resolver.profile(ordu)
            overrides = dict(profile.data.get('tool_policy', {}).get('overrides', {}))
            overrides[operation] = mode
            fixture.update(ordu, {'tool_policy': {'overrides': overrides}})

        # Inbox listing and private conversation reads are separate effective
        # permissions; either denial suppresses the reminder without exposing
        # the pending private source.
        set_override('inbox_list', 'deny')
        assert_hidden()
        set_override('inbox_list', 'allow')
        assert_restored()

        set_override('owner_conversation_get', 'deny')
        assert_hidden()
        set_override('owner_conversation_get', 'allow')
        assert_restored()

        with fixture.db.transaction() as uow:
            uow.connection.execute('UPDATE business_memberships SET active=0 WHERE principal_id=? AND business_id=?',
                                   (ordu.id, fixture.business_id))
        assert_hidden()
        with fixture.db.transaction() as uow:
            uow.connection.execute('UPDATE business_memberships SET active=1 WHERE principal_id=? AND business_id=?',
                                   (ordu.id, fixture.business_id))
        assert_restored()

        with fixture.db.transaction() as uow:
            uow.connection.execute('DELETE FROM inbox_watchers WHERE agent_id=?', (ordu.id,))
        assert_hidden()
        claim(service.runtime.inbox, ctx, session)
        assert_restored()
    finally:
        service.close()


def test_owner_pending_lookup_paginates_both_orders_and_keeps_page_memory_bounded(tmp_path):
    from datetime import datetime, timezone
    from time import perf_counter

    (service, fixture, ordu, ctx, registration, session, turn, path,
     native) = configured_ordu_checkpoint(tmp_path)
    try:
        agent_ctx = fixture.resolver.invocation_context(service.agent)
        task = invoke(service.runtime.application, agent_ctx, 'tasks_create',
            {'title': 'Private source'})['task']
        source = {'type': 'task', 'id': task['id'], 'business_id': fixture.business_id}

        def insert_invisible(count):
            now = datetime.now(timezone.utc).isoformat()
            with fixture.db.transaction() as uow:
                for _ in range(count):
                    event = uow.connection.execute(
                        "INSERT INTO inbox_events(business_id,source_type,source_id,event_type,summary,visibility_json,created_at) "
                        "VALUES(?,'owner_conversations',?,'private_message','Private message','{}',?)",
                        (fixture.business_id, 'missing-' + new_id(), now)).lastrowid
                    uow.connection.execute('INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)',
                                           (ordu.id, event))

        def pending(page_size):
            with fixture.db.read() as uow:
                return service.runtime.inbox.pending_owner_conversations(
                    ctx, session, uow, page_size=page_size)

        def visible_private_event(conversation_id):
            with fixture.db.read() as uow:
                return uow.connection.execute(
                    "SELECT e.id FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                    "WHERE d.agent_id=? AND e.source_type='owner_conversations' AND e.source_id=? "
                    "AND d.acknowledged_at IS NULL ORDER BY e.id DESC LIMIT 1",
                    (ordu.id, conversation_id)).fetchone()[0]

        # An older visible delivery still wins over more than 100 newer revoked
        # or missing sources, even when the keyset walk crosses several pages.
        insert_invisible(5)
        older = invoke(service.runtime.application, fixture.ctx, 'owner_conversation_send',
            {'source': source, 'text': 'Older authorized message'})
        conversation_id = older['conversation']['id']
        insert_invisible(125)
        checked = []
        original_visible = service.runtime.inbox.event_visible
        service.runtime.inbox.event_visible = lambda context, row, uow: (
            checked.append(row['id']) or original_visible(context, row, uow))
        assert pending(7) is True
        assert len(checked) == 6  # Five invisible sources, then short-circuit.
        service.runtime.inbox.event_visible = original_visible

        invoke(service.runtime.application, ctx, 'inbox_acknowledge',
            {'consumer_id': session, 'event_ids': [visible_private_event(conversation_id)], 'outcome': 'Handled test event'})
        newer = invoke(service.runtime.application, fixture.ctx, 'owner_conversation_send',
            {'source': source, 'text': 'Newer authorized message'})
        assert newer['conversation']['id'] == conversation_id
        checked = []
        service.runtime.inbox.event_visible = lambda context, row, uow: (
            checked.append(row['id']) or original_visible(context, row, uow))
        assert pending(11) is True
        assert len(checked) == 131  # Older invisible sources are paged before the visible event.
        service.runtime.inbox.event_visible = original_visible
        invoke(service.runtime.application, ctx, 'inbox_acknowledge',
            {'consumer_id': session, 'event_ids': [visible_private_event(conversation_id)], 'outcome': 'Handled test event'})
        assert pending(13) is False  # All remaining candidates are currently invisible.

        # Add a larger all-invisible backlog and observe that each SQL result
        # retains only one page in memory. The total scan remains finite.
        insert_invisible(1000)
        page_sizes = []

        class CursorSpy:
            def __init__(self, cursor, sql):
                self.cursor, self.sql = cursor, sql

            def fetchall(self):
                rows = self.cursor.fetchall()
                if 'AND e.id>? AND e.id<=?' in self.sql:
                    page_sizes.append(len(rows))
                return rows

            def fetchone(self):
                return self.cursor.fetchone()

            def __iter__(self):
                return iter(self.cursor)

            def __getattr__(self, name):
                return getattr(self.cursor, name)

        class ConnectionSpy:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, parameters=()):
                return CursorSpy(self.connection.execute(sql, parameters), sql)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        with fixture.db.read() as uow:
            uow.connection = ConnectionSpy(uow.connection)
            started = perf_counter()
            result = service.runtime.inbox.pending_owner_conversations(ctx, session, uow)
            elapsed = perf_counter() - started
        assert result is False
        assert sum(page_sizes) == 1130
        assert page_sizes and max(page_sizes) <= 100 and len(page_sizes) > 10
        print(f'Scanned 1,130 invisible private deliveries in {elapsed:.4f}s using {len(page_sizes)} bounded pages.')
    finally:
        service.runtime.inbox.event_visible = original_visible if 'original_visible' in locals() else service.runtime.inbox.event_visible
        service.close()


ORIGINAL_STOP = hook.checkpoint_stop


class NativeSession:
    """Drive one prepared session's hook events as Claude Code or Codex sends them."""
    def __init__(self, workspace, registration, runtime):
        self.workspace, self.registration, self.runtime = workspace, registration, runtime
        self.session, self.turn = new_id(), None

    def send(self, event, **fields):
        if self.runtime == 'codex':  # Claude payloads carry no turn ID.
            self.turn = new_id() if event == 'UserPromptSubmit' else self.turn
            fields.setdefault('turn_id', self.turn)
        return hook.record({'session_id':self.session,'cwd':str(self.workspace),'hook_event_name':event,
                            **fields}, self.registration, self.runtime)

    def tool(self, name, **tool_input):
        tool_id = new_id()
        for event in ('PreToolUse','PostToolUse'):
            self.send(event, tool_name=name, tool_use_id=tool_id, tool_input=tool_input)

    @property
    def checkpoint(self):
        path = self.workspace / '.bazarlyk/activity' / (self.session + '.json')
        return json.loads(path.read_text()).get('checkpoint', {})

    @property
    def receipt(self):
        return self.workspace / '.bazarlyk/activity' / (self.session + '.checkpoint.json')


def native_workspace(tmp_path, monkeypatch, runtime, **registration):
    workspace = tmp_path / 'agent'; (workspace / '.bazarlyk').mkdir(parents=True)
    path = workspace / '.bazarlyk/registration.json'
    path.write_text(json.dumps({'workspace':str(workspace),'agent_id':new_id(),'registration_id':new_id(),
        'service_url':'http://127.0.0.1:9','checkpoint_protocol':{runtime:1},**registration}))
    monkeypatch.setattr(hook, 'session_owner', lambda *_: hook.process_identity(os.getpid()))
    import bazarlyk.bootstrap.technical_reporting as technical
    reports = []
    monkeypatch.setattr(technical, 'record', lambda reg, session, component, operation, code, **_: reports.append(code))
    monkeypatch.setattr(technical, 'replay_http', lambda *a, **k: None)
    monkeypatch.setattr(technical, 'active', lambda *a, **k: False)
    return NativeSession(workspace, path, runtime), reports


def fake_service(monkeypatch, session):
    """Answer like the service: a reminder only when allowed and the saved turn is dirty."""
    from bazarlyk.agents.checkpoints import REMINDER
    posts = []
    def post(registration, body):
        posts.append(body['allow_correction'])
        dirty = session.checkpoint['dirty_sequence']  # Persisted before the request.
        return {'state':'checked','correction':REMINDER if body['allow_correction'] and dirty else None}
    monkeypatch.setattr(hook, 'checkpoint_stop', lambda *a, **k: ORIGINAL_STOP(*a, post=post, **k))
    return posts


def test_claude_activity_continues_in_subfolder_and_other_directory(tmp_path, monkeypatch):
    session, _ = native_workspace(tmp_path, monkeypatch, 'claude-code')
    session.send('SessionStart')  # A first binding at the registered root stays supported.
    activity = session.workspace / '.bazarlyk/activity' / (session.session + '.json')
    initial = json.loads(activity.read_text())
    assert initial['agent_id'] and initial['registration_id'] == json.loads(session.registration.read_text())['registration_id']

    subfolder = session.workspace / 'research'
    checkout = tmp_path / 'business-checkout'
    subfolder.mkdir()
    checkout.mkdir()
    for cwd in (subfolder, checkout):
        assert session.send('PostToolUse', cwd=str(cwd), tool_name='Read') == {}
        observed = json.loads(activity.read_text())
        assert observed['owner'] == initial['owner']
        assert observed['agent_id'] == initial['agent_id']
        assert observed['registration_id'] == initial['registration_id']
        assert observed['session_id'] == session.session
        assert observed['runtime'] == 'claude-code' and observed['state'] == 'running'


def test_claude_stop_after_cwd_change_reaches_one_correction_path(tmp_path, monkeypatch):
    session, reports = native_workspace(tmp_path, monkeypatch, 'claude-code')
    posts = fake_service(monkeypatch, session)
    session.send('SessionStart')
    session.send('UserPromptSubmit', prompt='Update the launch page')
    checkout = tmp_path / 'business-checkout'
    checkout.mkdir()
    tool_id = new_id()
    for event in ('PreToolUse', 'PostToolUse'):
        session.send(event, cwd=str(checkout), tool_name='Bash', tool_use_id=tool_id,
                     tool_input={'command':'make preview'})

    stopped = session.send('Stop', cwd=str(checkout), stop_hook_active=False)
    assert stopped['decision'] == 'block'
    assert session.checkpoint['dirty_sequence'] == 1
    assert session.checkpoint['state'] == 'correction_sent'
    assert session.send('Stop', cwd=str(checkout), stop_hook_active=False) == {}
    assert posts == [True, False] and reports == []


def test_changed_claude_cwd_without_prior_binding_rejects_before_stop(tmp_path, monkeypatch):
    session, _ = native_workspace(tmp_path, monkeypatch, 'claude-code')
    calls = []
    monkeypatch.setattr(hook, 'checkpoint_stop', lambda *args, **kwargs: calls.append(True))
    other = tmp_path / 'business-checkout'
    other.mkdir()
    with pytest.raises(ValueError, match='workspace mismatch'):
        session.send('Stop', cwd=str(other), stop_hook_active=False)
    assert not (session.workspace / '.bazarlyk/activity' / (session.session + '.json')).exists()
    assert calls == []


@pytest.mark.parametrize('field,value', [
    ('agent_id', 'different-agent'),
    ('registration_id', 'different-registration'),
    ('session_id', 'different-session'),
    ('runtime', 'codex'),
    ('boot_id', 'stale-boot'),
    ('pid_namespace', 'stale-pid-namespace'),
])
def test_changed_claude_cwd_rejects_mismatched_saved_observation(tmp_path, monkeypatch, field, value):
    session, _ = native_workspace(tmp_path, monkeypatch, 'claude-code')
    session.send('SessionStart')
    session.send('UserPromptSubmit', prompt='Work')
    activity = session.workspace / '.bazarlyk/activity' / (session.session + '.json')
    saved = json.loads(activity.read_text())
    saved[field] = value
    hook.write_state(activity, saved)
    before = activity.read_bytes()
    calls = []
    monkeypatch.setattr(hook, 'checkpoint_stop', lambda *args, **kwargs: calls.append(True))
    other = tmp_path / 'business-checkout'
    other.mkdir()

    with pytest.raises(ValueError, match='workspace mismatch'):
        session.send('Stop', cwd=str(other), stop_hook_active=False)
    assert activity.read_bytes() == before and calls == []


@pytest.mark.parametrize('owner_change', ['different-pid', 'different-start'])
def test_changed_claude_cwd_rejects_different_native_owner(tmp_path, monkeypatch, owner_change):
    session, _ = native_workspace(tmp_path, monkeypatch, 'claude-code')
    session.send('SessionStart')
    session.send('UserPromptSubmit', prompt='Work')
    activity = session.workspace / '.bazarlyk/activity' / (session.session + '.json')
    pid, start = json.loads(activity.read_text())['owner'].split(':')
    current_owner = '999999:1' if owner_change == 'different-pid' else f'{pid}:{int(start) + 1}'
    before = activity.read_bytes()
    monkeypatch.setattr(hook, 'session_owner', lambda *_: current_owner)
    monkeypatch.setattr(hook, 'owner_alive', lambda owner: True)
    calls = []
    monkeypatch.setattr(hook, 'checkpoint_stop', lambda *args, **kwargs: calls.append(True))
    other = tmp_path / 'business-checkout'
    other.mkdir()

    with pytest.raises(ValueError, match='workspace mismatch'):
        session.send('Stop', cwd=str(other), stop_hook_active=False)
    assert activity.read_bytes() == before and calls == []


def test_changed_claude_cwd_rejects_offline_observation_and_dead_owner(tmp_path, monkeypatch):
    session, _ = native_workspace(tmp_path, monkeypatch, 'claude-code')
    session.send('SessionStart')
    session.send('UserPromptSubmit', prompt='Work')
    session.send('SessionEnd')
    activity = session.workspace / '.bazarlyk/activity' / (session.session + '.json')
    before = activity.read_bytes()
    calls = []
    monkeypatch.setattr(hook, 'checkpoint_stop', lambda *args, **kwargs: calls.append(True))
    other = tmp_path / 'business-checkout'
    other.mkdir()
    with pytest.raises(ValueError, match='workspace mismatch'):
        session.send('Stop', cwd=str(other), stop_hook_active=False)
    assert json.loads(activity.read_text())['state'] == 'offline'
    assert activity.read_bytes() == before and calls == []

    # A current owner token is insufficient when the process/start identity is dead.
    session.send('SessionStart')
    before = activity.read_bytes()
    monkeypatch.setattr(hook, 'owner_alive', lambda owner: False)
    with pytest.raises(ValueError, match='workspace mismatch'):
        session.send('Stop', cwd=str(other), stop_hook_active=False)
    assert activity.read_bytes() == before and calls == []


def test_codex_cwd_boundary_registration_path_and_unsafe_activity_paths_remain(tmp_path, monkeypatch):
    session, _ = native_workspace(tmp_path, monkeypatch, 'codex')
    session.send('SessionStart')
    activity = session.workspace / '.bazarlyk/activity' / (session.session + '.json')
    before = activity.read_bytes()
    other = tmp_path / 'business-checkout'
    other.mkdir()
    with pytest.raises(ValueError, match='workspace mismatch'):
        session.send('PostToolUse', cwd=str(other), tool_name='Bash')
    assert activity.read_bytes() == before

    alternative = session.workspace / '.bazarlyk/other-registration.json'
    alternative.write_text(session.registration.read_text())
    with pytest.raises(ValueError, match='workspace mismatch'):
        hook.record({'hook_event_name':'SessionStart', 'session_id':session.session,
                     'cwd':str(session.workspace)}, alternative, 'codex')

    outside = tmp_path / 'activity-target'
    outside.mkdir()
    directory = session.workspace / '.bazarlyk/activity'
    directory.rename(session.workspace / '.bazarlyk/activity-original')
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='unsafe directory'):
        hook.record({'hook_event_name':'SessionStart', 'session_id':new_id(),
                     'cwd':str(session.workspace)}, session.registration, 'codex')


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
def test_one_reminder_per_substantive_root_turn(tmp_path, monkeypatch, runtime):
    from bazarlyk.agents.checkpoints import REMINDER
    session, reports = native_workspace(tmp_path, monkeypatch, runtime, read_operations=['tasks_overview'])
    posts = fake_service(monkeypatch, session)
    # A greeting that only reads gets no reminder.
    session.send('UserPromptSubmit', prompt='Hello')
    for name in ('Read','Grep','Monitor','mcp__bazarlyk__tasks_overview','mcp__bazarlyk__inbox_list'):
        session.tool(name)
    assert session.checkpoint['dirty_sequence'] == 0
    assert session.send('Stop', stop_hook_active=False) == {}
    greeting = session.checkpoint['root_turn_id']
    session.send('UserPromptSubmit', prompt='Publish the landing copy')
    session.tool('Bash', command='make publish')
    first = session.checkpoint['root_turn_id']
    assert first != greeting and session.checkpoint['dirty_sequence'] == 1
    blocked = session.send('Stop', stop_hook_active=False)
    assert blocked['decision'] == 'block' and blocked['reason'].endswith(REMINDER)
    assert 'actions_record' in REMINDER and '20 words' in REMINDER
    # Saves made in response, including the one completed-action record, keep the same root turn clean.
    for name, arguments in (('working_memory_update',{}),('agent_work_update',{'patch':[]}),
            ('notes_create',{'title':'Launch','body':'Shipped'}),('tasks_update',{}),
            ('tasks_comment',{}),('inbox_acknowledge',{}),('actions_record',{'text':'Published landing copy'})):
        session.tool('mcp__bazarlyk__' + name, **arguments)
    assert session.checkpoint['dirty_sequence'] == 1 and session.checkpoint['root_turn_id'] == first
    assert session.send('Stop', stop_hook_active=True) == {}
    assert session.send('Stop', stop_hook_active=False) == {}  # Duplicate Stop.
    assert posts == [True, True, False, False]
    assert json.loads(session.receipt.read_text())['root_turn_id'] == first
    # A later substantive turn may be reminded again, even after an ignored one.
    # Claude's notification-started turn begins at its first action after Stop.
    if runtime == 'codex':
        session.send('UserPromptSubmit', prompt='Inbox notice')
    session.tool('Edit', file_path=str(tmp_path / 'draft.md'))
    assert session.checkpoint['root_turn_id'] not in {greeting, first}
    assert session.send('Stop', stop_hook_active=False)['decision'] == 'block'
    assert posts[-1] is True and reports == []


@pytest.mark.parametrize('protocol', [{'codex':1}, {'claude-code':0}, 1, None])
def test_disabled_client_keeps_soft_hint_and_never_checkpoints(tmp_path, monkeypatch, protocol):
    session, _ = native_workspace(tmp_path, monkeypatch, 'claude-code', checkpoint_protocol=protocol)
    posts = fake_service(monkeypatch, session)
    event = {'session_id':session.session,'cwd':str(session.workspace),'hook_event_name':'PostToolUse','tool_name':'Bash'}
    assert hook.record(event, session.registration, 'claude-code', now=1000) == {}
    assert hook.record(event, session.registration, 'claude-code', now=1900)['hookSpecificOutput']['additionalContext']
    assert session.send('Stop', stop_hook_active=False) == {} and posts == [] and session.checkpoint == {}


@pytest.mark.parametrize('runtime', ['claude-code', 'codex'])
def test_crash_unavailable_service_and_stale_registration_never_block(tmp_path, monkeypatch, runtime):
    import socket, threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    session, reports = native_workspace(tmp_path, monkeypatch, runtime)
    # Forced termination before Stop leaves no receipt; the service is never asked.
    posts = fake_service(monkeypatch, session)
    session.send('UserPromptSubmit', prompt='Work')
    session.tool('Bash')
    assert session.send('SessionEnd') == {} and posts == [] and not session.receipt.exists()
    # A hook killed during its request has already spent this root turn locally.
    def killed(registration, body):
        raise KeyboardInterrupt
    monkeypatch.setattr(hook, 'checkpoint_stop', lambda *a, **k: ORIGINAL_STOP(*a, post=killed, **k))
    session.send('UserPromptSubmit', prompt='Resume')
    session.tool('Bash')
    with pytest.raises(KeyboardInterrupt):
        session.send('Stop', stop_hook_active=False)
    assert json.loads(session.receipt.read_text())['root_turn_id'] == session.checkpoint['root_turn_id']
    posts = fake_service(monkeypatch, session)
    assert session.send('Stop', stop_hook_active=False) == {} and posts == [False]

    # Real transport: an unavailable service or a revoked credential allows stopping.
    monkeypatch.setattr(hook, 'checkpoint_stop', ORIGINAL_STOP)
    credential = session.workspace / '.bazarlyk/credential'
    credential.write_text('c' * 48); credential.chmod(0o600)
    closed = socket.socket(); closed.bind(('127.0.0.1', 0)); port = closed.getsockname()[1]; closed.close()
    class Revoked(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps({'status':'error','error':{'code':'unauthenticated','message':'private'}}).encode()
            self.send_response(401); self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Revoked)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for url, code in (('http://127.0.0.1:%d' % port, 'checkpoint_unavailable'),
                          ('http://127.0.0.1:%d' % server.server_address[1], 'invalid_credential')):
            registration = json.loads(session.registration.read_text())
            session.registration.write_text(json.dumps({**registration, 'service_url':url}))
            session.send('UserPromptSubmit', prompt='More work')
            session.tool('Write', file_path=str(tmp_path / 'notes.md'))
            assert session.send('Stop', stop_hook_active=False) == {}
            assert session.checkpoint['state'] == 'unavailable' and reports[-1] == code
    finally:
        server.shutdown()


def test_prepared_claude_session_through_service_route(tmp_path, monkeypatch):
    import socket, threading, time
    import uvicorn
    from bazarlyk.agents.checkpoints import REMINDER
    from test_working_memory import save
    s = ServiceFixture(tmp_path, full=True)
    server = None
    try:
        app, checks = s.runtime.application, s.runtime.checkpoints
        ctx = s.runtime.resolver.invocation_context(s.manager)
        workspace = Path(s.fixture.resolver.resolve_agent_config(s.manager).data['workspace'])
        path = workspace / '.bazarlyk/registration.json'
        prepared = json.loads(path.read_text())
        # Preparation enables the supported Claude baseline and keeps Codex pinned.
        assert prepared['checkpoint_protocol'] == {'claude-code': 1, 'codex': 0}
        assert {'context_search', 'inbox_list', 'tasks_get'} <= set(prepared['read_operations'])
        assert not {'tasks_update', 'working_memory_update', 'agent_work_update'} & set(prepared['read_operations'])
        # Enabling the protocol adds no hook entry to either client.
        def counts(hooks):
            return {event: sum(len(group['hooks']) for group in groups) for event, groups in hooks.items()}
        assert counts(json.loads((workspace / '.claude/settings.local.json').read_text())['hooks']) == {
            'SessionStart': 2, 'UserPromptSubmit': 2, 'PreToolUse': 1, 'PostToolUse': 1,
            'PostToolUseFailure': 1, 'PermissionRequest': 1, 'Stop': 2, 'SessionEnd': 2}
        assert counts(json.loads((workspace / '.codex/hooks.json').read_text())['hooks']) == {
            'SessionStart': 2, 'UserPromptSubmit': 2, 'PreToolUse': 1, 'PostToolUse': 1,
            'PermissionRequest': 1, 'Stop': 2, 'SessionEnd': 2, 'Interrupt': 1}
        from bazarlyk.service.web import create_app
        listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(16)
        origin = 'http://127.0.0.1:%d' % listener.getsockname()[1]
        path.write_text(json.dumps({**prepared, 'service_url': origin}))
        web = create_app(s.runtime, origins=[origin], authentication=s.authentication)
        server = uvicorn.Server(uvicorn.Config(web, log_level='error', access_log=False, lifespan='off'))
        threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True).start()
        deadline = time.monotonic() + 30
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        session, reports = NativeSession(workspace, path, 'claude-code'), []
        import bazarlyk.bootstrap.technical_reporting as technical
        monkeypatch.setattr(technical, 'record', lambda reg, sid, component, operation, code, **_: reports.append(code))
        monkeypatch.setattr(technical, 'replay_http', lambda *a, **k: None)
        monkeypatch.setattr(hook, 'session_owner', lambda *_: hook.process_identity(os.getpid()))
        s.runtime.activity.register_session(ctx, s.registrations[s.manager.id], session.session, 'claude-code')
        memory = ok(app.invoke(ctx, 'working_memory_get', {}))['document']
        assert memory is None
        assert save(s, 'Launch copy drafted.', actor=s.manager).status == 'ok'
        memory = ok(app.invoke(ctx, 'working_memory_get', {}))['document']

        session.send('UserPromptSubmit', prompt='Create the launch task')
        session.tool('mcp__bazarlyk__tasks_create', title='Launch')
        blocked = session.send('Stop', stop_hook_active=False)
        assert blocked['decision'] == 'block' and blocked['reason'].endswith(REMINDER)
        # Two responses race on working memory: existing CAS picks one; the
        # reminder itself never writes memory, notes or current work.
        session.tool('mcp__bazarlyk__working_memory_update')
        with ThreadPoolExecutor(max_workers=2) as pool:
            saves = list(pool.map(lambda body: save(s, body, memory, actor=s.manager),
                                  ['Launch task created.', 'Stale copy of the handoff.']))
        assert sorted(r.status for r in saves) == ['error', 'ok']
        assert save(s, 'Stale copy of the handoff.', memory, actor=s.manager).error['code'] == 'revision_conflict'
        assert ok(app.invoke(ctx, 'working_memory_get', {}))['document']['revision'] == memory['revision'] + 1
        session.tool('mcp__bazarlyk__agent_work_update', patch=[])
        ok(app.invoke(ctx, 'agent_work_update', {'session_id': session.session, 'patch': [],
                                                  'expected_work_revision': 1}, {'idempotency_key': new_id()}))
        assert session.checkpoint['dirty_sequence'] == 1
        assert session.send('Stop', stop_hook_active=True) == {}
        assert session.checkpoint['state'] == 'checked'
        with s.fixture.db.read() as u:
            assert u.connection.execute("SELECT count(*) FROM agent_checkpoint_findings WHERE agent_id=? "
                                        "AND state='open'", (s.manager.id,)).fetchone()[0] == 0
            assert u.connection.execute('SELECT count(*) FROM task_notes').fetchone()[0] == 0

        # A greeting that only reads gets no reminder from the service either.
        session.send('UserPromptSubmit', prompt='Hi')
        session.tool('mcp__bazarlyk__context_search', query='launch')
        assert session.send('Stop', stop_hook_active=False) == {} and reports == []

        # A superseded session or a revoked credential (stale registration) lets
        # the agent stop and reports through the existing technical outbox.
        claim(s.runtime.inbox, ctx, new_id())
        session.send('UserPromptSubmit', prompt='Old terminal work')
        session.tool('Bash', command='make lint')
        assert session.send('Stop', stop_hook_active=False) == {}
        assert reports == ['inbox_session_superseded']
        with s.fixture.db.transaction() as u:
            u.connection.execute('DELETE FROM inbox_watchers WHERE agent_id=?', (s.manager.id,))
            u.connection.execute("UPDATE credentials SET revoked_at='2026-01-01T00:00:00Z' WHERE principal_id=?",
                                 (s.manager.id,))
        session.send('UserPromptSubmit', prompt='More work')
        session.tool('Bash', command='make deploy')
        assert session.send('Stop', stop_hook_active=False) == {}
        assert session.checkpoint['state'] == 'unavailable'
        assert reports == ['inbox_session_superseded', 'invalid_credential']
    finally:
        if server is not None:
            server.should_exit = True
        s.close()


def test_checkpoint_enabled_for_verified_codex_versions_only():
    from bazarlyk.bootstrap.preparation import checkpoint_protocols
    reports = {"claude-code": {"supported": False}}
    for version, expected in (("codex-cli 0.154.0", 1), ("codex-cli 0.156.1", 1), ("codex-cli 0.156.0", 0), ("", 0)):
        assert checkpoint_protocols({"codex": version}, reports)["codex"] == expected
