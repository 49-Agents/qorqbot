"""Owner business group replies and isolated per-agent task conversations."""
from dataclasses import replace
from qorqut_operations.contracts import plain
from test_owner_private_conversations import s, task, call, ok, error  # noqa: F401


def test_owner_business_group_reply_and_global_boundary(s):
    source, row = task(s)
    ok(call(s,s.owner,'tasks_comment',{'task_id':source['id'],'text':'Please coordinate here'},revision=row['revision']))
    focus=ok(call(s,s.owner_global,'focus_present',{'scope':'global','source':source}))['item']
    current=ok(call(s,s.owner,'tasks_get',{'task_id':source['id']}))['task']
    ok(call(s,s.owner_global,'focus_reply',{'scope':'global','item_id':focus['id'],'text':'Group follow-up','expected_source_revision':current['revision']}))
    global_source,global_task=task(s,global_scope=True)
    assert error(call(s,s.owner_global,'tasks_comment',{'scope':'global','task_id':global_source['id'],'text':'No change to global policy'},revision=global_task['revision']))=='channel_required'


def test_business_agent_dm_delivery_isolation_and_unread(s):
    source,_=task(s)
    args={'source':source,'agent_id':s.agent.id}
    peers=ok(call(s,s.owner,'owner_conversation_peers',{'source':source}))['peers']
    assert s.agent.id in {p['agent_id'] for p in peers}
    private=ok(call(s,s.owner,'owner_conversation_send',{**args,'text':'Review the implementation'}))
    assert private['conversation']['agent_id']==s.agent.id
    read=ok(call(s,s.agent_ctx,'owner_conversation_get',{'source':source}))
    assert read['messages'][0]['body']=='Review the implementation'
    reply=ok(call(s,s.agent_ctx,'owner_conversation_send',{'source':source,'text':'Ready to review'}))
    peers=ok(call(s,s.owner,'owner_conversation_peers',{'source':source}))['peers']
    assert next(p for p in peers if p['agent_id']==s.agent.id)['unread']
    own=ok(call(s,s.owner,'owner_conversation_get',args))
    ok(call(s,s.owner,'owner_conversation_mark_read',{**args,'sequence':own['conversation']['latest_sequence']}))
    peers=ok(call(s,s.owner,'owner_conversation_peers',{'source':source}))['peers']
    assert not next(p for p in peers if p['agent_id']==s.agent.id)['unread']
    assert not ok(call(s,s.owner,'owner_conversation_get',{'source':source}))['messages']
    assert error(call(s,s.coordinator_business,'owner_conversation_get',args))=='forbidden'
    with s.db.read() as u:
        assert s.app.private_conversations.readable(s.agent_ctx,private['conversation']['id'],u)
    with s.db.transaction() as u:
        u.connection.execute('UPDATE business_memberships SET active=0 WHERE principal_id=? AND business_id=?',(s.agent.id,s.fixture.business_id))
    assert error(call(s,s.owner,'owner_conversation_get',args))=='forbidden'


def test_business_agent_cannot_be_selected_for_global_or_other_business(s):
    source,_=task(s,global_scope=True)
    assert error(call(s,s.owner_global,'owner_conversation_send',{'scope':'global','source':source,'agent_id':s.agent.id,'text':'Wrong scope'}))=='forbidden'
    source={'type':'task','id':'missing','business_id':s.other_business}
    assert error(call(s,s.owner_global,'owner_conversation_get',{'scope':'global','source':source,'agent_id':s.agent.id}))=='forbidden'
