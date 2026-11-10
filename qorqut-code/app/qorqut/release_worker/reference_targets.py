"""Fresh reference attestations through configured authenticated target adapters."""
from __future__ import annotations
import json
from pathlib import Path
import time
from uuid import uuid4

from .lifetime import atomic_json, reference_lock, retire_process_pin


def configured_adapters(config):
    """No remote execution-target transport is installed: every non-local reference stays unknown."""
    return {}


def request_for(adapter, records):
    return {'version':1,'request_id':uuid4().hex,'nonce':uuid4().hex,
            'target':adapter.target,'target_instance':adapter.instance,
            'records':[{**value,'id':path.parent.name+'/'+path.name} for path,value in records]}


def validate_response(request, response):
    if not isinstance(response,dict) or any(response.get(key) != request[key] for key in
            ('version','request_id','nonce','target','target_instance')):
        raise ValueError('stale_or_wrong_target_probe_response')
    lifecycle=(response.get('provenance')=='trusted_target_lifecycle'
               and response.get('target_lifecycle') in {'destroyed','stopped'})
    if not lifecycle and (not response.get('boot_id') or not response.get('pid_namespace')
            or response.get('provenance') != 'installed_target_local_probe'):
        raise ValueError('unproved_target_probe_provenance')
    requested = {item['id']:item for item in request['records']}
    entries = {}
    for item in response.get('processes', []):
        original = requested.get(item.get('id'))
        if (original is None or item['id'] in entries or item.get('pid') != original['pid']
                or item.get('started') != original['started'] or item.get('state') not in {'alive','exited','unknown'}):
            raise ValueError('wrong_or_duplicate_process_probe_response')
        if item['id'] not in response.get('coverage', []): continue
        # Historical identity cannot be refined by unproved current target facts.
        if lifecycle and original.get('target_instance') == response['target_instance']:
            item={**item,'state':'exited','reason':'verified_exact_container_'+response['target_lifecycle'],'slot_evidence':[]}
        elif not all(original.get(key) for key in ('target_instance','boot_id','pid_namespace')):
            item = {**item,'state':'unknown','reason':'historical_target_identity_unproved','slot_evidence':[]}
        elif original['target_instance'] != response['target_instance']:
            item = {**item,'state':'unknown','reason':'target_instance_changed','slot_evidence':[]}
        elif original['boot_id'] != response['boot_id']:
            if item['state'] != 'exited' or item.get('reason') != 'verified_target_reboot':
                item={**item,'state':'unknown','reason':'boot_change_without_process_exit_evidence','slot_evidence':[]}
        elif (original['pid_namespace'] != response['pid_namespace'] and original['boot_id'] == response['boot_id']):
            item = {**item,'state':'unknown','reason':'pid_namespace_changed_without_lifecycle_proof','slot_evidence':[]}
        if item['state'] == 'alive' and item.get('observed_started') != original['started']:
            raise ValueError('unproved_live_process_identity')
        if item['state'] == 'exited' and item.get('observed_started') == original['started']:
            raise ValueError('unproved_exited_process_identity')
        if not response.get('capabilities',{}).get('exact_native_slots'):
            item={**item,'slot_evidence':[]}
        entries[item['id']] = item
    return entries


def reconcile(config, *, adapters=None, retire_exited=False):
    """Bounded fresh maintenance; only the invoked configured transport attests."""
    from qorqut.bootstrap.runtime_references import resolve_slots
    root = Path(config['root']); adapters = configured_adapters(config) if adapters is None else adapters
    records = []
    for directory in ('process-pins','cached-client-targets'):
        for path in sorted(root.joinpath('state',directory).glob('*.json')):
            try:
                value = json.loads(path.read_text())
                if not isinstance(value, dict):
                    continue
                if retire_exited and directory == 'process-pins' and value.get('cached_client', False) is False:
                    retire_process_pin(root, path)
                if value.get('target','local') != 'local': records.append((path,value))
            except (OSError,ValueError): continue
    outcomes = {}
    # Total work is bounded independently of the number of old records/targets.
    deadline = time.monotonic()+5
    for target in sorted({value.get('target') for _,value in records}|set(adapters), key=str):
        all_records = [(path,value) for path,value in records if value.get('target') == target]
        selected = []
        for index,(path,value) in enumerate(all_records):
            if index>=1000 or not all(key in value for key in ('pid','started')):
                outcomes[path.parent.name+'/'+path.name]={'state':'unknown','reason':
                    'target_probe_batch_limit' if index>=1000 else 'reference_identity_incomplete'}
            else:selected.append((path,value))
        adapter = adapters.get(target)
        if adapter is None or adapter.target!=target or time.monotonic() >= deadline:
            for path,_ in selected: outcomes[path.parent.name+'/'+path.name] = {'state':'unknown','reason':'configured_target_transport_unavailable'}
            outcomes['target:'+str(target)]={'state':'unknown','reason':'configured_target_transport_unavailable','references':[]}
            continue
        request = request_for(adapter, selected)
        try:
            response=adapter.probe(request)
            entries = validate_response(request, response)
            outcomes['target:'+str(target)]={'state':'unknown' if response.get('errors') else 'observed',
                'reason':'target_reference_coverage_incomplete' if response.get('errors') else 'fresh_target_execution_pins',
                'references':response.get('additional_references',[])}
        except Exception as error:
            entries = {}
            from qorqut_operations.contracts import DomainError
            failure = ('target_probe_'+error.code if isinstance(error,DomainError) else
                       'target_probe_missing_helper' if isinstance(error,FileNotFoundError) else
                       'target_probe_invalid_response' if isinstance(error,(ValueError,KeyError,TypeError)) else
                       'target_probe_unavailable')
            outcomes['target:'+str(target)]={'state':'unknown','reason':failure,'references':[]}
        else:failure='target_probe_incomplete_or_denied'
        for path, original in selected:
            key=path.parent.name+'/'+path.name
            item = entries.get(key,{'state':'unknown','reason':failure})
            outcomes[key] = {name:item[name] for name in ('state','reason','pid','started','observed_started') if name in item}
            if item['state'] == 'alive' and original.get('cached_client'):
                with reference_lock(root):
                    current = json.loads(path.read_text())
                    # Discovery concurrent with the probe cannot be overwritten.
                    resolve_slots(current,item.get('slot_evidence', []))
                    atomic_json(path,current)
    # Snapshot is diagnostic only. Scanners always invoke a new probe.
    atomic_json(root/'state/reference-target-status.json',{'observations':outcomes})
    return outcomes
