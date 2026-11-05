import io
import json
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from bazarlyk.bootstrap import claude_session, handoff, recovery, supervisor as module
from bazarlyk.bootstrap.client import load_registration, write_json_private
from qorqut_operations.contracts import DomainError, new_id
from test_dual_cli import registration


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = registration(tmp_path, capture=False)
    selected = load_registration(path)
    root = tmp_path / 'install'
    config = root / 'config/installation.json'
    write_json_private(config, {'format':1,'root':str(root)})
    monkeypatch.setenv('QORQUT_INSTALLATION_CONFIG',str(config))
    monkeypatch.setattr(handoff,'running',lambda root:True)
    from contextlib import contextmanager
    from types import SimpleNamespace
    from bazarlyk.bootstrap import catalog_revision
    @contextmanager
    def fake_observer(root, deadline):
        observe = lambda: catalog_revision.observe(root, max(0, deadline-time.monotonic()))
        yield SimpleNamespace(observe=observe, hint=observe)
    monkeypatch.setattr(catalog_revision, 'open_observer', fake_observer)
    monkeypatch.setattr(catalog_revision, 'observe', lambda root, timeout: 'fixture-revision')
    native = {'registration_id':selected['registration_id'],'session_id':new_id(),
        'owner':'123:456','runtime':'codex','event':'prompt','registration_path':str(path)}
    def attest(*args):
        write_json_private(tmp_path/'.bazarlyk/native-binding.json',native)
        return dict(native)
    monkeypatch.setattr('bazarlyk.bootstrap.native_binding.attest',attest)
    monkeypatch.setattr(claude_session,'snapshot_transcript',lambda *a,**k:None)
    worker = module.Supervisor({'root':str(root)},alive=lambda owner:True)
    worker.reference_check_at = 10**12
    yield path, selected, root, native, worker
    worker.pool.shutdown(wait=True)


def invoke(path,native):
    output = io.StringIO()
    claude_session.run({'hook_event_name':'UserPromptSubmit','session_id':native['session_id'],
        'cwd':str(path.parent.parent),'prompt':'Continue the authorized code change'},path,
        stream=output,diagnostics=io.StringIO(),runtime=native['runtime'])
    return json.loads(output.getvalue())


def test_slow_request_keeps_running_and_only_hook_delivery_completes_recovery(setup,monkeypatch):
    path,selected,root,native,worker = setup
    monkeypatch.setattr('bazarlyk.bootstrap.catalog_revision.observe', lambda root, timeout: 'fixture-revision')
    monkeypatch.setattr(handoff,'HOOK_WAIT',.05)
    begun,release = threading.Event(),threading.Event()
    calls = []
    class Client:
        def __init__(self,*a,**k):pass
        def post(self,route,body):
            calls.append(route)
            if route.endswith('/bootstrap'):
                begun.set()
                assert release.wait(3)
                return {'ready':True,'effective_config_id':'current','agentic_catalog_revision':'fixture-revision','mandatory_context':'Current mandatory instructions'}
            return {}
    monkeypatch.setattr(module,'BootstrapClient',Client)
    # First hook queues one request and returns while the service still works.
    before = time.monotonic()
    first = invoke(path,native)
    assert time.monotonic()-before < .5
    assert 'Continue the owner' in first['hookSpecificOutput']['additionalContext']
    episode = recovery.read(selected,native['session_id'])
    worker.tick()
    assert begun.wait(1)
    for _ in range(3):
        invoke(path,native)
        worker.tick()
        assert recovery.read(selected,native['session_id'])['failure_id'] == episode['failure_id']
    assert calls == ['/api/agent/bootstrap']
    release.set()
    next(iter(worker.recoveries.values())).result(timeout=3)
    worker.tick()
    saved = handoff.read(handoff.result_path(root,native))
    assert saved['response']['ready'] and not saved['consumed']
    assert recovery.read(selected,native['session_id']) is not None
    # The same native hook emits the saved response without a second request.
    result = invoke(path,native)
    assert 'Current mandatory instructions' in result['hookSpecificOutput']['additionalContext']
    assert recovery.read(selected,native['session_id']) is None
    assert handoff.read(handoff.result_path(root,native))['consumed']
    assert calls == ['/api/agent/bootstrap']
    receipt = next(root.joinpath('state/agent-followups').glob('*-receipt.json'))
    item = handoff.read(receipt)
    assert item['body']['effective_config_id'] == 'current'
    worker.tick()
    next(iter(worker.followups.values())).result(timeout=3)
    worker.tick()
    assert not receipt.exists()
    assert calls == ['/api/agent/bootstrap','/api/agent/bootstrap/receipt']


def test_owner_change_during_response_cannot_publish_context(setup,monkeypatch):
    path,selected,root,native,worker = setup
    monkeypatch.setattr('bazarlyk.bootstrap.catalog_revision.observe', lambda root, timeout: 'fixture-revision')
    monkeypatch.setattr(handoff,'HOOK_WAIT',0)
    invoke(path,native)
    item = handoff.read(handoff.pending_path(root,native))
    class Client:
        def __init__(self,*a,**k):pass
        def post(self,*a):
            write_json_private(path.parent/'native-binding.json',{**native,'owner':'123:999'})
            return {'ready':True,'agentic_catalog_revision':'fixture-revision','effective_config_id':'current','mandatory_context':'Wrong owner context'}
    monkeypatch.setattr(module,'BootstrapClient',Client)
    with pytest.raises(DomainError) as error:worker.recover(item)
    assert error.value.code == 'inbox_generation_stale'
    assert not handoff.result_path(root,native).exists()


def test_failed_stdout_does_not_consume_context_or_record_delivery(setup,monkeypatch):
    path,selected,root,native,worker = setup
    monkeypatch.setattr(handoff,'HOOK_WAIT',0)
    invoke(path,native)
    item = handoff.read(handoff.pending_path(root,native))
    handoff.publish(root,selected,item,response={'ready':True,'agentic_catalog_revision':'fixture-revision','effective_config_id':'current',
        'mandatory_context':'Current instructions'})
    class Broken(io.StringIO):
        def flush(self):raise OSError('closed native pipe')
    with pytest.raises(OSError):
        claude_session.run({'hook_event_name':'UserPromptSubmit','session_id':native['session_id']},path,
            stream=Broken(),diagnostics=io.StringIO(),runtime='codex')
    assert not handoff.read(handoff.result_path(root,native))['consumed']
    assert recovery.read(selected,native['session_id'])
    assert not list(root.joinpath('state/agent-followups').glob('*-receipt.json'))


def test_episode_is_stable_under_concurrent_failures_and_old_clear_cannot_erase_new_owner(tmp_path):
    reg = {'workspace':str(tmp_path),'registration_id':new_id()}
    session = new_id()
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda _:recovery.record(reg,session,'codex','busy',owner='one'),range(20)))
    assert len({row['failure_id'] for row in rows}) == 1
    fresh = recovery.record(reg,session,'codex','busy',owner='two')
    recovery.clear(reg,session,failure_id=rows[0]['failure_id'])
    assert recovery.read(reg,session) == fresh


def test_identity_denial_from_background_blocks_hook_but_transient_failure_does_not(setup,monkeypatch):
    path,selected,root,native,worker = setup
    monkeypatch.setattr(handoff,'HOOK_WAIT',0)
    invoke(path,native)
    item = handoff.read(handoff.pending_path(root,native))
    class Client:
        def __init__(self,*a,**k):pass
        def post(self,*a):raise DomainError('invalid_credential','Credential revoked')
    monkeypatch.setattr(module,'BootstrapClient',Client)
    worker.recover(item)
    result = invoke(path,native)
    assert result['decision'] == 'block'
    assert 'identity was refused' in result['reason']


def test_new_hook_request_is_not_deleted_by_previous_completion(setup,monkeypatch):
    path,selected,root,native,worker = setup
    monkeypatch.setattr(handoff,'HOOK_WAIT',0)
    invoke(path,native)
    class Client:
        def __init__(self,*a,**k):pass
        def post(self,*a):return {'ready':True,'agentic_catalog_revision':'fixture-revision','effective_config_id':'v1','mandatory_context':'Current instructions'}
    monkeypatch.setattr(module,'BootstrapClient',Client)
    worker.tick()
    next(iter(worker.recoveries.values())).result(timeout=2)
    invoke(path,native)  # consume the first result, before its future is reaped
    saved = handoff.read(handoff.result_path(root,native))
    write_json_private(handoff.result_path(root,native),{**saved,'published_at':0})
    invoke(path,native)  # queue a later freshness check in the same pending slot
    pending = handoff.pending_path(root,native)
    new_request = handoff.read(pending)['request_id']
    worker.tick()
    assert pending.exists() and handoff.read(pending)['request_id'] == new_request


def test_restarted_helper_recovers_existing_failure_and_queues_one_wake(setup,monkeypatch):
    path,selected,root,native,worker = setup
    write_json_private(path.parent/'native-binding.json',native)
    write_json_private(root/'state/agent-supervisor'/f"{native['registration_id']}.json",
        {**native,'generation':1,'desired_state':'running'})
    episode=recovery.record(selected,native['session_id'],'codex','bootstrap_unavailable',owner=native['owner'])
    worker.seed_context_recovery()
    pending=handoff.pending_path(root,native)
    first=handoff.read(pending)
    worker.seed_context_recovery()
    assert handoff.read(pending)['request_id']==first['request_id']
    notices=[]
    class Client:
        def __init__(self,*a,**k):pass
        def post(self,route,body):
            if route.endswith('/bootstrap'):
                return {'ready':True,'agentic_catalog_revision':'fixture-revision','effective_config_id':'v2','mandatory_context':'Updated instructions',
                    'inbox_available':True,'inbox_generation':1,'inbox_consumer_id':native['session_id'],
                    'inbox_session_note':'Updated managed inbox guidance.'}
            assert route.endswith('/recovery')
            notices.append(body)
            return {'state':'submitted'}
    monkeypatch.setattr(module,'BootstrapClient',Client)
    worker.recover(first)
    worker.reconcile_followups()
    next(iter(worker.followups.values())).result(timeout=2)
    worker.reconcile_followups()
    restarted=module.Supervisor({'root':str(root)},alive=lambda owner:True)
    try:
        restarted.seed_context_recovery()
        restarted.reconcile_followups()
        assert notices==[{'registration_id':native['registration_id'],'consumer_id':native['session_id'],
            'notification_id':episode['failure_id'],'runtime':'codex','owner':native['owner'],'binding_generation':1}]
        assert recovery.read(selected,native['session_id']) is not None
        monkeypatch.setattr(handoff,'HOOK_WAIT',0)
        assert 'Updated instructions' in invoke(path,native)['hookSpecificOutput']['additionalContext']
        assert recovery.read(selected,native['session_id']) is None
        assert handoff.read(path.parent/'native-binding.json')['owner']==native['owner']
    finally:restarted.pool.shutdown(wait=True)


def test_retry_backoff_keeps_one_request_and_one_episode(setup,monkeypatch):
    path,selected,root,native,worker = setup
    monkeypatch.setattr(handoff,'HOOK_WAIT',0)
    invoke(path,native)
    pending=handoff.pending_path(root,native)
    request_id=handoff.read(pending)['request_id']
    clock=[100.0];calls=[]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    class Client:
        def __init__(self,*a,**k):pass
        def post(self,*a):
            calls.append(clock[0])
            raise DomainError('bootstrap_unavailable','Timed out',retryable=True)
    monkeypatch.setattr(module,'BootstrapClient',Client)
    for delay in (1,2,5):
        worker.tick()
        with pytest.raises(DomainError):worker.recoveries[pending].result(timeout=2)
        worker.tick()
        assert worker.retry_at[pending][0]==clock[0]+delay
        before=len(calls)
        worker.tick()
        assert len(calls)==before
        assert handoff.read(pending)['request_id']==request_id
        clock[0]+=delay
    assert calls==[100.0,101.0,103.0]


def test_warm_prompt_refresh_does_not_create_an_episode_or_retransmit_context(setup,monkeypatch):
    path,selected,root,native,worker=setup
    monkeypatch.setattr(handoff,'HOOK_WAIT',0)
    invoke(path,native)
    item=handoff.read(handoff.pending_path(root,native))
    handoff.publish(root,selected,item,response={'ready':True,'agentic_catalog_revision':'fixture-revision','effective_config_id':'current','mandatory_context':'Rules'})
    invoke(path,native)
    handoff.discard_pending(handoff.pending_path(root,native),item)
    for _ in range(3):
        assert invoke(path,native)=={}
        assert recovery.read(selected,native['session_id']) is None
    assert not handoff.pending_path(root,native).exists()
    # A later freshness check keeps the delivered digest and never blocks the prompt.
    saved=handoff.read(handoff.result_path(root,native))
    write_json_private(handoff.result_path(root,native),{**saved,'published_at':0})
    assert 'bootstrap_pending' in invoke(path,native)['hookSpecificOutput']['additionalContext']
    pending=handoff.read(handoff.pending_path(root,native))
    assert pending['bootstrap_body']['last_effective_config_id']=='current'
    assert recovery.read(selected,native['session_id']) is None
    handoff.publish(root,selected,pending,response={'ready':True,'agentic_catalog_revision':'fixture-revision','effective_config_id':'current','refresh_required':False})
    assert invoke(path,native)=={}


@pytest.mark.parametrize('unchanged',[True,False])
def test_busy_native_tool_boundary_delivers_without_http_and_only_receipts_complete_output(setup,monkeypatch,unchanged):
    from bazarlyk.bootstrap import activity_context
    path,selected,root,native,worker=setup
    monkeypatch.setattr(handoff,'HOOK_WAIT',.05)
    monkeypatch.setattr('bazarlyk.bootstrap.catalog_revision.observe', lambda root, timeout: 'fixture-revision')
    monkeypatch.setattr('bazarlyk.bootstrap.activity_hook.session_owner',lambda *a:native['owner'])
    invoke(path,native)
    item=handoff.read(handoff.pending_path(root,native))
    response={'ready':True,'agentic_catalog_revision':'fixture-revision','effective_config_id':'old','mandatory_context':'Earlier rules'}
    handoff.publish(root,selected,item,response=response)
    invoke(path,native)
    recovery.record(selected,native['session_id'],'codex','bootstrap_pending',owner=native['owner'])
    handoff.offer(root,path,selected,native,{'event':'prompt','last_effective_config_id':None})
    item=handoff.read(handoff.pending_path(root,native))
    response={'ready':True,'agentic_catalog_revision':'fixture-revision','effective_config_id':'old' if unchanged else 'new','mandatory_context':'rules '*1100}
    handoff.publish(root,selected,item,response=response)
    monkeypatch.setattr(claude_session.BootstrapClient,'post',lambda *a,**kw:pytest.fail('Tool hook performed HTTP'))
    hook={'hook_event_name':'PostToolUse','session_id':native['session_id'],'cwd':str(path.parent.parent)}
    if not unchanged:
        # New instructions require real composer delivery, never a fabricated receipt.
        stream=io.StringIO()
        assert not activity_context.emit(hook,path,'codex',{},stream)
        assert stream.getvalue()==''
        assert recovery.read(selected,native['session_id'])
        assert not handoff.read(handoff.result_path(root,native))['consumed']
        return
    class Broken:
        def write(self,*a):raise BrokenPipeError()
    with pytest.raises(BrokenPipeError):activity_context.emit(hook,path,'codex',{},Broken())
    assert not handoff.read(handoff.result_path(root,native))['consumed']
    assert recovery.read(selected,native['session_id'])
    stream=io.StringIO()
    assert activity_context.emit(hook,path,'codex',{},stream)
    assert 'unchanged' in json.loads(stream.getvalue())['hookSpecificOutput']['additionalContext']
    assert handoff.read(handoff.result_path(root,native))['consumed']
    assert recovery.read(selected,native['session_id']) is None
    assert not activity_context.emit(hook,path,'codex',{},io.StringIO())


def test_old_pending_repair_is_upgraded_to_context_delivery(setup,monkeypatch):
    path,selected,root,native,worker=setup
    write_json_private(path.parent/'native-binding.json',native)
    write_json_private(handoff.pending_path(root,native),native)
    class Client:
        def __init__(self,*a,**kw):pass
        def post(self,*a):return {'ready':True,'agentic_catalog_revision':'fixture-revision','effective_config_id':'current','mandatory_context':'Rules'}
    monkeypatch.setattr(module,'BootstrapClient',Client)
    worker.tick()
    next(iter(worker.recoveries.values())).result(timeout=3)
    worker.tick()
    saved=handoff.read(handoff.result_path(root,native))
    assert saved['request_id'] and not saved['consumed']


def test_transient_warm_check_failure_does_not_invalidate_delivered_instructions(setup,monkeypatch):
    path,selected,root,native,worker=setup
    write_json_private(path.parent/'native-binding.json',native)
    handoff.offer(root,path,selected,native,{'event':'prompt','last_effective_config_id':'delivered'})
    item=handoff.read(handoff.pending_path(root,native))
    class Client:
        def __init__(self,*a,**k):pass
        def post(self,*a):raise DomainError('bootstrap_unavailable','Backend restarting',retryable=True)
    monkeypatch.setattr(module,'BootstrapClient',Client)
    with pytest.raises(DomainError):worker.recover(item)
    assert recovery.read(selected,native['session_id']) is None
    assert handoff.pending_path(root,native).exists()


def test_warm_check_preserves_inbox_and_review_metadata(setup,monkeypatch):
    path,selected,root,native,worker=setup
    write_json_private(path.parent/'native-binding.json',native)
    handoff.offer(root,path,selected,native,{'event':'prompt','last_effective_config_id':None})
    item=handoff.read(handoff.pending_path(root,native))
    response={'ready':True,'agentic_catalog_revision':'fixture-revision','effective_config_id':'current','mandatory_context':'Rules',
        'review':{'role_id':'dev'},'inbox_available':True,'inbox_generation':1,
        'inbox_session_note':'Current managed inbox guidance.'}
    handoff.publish(root,selected,item,response=response)
    handoff.consume(root,selected,item)
    value,deferred=handoff.request(path,selected,native,{'event':'prompt','last_effective_config_id':'current'})
    assert value['review']==response['review']
    assert value['inbox_available'] and value['inbox_generation']==1
    assert value['inbox_session_note']==response['inbox_session_note']
    assert value['refresh_required'] is False and deferred['deferred']
