"""Cached commands are owned by exact native lifetimes and individual slots.

Projection discovery is conservative. No shipped native adapter currently proves
per-slot reload; generic hook/MCP/watcher activity cannot release these records.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re

from qorqut_operations.contracts import DomainError
from bazarlyk.release_worker.lifetime import atomic_json, exclusion, identity, local_instance, reference_lock, process_state


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def owner_identity(owner, *, registration_id=None, session_id=None, binding=None):
    pid, started = owner.split(':')[:2]
    value = {**(binding or local_instance()), 'pid': int(pid), 'started': started,
             'registration_id': registration_id or 'legacy_unknown_registration',
             'session_id': session_id or None,
             'conversation_provenance': 'verified' if session_id else 'ambiguous'}
    value['target'] = value.get('execution_target', value.get('target', 'local'))
    # Never persist private native arguments or claim a thread from a shared PID.
    return {name: value.get(name) for name in ('host','target','target_instance','boot_id','pid_namespace',
             'pid','started','registration_id','session_id','conversation_provenance')}


def paths(root, value):
    text = json.dumps(value) if not isinstance(value, str) else value
    # Only managed installation paths; original command/private arguments stay out.
    return sorted(set(re.findall(re.escape(str(Path(root))) + r'/[^\s\"\'\\;,\]\}]+', text)))


def projection_slots(root, projections):
    """Capture overlays, event/matcher/command indexes and MCP server separately."""
    result = []
    for source, body in projections.items():
        if isinstance(body, bytes): body = body.decode(errors='replace')
        projection = digest(body)
        try:
            value = json.loads(body)
        except (ValueError, TypeError):
            try:
                import tomllib
                value = tomllib.loads(body)
            except (ValueError, TypeError): value = None
        entries = []
        if isinstance(value, dict):
            for event, matchers in value.get('hooks', {}).items():
                for index, matcher in enumerate(matchers):
                    for command_index, command in enumerate(matcher.get('hooks', [])):
                        entries.append((['hook',event,matcher.get('matcher',''),index,command_index],command))
            for key in ('mcpServers','mcp_servers'):
                for server, entry in value.get(key, {}).items(): entries.append((['mcp',server],entry))
        if not entries: entries = [(['projection'], body)]
        for location, invocation in entries:
            references = paths(root, invocation)
            if not references: continue
            invocation_text = json.dumps(invocation)
            kind = 'mcp' if location[0] == 'mcp' else next((name for name, marker in
                (('activity','activity_hook'),('transcript','transcript_snapshot'),('browser','browser'),
                 ('watcher','watcher'),('bootstrap','claude_session'),('bootstrap','codex_session'))
                if marker in invocation_text), 'unknown')
            provenance = {'source': str(source), 'location': location, 'projection': projection,
                          'invocation': digest(invocation), 'kind': kind}
            result.append({'id':digest(provenance), **provenance, 'references':references,
                           'state':'pending', 'reason':'native_slot_reload_unproved' if kind != 'unknown' else 'unknown_invocation_root'})
        # Retain roots not accounted for by recognized slots (scripts/imports/config).
        covered = {ref for entry in result if entry['source'] == str(source) for ref in entry['references']}
        remainder = set(paths(root, body)) - covered
        if remainder:
            provenance = {'source':str(source),'location':['unresolved'],'projection':projection,
                          'invocation':digest(sorted(remainder)),'kind':'unknown'}
            result.append({'id':digest(provenance),**provenance,'references':sorted(remainder),
                           'state':'pending','reason':'unknown_invocation_root'})
    return result


def cache(root, owner, references=(), *, registration_id=None, session_id=None, binding=None, slots=(), invocation_observed=False):
    group = owner_identity(owner, registration_id=registration_id, session_id=session_id, binding=binding)
    group['installation_root'] = str(Path(root))
    if group['target'] == 'local' and identity(group['pid']) != group['started']:
        raise DomainError('inbox_binding_ambiguous','Cached commands need a verified native process start')
    if group['target'] == 'local' and all(group.get(key) for key in ('target_instance','boot_id','pid_namespace')):
        if process_state(group)[0] != 'alive':
            raise DomainError('inbox_binding_ambiguous','Cached commands need the same verified target/boot/namespace lifetime')
    path = Path(root)/'state/process-pins'/('cached-'+digest(group)+'.json')
    with exclusion(root, shared=True), reference_lock(root):
        value = json.loads(path.read_text()) if path.exists() else {'format':2,'cached_client':True,**group,'slots':[]}
        existing = {slot['id']:slot for slot in value['slots']}
        for slot in slots:
            # Observing the old captured invocation again invalidates a migrated claim.
            previous = existing.get(slot['id'])
            if previous and previous['state']=='pending' and not invocation_observed:continue
            existing[slot['id']] = {**slot,'state':'pending','discovery':(previous or {}).get('discovery',0)+1}
        if references:
            refs = sorted(set(map(str,references)))
            entry = {'source':'legacy_coarse','location':['unresolved'],'projection':digest(refs),
                     'invocation':digest(refs),'kind':'unknown'}
            entry = {'id':digest(entry),**entry,'references':refs,'state':'pending','reason':'legacy_slot_identity_unproved'}
            existing[entry['id']] = entry
        value['slots'] = sorted(existing.values(), key=lambda slot:slot['id'])
        value['references'] = sorted({ref for slot in value['slots'] for ref in slot['references']})
        atomic_json(path, value)
    return path


def observed(root, owner, feature):
    """Compatibility for generic counters: they never attest any invocation slot."""
    return False


def pending_references(value):
    if value.get('format') != 2:
        return value.get('references', []), ['legacy_slot_identity_unproved']
    pending = [slot for slot in value['slots'] if slot.get('state') != 'resolved']
    return sorted({ref for slot in pending for ref in slot['references']}), sorted({slot['reason'] for slot in pending})


def resolve_slots(value, evidence):
    """Called only for a fresh response from the configured trusted probe adapter."""
    for item in evidence:
        if item.get('owner') != {key:value.get(key) for key in owner_identity_keys()}:
            continue
        for slot in value.get('slots', []):
            if slot['kind'] == 'unknown':continue
            provenance = item.get('native_provenance', {})
            exact = (isinstance(provenance, dict) and provenance.get('client_build')
                     and provenance.get('supported_contract') and provenance.get('slot_enumeration_complete') is True
                     and provenance.get('source') == slot['source'] and provenance.get('location') == slot['location'])
            replacement = item.get('replacement_references', [])
            stable = bool(replacement) and all(
                '..' not in Path(reference).parts and (reference == str(Path(value['installation_root'])/'bin/agent-helper')
                or reference.startswith(str(Path(value['installation_root'])/'runtime/agent-tools')+'/'))
                for reference in replacement)
            if (item.get('slot_id') == slot['id'] and item.get('projection') == slot['projection']
                    and item.get('invocation') == slot['invocation'] and item.get('discovery') == slot.get('discovery')
                    and exact and (stable or item.get('result') == 'native_slot_removed')
                    and item.get('result') in {'stable_invocation_verified','native_slot_removed'}):
                slot['state'] = 'resolved'; slot['reason'] = item['result']
    return value


def owner_identity_keys():
    return ('installation_root','host','target','target_instance','boot_id','pid_namespace','pid','started',
            'registration_id','session_id','conversation_provenance')
