from concurrent.futures import wait
import json
from pathlib import Path

import pytest
from types import SimpleNamespace

from bazarlyk.bootstrap.supervisor import Supervisor
from test_dual_cli import registration


class Process:
    def __init__(self):self.code=None;self.stopped=False
    def poll(self):return self.code
    def terminate(self):self.stopped=True


def delegated(tmp_path):
    """A registration whose agent hosts a delegated Codex runtime: the only session the supervisor keeps a process for."""
    path=registration(tmp_path,capture=False)
    saved={**json.loads(path.read_text()),'durable_delegation_enabled':True}
    path.write_text(json.dumps(saved))
    return path,saved


def test_crashed_delegated_host_is_respawned_and_pause_or_exit_never_restarts_native_client(tmp_path):
    path,saved=delegated(tmp_path);root=tmp_path/'install'
    state=root/'state/agent-supervisor';state.mkdir(parents=True)
    integration=state/'one.json'
    integration.write_text(json.dumps({'registration_id':saved['registration_id'],'session_id':'native-session',
        'generation':1,'owner':'native-process','runtime':'codex','registration_path':str(path),'desired_state':'running'}))
    children=[];commands=[]
    def spawn(command,**kwargs):
        commands.append(command);child=Process();children.append(child);return child
    supervisor=Supervisor({'root':str(root)},spawn=spawn,alive=lambda owner:True)
    supervisor.tick();supervisor.tick()
    assert len(children)==1
    children[0].code=9;supervisor.tick()
    assert len(children)==2
    value=json.loads(integration.read_text());value['desired_state']='paused';integration.write_text(json.dumps(value))
    supervisor.tick();supervisor.tick()
    assert children[-1].stopped and len(children)==2
    # The only process is the delegated-runtime host of the exact session; the native client is never launched.
    assert all(command[1:3]==['bazarlyk.bootstrap.supervisor','--watch'] for command in commands)


@pytest.mark.parametrize('runtime,delegation',[('claude-code',True),('codex',False)])
def test_a_session_without_a_delegated_runtime_gets_no_process(tmp_path,runtime,delegation):
    path,saved=delegated(tmp_path);root=tmp_path/'install'
    path.write_text(json.dumps({**saved,'durable_delegation_enabled':delegation}))
    state=root/'state/agent-supervisor';state.mkdir(parents=True)
    (state/'one.json').write_text(json.dumps({'registration_id':saved['registration_id'],'session_id':'native-session',
        'generation':1,'owner':'native-process','runtime':runtime,'registration_path':str(path),'desired_state':'running'}))
    children=[]
    supervisor=Supervisor({'root':str(root)},spawn=lambda *a,**k:children.append(Process()),alive=lambda owner:True)
    supervisor.reference_check_at=10**9
    supervisor.tick();supervisor.tick()
    assert not children and not supervisor.children


def test_supervisor_reconstructs_from_disk_but_dead_session_stays_offline(tmp_path):
    supervisor=Supervisor({'root':str(tmp_path)},spawn=lambda *a,**kw:None,alive=lambda owner:False)
    supervisor.tick()
    assert not supervisor.children


def test_foreign_host_and_terminal_denial_are_not_respawned(tmp_path):
    from bazarlyk.release_worker.lifetime import atomic_json
    path,saved=delegated(tmp_path);root=tmp_path/'install'
    item={'registration_id':saved['registration_id'],'session_id':'native-session','generation':1,'owner':'native-process',
        'runtime':'codex','registration_path':str(path),'desired_state':'running','host':'another-host'}
    state=root/'state/agent-supervisor/one.json';atomic_json(state,item)
    children=[]
    supervisor=Supervisor({'root':str(root)},spawn=lambda *a,**k:children.append(Process()),alive=lambda owner:True)
    supervisor.tick();assert not children
    item.pop('host');atomic_json(state,item)
    atomic_json(root/'state/helper-faults'/f"{saved['registration_id']}.json",{**item,'code':'forbidden'})
    supervisor.tick();assert not children
    supervisor.pool.shutdown(wait=True)


def test_background_repair_cannot_apply_a_different_native_observation(tmp_path):
    import pytest
    from qorqut_operations.contracts import DomainError
    from bazarlyk.release_worker.lifetime import atomic_json
    path=registration(tmp_path,capture=False);saved=json.loads(path.read_text())
    item={'registration_path':str(path),'registration_id':saved['registration_id'],'session_id':'one',
        'owner':'123:456','runtime':'codex','event':'prompt'}
    atomic_json(tmp_path/'.bazarlyk/native-binding.json',{**item,'owner':'123:999'})
    supervisor=Supervisor({'root':str(tmp_path/'install')})
    with pytest.raises(DomainError) as error:supervisor.recover(item)
    assert error.value.code=='inbox_generation_stale'
    supervisor.pool.shutdown(wait=True)


def test_a_pending_legacy_handoff_is_retried_without_claiming_a_binding(tmp_path,monkeypatch):
    from bazarlyk.bootstrap import supervisor as module
    from qorqut_operations.contracts import DomainError
    from bazarlyk.release_worker.lifetime import atomic_json
    path=registration(tmp_path,capture=False);saved=json.loads(path.read_text());root=tmp_path/'install'
    item={'registration_path':str(path),'registration_id':saved['registration_id'],'session_id':'one',
        'owner':'123:456','runtime':'claude-code','event':'prompt','host':__import__('socket').gethostname()}
    atomic_json(tmp_path/'.bazarlyk/native-binding.json',item)
    atomic_json(root/'state/agent-recovery/pending.json',item)
    class Client:
        def __init__(self,registration,*,timeout):pass
        def post(self,route,body):
            # The backend keeps the pre-fencing protocol while a legacy helper consumer lives: no generation.
            return {'ready':True,'inbox_consumer_id':'one','inbox_legacy_handoff':'pending'}
    monkeypatch.setattr(module,'BootstrapClient',Client)
    supervisor=Supervisor({'root':str(root)},spawn=lambda *a,**k:pytest.fail('a pending handoff must not spawn a process'),alive=lambda owner:True)
    with pytest.raises(DomainError) as error:supervisor.recover(item)
    assert error.value.code=='bootstrap_unavailable'
    clock=[1000.0];monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    supervisor.reference_check_at=10**9
    pending=root/'state/agent-recovery/pending.json'
    supervisor.tick()  # starts the repair attempt
    wait(list(supervisor.recoveries.values()))
    supervisor.tick()  # observes its failure
    supervisor.pool.shutdown(wait=True)
    assert pending.exists() and supervisor.retry_at[pending][0]==1001.0
    # No fenced entry, generation or candidate is claimed; the hook observation is evidence of the session, never of ownership.
    assert not list((root/'state/agent-supervisor').glob('*.json'))
    assert not (root/'state/legacy-candidates').exists()
    assert not (tmp_path/'.bazarlyk/session-binding.json').exists()
