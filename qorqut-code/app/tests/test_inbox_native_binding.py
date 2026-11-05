from dataclasses import replace
from datetime import timedelta
import json
import pytest

from qorqut_operations.contracts import DomainError, new_id
from inbox_support import owner, pending_count
from test_agent_inbox import inbox, story, subscribe, edit


def binding(s, consumer, *, event='startup', owner='123:456'):
    with s.fixture.db.read() as uow:
        workspace=uow.repository('machine_registrations').get(s.registrations[s.agent.id])['workspace']
    return {'agent_id':s.agent.id,'registration_id':s.registrations[s.agent.id],
        'workspace':workspace,'session_id':consumer,'runtime':'codex',
        'pid':123,'started':'456','owner':owner,'execution_target':'local','event':event}


def select(s, consumer, **options):
    return s.runtime.inbox.start_session(s.runtime.resolver.invocation_context(s.agent),s.registrations[s.agent.id],consumer,options.pop('event','startup'),
        binding=binding(s,consumer),**options)


def test_verified_selection_renews_without_expiry_and_preserves_pending(inbox):
    s=inbox
    result=s.runtime.application.invoke(s.fixture.ctx,'agents_message_send',{'to_agent_id':s.agent.id,'message':'New facts'}, {'idempotency_key':new_id()})
    assert result.status=='ok',result.to_dict()
    current=new_id();first=select(s,current)
    assert first['inbox_generation']==1
    bound=replace(s.ctx(),inbox_session_id=current,inbox_generation=1)
    assert owner(s.runtime.inbox,bound,current)['generation']==1
    renewed=select(s,current)
    assert renewed['inbox_generation']==1
    assert pending_count(s.runtime.inbox,bound)==1
    with pytest.raises(DomainError) as error: owner(s.runtime.inbox,s.runtime.resolver.invocation_context(s.agent),s.consumer)
    assert error.value.code=='inbox_session_superseded'


def test_expiry_never_displaces_bound_live_owner_and_verified_death_recovers(inbox):
    s=inbox;old=new_id();select(s,old);s.now+=timedelta(minutes=16)
    candidate=new_id()
    with pytest.raises(DomainError) as error:s.runtime.inbox.start_session(s.runtime.resolver.invocation_context(s.agent),s.registrations[s.agent.id],candidate,'startup')
    assert error.value.code=='inbox_binding_required'
    with pytest.raises(DomainError):owner(s.runtime.inbox,s.ctx(),candidate)
    recovered=s.runtime.inbox.start_session(s.runtime.resolver.invocation_context(s.agent),s.registrations[s.agent.id],candidate,'resume',
        binding={**binding(s,candidate),'event':'prompt'},old_state='dead',expected_generation=1)
    assert recovered['inbox_generation']==2
    with pytest.raises(DomainError) as error:select(s,old)
    assert error.value.code=='inbox_session_superseded'


def test_generation_comparison_rejects_delayed_dispatch_and_acknowledgement(inbox):
    s=inbox; current=new_id();select(s,current)
    stale=replace(s.ctx(),inbox_session_id=current,inbox_generation=0)
    with pytest.raises(DomainError) as error:owner(s.runtime.inbox,stale,current)
    assert error.value.code=='inbox_generation_stale'
    with pytest.raises(DomainError) as error:select(s,new_id(),expected_generation=0)
    assert error.value.code=='inbox_generation_stale'


def test_unknown_target_and_pid_reuse_do_not_implicitly_transfer(inbox):
    s=inbox;old=new_id();select(s,old)
    with pytest.raises(DomainError) as error:
        s.runtime.inbox.start_session(s.runtime.resolver.invocation_context(s.agent),s.registrations[s.agent.id],new_id(),'resume',
            binding=binding(s,new_id()),old_state='unknown')
    assert error.value.code in {'forbidden','inbox_binding_ambiguous'}
    with pytest.raises(DomainError) as error:
        s.runtime.inbox.start_session(s.runtime.resolver.invocation_context(s.agent),s.registrations[s.agent.id],old,'resume',
            binding=binding(s,old,owner='123:999'))
    assert error.value.code=='inbox_binding_ambiguous'


def test_same_conversation_verified_dead_process_rebind_fences_old_generation(inbox):
    s=inbox;current=new_id();select(s,current)
    renewed=s.runtime.inbox.start_session(s.runtime.resolver.invocation_context(s.agent),s.registrations[s.agent.id],current,'resume',
        binding=binding(s,current,event='resume',owner='123:999'),old_state='dead',expected_generation=1)
    assert renewed['inbox_generation']==2
    for generation in (None,1,True):
        stale=replace(s.ctx(),inbox_session_id=current,inbox_generation=generation)
        with pytest.raises(DomainError) as error:owner(s.runtime.inbox,stale,current)
        assert error.value.code=='inbox_generation_stale'
    assert owner(s.runtime.inbox,replace(s.ctx(),inbox_session_id=current,inbox_generation=2),current)['generation']==2
