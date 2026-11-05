import json
from pathlib import Path
from bazarlyk.bootstrap.bridge_binding import BridgeBinding


def fixture(tmp_path,args):
    proc=tmp_path/'proc/1';proc.mkdir(parents=True);proc.joinpath('cmdline').write_bytes(b'\0'.join(args))
    workspace=tmp_path/'agent';workspace.joinpath('.bazarlyk').mkdir(parents=True)
    registration={'registration_id':'one','workspace':str(workspace)}
    return proc.parent,workspace,registration


def test_two_shared_threads_and_out_of_order_markers_never_bind(tmp_path):
    proc,workspace,registration=fixture(tmp_path,[b'codex',b'app-server'])
    bridge=BridgeBinding(registration,proc=proc,ancestor=lambda runtime:{'pid':1,'started':'123'} if runtime=='codex' else None)
    for session in ('first','second','first'):
        workspace.joinpath('.bazarlyk/session-binding.json').write_text(json.dumps({'registration_id':'one','session_id':session,'generation':1,'owner':'1:123','runtime':'codex'}))
        assert bridge.headers()=={'X-Qorqut-Binding-Reason':'shared_app_server_without_thread_provenance'}
        assert bridge.bound is None


def test_process_scoped_association_is_immutable_and_loss_is_precise(tmp_path,monkeypatch):
    proc,workspace,registration=fixture(tmp_path,[b'codex'])
    monkeypatch.setattr('bazarlyk.bootstrap.bridge_binding.owner_alive',lambda owner:True)
    native={'owner':'1:123','session_id':'first'}
    value={'registration_id':'one','session_id':'first','generation':1,'owner':'1:123','runtime':'codex'}
    workspace.joinpath('.bazarlyk/native-binding.json').write_text(json.dumps(native))
    workspace.joinpath('.bazarlyk/session-binding.json').write_text(json.dumps(value))
    bridge=BridgeBinding(registration,proc=proc,ancestor=lambda runtime:{'pid':1,'started':'123'} if runtime=='codex' else None)
    assert bridge.headers()['X-Qorqut-Native-Session']=='first'
    value['session_id']='second';workspace.joinpath('.bazarlyk/session-binding.json').write_text(json.dumps(value))
    assert bridge.headers()['X-Qorqut-Native-Session']=='first'
    monkeypatch.setattr('bazarlyk.bootstrap.bridge_binding.owner_alive',lambda owner:False)
    assert bridge.headers()['X-Qorqut-Binding-Reason']=='native_process_binding_lost'


def test_a_fenced_bridge_always_identifies_itself_so_only_a_pre_fencing_bridge_sends_no_binding_header(tmp_path,monkeypatch):
    proc,workspace,registration=fixture(tmp_path,[b'codex'])
    monkeypatch.setattr('bazarlyk.bootstrap.bridge_binding.owner_alive',lambda owner:True)
    bridge=BridgeBinding(registration,proc=proc,ancestor=lambda runtime:{'pid':1,'started':'123'} if runtime=='codex' else None)
    unbound={'X-Qorqut-Binding-Reason':'native_session_association_unavailable'}
    assert bridge.headers()==unbound  # no hook observation yet
    native=workspace/'.bazarlyk/native-binding.json';binding=workspace/'.bazarlyk/session-binding.json'
    native.write_text(json.dumps({'owner':'9:9','session_id':'first'}))
    binding.write_text(json.dumps({'registration_id':'one','session_id':'first','generation':1,'owner':'1:123','runtime':'codex'}))
    assert bridge.headers()==unbound  # a different native owner
    native.write_text('{')
    assert bridge.headers()==unbound  # unreadable observation
    # No ancestor at all (a diagnostic or detached launch) is also explicit.
    detached=BridgeBinding(registration,proc=proc,ancestor=lambda runtime:None)
    assert detached.headers()==unbound
