from dataclasses import replace
from pathlib import Path

import pytest

from bazarlyk.catalog.packages import load_package
from qorqut_operations.contracts import new_id
from bazarlyk.policy.resolver import resolve_operation_policy
from bazarlyk.agents.terminal_control import TOOLS
from test_service_transports import ServiceFixture


class Backend:
    def __init__(self):
        self.current = {'state':'present', 'command':'codex', 'session_id':'$1'}
        self.calls = []
        self.fail = False
    def snapshot(self, row): return dict(self.current)
    def validate(self, action, snapshot): pass
    def execute(self, row, action, message, expected):
        self.calls.append((row['agent_id'], action, message))
        if self.fail: raise RuntimeError('uncertain send')
        return 'confirmed'


def control_setup(tmp_path, *, dev=False):
    """The service with a controller-granted Ordubashy and a recording terminal backend."""
    s = ServiceFixture(tmp_path, full=True, dev=dev)
    r, f = s.runtime, s.fixture
    package = load_package(Path(__file__).resolve().parents[1], 'plugins/agent-terminal-control/plugin.yaml', 'plugin', r.registry)
    destination = r.catalog.artifact_path(package)
    for relative, content in package.files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    with f.db.transaction() as u:
        if not u.connection.execute('SELECT 1 FROM artifacts WHERE kind=? AND artifact_id=? AND digest=?',
                ('plugin',package.id,package.digest)).fetchone():
            u.repository('artifacts').insert({'kind':'plugin','artifact_id':package.id,'version':package.version,
                'digest':package.digest,'stored_path':str(destination),'metadata_json':{'manifest_path':package.manifest_path}})
    actor = f.create('coordinator', 'ordubashy', plugins=[{**package.ref,'config':{}}])
    with f.db.read() as u:
        ctx = r.policy.context_for(actor.id, business_id=None, review_business_id=None, uow=u)
        owner = r.policy.context_for(f.owner.id, business_id=None, review_business_id=None, uow=u)
    backend = Backend(); r.terminal_control.backend = backend
    return s, ctx, owner, backend


@pytest.fixture
def _setup(tmp_path):
    s, ctx, owner, backend = control_setup(tmp_path)
    yield s, ctx, owner, backend
    s.runtime.stop()


def invoke(s, ctx, name, args, key=None):
    return s.runtime.application.invoke(ctx, name, args, {'idempotency_key':key or new_id()})


def issue(s, owner, action='ping', **extra):
    result = invoke(s, owner, 'terminal_command_issue', {'target_agent_id':s.agent.id,'action':action,**extra})
    assert result.status=='ok', result.to_dict()
    return result.data['records'][0]['id']


def test_lifecycle_allow_policy_and_role_boundaries(setup):
    s, ctx, owner, backend = setup
    for operation in ('start','restart','kill'):
        name = 'agent_terminal_'+operation
        for override in ({}, {name:'allow'}):
            assert resolve_operation_policy(name, {}, override, s.runtime.registry, role_id='ordubashy').mode=='allow'
        assert resolve_operation_policy(name, {}, {name:'deny'}, s.runtime.registry, role_id='ordubashy').mode=='deny'
        assert resolve_operation_policy(name, {name:'allow'}, {}, s.runtime.registry, role_id='business-agent').mode=='deny'
    denied = invoke(s, replace(ctx, principal=s.manager), 'agent_terminal_kill', {'target_agent_id':s.agent.id,'rationale':'No grant'})
    assert denied.status=='error'
    assert not s.runtime.terminal_control.tick()
    assert backend.calls==[]


def test_owner_command_ping_is_exact_single_use_and_no_blind_retry(setup):
    s, ctx, owner, backend = setup
    assert invoke(s, ctx, 'terminal_command_issue', {'target_agent_id':s.agent.id,'action':'ping','message':'Wake up'}).status=='error'
    command = issue(s, owner, message='Owner says: check your task')
    key = new_id()
    result = invoke(s, ctx, 'agent_terminal_ping', {'command_id':command},key)
    assert result.status=='ok', result.to_dict()
    assert invoke(s, ctx, 'agent_terminal_ping', {'command_id':command},key).status=='ok'
    assert invoke(s, ctx, 'agent_terminal_ping', {'command_id':command}).status=='error'
    backend.fail=True
    assert s.runtime.terminal_control.tick()
    assert not s.runtime.terminal_control.tick()
    assert backend.calls==[(s.agent.id,'ping','Owner says: check your task')]
    action=result.data['records'][-1]
    status=invoke(s,ctx,'agent_terminal_status',{'target_agent_id':s.agent.id,'action_id':action['id']})
    assert status.data['action']['state']=='uncertain'


def test_stale_command_and_changed_session_do_not_execute(setup):
    s,ctx,owner,backend=setup
    command=issue(s,owner,action='restart')
    backend.current['session_id']='$2'
    assert invoke(s,ctx,'agent_terminal_execute_command',{'command_id':command}).status=='error'
    command=issue(s,owner,action='restart')
    result=invoke(s,ctx,'agent_terminal_execute_command',{'command_id':command})
    assert result.status=='ok',result.to_dict()
    backend.current['session_id']='$3'
    assert s.runtime.terminal_control.tick()
    assert backend.calls==[]


def test_direct_lifecycle_applies_once(setup):
    s,ctx,owner,backend=setup
    key=new_id()
    result=invoke(s,ctx,'agent_terminal_restart',{'target_agent_id':s.agent.id,'rationale':'Restart this agent'},key)
    assert result.status=='ok',result.to_dict()
    assert invoke(s,ctx,'agent_terminal_restart',{'target_agent_id':s.agent.id,'rationale':'Restart this agent'},key).status=='ok'
    assert s.runtime.terminal_control.tick()
    assert backend.calls==[(s.agent.id,'restart',None)]
    assert not s.runtime.terminal_control.tick()


def test_agent_cannot_use_input_broadcast_as_bypass(setup):
    s,ctx,owner,backend=setup
    with s.fixture.db.read() as u:
        assert not s.runtime.broadcasts._input_allowed(ctx,u)
        assert s.runtime.broadcasts._input_allowed(owner,u)


def test_revoked_plugin_expired_command_and_forged_fields(setup):
    s,ctx,owner,backend=setup
    command=issue(s,owner,message='Ping directly')
    assert invoke(s,ctx,'agent_terminal_ping',{'command_id':command,'message':'substituted'}).status=='error'
    with s.fixture.db.transaction() as u:
        row=u.repository('terminal_commands').get(command)
        u.repository('terminal_commands').update(command,{'expires_at':'2000-01-01T00:00:00Z'},expected_revision=row['revision'])
    assert invoke(s,ctx,'agent_terminal_ping',{'command_id':command}).status=='error'
    command=issue(s,owner,action='kill')
    result=invoke(s,ctx,'agent_terminal_execute_command',{'command_id':command})
    assert result.status=='ok',result.to_dict()
    s.fixture.update(ctx.principal,{'plugins':[]})
    assert s.runtime.terminal_control.tick()
    assert backend.calls==[]


def test_changed_session_after_queue_does_not_dispatch(setup):
    s,ctx,owner,backend=setup
    result=invoke(s,ctx,'agent_terminal_kill',{'target_agent_id':s.agent.id,'rationale':'Terminate selected session'})
    assert result.status=='ok',result.to_dict()
    backend.current['session_id']='$new'
    assert s.runtime.terminal_control.tick()
    assert not s.runtime.terminal_control.tick()
    assert backend.calls==[]
    with s.fixture.db.read() as u:
        states=[row['state'] for row in u.connection.execute('SELECT state FROM terminal_actions')]
    assert states==['failed']


def test_agent_cannot_resend_owner_input_broadcast(setup):
    from test_broadcasts import definition
    s,ctx,owner,backend=setup
    created=invoke(s,owner,'broadcasts_create',{'scope':'global','definition':definition(s.agent.id,delivery='input')})
    assert created.status=='ok',created.to_dict()
    row=created.data['records'][0]
    result=s.runtime.application.invoke(ctx,'broadcasts_send_now',{'scope':'global','broadcast_id':row['id']},
        {'idempotency_key':new_id(),'expected_revision':row['revision']})
    assert result.status=='error'
    assert 'Only the owner' in result.error['message']


@pytest.mark.parametrize('action',['start','restart','kill'])
def test_batch_deduplicates_business_and_explicit_agents(setup, action):
    s,ctx,owner,backend=setup
    args={'target_agent_id':s.agent.id,'target_agent_ids':[s.agent.id,s.manager.id,s.agent.id],
          'business_ids':[s.fixture.business_id,s.fixture.business_id],'rationale':'All selected business agents'}
    result=invoke(s,ctx,'agent_terminal_'+action,args)
    assert result.status=='ok',result.to_dict()
    with s.fixture.db.read() as u:
        receipts=[dict(row) for row in u.connection.execute('SELECT * FROM terminal_actions')]
    assert len(receipts)==2
    assert {r['target_id'] for r in receipts}=={s.agent.id,s.manager.id}
    assert len({r['batch_id'] for r in receipts})==1
    assert s.runtime.terminal_control.tick()
    assert s.runtime.terminal_control.tick()
    assert not s.runtime.terminal_control.tick()
    assert {row[0] for row in backend.calls}=={s.agent.id,s.manager.id}
    status=invoke(s,ctx,'agent_terminal_status',{'batch_id':receipts[0]['batch_id']})
    assert status.status=='ok',status.to_dict()
    assert [row['state'] for row in status.data['actions']]==['completed','completed']


def test_business_selection_resolves_to_its_business_agent_at_invocation(setup):
    s,ctx,owner,backend=setup
    result=invoke(s,ctx,'agent_terminal_kill',{'business_ids':[s.fixture.business_id],'rationale':'Frozen selection'})
    assert result.status=='ok',result.to_dict()
    added=s.fixture.create('late-arrival','global-marketing')
    while s.runtime.terminal_control.tick(): pass
    # Global agents, including one added later with business access, have no business home.
    assert [row[0] for row in backend.calls]==[s.agent.id]
    assert added.id not in {row[0] for row in backend.calls}


def test_owner_ping_batch_is_frozen_single_use_and_preserves_partial_results(setup):
    s,ctx,owner,backend=setup
    issued=invoke(s,owner,'terminal_command_issue',{'business_ids':[s.fixture.business_id],
        'target_agent_ids':[s.manager.id],'action':'ping','message':'Ping the agents directly: report your blockers.'})
    assert issued.status=='ok',issued.to_dict()
    command=issued.data['records'][0]['id']
    s.fixture.create('late-arrival','global-marketing')
    result=invoke(s,ctx,'agent_terminal_ping',{'command_id':command})
    assert result.status=='ok',result.to_dict()
    assert invoke(s,ctx,'agent_terminal_ping',{'command_id':command}).status=='error'
    backend.fail=True
    assert s.runtime.terminal_control.tick()
    backend.fail=False
    assert s.runtime.terminal_control.tick()
    assert not s.runtime.terminal_control.tick()
    status=invoke(s,ctx,'agent_terminal_status',{'batch_id':result.data['batch_id']})
    assert sorted(row['state'] for row in status.data['actions'])==['completed','uncertain']
    assert len(backend.calls)==2
    assert {row[0] for row in backend.calls}=={s.agent.id,s.manager.id}
    assert {row[2] for row in backend.calls}=={'Ping the agents directly: report your blockers.'}


@pytest.mark.parametrize('selectors',[{}, {'target_agent_ids':[]}, {'business_ids':[]},
    {'business_ids':[new_id()]}, {'target_agent_ids':[new_id()]},
    {'target_agent_ids':[new_id() for _ in range(101)]}])
def test_invalid_batch_selectors_never_create_partial_work(setup,selectors):
    s,ctx,owner,backend=setup
    result=invoke(s,ctx,'agent_terminal_start',{'rationale':'Invalid selector',**selectors})
    assert result.status=='error'
    with s.fixture.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM terminal_actions').fetchone()[0]==0
    assert backend.calls==[]


def test_multiple_businesses_resolve_union_and_reject_unauthorized_selection(setup):
    s,ctx,owner,backend=setup
    f=s.fixture
    business=new_id()
    with f.db.transaction() as u:
        original=dict(u.repository('businesses').get(f.business_id))
        original.update(id=business,slug='second',name='Second',workspace=str(f.root/'second'),hq_path=str(f.root/'second/hq'))
        u.repository('businesses').insert(original)
        # Move a fixture profile to a second business to test selector ownership.
        u.connection.execute('UPDATE agent_profiles SET home_business_id=?,namespace=? WHERE agent_id=?',(business,business,s.manager.id))
    with f.db.read() as u:
        from qorqut_operations.contracts import DomainError
        with pytest.raises(DomainError,match='outside current scope'):
            s.runtime.terminal_control.select_targets(ctx,{'business_ids':[f.business_id,business]},u)
        expanded=replace(ctx,allowed_business_ids=ctx.allowed_business_ids|{business})
        rows=s.runtime.terminal_control.select_targets(expanded,{'business_ids':[f.business_id,business],
            'target_agent_ids':[s.agent.id,s.manager.id]},u)
        assert {row['agent_id'] for row in rows}=={s.agent.id,s.manager.id}


def test_batch_rechecks_each_target_after_queue_without_replaying_peers(setup):
    s,ctx,owner,backend=setup
    result=invoke(s,ctx,'agent_terminal_restart',{'target_agent_ids':[s.agent.id,s.manager.id], 'rationale':'Restart both'})
    assert result.status=='ok',result.to_dict()
    backend.snapshot=lambda row: dict(backend.current,session_id='$changed') if row['agent_id']==s.agent.id else dict(backend.current)
    assert s.runtime.terminal_control.tick()
    assert s.runtime.terminal_control.tick()
    assert not s.runtime.terminal_control.tick()
    assert backend.calls==[(s.manager.id,'restart',None)]
    with s.fixture.db.read() as u:
        states={row['target_id']:row['state'] for row in u.connection.execute('SELECT target_id,state FROM terminal_actions')}
    assert states=={s.agent.id:'failed',s.manager.id:'completed'}
