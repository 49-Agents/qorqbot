"""Full Focus completion reviews are private, read-only and once per root turn."""
import json
import os
from pathlib import Path

import pytest

from bazarlyk.agents.checkpoints import FOCUS_INSTRUCTION
from bazarlyk.bootstrap import activity_hook as hook
from qorqut_operations.contracts import new_id, DomainError, Principal, plain
from host_foundation_support import build_instance, register_sources, create_registered_global, create_worker, select_coordinator, set_agent_overrides
from types import SimpleNamespace


def ok(result):
    assert result.status=='ok', result.to_dict()
    return plain(result.data)


def call(s,ctx,name,args):
    return s.instance.invoke(ctx,name,args)


@pytest.fixture
def s(tmp_path):
    instance,_=build_instance(tmp_path)
    register_sources(instance,instance.business,'first')
    peer=select_coordinator(instance, create_registered_global(instance, "ordubashy"))
    business_agent=create_worker(instance, "colleague")
    with instance.database.transaction() as u:
        row=u.repository('agent_profiles',business_id=None).get(peer)
        profile=row['profile_json']
        profile['scope']['global_capabilities'].append('coordinate_businesses')
        profile['profile_revision']+=1
        u.repository('agent_profiles',business_id=None).update(peer,{'profile_json':profile},expected_revision=row['revision'])
        registration=new_id()
        u.repository('machine_registrations').insert({'id':registration,'principal_id':peer,
            'service_url':'http://127.0.0.1:9','profile_revision':1,'workspace':str(instance.paths.agent_workspace(peer))})
    runtime=instance.runtime
    return SimpleNamespace(instance=instance,runtime=runtime,db=instance.database,app=runtime.application,
        peer=peer,registration=registration,
        coordinator=runtime.resolver.invocation_context(Principal(peer,'agent')),
        agent_ctx=runtime.resolver.invocation_context(Principal(business_agent,'agent'),business_id=instance.business))


def present(s,key,**fields):
    return ok(call(s,s.coordinator,'focus_present',{'scope':'global','reference':{
        'key':key,'title':key,'target':{'type':'url','url':'https://example.com/'+key}},**fields}))['item']


def test_complete_snapshot_and_once_per_conversation_turn(s):
    cards=[present(s,str(i)) for i in range(35)]
    deferred=present(s,'later',deferred_until='2099-01-01T00:00:00Z')
    archived=present(s,'archived')
    ok(call(s,s.coordinator,'focus_set_archived',{'scope':'global','item_id':archived['id'],
        'expected_card_revision':archived['revision'],'archived':True}))
    session,turn=new_id(),new_id()
    s.runtime.activity.register_session(s.coordinator,s.registration,session,'claude-code')
    directory=s.instance.paths.agent_workspace(s.peer)/'.bazarlyk/activity'
    directory.mkdir(parents=True,exist_ok=True)
    observation={'agent_id':s.peer,'registration_id':s.registration,
        'session_id':session,'runtime':'claude-code','checkpoint':{'protocol':1,
        'turn_id':turn,'root_turn_id':turn,'dirty_sequence':1}}
    def inspect():
        hook.write_state(directory/(session+'.json'),observation)
        return s.runtime.checkpoints.check(s.coordinator,s.registration,session,
            observation['checkpoint']['turn_id'],allow_correction=True,claim_id=new_id(),focus_only=True)
    with s.db.read() as u:
        before=u.connection.execute('SELECT count(*) FROM inbox_events').fetchone()[0]
    result=inspect()
    assert result['correction']==FOCUS_INSTRUCTION
    board=result['focus_board']
    assert board['complete'] and board['includes_postponed'] and 'next_offset' not in board
    assert [row['id'] for row in board['items']]==[row['id'] for row in cards]+[deferred['id']]
    assert all('thread' not in row for row in board['items'])
    assert inspect()['correction'] is None
    observation['checkpoint']['turn_id']=new_id()  # same root: no repeat
    assert 'focus_board' not in inspect()
    observation['checkpoint'].update(turn_id=new_id(),root_turn_id=new_id())
    assert inspect()['focus_board']['complete']
    with s.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM inbox_events').fetchone()[0]==before
        with pytest.raises(DomainError):
            s.app.attention.completion_snapshot(s.agent_ctx,u)


def test_peer_can_reorder_but_revisions_and_scope_still_apply(s):
    a,b=present(s,'first'),present(s,'second')
    args={'scope':'global','item_id':b['id'],'expected_card_revision':b['revision'],'before_id':a['id']}
    ok(call(s,s.coordinator,'focus_move',args))
    board=ok(call(s,s.coordinator,'focus_get',{'scope':'global'}))
    assert [row['id'] for row in board['items']]==[b['id'],a['id']]
    assert call(s,s.coordinator,'focus_move',args).error['code']=='revision_conflict'
    set_agent_overrides(s.instance,s.agent_ctx.principal_id,{'focus_move':'allow'},[s.instance.business])
    assert call(s,s.agent_ctx,'focus_move',args).status=='error'
    set_agent_overrides(s.instance,s.peer,{'focus_move':'deny'},[s.instance.business])
    assert call(s,s.coordinator,'focus_move',args).error['code']=='policy_denied'


@pytest.mark.parametrize('runtime',['claude-code','codex'])
def test_large_snapshot_delivered_once_without_copying_board_into_receipts(tmp_path,runtime):
    session,turn,registration=new_id(),new_id(),new_id()
    directory=tmp_path/'.bazarlyk/activity'
    directory.mkdir(parents=True)
    path=directory/(session+'.json')
    state=hook.checkpoint_activity({'hook_event_name':'UserPromptSubmit','turn_id':turn},{},runtime=runtime)
    value={'checkpoint':state}
    native={'session_id':session,'turn_id':state['turn_id'],'stop_hook_active':False}
    reg={'registration_id':registration,'workspace':str(tmp_path)}
    board={'complete':True,'items':[{'id':str(i),'reason':'x'*1000} for i in range(40)]}
    def post(*args): return {'correction':FOCUS_INSTRUCTION,'focus_board':board}
    result=hook.checkpoint_stop(native,reg,path,value,post=post)
    assert result['decision']=='block' and len(result['reason'])>40000
    assert json.dumps(board,ensure_ascii=False,separators=(',',':')) in result['reason']
    assert path.stat().st_size<4096 and path.with_suffix('.checkpoint.json').stat().st_size<4096
    assert hook.matches_continuation(result['reason'], value['checkpoint'])
    assert not hook.matches_continuation('another prompt', value['checkpoint'])
    continued=hook.checkpoint_activity({'hook_event_name':'UserPromptSubmit','turn_id':new_id(),
        'prompt':result['reason']},value['checkpoint'],runtime=runtime)
    assert continued['root_turn_id']==state['root_turn_id'] and continued['spent']
    assert hook.checkpoint_stop(native,reg,path,value,post=post)=={}


def test_partial_snapshot_is_never_presented_as_full(tmp_path):
    session,turn=new_id(),new_id()
    path=tmp_path/(session+'.json')
    value={'checkpoint':hook.checkpoint_activity({'hook_event_name':'UserPromptSubmit','turn_id':turn},{})}
    native={'session_id':session,'turn_id':turn,'stop_hook_active':False}
    reg={'registration_id':new_id()}
    assert hook.checkpoint_stop(native,reg,path,value,post=lambda *a:{
        'correction':FOCUS_INSTRUCTION,'focus_board':{'items':[]}})=={}
    assert value['checkpoint']['state']=='unavailable'


@pytest.mark.parametrize('runtime',['claude-code','codex'])
def test_prepared_focus_mode_routes_stop_and_does_not_repeat_after_edits(tmp_path,monkeypatch,runtime):
    workspace=tmp_path/'agent'
    (workspace/'.bazarlyk').mkdir(parents=True)
    path=workspace/'.bazarlyk/registration.json'
    path.write_text(json.dumps({'workspace':str(workspace),'agent_id':new_id(),'registration_id':new_id(),
        'service_url':'http://127.0.0.1:9','checkpoint_protocol':{runtime:2}}))
    monkeypatch.setattr(hook,'session_owner',lambda *_:hook.process_identity(os.getpid()))
    from bazarlyk.bootstrap import technical_reporting as technical
    monkeypatch.setattr(technical,'record',lambda *a,**k:None)
    monkeypatch.setattr(technical,'replay_http',lambda *a,**k:None)
    monkeypatch.setattr(technical,'active',lambda *a,**k:False)
    posts=[]
    def post(reg,body):
        posts.append(body)
        assert body['focus_only'] is True
        return {'correction':FOCUS_INSTRUCTION,'focus_board':{'complete':True,'items':[]}}
    original=hook.checkpoint_stop
    monkeypatch.setattr(hook,'checkpoint_stop',lambda *a,**k:original(*a,post=post,**k))
    session,turn=new_id(),new_id()
    def send(event,**fields):
        return hook.record({'session_id':session,'cwd':str(workspace),'hook_event_name':event,
            **({'turn_id':turn} if runtime=='codex' else {}),**fields},path,runtime)
    send('UserPromptSubmit',prompt='This is a priority')
    assert send('Stop',stop_hook_active=False)['decision']=='block'
    send('PostToolUse',tool_name='mcp__bazarlyk__focus_move')
    assert send('Stop',stop_hook_active=True)=={}
    assert send('Stop',stop_hook_active=False)=={}
    assert [p['allow_correction'] for p in posts]==[True,False,False]
