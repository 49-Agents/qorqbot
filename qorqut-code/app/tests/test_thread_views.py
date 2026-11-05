"""Private navigation on synthetic composed hosts: ordering, access, and read-only related projections."""
import json
import pytest
from dataclasses import replace

from qorqut_operations.contracts import DomainError, Principal, new_id, plain
from test_focus_customer_projection import host, call, ok  # noqa: F401


@pytest.fixture(autouse=True)
def configured_coordinator(host):
    with host.instance.database.transaction() as u:
        u.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'",
            (host.instance.registered['ordubashy'],))


def task(host, title, parent=None, status='open'):
    identity = new_id()
    with host.instance.database.transaction() as u:
        u.connection.execute('INSERT INTO tasks(business_id,id,title,status,created_by,created_at,updated_at,parent_id) VALUES(?,?,?,?,?,?,?,?)',
            (host.first, identity, title, status, host.instance.owner, '2026-10-01T10:00:00Z', '2026-10-01T10:00:00Z', parent))
    return identity


def source(host, identity):
    return {'type': 'task', 'id': identity, 'business_id': host.first}


def send(host, identity, text):
    return ok(call(host, host.owner, 'owner_conversation_send', {'scope':'global', 'source':source(host,identity), 'text':text}))


def listing(host, **args):
    return ok(call(host, host.owner, 'owner_conversations_list', {'scope':'global', **args}))


def related(host, identity, **args):
    return ok(call(host, host.owner, 'owner_conversation_related', {'scope':'global','source':source(host,identity), **args}))


def test_directory_uses_messages_not_task_changes_reads_or_focus(host):
    a,b,empty=task(host,'Completed conversation',status='done'),task(host,'Other conversation'),task(host,'No messages')
    first=send(host,a,'First');send(host,b,'Second')
    assert [x['source']['id'] for x in listing(host)['items']]==[b,a]
    with host.instance.database.transaction() as u:
        u.connection.execute('UPDATE tasks SET updated_at=? WHERE id=?',('2099-01-01T00:00:00Z',a))
        counts=[u.connection.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('owner_conversation_messages','focus_cards','owner_conversation_reads')]
    for _ in range(2):
        assert [x['source']['id'] for x in listing(host)['items']]==[b,a]
        related(host,a)
    with host.instance.database.transaction() as u:
        assert counts==[u.connection.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('owner_conversation_messages','focus_cards','owner_conversation_reads')]
    ok(call(host,host.owner,'owner_conversation_mark_read',{'scope':'global','source':source(host,a),'sequence':first['conversation']['latest_sequence']}))
    assert [x['source']['id'] for x in listing(host)['items']]==[b,a]
    send(host,a,'New message')
    page=listing(host,limit=1)
    assert page['items'][0]['source']['id']==a
    assert [x['source']['id'] for x in listing(host,limit=1,before=page['next_before'])['items']]==[b]
    assert empty not in str(listing(host))
    coordinator=replace(host.owner,principal=Principal(host.instance.registered['ordubashy'],'agent'))
    ok(call(host,coordinator,'owner_conversation_send',{'scope':'global','source':source(host,b),'text':'Reply from Ordubashy'}))
    assert listing(host)['items'][0]['source']['id']==b
    assert listing(host)['items'][0]['unread'] is True


def test_directory_checks_current_access_and_hides_retired_channels(host,monkeypatch):
    a,b=task(host,'Secret title'),task(host,'Readable')
    send(host,b,'Visible message');send(host,a,'SECRET PREVIEW')
    service=host.instance.runtime.application.private_conversations
    original=service.source_check
    def check(ctx,kind,identity,uow,role='owner'):
        if identity==a:raise DomainError('forbidden','Revoked')
        return original(ctx,kind,identity,uow)
    monkeypatch.setattr(service,'source_check',check)
    first=listing(host,limit=1)
    assert first['items']==[] and first['next_before']
    assert a not in str(first) and 'SECRET' not in str(first)
    assert listing(host,before=first['next_before'])['items'][0]['source']['id']==b
    result=call(host,replace(host.owner,principal=Principal(new_id(),'human')),'owner_conversations_list',{'scope':'global'})
    # An unrelated human cannot enumerate the installation owner's private channels.
    assert result.status=='error'
    with host.instance.database.transaction() as u:
        u.connection.execute('UPDATE owner_conversations SET peer_agent_id=? WHERE source_id=?',
            (host.instance.registered['ordubashy'],b))
    assert listing(host)['items']==[]


def test_related_children_links_actions_deduplicate_and_never_write_messages(host,monkeypatch):
    parent=task(host,'Parent');child=task(host,'Child',parent);other=task(host,'Linked task')
    with host.instance.database.transaction() as u:
        for target in (child,other):
            u.connection.execute("INSERT INTO relationships(business_id,id,from_type,from_id,to_type,to_id,role,created_at,updated_at,created_by) VALUES(?,?,'task',?,'task',?,'concerns',?,?,?)",
                (host.first,new_id(),parent,target,'2026-10-02T10:00:00Z','2026-10-02T10:00:00Z',host.instance.owner))
        action=new_id()
        u.connection.execute("INSERT INTO action_logs(business_id,id,action_key,action_text,recorded_by,category,origin,occurred_at,refs_json,created_at,updated_at) VALUES(?,?,?,?,?,'business-operational','manual',?,?,?,?)",
            (host.first,action,'business/'+host.first+'/'+action,'Checked result',host.instance.owner,'2026-10-03T10:00:00Z',json.dumps({'task':parent}),'2026-10-03T10:00:00Z','2026-10-03T10:00:00Z'))
        u.connection.execute('INSERT INTO action_businesses(action_key,business_id,created_at) VALUES(?,?,?)',
            ('business/'+host.first+'/'+action,host.first,'2026-10-03T10:00:00Z'))
        u.connection.execute("INSERT INTO relationships(business_id,id,from_type,from_id,to_type,to_id,role,created_at,updated_at,created_by) VALUES(?,?,'action',?,'task',?,'concerns',?,?,?)",
            (host.first,new_id(),action,parent,'2026-10-03T10:00:00Z','2026-10-03T10:00:00Z',host.instance.owner))
    data=related(host,parent)
    assert len(data['items'])==3
    assert {x['type'] for x in data['items']}=={'task','action'}
    assert [x['id'] for x in data['items']].count(child)==1
    pages=[];offset=0
    while offset is not None:
        page=related(host,parent,limit=1,offset=offset);pages+=page['items'];offset=page['next_offset']
    assert pages==data['items']
    resolver=host.instance.runtime.application.attention.legacy
    original=resolver.resolve
    def denied(ctx,source,uow):
        if source['id']==child:raise DomainError('forbidden','No longer readable')
        return original(ctx,source,uow)
    monkeypatch.setattr(resolver,'resolve',denied)
    assert child not in str(related(host,parent))
    with host.instance.database.transaction() as u:
        assert u.connection.execute('SELECT count(*) FROM owner_conversation_messages').fetchone()[0]==0


def test_today_is_no_longer_an_operation_argument(host):
    from bazarlyk.workspaces import WorkspaceRegistry
    assert 'threads' not in {item.key for item in WorkspaceRegistry().all()}
    result=call(host,host.owner,'focus_get',{'scope':'global','today':True})
    assert result.status=='error' and result.error['code']=='validation_error'


def test_related_signoff_targets_and_focus_references_are_visual_only(host):
    parent=task(host,'Parent');child=task(host,'Referenced child')
    signoff=new_id();at='2026-10-04T12:00:00Z'
    with host.instance.database.transaction() as u:
        targets=[{'resource':{'resource_type':'tasks','resource_id':parent,'business_id':host.first},'revision':1}]
        u.connection.execute("""INSERT INTO signoffs(id,review_business_id,review_scope_kind,proposer_principal_id,
            operation_name,operation_version,proposal_revision,payload_hash,change_set,preview_json,
            expected_targets_json,policy_binding_json,affected_scope_json,state,status_revision,updated_sequence,created_at,updated_at)
            VALUES(?,?,'business',?,'tasks_update',1,1,?, ?, ?,?,'{}',?,'pending',1,999,?,?)""",
            (signoff,host.first,host.instance.owner,'a'*64,b'{}',json.dumps({'title':'Approve task change'}),json.dumps(targets),json.dumps({'kind':'business','business_id':host.first}),at,at))
        u.connection.execute("INSERT INTO relationships(business_id,id,from_type,from_id,to_type,to_id,role,created_at,updated_at,created_by) VALUES(?,?,'signoff',?,'task',?,'concerns',?,?,?)",
            (host.first,new_id(),signoff,parent,at,at,host.instance.owner))
    ok(call(host,host.owner,'focus_present',{'scope':'global','source':source(host,parent),'references':[source(host,child),{'type':'signoff','id':signoff,'business_id':host.first}]}))
    data=related(host,parent)
    assert len(data['items'])==2
    assert {x['id'] for x in data['items']}=={child,signoff}
    assert next(x for x in data['items'] if x['id']==signoff)['state']=='pending'
    with host.instance.database.transaction() as u:
        assert u.connection.execute('SELECT count(*) FROM owner_conversations').fetchone()[0]==0
        assert u.connection.execute('SELECT count(*) FROM owner_conversation_messages').fetchone()[0]==0

    reference=ok(call(host,host.owner,'focus_present',{'scope':'global','reference':{'key':'reference-fixture','title':'Design preview','target':{'type':'url','url':'https://example.test/preview'}}}))['item']
    for binding in ({'type':'signoff','id':signoff,'business_id':host.first},{'type':'reference','id':reference['id'],'business_id':None}):
        ok(call(host,host.owner,'owner_conversation_send',{'scope':'global','source':binding,'text':'Discuss this record'}))
    directory=listing(host)['items']
    assert [item['source']['type'] for item in directory]==['reference','signoff']
    assert directory[0]['resolved_source']['url']=='https://example.test/preview'
