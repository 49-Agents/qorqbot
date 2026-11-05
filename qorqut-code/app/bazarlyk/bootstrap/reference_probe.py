"""Installation-managed, target-local lifetime probe; no model or business API."""
import json
from pathlib import Path
import sys
import time
from bazarlyk.release_worker.lifetime import identity, local_instance, exited_process


def probe(request, *, reference_root=None):
    current = local_instance()
    deadline = time.monotonic()+.5
    processes = []
    for item in request['records']:
        state, reason = 'unknown', 'historical_target_identity_unproved'
        observed = None
        if time.monotonic()>deadline:reason='target_probe_budget_exceeded'
        elif all(item.get(key) for key in ('target_instance','boot_id','pid_namespace','started')):
            if item['target_instance'] != request['target_instance']:
                reason = 'target_instance_mismatch'
            elif item['boot_id'] != current['boot_id']:
                state, reason = 'exited', 'verified_target_reboot'
            elif item['pid_namespace'] != current['pid_namespace']:
                reason = 'pid_namespace_changed_without_lifecycle_proof'
            else:
                observed = identity(item['pid'])
                if observed == item['started']:
                    state, reason = 'alive', 'exact_process_lifetime'
                elif observed is not None or exited_process(item['pid']):
                    state, reason = 'exited', 'verified_process_exit_or_pid_reuse'
                else:
                    reason = 'process_identity_unreadable'
        processes.append({'id':item['id'],'pid':item['pid'],'started':item['started'],
                          'observed_started':observed,'state':state,'reason':reason,'slot_evidence':[]})
    # Target-local execution pins cover closed imports even after parent slot
    # migration. This root is the installed helper's config, never request input.
    references=[];errors=[]
    if reference_root is None:
        from bazarlyk.installation import installation_config
        config=installation_config()
        reference_root=config['root'] if config else None
    if reference_root is None:errors.append('target_installation_config_unavailable')
    else:
        from bazarlyk.release_worker.lifetime import process_state
        from bazarlyk.bootstrap.runtime_references import pending_references
        try:
            for index,path in enumerate(Path(reference_root).joinpath('state/process-pins').glob('*.json')):
                if index>=1000 or time.monotonic()>deadline:
                    errors.append('target_reference_scan_incomplete');break
                try:
                    if path.is_symlink() or path.stat().st_size>1024*1024:raise ValueError('unsafe pin')
                    value=json.loads(path.read_text())
                    owned=(value.get('target')==request['target'] and value.get('target_instance')==request['target_instance'])
                    if not owned and value.get('host')!=current['host']:continue
                    local_value={**value,'target':'local','target_instance':current['target_instance'],'host':current['host']} if owned else value
                    if process_state(local_value)[0]!='exited':
                        references.extend(pending_references(value)[0] if value.get('cached_client') else value['references'])
                except (OSError,ValueError,KeyError):errors.append('target_reference_pin_unreadable')
        except OSError:errors.append('target_reference_root_unreadable')
    return {'version':1,'request_id':request['request_id'],'nonce':request['nonce'],
            'target':request['target'],'target_instance':request['target_instance'],
            'boot_id':current['boot_id'],'pid_namespace':current['pid_namespace'],
            'provenance':'installed_target_local_probe','coverage':[item['id'] for item in request['records']],
            'processes':processes,'errors':sorted(set(errors)), 'additional_references':sorted(set(references)),
            'capabilities':{'exact_native_slots':False,'target_execution_pins':True}}


def main():
    request = json.loads(sys.stdin.read(1024*1024))
    if request['version'] != 1 or len(request['records']) > 1000:
        raise ValueError('Invalid bounded reference probe request')
    print(json.dumps(probe(request), sort_keys=True))


if __name__ == '__main__': main()
