from dataclasses import replace
import pytest
from qorqut_operations.contracts import new_id, plain
from test_signoffs import scenario
from test_management_backend import install_coordinator
from test_domain_tasks import make_tasks, invoke
from foundation_support import context


def test_owner_order_gate_with_legacy_role_and_explicit_allows(scenario, tmp_path):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from bazarlyk.agents.resolver import effective_tool_policy
    from bazarlyk.catalog.packages import load_package
    from qorqut_operations.contracts import DomainError, InvocationContext, InvocationOptions, Principal
    from bazarlyk.domains.common import PlanBuilder
    from bazarlyk.inbox.focus import FocusService
    from bazarlyk.policy import WorkerProfile, resolve_operation_policy
    import yaml

    s = scenario
    # Publish an isolated pre-upgrade role, including the now-inert old grant.
    original = s.catalog.get('role', s.roles['ordubashy'])
    if original.version=='1.55.3':
        old = original
        assert old.manifest['default_tool_policy']['focus_move']=='allow'
    else:
        source = tmp_path / 'old-role'
        for path, content in original.files.items():
            destination = source / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        manifest_path = source / 'roles/ordubashy/role.yaml'
        manifest = yaml.safe_load(manifest_path.read_text())
        manifest['version'] = '1.55.3'
        manifest['default_tool_policy']['focus_move'] = 'allow'
        manifest_path.write_text(yaml.safe_dump(manifest))
        old = load_package(source, 'roles/ordubashy/role.yaml', 'role', s.registry)
        target = s.catalog.artifact_path(old)
        for path, content in old.files.items():
            destination = target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        with s.db.transaction() as u:
            u.repository('artifacts').insert({'kind':'role','artifact_id':'ordubashy','version':old.version,
                'digest':old.digest,'stored_path':str(target),'metadata_json':{'manifest_path':'roles/ordubashy/role.yaml'}})
    s.roles['ordubashy'] = old.ref
    coordinator = install_coordinator(s)
    s.change_override('focus_move', 'allow', actor=coordinator)
    s.change_override('focus_move', 'allow', actor=s.agent)
    fake = SimpleNamespace(app=s.app, clock=lambda:datetime.now(timezone.utc),
        domains=SimpleNamespace(stories=SimpleNamespace(relationships=SimpleNamespace(items=None))))
    FocusService(fake, s.service).register()
    attention = s.app.attention
    owner = replace(s.owner_context, business_id=None, review_business_id=None)
    agent = replace(s.agent_context(coordinator), business_id=None, review_business_id=None)

    def call(ctx, op, args):
        result = s.app.invoke(ctx, op, {'scope':'global', **args}, {'idempotency_key':new_id()})
        assert result.status == 'ok', result.to_dict()
        return plain(result.data)

    def present(key):
        return call(agent, 'focus_present', {'reference':{'key':key,'title':key,
            'target':{'type':'url','url':'https://example.com/'+key}}})['item']

    a, b = present('a'), present('b')
    move = {'scope':'global','item_id':b['id'],'expected_card_revision':b['revision'],'before_id':a['id']}
    call(owner, 'focus_move', move)
    assert [row['id'] for row in call(agent, 'focus_get', {})['items']] == [b['id'],a['id']]
    # Reads/configuration remain usable with BOTH the old role and stored allow.
    profile, _ = s.resolver.profile(coordinator)
    tools = effective_tool_policy(profile, s.catalog.get('role', profile.data['role']), s.registry)
    tool = next(row for row in tools if row['canonical_operation']=='focus_move')
    assert tool['effective_mode']=='deny' and not tool['available']
    assert 'focus_move' not in [row['canonical_operation'] for row in tools if row['available']]
    decision = resolve_operation_policy('focus_move', {'focus_move':'allow'}, {'focus_move':'allow'}, s.registry)
    assert (decision.mode,decision.provenance)==('deny','focus_order_owner_only')

    # A system worker's explicit allow, even with owner initiation, is inert.
    worker = Principal(new_id(), 'system', 'Order worker')
    with s.db.transaction() as u:
        u.repository('principals').insert({'id':worker.id,'kind':worker.kind,'display_name':worker.display_name})
    s.worker_profiles[worker.id] = WorkerProfile({'focus_move':'allow'},
        global_capabilities=frozenset({'read_global_context'}))
    worker_context = InvocationContext(worker, source='worker', initiated_by_principal_id=s.owner.id)
    viewer = replace(owner, principal=s.viewer)
    # Giving another human business ownership or Global capabilities is insufficient.
    with s.db.transaction() as u:
        u.connection.execute('UPDATE business_memberships SET is_owner=1 WHERE principal_id=?', (s.viewer.id,))
    current = call(owner, 'focus_get', {'item_id':b['id']})['item']
    move['expected_card_revision'] = current['revision']
    with s.db.read() as u:
        before = [tuple(row) for row in u.connection.execute('SELECT id,position,revision FROM focus_cards ORDER BY position,id')]
        board_revision = u.connection.execute("SELECT value FROM change_sequences WHERE name='focus_board'").fetchone()[0]
    for ctx in (agent, s.agent_context(), viewer, worker_context):
        denied = s.app.invoke(ctx, 'focus_move', move, {'idempotency_key':new_id()})
        assert denied.status=='error' and denied.error['code'] in {'policy_denied','forbidden'}, denied.to_dict()
        # Direct domain calls and the exact pre-upgrade prepared shape also recheck identity.
        old_plan = PlanBuilder(ctx, 'focus_move', move, 'Update owner attention',
            namespace='global', affected_scope={'kind':'global'}).build()
        with s.db.transaction() as u:
            with pytest.raises(DomainError, match='Only the installation owner'):
                attention.prepare('focus_move',ctx,{**move,'item_id':'missing'},InvocationOptions(),u)
            with pytest.raises(DomainError, match='Only the installation owner'):
                attention.apply(ctx,old_plan,u)
            with pytest.raises(DomainError, match='Only the installation owner'):
                attention.apply(ctx,old_plan,u,_email_validated=True)
    with s.db.read() as u:
        assert [tuple(row) for row in u.connection.execute('SELECT id,position,revision FROM focus_cards ORDER BY position,id')]==before
        assert u.connection.execute("SELECT value FROM change_sequences WHERE name='focus_board'").fetchone()[0]==board_revision

    # Prepared ownership is not cached: a changed installation owner is rechecked.
    with s.db.transaction() as u:
        prepared = attention.prepare('focus_move',owner,move,InvocationOptions(),u)
        u.connection.execute("UPDATE installation SET owner_principal_id=? WHERE id='service'", (s.viewer.id,))
        with pytest.raises(DomainError, match='Only the installation owner'):
            attention.apply(owner,prepared,u)
        u.connection.execute("UPDATE installation SET owner_principal_id=? WHERE id='service'", (s.owner.id,))

    # Coordinator curation changes contents/framing and retains the owner's order.
    c = present('c')
    call(agent,'focus_edit',{'item_id':a['id'],'expected_card_revision':a['revision'],'changes':{'reason':'Current context','urgent':True}})
    a = call(agent,'focus_get',{'item_id':a['id']})['item']
    call(agent,'focus_set_archived',{'item_id':a['id'],'expected_card_revision':a['revision'],'archived':True})
    assert [row['id'] for row in call(agent,'focus_get',{})['items']]==[b['id'],c['id']]
    present('a')
    present('b')
    call(agent,'focus_reply',{'item_id':a['id'],'text':'Still able to curate and reply'})
    assert [row['id'] for row in call(agent,'focus_get',{})['items']]==[b['id'],a['id'],c['id']]


def test_task_thread_semantics_and_replay(tmp_path):
    db,app=make_tasks(tmp_path)
    assert invoke(app,'tasks_create',{'id':'a','title':'A'}).status=='ok'
    assert invoke(app,'tasks_update',{'task_id':'a','changes':{'title':'A'}},revision=1).status=='ok'
    assert invoke(app,'tasks_comment',{'task_id':'a','text':'Working'},revision=1).status=='ok'
    result=invoke(app,'tasks_update',{'task_id':'a','changes':{'title':'Changed','status':'done'}},revision=2,key='change')
    assert result.status=='ok',result.to_dict()
    assert invoke(app,'tasks_update',{'task_id':'a','changes':{'title':'Changed','status':'done'}},revision=2,key='change').status=='ok'
    thread=app.invoke(context(),'tasks_thread_get',{'task_id':'a'})
    assert thread.status=='ok',thread.to_dict()
    notes=thread.data['notes']
    assert len(notes)==2
    assert sum(n.get('entry_kind')=='system' for n in notes)==1
    assert any(n['body']=='State changed from open to done. Task details changed.' for n in notes)


def test_reject_atomic_message_revision_and_retry(scenario):
    s=scenario
    install_coordinator(s)
    proposal=s.propose()
    assert s.decide(proposal,'reject').error['code']=='message_required'
    rejected=s.decide(proposal,'reject',feedback='Please revise',idempotency_key='reject')
    assert rejected.status=='ok',rejected.to_dict()
    message=rejected.data['signoff']['decision']['message_id']
    replay=s.decide(proposal,'reject',feedback='Please revise',idempotency_key='reject')
    assert replay.data['signoff']['decision']['message_id']==message
    notes=s.app.invoke(s.owner_context,'signoffs_notes_list',{'signoff_id':proposal['id']})
    assert notes.status=='ok',notes.to_dict()
    assert len(notes.data['notes'])==1
    assert notes.data['notes'][0]['body']=='Please revise'


def test_comment_is_not_a_decision_and_only_current_eligible_authors_count(scenario):
    s=scenario
    coordinator=install_coordinator(s)
    proposal=s.propose()
    with s.db.transaction() as u:
        row=s.service._row(s.owner_context,proposal['id'],u)
        s.service.notes.append(s.agent_context(),row,'Agent says no',u)
    assert s.decide(proposal,'reject').error['code']=='message_required'
    # The owner's ordinary comment now belongs to the private channel; the configured
    # coordinator is the other eligible author of a shared signoff note.
    s.change_override('signoffs_comment','allow',actor=coordinator)
    refusal=s.app.invoke(s.owner_context,'signoffs_comment',{'signoff_id':proposal['id'],'text':'Owner message'},{'idempotency_key':'note'})
    assert refusal.error['code']=='channel_required',refusal.to_dict()
    result=s.app.invoke(s.agent_context(coordinator),'signoffs_comment',{'signoff_id':proposal['id'],'text':'Coordinator message'},{'idempotency_key':'coordinator-note'})
    assert result.status=='ok',result.to_dict()
    detail=s.detail(proposal)
    assert detail['status_revision']==proposal['status_revision'] and detail['payload_hash']==proposal['payload_hash']
    assert s.decide(proposal,'reject').status=='ok'


def test_stale_decision_leaves_no_message(scenario):
    s=scenario
    install_coordinator(s)
    proposal=s.propose()
    stale={**proposal,'status_revision':proposal['status_revision']+1}
    assert s.decide(stale,'reject',feedback='Must not persist').status=='error'
    with s.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM signoff_notes').fetchone()[0]==0


def test_persistent_reference_board_order_binding_and_retry(scenario):
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from bazarlyk.inbox.focus import FocusService
    from bazarlyk.domains.relationships.items import ContextItems
    s=scenario
    coordinator=install_coordinator(s)
    fake=SimpleNamespace(app=s.app,clock=lambda:datetime.now(timezone.utc),domains=SimpleNamespace(stories=SimpleNamespace(relationships=SimpleNamespace(items=None))))
    focus=FocusService(fake,s.service).register()
    owner=replace(s.owner_context,business_id=None,review_business_id=None)
    def call(op,args,key=None):
        result=s.app.invoke(owner,op,{'scope':'global',**args},{'idempotency_key':key or new_id()})
        assert result.status=='ok',result.to_dict()
        return plain(result.data)
    presentation={'reference':{'key':'a','title':'Preview','target':{'type':'url','url':'https://example.com/a'}},
        'display_name':'Page review','blocker':True,'urgent':True,
        'requested_response':'Review [the page](https://example.com/a) and flag issues.'}
    a=call('focus_present',presentation,'present')['item']
    replay=call('focus_present',presentation,'present')['item']
    assert a['id']==replay['id']
    assert a['display_name']=='Page review' and a['label']=='Preview'
    assert a['blocker'] is True and a['urgent'] is True and 'recommendation' not in a
    assert a['requested_response_parts'][1]=={'type':'link','text':'the page','url':'https://example.com/a'}
    # Re-presenting without framing must retain it, and flags can be cleared separately.
    a=call('focus_present',{'reference':presentation['reference']})['item']
    assert a['display_name']=='Page review' and a['blocker'] is True
    a=call('focus_edit',{'item_id':a['id'],'expected_card_revision':a['revision'],
        'changes':{'display_name':'Review this longer name without word rejection','blocker':False}})['item']
    assert a['display_name']=='Review this longer name without word rejection'
    assert a['blocker'] is False and a['urgent'] is True
    b=call('focus_present',{'reference':{'key':'b','title':'Second','target':{'type':'url','url':'https://example.com/b'}}})['item']
    # A Reference reply is an ordinary team message, so the owner is refused and the
    # configured coordinator writes it.
    refused=s.app.invoke(owner,'focus_reply',{'scope':'global','item_id':a['id'],'text':'Owner message'},{'idempotency_key':new_id()})
    assert refused.error['code']=='channel_required',refused.to_dict()
    s.change_override('focus_reply','allow',actor=coordinator)
    def coordinator_call(op,args,key=None):
        result=s.app.invoke(s.agent_context(coordinator),op,{'scope':'global',**args},{'idempotency_key':key or new_id()})
        assert result.status=='ok',result.to_dict()
        return plain(result.data)
    coordinator_call('focus_reply',{'item_id':a['id'],'text':'Keep this message','summary':'Keep this reference message available'},'message')
    coordinator_call('focus_reply',{'item_id':a['id'],'text':'Keep this message','summary':'Keep this reference message available'},'message')
    call('focus_move',{'item_id':b['id'],'expected_card_revision':b['revision'],'before_id':a['id']})
    assert [c['id'] for c in call('focus_get',{})['items']]==[b['id'],a['id']]
    changed=call('focus_edit',{'item_id':a['id'],'expected_card_revision':a['revision'],'changes':{'target':{'type':'url','url':'https://example.com/new'}}})['item']
    archived=call('focus_set_archived',{'item_id':a['id'],'expected_card_revision':changed['revision'],'archived':True})['item']
    assert len(call('focus_get',{})['items'])==1
    restored=call('focus_set_archived',{'item_id':a['id'],'expected_card_revision':archived['revision'],'archived':False})['item']
    thread=call('focus_get',{'item_id':a['id'],'conversation':True})['item']['thread']
    assert len(thread['notes'])==1 and thread['notes'][0]['body']=='Keep this message'
    assert thread['notes'][0]['summary']=='Keep this reference message available'
    assert restored['conversation']==a['conversation']
    with s.db.read() as u:
        # Only the owner and the configured coordinator curate Focus, and the owner's
        # ordinary messages are private now, so the coordinator's own Reference reply
        # raises no event for itself. The owner's two presentation edits (name and flags,
        # then the opening link) each notify the coordinator once; moves and archival do not.
        assert [r[0] for r in u.connection.execute("SELECT event_type FROM inbox_events WHERE source_type='focus_cards' ORDER BY id")]==['focus_edited','focus_edited']
    # A different card moving does not invalidate this card's expected revision.
    call('focus_move',{'item_id':b['id'],'expected_card_revision':call('focus_get',{'item_id':b['id']})['item']['revision'],'after_id':a['id']})
    call('focus_move',{'item_id':a['id'],'expected_card_revision':restored['revision'],'after_id':b['id']})
    stale=s.app.invoke(owner,'focus_move',{'scope':'global','item_id':a['id'],'expected_card_revision':restored['revision'],'position':'top'},{'idempotency_key':new_id()})
    assert stale.error['code']=='revision_conflict'
    current=call('focus_get',{'item_id':b['id']})['item']
    call('focus_set_archived',{'item_id':b['id'],'expected_card_revision':current['revision'],'archived':True})
    current=call('focus_get',{'item_id':a['id']})['item']
    missing=s.app.invoke(owner,'focus_move',{'scope':'global','item_id':a['id'],'expected_card_revision':current['revision'],'before_id':b['id']},{'idempotency_key':new_id()})
    assert missing.error['code']=='revision_conflict'

from test_ordubashy_owner_inbox import inbox


def test_focus_replies_reuse_task_and_story_writers_and_recheck_access(inbox):
    import json
    from types import SimpleNamespace
    from bazarlyk.inbox.focus import FocusService
    from bazarlyk.domains.relationships.items import ContextItems
    from test_ordubashy_owner_inbox import task
    s=inbox
    from bazarlyk.transport.auth import human_global_capabilities
    s.policy.human_global_capabilities=human_global_capabilities
    with s.db.transaction() as u:
        u.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'",(s.ordubashy.id,))
        row=u.repository('agent_profiles',business_id=None).get(s.ordubashy.id)
        profile=row['profile_json'];profile['runtime']='codex'
        u.connection.execute('UPDATE agent_profiles SET profile_json=? WHERE agent_id=?',(json.dumps(profile),s.ordubashy.id))
    s.inbox.domains.stories.relationships=SimpleNamespace(items=ContextItems(s.inbox.domains.stories,s.app))
    focus=FocusService(s.inbox,s.service).register()
    owner=replace(s.owner_context,business_id=None,review_business_id=None)
    def call(op,args):
        result=s.app.invoke(owner,op,{'scope':'global',**args},{'idempotency_key':new_id()})
        assert result.status=='ok',result.to_dict()
        return plain(result.data)
    # Team replies are written by the configured coordinator; the owner's own comments
    # belong to the private channel.
    s.change_override('focus_reply','allow',actor=s.ordubashy)
    coordinator=replace(s.ctx(),business_id=None,review_business_id=None)
    def agent_call(op,args):
        result=s.app.invoke(coordinator,op,{'scope':'global',**args},{'idempotency_key':new_id()})
        assert result.status=='ok',result.to_dict()
        return plain(result.data)
    task(s)
    card=call('focus_present',{'source':{'type':'task','id':'owner-task','business_id':s.business}})['item']
    assert not card.get('unavailable'),card
    original_title=card['label']
    card=call('focus_edit',{'item_id':card['id'],'expected_card_revision':card['revision'],
        'changes':{'display_name':'Landing page review','urgent':True}})['item']
    assert card['display_name']=='Landing page review' and card['label']==original_title
    assert card['source_revision']==1 and card['urgent'] is True and card['blocker'] is False
    refused=s.app.invoke(owner,'focus_reply',{'scope':'global','item_id':card['id'],'text':'Owner message','expected_source_revision':1},{'idempotency_key':new_id()})
    assert refused.error['code']=='channel_required',refused.to_dict()
    result=agent_call('focus_reply',{'item_id':card['id'],'text':'Source-owned message','expected_source_revision':1})
    assert result['task']['notes'][0]['body']=='Source-owned message'
    thread=call('focus_get',{'item_id':card['id'],'conversation':True})['item']['thread']
    assert any(n['body']=='Source-owned message' for n in thread['notes'])
    agent_thread=s.app.invoke(replace(s.ctx(),business_id=None,review_business_id=None),'focus_get',
        {'scope':'global','item_id':card['id'],'conversation':True})
    assert agent_thread.status=='ok',agent_thread.to_dict()
    assert any(n['body']=='Source-owned message' for n in agent_thread.data['item']['thread']['notes'])
    created=s.app.invoke(s.owner_context,'stories_create',{'title':'Campaign','purpose':'Review assets'},{'idempotency_key':new_id()})
    assert created.status=='ok',created.to_dict()
    story=created.data['story']
    reference={'key':'preview','title':'Preview','target':{'type':'url','url':'https://example.com'}}
    context_ref={'type':'story','id':story['id'],'business_id':s.business}
    card=call('focus_present',{'reference':reference,'context':context_ref})['item']
    agent_call('focus_reply',{'item_id':card['id'],'text':'Story-owned message'})
    with s.db.read() as u:
        assert u.connection.execute("SELECT count(*) FROM story_events WHERE story_id=? AND kind='note'",(story['id'],)).fetchone()[0]==1
        assert u.connection.execute('SELECT count(*) FROM focus_notes').fetchone()[0]==0
    thread=call('focus_get',{'item_id':card['id'],'conversation':True})['item']['thread']
    assert thread['notes'][0]['body']=='Story-owned message'
    with s.db.read() as u:
        # The reply reused the Story writer under the coordinator's own identity, so it
        # is not notified about its own note.
        delivered=u.connection.execute("SELECT e.event_type FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id WHERE e.source_type='stories' AND e.source_id=? AND d.agent_id=?",(story['id'],s.ordubashy.id)).fetchall()
        assert delivered==[]
    denied=s.app.invoke(owner,'focus_present',{'scope':'global','reference':reference}, {'idempotency_key':new_id()})
    assert denied.error['code']=='validation_error'
    from bazarlyk.domains.tasks import TaskOperations
    tasks=s.registry.get('tasks_create').apply.__self__;tasks.stories=s.inbox.domains.stories;tasks.application=s.app
    created=s.app.invoke(replace(s.ctx(),business_id=None,review_business_id=None),'tasks_create',
        {'scope':'global','kind':'owner_request','title':'Upload photos','assignee_ids':[s.owner.id],
         'focus':{'reason':'Need the originals','display_name':'Upload photos','blocker':True}},{'idempotency_key':'create-and-present'})
    assert created.status=='ok',created.to_dict()
    task_id=created.data['task']['id']
    assert any((c.get('source') or {}).get('id')==task_id and c['display_name']=='Upload photos' and c['blocker'] is True for c in call('focus_get',{})['items'])
    replay=s.app.invoke(replace(s.ctx(),business_id=None,review_business_id=None),'tasks_create',
        {'scope':'global','kind':'owner_request','title':'Upload photos','assignee_ids':[s.owner.id],
         'focus':{'reason':'Need the originals','display_name':'Upload photos','blocker':True}},{'idempotency_key':'create-and-present'})
    assert replay.data['task']['id']==task_id
    with s.db.transaction() as u:
        u.connection.execute('UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?',(s.business,s.ordubashy.id))
    assert call('focus_get',{'item_id':card['id'],'conversation':True})['item']=={'id':card['id'],'revision':card['revision'],'archived':0,'unavailable':True}


def test_focus_http_owner_csrf_and_read_does_not_write(tmp_path):
    from starlette.testclient import TestClient
    from test_service_transports import ServiceFixture, ORIGIN
    service=ServiceFixture(tmp_path,full=True)
    with TestClient(service.web) as client:
        body={'scope':'global','reference':{'key':'report','title':'Report','target':{'type':'url','url':'https://example.com'}},'idempotency_key':'present'}
        assert client.post('/api/root/operations/focus_present',json=body).status_code==403
        response=client.post('/api/root/operations/focus_present',headers=ORIGIN,json=body)
        assert response.json()['status']=='ok',response.text
        with service.fixture.db.read() as u:
            revision=u.connection.execute("SELECT value FROM change_sequences WHERE name='focus_board'").fetchone()[0]
            cards=u.connection.execute('SELECT count(*) FROM focus_cards').fetchone()[0]
        for _ in range(3):
            result=client.post('/api/root/operations/focus_get',headers=ORIGIN,json={'scope':'global'})
            assert result.json()['status']=='ok',result.text
        with service.fixture.db.read() as u:
            assert u.connection.execute("SELECT value FROM change_sequences WHERE name='focus_board'").fetchone()[0]==revision
            assert u.connection.execute('SELECT count(*) FROM focus_cards').fetchone()[0]==cards
            assert u.connection.execute('SELECT count(*) FROM focus_notes').fetchone()[0]==0


def test_legacy_focus_migration_preserves_order_binding_and_guard(tmp_path,monkeypatch):
    from bazarlyk.storage import Database
    from test_signoffs import Scenario
    from test_ordubashy_owner_inbox import task
    import json
    migrations=Database.migrations()
    with monkeypatch.context() as patch:
        patch.setattr(Database,'migrations',staticmethod(lambda:migrations[:65]))
        s=Scenario(tmp_path);coordinator=install_coordinator(s);task(s)
        browser={'request_id':'legacy','state':'pending','prompt':'Finish login','app':'Chrome','tab':'Login'}
        with s.db.transaction() as u:
            u.repository('agent_focus').insert({'agent_id':coordinator.id,'status':'Keep this context',
                'items_json':[{'type':'task','id':'owner-task','business_id':s.business,'reason':'Review this','needs_input':True}],
                'browser_json':browser,'updated_at':'2026-09-15T00:00:00Z'})
    s.db.initialize();s.db.initialize()
    with s.db.read() as u:
        rows=u.connection.execute('SELECT * FROM focus_cards').fetchall()
        assert len(rows)==1 and rows[0]['position']==0 and rows[0]['reason']=='Review this'
        assert rows[0]['display_name']=='' and rows[0]['blocker']==0 and rows[0]['urgent']==0
        assert json.loads(rows[0]['source_json'])=={'type':'task','id':'owner-task','business_id':s.business}
        assert u.repository('agent_focus').get(coordinator.id)['browser_json']==browser
        assert u.connection.execute('SELECT count(*) FROM signoff_notes').fetchone()[0]==0
        assert u.connection.execute('PRAGMA foreign_key_check').fetchone() is None
