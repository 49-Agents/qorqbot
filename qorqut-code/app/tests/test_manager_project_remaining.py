"""Manager-only behavioral probes of immutable v1.3 host966e9c7; synthetic stores only."""
import json
from qorqut_operations.contracts import new_id
from test_project_records import call,context,context_global,host,item_of,list_items,ok,seed_project,task
from test_project_pagination import insert_tasks


def test_global_task_candidate_cursor_round_trips(host):
    project=seed_project(host,name='Manager Global Cursor',slug='manager-global-cursor')
    expected=set(insert_tasks(host,None,3,prefix='Global candidate'))
    args={'project_id':project,'source_business_id':None,'type':'task','limit':1}
    first=ok(call(host,context(host,project),'project_items_candidates',args))
    assert len(first['items'])==1 and first['next_cursor'], first
    second=call(host,context(host,project),'project_items_candidates',{**args,'cursor':first['next_cursor']})
    assert second.status=='ok', {'first':first,'second_error':second.error}
    assert second.data['items'][0]['id']!=first['items'][0]['id']


def test_link_time_does_not_replace_source_change_order(host):
    project=seed_project(host,name='Manager Source Order',slug='manager-source-order')
    foreign=seed_project(host,name='Manager Old Source',slug='manager-old-source')
    newer=task(host,project,'Newer owned source')
    older=task(host,foreign,'Older source linked today')
    ok(call(host,context(host,project),'project_items_link',{'project_id':project,'item':{'type':'task','id':older['id'],'business_id':foreign}}))
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET updated_at='2026-02-01T00:00:00Z' WHERE business_id=? AND id=?",(project,newer['id']))
        uow.connection.execute("UPDATE tasks SET updated_at='2026-01-01T00:00:00Z' WHERE business_id=? AND id=?",(foreign,older['id']))
    page=list_items(host,context(host,project),project,section='work',limit=2)
    assert [x['id'] for x in page['items']]==[newer['id'],older['id']], [(x['label'],x.get('updated_at')) for x in page['items']]


def test_done_story_retains_container_presentation(host):
    project=seed_project(host,name='Manager Story State',slug='manager-story-state')
    story=ok(call(host,context(host,project),'tasks_create',{'title':'Container story','task_type':'story'}))['task']
    with host.db.transaction() as uow:
        uow.connection.execute("UPDATE tasks SET status='done' WHERE business_id=? AND id=?",(project,story['id']))
    page=list_items(host,context(host,project),project,section='work')
    assert story['id'] in {x['id'] for x in page['items']}, page


def test_owner_dev_reports_after_first_reader_page_are_reachable(host):
    project=seed_project(host,name='Manager Dev Pages',slug='manager-dev-pages')
    expected=[new_id() for _ in range(60)]
    stamp='2030-01-01T00:00:00Z'
    with host.db.transaction() as uow:
        for position,identity in enumerate(expected):
            uow.connection.execute("INSERT INTO dev_tickets(id,title,details,kind,status,reporter_id,business_id,created_at,updated_at,revision) VALUES(?,?,?,'bug','open',?,?,?,?,1)",
                (identity,'Report '+str(position),'Full report body '+identity,host.instance.owner,project,stamp,stamp))
    canonical=ok(call(host,context_global(host),'dev_tickets_list',{'status':'all','details':True,'limit':200}))
    assert set(expected)<={x['id'] for x in canonical['items']}
    page=list_items(host,context(host,project),project,section='other',limit=100)
    late=sorted(expected)[-1]
    exact=call(host,context(host,project),'project_item_get',{'project_id':project,'item':{'type':'dev_ticket','id':late,'business_id':project}})
    assert set(expected)<={x['id'] for x in page['items']} and exact.status=='ok', {'expected':60,'found':len([x for x in page['items'] if x['id'] in expected]),'cursor':page['next_cursor'],'exact_late_status':exact.status,'exact_late_error':exact.error}


def test_readable_global_action_can_be_explicitly_linked_to_another_project(host):
    project=seed_project(host,name='Manager Action Link',slug='manager-action-link')
    foreign=seed_project(host,name='Manager Action Source',slug='manager-action-source')
    source=ok(call(host,context_global(host),'actions_record',{'text':'Foreign project Action I can read','category':'business-operational','real_time':'2026-10-20','business_ids':[foreign]}))['action']
    canonical=ok(call(host,context_global(host),'actions_get',{'action_id':source['action_id']}))
    assert canonical['action']['action_id']==source['action_id']
    result=call(host,context(host,project),'project_items_link',{'project_id':project,'item':{'type':'action','id':source['action_id'],'business_id':None}})
    assert result.status=='ok', result.error


def test_filtered_window_has_bounded_authorized_work(host,monkeypatch):
    project=seed_project(host,name='Manager Bounded Read',slug='manager-bounded-read')
    insert_tasks(host,project,1005,prefix='Closed',status='done',updated='2030-01-02T00:00:00Z')
    insert_tasks(host,project,1,prefix='Visible',updated='2030-01-01T00:00:00Z')
    inventory=host.runtime.projects.inventory
    original=inventory.resolve_target
    original_window=inventory._window
    windows=[]
    def observed_window(*args,**kwargs):
        result=original_window(*args,**kwargs)
        windows.append({'after':args[3],'limit':args[4],'count':len(result),'first':result[0] if result else None,'last':result[-1] if result else None})
        return result
    monkeypatch.setattr(inventory,'_window',observed_window)
    count=0
    def observed(*args,**kwargs):
        nonlocal count
        count+=1
        return original(*args,**kwargs)
    monkeypatch.setattr(inventory,'resolve_target',observed)
    page=list_items(host,context(host,project),project,section='work',limit=1)
    print('WINDOWS',json.dumps(windows))
    print('BOUNDED_PROBE', {'canonical_reads':count,'requested':1,'items':len(page['items']),'cursor':page['next_cursor']})
    assert count<=500, {'canonical_reads':count,'requested':1,'items':len(page['items']),'cursor':page['next_cursor']}
    assert page['items'] or page['next_cursor']


def test_global_task_distinct_timestamps_cursor_is_accepted(host):
    project=seed_project(host,name='Manager Distinct Cursor',slug='manager-distinct-cursor')
    identities=[]
    for day in (1,2,3):identities.extend(insert_tasks(host,None,1,prefix='Manager distinct',updated=f'2030-02-0{day}T00:00:00Z'))
    args={'project_id':project,'source_business_id':None,'type':'task','limit':1,'query':'Manager distinct'}
    first=ok(call(host,context(host,project),'project_items_candidates',args))
    assert first['next_cursor'],first
    second=call(host,context(host,project),'project_items_candidates',{**args,'cursor':first['next_cursor']})
    assert second.status=='ok',second.error


def test_owner_journal_stays_out_of_project_inventory_even_for_owner(host):
    project=seed_project(host,name='Manager Journal Boundary',slug='manager-journal-boundary')
    note=ok(call(host,context_global(host),'notes_create',{'title':'Manager private journal','date':'2026-10-27','body':'Synthetic owner-private journal body.'}))['document']
    direct=ok(call(host,context_global(host),'notes_get',{'document_id':note['id']}))
    assert direct['document']['id']==note['id']
    result=ok(call(host,context(host,project),'project_items_candidates',{'project_id':project,'source_business_id':None,'type':'material','query':'Manager private journal'}))
    assert note['id'] not in {item['id'] for item in result['items']},result
