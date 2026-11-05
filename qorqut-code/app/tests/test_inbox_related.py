from foundation_support import insert_action
import json

from qorqut_operations.contracts import new_id
from inbox_support import pending_count
from test_agent_inbox import inbox, story, subscribe


def test_broad_story_tracks_all_registered_linked_item_kinds(inbox):
    s = inbox; parent = story(s); business = s.fixture.business_id
    with s.fixture.db.transaction() as u:
        u.repository('data_tables', business_id=business).insert({'id':'table-one','title':'Linked table',
            'columns_json':[{'id':'name','name':'Name','type':'text'}], 'created_by':s.fixture.owner.id})
        insert_action(u, business, 'action-one', tag='coding', summary='Linked action', actor_id=s.fixture.owner.id, occurred_at='2026-09-10T00:00:00Z')
        u.repository('context_entities', business_id=business).insert({'id':'entity-one','label':'Linked reference'})
        for kind, identity in [('table','table-one'),('action','action-one'),('entity','entity-one'),('agent',s.agent.id),('person',s.fixture.owner.id)]:
            u.repository('relationships', business_id=business).insert({'id':new_id(),'from_type':kind,'from_id':identity,
                'to_type':'story','to_id':parent['id'],'role':'concerns','context':'Inbox test','created_by':s.fixture.owner.id})
    subscribe(s, parent, related=True)
    with s.fixture.db.transaction() as u:
        u.actor_id = s.fixture.owner.id
        u.connection.execute("UPDATE data_tables SET title='Updated table',revision=revision+1 WHERE id='table-one'")
        u.repository('data_rows',business_id=business).insert({'id':'row-one','table_id':'table-one','position':0,'cells_json':{'name':'First'},'created_by':s.fixture.owner.id,'updated_by':s.fixture.owner.id})
        u.repository('data_rows',business_id=business).insert({'id':'row-two','table_id':'table-one','position':1,'cells_json':{'name':'Second'},'created_by':s.fixture.owner.id,'updated_by':s.fixture.owner.id})
        u.repository('action_log_labels',business_id=business).insert({'log_id':'action-one','label':'reviewed'})
        u.connection.execute("UPDATE context_entities SET label='Updated reference',revision=revision+1 WHERE id='entity-one'")
        u.connection.execute("UPDATE business_memberships SET person_json=?,revision=revision+1 WHERE business_id=? AND principal_id=?",
            (json.dumps({'role':'Updated role'}),business,s.fixture.owner.id))
    s.fixture.update(s.agent, {'assignment_brief':'Updated linked agent assignment'})
    with s.fixture.db.read() as u:
        captured={row[0] for row in u.connection.execute(
            'SELECT e.source_type FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id WHERE d.agent_id=?', (s.agent.id,))}
        assert captured=={'data_tables','action_logs','context_entities','principals','agent_profiles'}
    count=pending_count(s.runtime.inbox, s.ctx(),s.consumer)
    events=s.call('inbox_list',{'consumer_id':s.consumer}).data['items']
    assert count==5, [(e['source_type'],e['summary']) for e in events]
    assert {event['source_type'] for event in events}=={'data_tables','action_logs','context_entities','principals','agent_profiles'}
    assert len([event for event in events if event['source_type']=='data_tables'])==1
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {
        'inbox_list': 'allow', 'tables_get': 'deny'}}})
    visible=s.call('inbox_list', {'consumer_id':s.consumer}).data['items']
    assert {event['source_type'] for event in visible}=={
        'action_logs','context_entities','principals','agent_profiles'}


def test_broad_story_stops_at_one_edge_and_unlink_stops_future_following(inbox):
    s=inbox; parent=story(s)
    def call(op,args):
        result=s.runtime.application.invoke(s.fixture.ctx,op,args,{'idempotency_key':new_id()})
        assert result.status=='ok',result.to_dict()
        return result.data
    direct=call('tasks_create',{'title':'Direct task'})['task']
    indirect=call('tasks_create',{'title':'Indirect task'})['task']
    link=call('stories_link_add',{'story_id':parent['id'],'resource_type':'tasks','resource_id':direct['id']})
    with s.fixture.db.transaction() as u:
        u.repository('relationships',business_id=s.fixture.business_id).insert({'id':new_id(),'from_type':'task','from_id':direct['id'],
            'to_type':'task','to_id':indirect['id'],'role':'blocked_by','context':'Dependency','created_by':s.fixture.owner.id})
    subscribe(s,parent,related=True)
    with s.fixture.db.transaction() as u:
        u.actor_id=s.fixture.owner.id
        u.connection.execute('UPDATE tasks SET title=?,revision=revision+1 WHERE id=?',('Indirect changed',indirect['id']))
    assert pending_count(s.runtime.inbox, s.ctx(),s.consumer)==0
    with s.fixture.db.transaction() as u:
        u.actor_id=s.fixture.owner.id
        u.connection.execute("DELETE FROM relationships WHERE (from_type='story' AND from_id=?) OR (to_type='story' AND to_id=?)",(parent['id'],parent['id']))
    existing=pending_count(s.runtime.inbox, s.ctx(),s.consumer)
    assert existing==1  # The Story's own unlink is still an update.
    with s.fixture.db.transaction() as u:
        u.actor_id=s.fixture.owner.id
        u.connection.execute('UPDATE tasks SET title=?,revision=revision+1 WHERE id=?',('Direct after unlink',direct['id']))
    assert pending_count(s.runtime.inbox, s.ctx(),s.consumer)==existing


def test_same_source_id_in_another_business_does_not_match_subscription(inbox):
    s = inbox
    parent = story(s)
    subscribe(s, parent, related=True)
    other_business = new_id()
    with s.fixture.db.transaction() as u:
        original = u.repository('businesses').get(s.fixture.business_id)
        u.repository('businesses').insert({
            'id': other_business, 'slug': 'other-inbox-test', 'name': 'Other inbox test',
            'workspace': original['workspace'] + '-other', 'hq_path': original['hq_path'] + '-other'})
        u.repository('stories', business_id=other_business).insert({
            'id': parent['id'], 'title': 'Same identity, other business', 'purpose': 'Isolation', 'state': 'planned',
            'created_by': s.fixture.owner.id})
        u.actor_id = s.fixture.owner.id
        u.connection.execute('UPDATE stories SET title=?,revision=revision+1 WHERE business_id=? AND id=?',
                             ('Other changed', other_business, parent['id']))
    assert pending_count(s.runtime.inbox, s.ctx(), s.consumer) == 0
    with s.fixture.db.transaction() as u:
        u.actor_id = s.fixture.owner.id
        u.connection.execute('UPDATE stories SET title=?,revision=revision+1 WHERE business_id=? AND id=?',
                             ('Subscribed changed', s.fixture.business_id, parent['id']))
    events = s.call('inbox_list', {'consumer_id': s.consumer}).data['items']
    assert len(events) == 1
    assert events[0]['business_id'] == s.fixture.business_id
