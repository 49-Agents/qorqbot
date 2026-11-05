import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bazarlyk.bootstrap.codex_session import process_identity
from bazarlyk.bootstrap.runtime_references import (cache,projection_slots,pending_references,
    owner_identity_keys,resolve_slots,observed)
from bazarlyk.release_worker.lifetime import atomic_json,local_instance,process_state


def capture(tmp_path, registration='one', session='thread-one', projections=None):
    owner=process_identity(os.getpid())
    slots=projection_slots(tmp_path,projections or {'.claude/settings.local.json':json.dumps({'hooks':{
        'SessionStart':[{'matcher':'startup','hooks':[{'command':str(tmp_path/'releases/old/python')+' -m bazarlyk.bootstrap.claude_session'}]}]}})})
    path=cache(tmp_path,owner,registration_id=registration,session_id=session,slots=slots)
    return path,json.loads(path.read_text())


def evidence(value, slot, **changes):
    return {'owner':{key:value.get(key) for key in owner_identity_keys()},'slot_id':slot['id'],
        'projection':slot['projection'],'invocation':slot['invocation'],'discovery':slot['discovery'],
        'native_provenance':{'client_build':'fixture-build','supported_contract':'isolated_exact_slot_contract',
            'source':slot['source'],'location':slot['location'],'slot_enumeration_complete':True},
        'replacement_references':[str(Path(value['installation_root'])/'bin/agent-helper')],
        'result':'stable_invocation_verified',**changes}


def test_two_registrations_threads_and_ambiguous_bucket_are_separate(tmp_path):
    first,value=capture(tmp_path)
    second,_=capture(tmp_path,'two')
    third,_=capture(tmp_path,'one','thread-two')
    ambiguous,_=capture(tmp_path,'one',None)
    assert len({first,second,third,ambiguous})==4
    resolve_slots(value,[evidence(value,value['slots'][0])]);atomic_json(first,value)
    assert pending_references(json.loads(ambiguous.read_text()))[0]
    assert pending_references(json.loads(second.read_text()))[0]


def test_slots_cover_distinct_hooks_overlay_activity_transcript_browser_and_unknown(tmp_path):
    def command(name):return {'command':str(tmp_path/'releases/old/python')+' -m bazarlyk.bootstrap.'+name}
    settings={'hooks':{'Stop':[{'matcher':'same','hooks':[command('activity_hook'),command('activity_hook'),command('transcript_snapshot')]}]}}
    slots=projection_slots(tmp_path,{'.claude/settings.json':json.dumps(settings),'.claude/settings.local.json':json.dumps(settings),
        '.mcp.json':json.dumps({'mcpServers':{'first':command('mcp_reconnect'),'second':command('mcp_reconnect')}}),
        '.bazarlyk/browser':str(tmp_path/'releases/old/browser-wrapper')+' --browser',
        '.bazarlyk/unknown':str(tmp_path/'releases/old/private-script')+' secret-token'})
    assert len(slots)==10 and len({slot['id'] for slot in slots})==10
    assert {'activity','transcript','browser','mcp','unknown'}<=set(slot['kind'] for slot in slots)
    assert 'secret-token' not in json.dumps(slots)
    value={'format':2,'slots':slots}
    assert any(reason=='unknown_invocation_root' for reason in pending_references(value)[1])


def test_union_later_changed_slot_and_concurrent_discovery(tmp_path):
    path,value=capture(tmp_path)
    body=path.read_bytes();capture(tmp_path);assert path.read_bytes()==body
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda index:capture(tmp_path,projections={str(index):str(tmp_path/f'releases/{index}/script')}),range(24)))
    actual=json.loads(path.read_text())
    assert len(actual['slots'])==25
    assert {str(tmp_path/f'releases/{index}/script') for index in range(24)}<=set(actual['references'])
    assert len({item[0] for item in results})==1
    original_ids={slot['id'] for slot in actual['slots']}
    capture(tmp_path,projections={'.claude/settings.local.json':str(tmp_path/'releases/changed/script')})
    current=json.loads(path.read_text());assert original_ids<set(slot['id'] for slot in current['slots'])


def test_exact_slot_only_and_old_reobservation_reopens(tmp_path):
    path,value=capture(tmp_path)
    item=evidence(value,value['slots'][0])
    wrong={**item,'owner':{**item['owner'],'registration_id':'other'}}
    resolve_slots(value,[wrong]);assert pending_references(value)[0]
    resolve_slots(value,[{**item,'projection':'wrong'}]);assert pending_references(value)[0]
    resolve_slots(value,[item]);assert not pending_references(value)[0]
    atomic_json(path,value)
    capture(tmp_path)
    current=json.loads(path.read_text());assert pending_references(current)[0]
    resolve_slots(current,[item]);assert pending_references(current)[0]  # stale discovery epoch
    assert observed(tmp_path,process_identity(os.getpid()),'mcp') is False


def test_boot_instance_namespace_and_pid_reuse_are_separate():
    value={**local_instance(),'pid':os.getpid(),'started':process_identity(os.getpid()).split(':')[1]}
    assert process_state(value)[0]=='alive'
    assert process_state({**value,'started':'0'})[0]=='exited'
    assert process_state({**value,'boot_id':'old-boot'})[0]=='exited'
    assert process_state({**value,'target_instance':'different-machine'})[0]=='unknown'
    assert process_state({**value,'pid_namespace':'other-namespace'})[0]=='unknown'


def test_generic_provenance_unenumerated_overlay_and_unstable_replacement_cannot_migrate(tmp_path):
    _,value=capture(tmp_path);item=evidence(value,value['slots'][0])
    for changes in ({'native_provenance':'generic_mcp_call'},
        {'native_provenance':{**item['native_provenance'],'slot_enumeration_complete':False}},
        {'native_provenance':{**item['native_provenance'],'location':['other-overlay']}},
        {'replacement_references':[str(tmp_path/'releases/other/python')]},
        {'replacement_references':[str(tmp_path/'runtime/agent-tools/x/../../../releases/other')]}):
        resolve_slots(value,[{**item,**changes}]);assert pending_references(value)[0]
