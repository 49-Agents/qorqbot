from datetime import timedelta

import pytest

from qorqut_operations.contracts import DomainError, new_id
from inbox_support import owner, pending_count
from test_agent_inbox import inbox, story, subscribe, edit


from dataclasses import replace
from test_inbox_native_binding import binding


def context(s, consumer, generation=1, principal=None):
    return replace(s.runtime.resolver.invocation_context(principal or s.agent),
        inbox_session_id=consumer,inbox_generation=generation)


def startup(s, consumer, event='startup', principal=None, registration=None):
    principal = principal or s.agent
    registration = registration or s.registrations[principal.id]
    native = binding(s, consumer, event=event)
    if principal != s.agent:
        with s.fixture.db.read() as uow:
            native.update(agent_id=principal.id,registration_id=registration,
                workspace=uow.repository('machine_registrations').get(registration)['workspace'])
    return s.runtime.inbox.start_session(s.runtime.resolver.invocation_context(principal),
        registration, consumer, event, binding=native)


def test_verified_startup_replaces_generation_zero_owner_without_clearing_pending(inbox):
    s=inbox; row=story(s);subscribe(s,row);edit(s,row)
    current=new_id(); assert startup(s,current)['inbox_generation']==1
    with pytest.raises(DomainError) as error:owner(s.runtime.inbox,s.ctx(),s.consumer)
    assert error.value.code=='inbox_session_superseded'
    ctx=context(s,current)
    assert owner(s.runtime.inbox,ctx,current)['generation']==1
    assert pending_count(s.runtime.inbox,ctx)==1
    items=s.runtime.application.invoke(ctx,'inbox_list',{}).data['items']
    stale=s.call('inbox_acknowledge',{'consumer_id':s.consumer,'event_ids':[items[0]['id']]})
    assert stale.status=='error'
    assert pending_count(s.runtime.inbox,ctx)==1


@pytest.mark.parametrize('event',['startup','resume','compact','prompt','clear','fork'])
def test_superseded_session_cannot_reclaim_even_after_expiry_or_replay(inbox,event):
    s=inbox; old,current=new_id(),new_id();startup(s,old);startup(s,current)
    s.now+=timedelta(minutes=16)
    with pytest.raises(DomainError) as error:startup(s,old,event)
    assert error.value.code=='inbox_session_superseded'
    with pytest.raises(DomainError):owner(s.runtime.inbox,context(s,old),old)
    assert owner(s.runtime.inbox,context(s,current,2),current)['consumer_id']==current


@pytest.mark.parametrize('event',['startup','resume','compact','prompt'])
def test_unknown_unverified_bootstrap_cannot_take_ownership(inbox,event):
    s=inbox;current=new_id();startup(s,current)
    with pytest.raises(DomainError) as error:
        s.runtime.inbox.start_session(s.runtime.resolver.invocation_context(s.agent),s.registrations[s.agent.id],new_id(),event)
    assert error.value.code=='inbox_binding_required'


def test_current_session_can_renew_without_generation_change(inbox):
    s=inbox; current=new_id();startup(s,current)
    assert owner(s.runtime.inbox,context(s,current),current)['generation']==1
    assert startup(s,current,'resume')['inbox_generation']==1
    assert owner(s.runtime.inbox,context(s,current),current)['generation']==1
    with s.fixture.db.read() as uow:
        row=uow.connection.execute('SELECT * FROM inbox_watchers WHERE agent_id=?',(s.agent.id,)).fetchone()
        assert row['lease_until']==row['last_check_at']=='1970-01-01T00:00:00.000000Z'
    assert startup(s,current,'resume')['inbox_consumer_id']==current


def test_takeover_is_scoped_and_registration_bound(inbox):
    s=inbox;current=new_id();startup(s,current)
    with pytest.raises(DomainError) as error:startup(s,new_id(),registration=s.registrations[s.manager.id])
    assert error.value.code=='forbidden'
    startup(s,new_id(),principal=s.manager)
    assert owner(s.runtime.inbox,context(s,current),current)['generation']==1


def test_denied_inbox_cannot_take_over(inbox):
    s=inbox;current=new_id();startup(s,current)
    s.fixture.update(s.agent,{'tool_policy':{'overrides':{'inbox_list':'deny'}}})
    with pytest.raises(DomainError) as error:startup(s,new_id())
    assert error.value.code=='forbidden'


@pytest.mark.parametrize('runtimes',[('claude-code','codex'),('codex','claude-code')])
def test_http_bootstrap_uses_verified_adapter_across_runtimes(inbox,runtimes):
    from starlette.testclient import TestClient
    from bazarlyk.bootstrap.preparation import prepare_agent
    s=inbox
    prepare_agent(s.runtime.resolver,s.agent,endpoint='http://testserver',
        versions={'claude-code':'2.1.267 (Claude Code)','codex':'codex-cli 0.154.0'})
    class Adapter:
        def verify(self,agent,registration,session,**kwargs):return binding(s,session),'alive'
    s.runtime.native_binding_adapter=Adapter()
    headers={'Authorization':'Bearer '+s.tokens[s.agent.id]}
    old,current=new_id(),new_id()
    with TestClient(s.web) as client:
        for generation,(consumer,runtime) in enumerate(zip((old,current),runtimes),1):
            response=client.post('/api/agent/bootstrap',headers=headers,json={
                'registration_id':s.registrations[s.agent.id],'session_id':consumer,'event':'startup','runtime':runtime})
            assert response.status_code==200,response.text
            assert response.json()['data']['inbox_generation']==generation
            assert owner(s.runtime.inbox,context(s,consumer,generation),consumer)['generation']==generation
        with pytest.raises(DomainError) as error:owner(s.runtime.inbox,context(s,old,1),old)
        assert error.value.code=='inbox_session_superseded'


@pytest.mark.parametrize('failed,delivered',[(True,False),(False,False),(True,True)])
def test_failed_hook_never_manufactures_startup(tmp_path,failed,delivered):
    from bazarlyk.bootstrap import claude_session,recovery
    from bazarlyk.bootstrap.client import load_registration
    from test_dual_cli import registration
    path=registration(tmp_path,capture=False);session=new_id();sent=[]
    if failed:recovery.record(load_registration(path),session,'claude-code','bootstrap_unavailable')
    class Client:
        def __init__(self,registration):pass
        def post(self,route,body):
            sent.append(body['event'])
            return {'ready':True,'effective_config_id':'digest','mandatory_context':'Shared policy'}
    claude_session.handle_hook({'hook_event_name':'UserPromptSubmit','session_id':session,'prompt':'hey'},path,
        runtime='claude-code',client_factory=Client)
    assert sent==['prompt']
