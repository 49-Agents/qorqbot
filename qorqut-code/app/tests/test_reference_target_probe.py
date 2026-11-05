"""Configured Docker transport boundary with isolated helper/provider contracts."""
import json
import os
from types import SimpleNamespace

import pytest

from bazarlyk.bootstrap.codex_session import process_identity
from bazarlyk.bootstrap.reference_probe import probe
from bazarlyk.bootstrap.runtime_references import cache,projection_slots,pending_references
from bazarlyk.release_worker.lifetime import atomic_json,local_instance
from bazarlyk.release_worker.reference_targets import DockerReferenceAdapter,reconcile
from bazarlyk.sandbox.docker import Docker
from test_cached_reference_slots import evidence

INSTANCE='a'*64
BUSINESS='00000000-0000-4000-8000-000000000001'


def _setup(tmp_path, *, transform=None):
    target={'target':'configured-container','transport':'docker','installation':'a'*20,'business_id':BUSINESS,
            'container_id':INSTANCE,'generation':3,'reference_roots':[str(tmp_path)]}
    calls=[]
    def runner(command,**kwargs):
        calls.append((command,kwargs))
        assert kwargs['timeout']<=1
        if 'inspect' in command:
            labels={'qorqut.managed':'business-sandbox','qorqut.installation':'a'*20,'qorqut.business':BUSINESS,
                    'qorqut.role':'agent','qorqut.generation':'3'}
            return SimpleNamespace(returncode=0,stdout=json.dumps([{'Id':INSTANCE,'Config':{'Labels':labels},'State':{'Running':True}}]),stderr='')
        assert command[-1]=='/opt/qorqut/bin/reference-probe' and INSTANCE in command
        request=json.loads(kwargs['input']);response=probe(request,reference_root=tmp_path/'remote-managed-runtime')
        if transform:response=transform(request,response)
        return SimpleNamespace(returncode=0,stdout=json.dumps(response),stderr='')
    adapter=DockerReferenceAdapter(target,docker=Docker(command=('fixture-docker',),runner=runner))
    return {'root':str(tmp_path),'execution_targets':[target]},adapter,calls


def remote_cache(root, *, registration='one', session='first', pid=None, started=None, invocation_observed=False):
    binding={**local_instance(),'target':'configured-container','target_instance':local_instance()['target_instance']+':'+INSTANCE}
    owner=f'{pid or os.getpid()}:{started or process_identity(os.getpid()).split(":")[1]}'
    slots=projection_slots(root,{'.claude/settings.json':str(root/'releases/old/python')+' -m bazarlyk.bootstrap.activity_hook'})
    path=cache(root,owner,binding=binding,registration_id=registration,session_id=session,slots=slots,invocation_observed=invocation_observed)
    return path,json.loads(path.read_text())


def test_exact_slot_evidence_cannot_clear_another_registration_thread_or_overlay(tmp_path):
    first,value=remote_cache(tmp_path)
    other,_=remote_cache(tmp_path,registration='two')
    thread,_=remote_cache(tmp_path,session='second')
    slot=value['slots'][0];item=evidence(value,slot)
    def transform(request,response):
        response['capabilities']['exact_native_slots']=True
        for process in response['processes']:process['slot_evidence']=[item]
        return response
    config,adapter,_=_setup(tmp_path,transform=transform)
    reconcile(config,adapters={adapter.target:adapter})
    assert not pending_references(json.loads(first.read_text()))[0]
    assert pending_references(json.loads(other.read_text()))[0]
    assert pending_references(json.loads(thread.read_text()))[0]
    remote_cache(tmp_path)  # Old command observation reopens it.
    reconcile(config,adapters={adapter.target:adapter})  # Stale evidence cannot close again.
    assert pending_references(json.loads(first.read_text()))[0]


def test_unavailable_fixed_helper_and_unproved_historical_identity_retain(tmp_path):
    config,adapter,_=_setup(tmp_path)
    path,value=remote_cache(tmp_path,pid=99999999,started='1')
    value.pop('boot_id');atomic_json(path,value)
    assert reconcile(config,adapters={adapter.target:adapter})['process-pins/'+path.name]['state']=='unknown'
    def unavailable(*args,**kwargs):raise FileNotFoundError('missing fixed helper')
    adapter.probe=unavailable
    result=reconcile(config,adapters={adapter.target:adapter})
    assert result['process-pins/'+path.name]['reason']=='target_probe_missing_helper'


def test_pid_reuse_exit_does_not_mean_host_or_namespace_replacement(tmp_path):
    config,adapter,_=_setup(tmp_path)
    path,value=remote_cache(tmp_path,started='1')
    assert reconcile(config,adapters={adapter.target:adapter})['process-pins/'+path.name]['state']=='exited'
    value['target_instance']='b'*64;atomic_json(path,value)
    assert reconcile(config,adapters={adapter.target:adapter})['process-pins/'+path.name]['state']=='unknown'


@pytest.mark.parametrize('state',['stopped','destroyed','replacement'])
def test_trusted_docker_lifecycle_proof_uses_original_exact_container(tmp_path,state):
    config,adapter,_=_setup(tmp_path)
    path,value=remote_cache(tmp_path,pid=99999999,started='1')
    # Exact container lifecycle proof can resolve even an unknown old boot.
    value.pop('boot_id');atomic_json(path,value)
    def runner(command,**kwargs):
        assert 'inspect' in command and command[-1]==INSTANCE and kwargs['timeout']<=1
        if state=='destroyed':
            return SimpleNamespace(returncode=1,stdout='',stderr='Error: No such container: '+INSTANCE)
        labels={**adapter.ownership.labels,'qorqut.role':'agent','qorqut.generation':'3'}
        return SimpleNamespace(returncode=0,stdout=json.dumps([{'Id':INSTANCE if state=='stopped' else 'b'*64,
            'Config':{'Labels':labels},'State':{'Running':False}}]),stderr='')
    adapter.docker=Docker(command=('fixture-docker',),runner=runner)
    outcome=reconcile(config,adapters={adapter.target:adapter})['process-pins/'+path.name]
    assert outcome['state']==('unknown' if state=='replacement' else 'exited')


def test_concurrent_discovery_cannot_be_erased_by_inflight_native_slot_evidence(tmp_path):
    path,value=remote_cache(tmp_path)
    item=evidence(value,value['slots'][0])
    def transform(request,response):
        remote_cache(tmp_path,invocation_observed=True)
        response['capabilities']['exact_native_slots']=True
        for process in response['processes']:process['slot_evidence']=[item]
        return response
    config,adapter,_=_setup(tmp_path,transform=transform)
    reconcile(config,adapters={adapter.target:adapter})
    assert pending_references(json.loads(path.read_text()))[0]
