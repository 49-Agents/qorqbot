"""Read-only supplier baselines and explicit custom source retention."""
from __future__ import annotations

from copy import deepcopy
import json

from bazarlyk.private_files import read_private_file
from .plans import _validate_manifest
from .support import digest
from .upgrade import MAX_RECORD, _unique, history_directory, history_snapshot


def comparison_baseline(paths, record):
    """Return verified migration overrides and the proof identity that authorizes them.

    The caller has already validated the installed binding and completed layout proof.
    This function reads only that hash-linked immutable evidence.
    """
    metadata = record.get('layout_upgrade')
    if metadata is None:
        return {'layout_proof': None, 'files': {}}
    from .layout_intent import _read_intent
    intent = _read_intent(history_directory(paths) / ('layout-' + metadata['plan_digest'] + '.json'))
    raw = read_private_file(history_snapshot(paths, metadata['prior_binding_sha256']), max_bytes=MAX_RECORD)
    if (digest(raw) != metadata['prior_binding_sha256'] or intent['receipt_sha256'] != metadata['intent_sha256']
            or intent['prior_binding_sha256'] != metadata['prior_binding_sha256']):
        raise ValueError('Layout comparison proof differs from binding')
    prior = json.loads(raw, object_pairs_hook=_unique)
    supplier = dict(_validate_manifest(prior['import_manifest']))
    for key, package in (('management_seed', 'agentic-management'), ('overseer_seed', 'overseer')):
        for name, sha in prior[key]['files'].items():
            path = 'packages/' + package + '/' + name
            if path in supplier and supplier[path] != sha:
                raise ValueError('Ambiguous legacy supplier baseline: ' + path)
            supplier[path] = sha
    plan = intent['plan']
    overrides, origins = {}, {}
    for old, targets in plan['path_map'].items():
        if not isinstance(targets, list) or len(targets) != len(set(targets)):
            raise ValueError('Ambiguous layout comparison mapping: ' + old)
        for target in targets:
            if target in origins and origins[target] != old:
                raise ValueError('Conflicting layout comparison targets: ' + target)
            origins[target] = old
        captured = plan['legacy_authored_files'].get(old)
        if old not in supplier or captured is None or captured['sha256'] != supplier[old]:
            continue
        for target in targets:
            item = plan['files'].get(target)
            if not isinstance(item, dict) or 'sha256' not in item:
                raise ValueError('Missing layout comparison target: ' + target)
            sha = item['sha256']
            overrides[target] = sha
    return {'layout_proof': {'plan_digest': metadata['plan_digest'],
                             'intent_sha256': metadata['intent_sha256'],
                             'prior_binding_sha256': metadata['prior_binding_sha256']},
            'files': dict(sorted(overrides.items()))}


def _custom(source_id):
    return source_id.startswith('custom-') or source_id == 'email-guidance'


def target_inventory(prior, standard, selected, manifest_sha):
    """Retain absent explicit custom snapshots without changing their authority."""
    from .installation import _verify_inventory
    from .view import HostAgenticView
    HostAgenticView(None, prior)
    result = deepcopy(standard)
    sources = {s['id']: s for s in result['sources']}
    prior_ids = {s['id'] for s in prior['sources']}
    if any(_custom(sid) and sid not in prior_ids for sid in sources):
        raise ValueError('New explicit custom source requires separate registration')
    custom = set()
    for source in prior['sources']:
        sid = source['id']
        if not _custom(sid):
            continue
        if sid in sources:
            if sources[sid] != source:
                raise ValueError('Conflicting explicit custom source: ' + sid)
        else:
            result['sources'].append(deepcopy(source))
            sources[sid] = source
        custom.add(sid)
    packages = {p['id']: p for p in result['packages']}
    for old in prior['packages']:
        chosen = {p: f for p, f in old['files'].items() if f['provenance']['source_id'] in custom}
        if not chosen:
            continue
        ident = old['id']
        if ident not in packages:
            # An independent package must be wholly owned by retained custom sources.
            if len(chosen) != len(old['files']):
                raise ValueError('Ambiguous mixed-source custom package: ' + ident)
            package = deepcopy(old)
            result['packages'].append(package)
            packages[ident] = package
            continue
        package = packages[ident]
        for key in set(old) | set(package):
            if key not in ('files', 'entries') and old.get(key) != package.get(key):
                raise ValueError('Conflicting custom package: ' + ident)
        for path, item in chosen.items():
            if path in package['files'] and package['files'][path] != item:
                raise ValueError('Conflicting custom package path: ' + ident + '/' + path)
            package['files'][path] = deepcopy(item)
        entries = {e['id']: e for e in package['entries']}
        for entry in old['entries']:
            if entry['path'] not in chosen:
                continue
            if entry['id'] in entries and entries[entry['id']] != entry:
                raise ValueError('Conflicting custom entry: ' + ident + '/' + entry['id'])
            if entry['id'] not in entries:
                package['entries'].append(deepcopy(entry))
        package['entries'].sort(key=lambda e: e['id'])
        package['files'] = dict(sorted(package['files'].items()))
    for field, key in (('assets', lambda x: x['destination']),
                       ('bindings', lambda x: (x['kind'], x['native_id']))):
        existing = {key(x): x for x in result[field]}
        for item in prior[field]:
            if item['source_id'] not in custom:
                continue
            identity = key(item)
            if identity in existing and existing[identity] != item:
                raise ValueError('Conflicting custom ' + field + ': ' + str(identity))
            if identity not in existing:
                result[field].append(deepcopy(item))
                existing[identity] = item
    result['sources'].sort(key=lambda s: s['id'])
    result['source_digest'] = digest(result['sources'])
    result['coverage']['asset_count'] = len(result['assets'])
    result['coverage']['package_count'] = len(result['packages'])
    _verify_inventory(result, selected, manifest_sha)
    HostAgenticView(None, result)
    return result
