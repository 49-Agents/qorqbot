"""Manager-only probes on the immutable submission, using synthetic composed stores."""
import pytest
from qorqut_operations.contracts import InvocationOptions,new_id
from test_project_records import host,seed_project,context,context_global,call,ok,task,list_items


def link(host,project,source,record):
    return ok(call(host,context(host,project),'project_items_link',{'project_id':project,
                   'item':{'type':'task','id':record['id'],'business_id':source}}))


def test_linked_hierarchy_keeps_real_parent_identity(host):
    project=seed_project(host,name='Hierarchy target',slug='manager-hierarchy-target')
    foreign=seed_project(host,name='Hierarchy source',slug='manager-hierarchy-source')
    parent=task(host,foreign,'Linked parent')
    child=ok(call(host,context(host,foreign),'tasks_create',{'title':'Linked child','parent_id':parent['id']}))['task']
    link(host,project,foreign,parent)
    link(host,project,foreign,child)
    page=list_items(host,context(host,project),project,section='work',root_tasks_only=True)
    assert [item['id'] for item in page['items']]==[parent['id']],page


def test_default_page_size_is_twenty_five(host):
    project=seed_project(host,name='Default page',slug='manager-default-page')
    for position in range(26):
        task(host,project,'Item '+str(position))
    page=list_items(host,context(host,project),project,section='work')
    assert len(page['items'])==25 and page['next_cursor'],page


def test_unlink_uses_normal_transport_revision_option(host):
    from bazarlyk.transport.mcp import split_options
    project=seed_project(host,name='Unlink target',slug='manager-unlink-target')
    foreign=seed_project(host,name='Unlink source',slug='manager-unlink-source')
    row=task(host,foreign,'Unlink me')
    linked=link(host,project,foreign,row)
    args,options=split_options({'project_id':project,'link_id':linked['link']['id'],
                               'expected_revision':linked['link']['revision'],'idempotency_key':new_id()})
    result=host.runtime.application.invoke(context(host,project),'project_items_unlink',args,options)
    assert result.status=='ok',result.error


def test_closed_explicit_task_is_hidden_by_default(host):
    project=seed_project(host,name='Closed target',slug='manager-closed-target')
    foreign=seed_project(host,name='Closed source',slug='manager-closed-source')
    row=task(host,foreign,'Finished linked item')
    link(host,project,foreign,row)
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET status='done' WHERE business_id=? AND id=?",(foreign,row['id']))
    page=list_items(host,context(host,project),project,section='work')
    assert row['id'] not in {item['id'] for item in page['items']},page


def test_replayed_link_rechecks_revoked_source_access(host):
    project=seed_project(host,name='Replay target',slug='manager-replay-target')
    foreign=seed_project(host,name='Replay source',slug='manager-replay-source')
    row=task(host,foreign,'Private source title')
    args={'project_id':project,'item':{'type':'task','id':row['id'],'business_id':foreign}}
    options=InvocationOptions(idempotency_key=new_id())
    initial=host.runtime.application.invoke(context(host,project),'project_items_link',args,options)
    assert initial.status=='ok',initial.error
    with host.db.transaction() as uow:
        uow.connection.execute('UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?',
                                (foreign,host.instance.owner))
    denied=call(host,context(host,project),'project_item_get',args)
    assert denied.status=='error',denied.data
    replay=host.runtime.application.invoke(context(host,project),'project_items_link',args,options)
    assert replay.status=='error',replay.data


def test_global_routine_candidate_and_exact_read(host):
    project=seed_project(host,name='Global routines',slug='manager-global-routines')
    identity=new_id()
    with host.db.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO routines(namespace,business_id,id,name,scope,target,schedule,timezone,status,job_kind,"
            "arguments_json,timeout_seconds,metadata_json,revision,created_at,updated_at) "
            "VALUES('global',NULL,?,'Global recap','global','root','0 0 * * *','UTC','paused','test.fixture',"
            "'{}',900,'{}',1,'2026-10-01T00:00:00Z','2026-10-01T00:00:00Z')",(identity,))
    direct=ok(call(host,context_global(host),'routines_get',{'scope':'global','routine_id':identity}))
    assert direct['routine']['id']==identity
    candidates=ok(call(host,context(host,project),'project_items_candidates',{'project_id':project,
                  'source_business_id':None,'type':'routine'}))
    assert identity in {item['id'] for item in candidates['items']},candidates


def test_project_record_is_available_through_root_http_route(host,tmp_path):
    from starlette.testclient import TestClient
    from bazarlyk.service.web import create_app
    from bazarlyk.transport.auth import Authentication
    from bazarlyk.transport.compatibility import DashboardCompatibility
    dashboard=tmp_path/'dashboard'
    (dashboard/'js').mkdir(parents=True)
    (dashboard/'css').mkdir()
    web=create_app(host.runtime,dashboard_root=dashboard,origins=['http://testserver'],
       authentication=Authentication(host.db,host.runtime.paths),
       compatibility=DashboardCompatibility(host.runtime),manage_lifecycle=False)
    with TestClient(web,base_url='http://testserver') as client:
        response=client.post('/api/root/operations/project_record_get',headers={'Origin':'http://testserver'},
               json={'scope':'global','project_id':host.first})
        assert response.status_code==200,response.text
