"""Read-only migrated binding construction from retained layout evidence."""
from __future__ import annotations

import base64
from copy import deepcopy
import json
import hashlib
import re

from bazarlyk.telegram.private_files import read_private_file
from qorqut_agentic.changes import normalize_attribution
from . import layout_git, layout_intent
from .layout_inspection import transform_legacy, transform_inventory, validate_catalog
from .plans import build_import_plan, _validate_manifest
from .support import canonical, digest, relative
from .upgrade import MAX_RECORD, _unique, history_directory, history_snapshot

_SHA = re.compile(r'[0-9a-f]{64}\Z')
_OID = re.compile(r'[0-9a-f]{40}\Z')
_RESOURCE = 'qorqut-agentic/src/qorqut_agentic/resources/defaults/user-preferences'


def validate_legacy_provenance(prior):
    """Validate supported legacy supplier records before any migration effect."""
    for key, resource, required in (
        ('management_seed', 'management', {'agentic.yaml', 'bootstrap.md', 'skills/manage/SKILL.md'}),
        ('overseer_seed', 'overseer', {'agentic.yaml', 'instructions/extract.md'}),
    ):
        seed = prior.get(key)
        if (not isinstance(seed, dict) or set(seed) != {'distribution', 'version', 'resource', 'files'}
                or seed['distribution'] != 'qorqut-agentic' or not isinstance(seed['version'], str)
                or not seed['version'].strip() or seed['resource'] != 'qorqut_agentic/resources/' + resource
                or not isinstance(seed['files'], dict) or not required <= seed['files'].keys()
                or len(seed['files']) > 100000):
            raise ValueError('Unsupported legacy ' + key + ' provenance')
        for path, sha in seed['files'].items():
            try:
                relative(path)
            except (ValueError, TypeError) as exc:
                raise ValueError('Unsupported legacy ' + key + ' provenance') from exc
            if len(path) > 4096 or len(path.split('/')) > 64 or not isinstance(sha, str) or not _SHA.fullmatch(sha):
                raise ValueError('Unsupported legacy ' + key + ' provenance')


def derive_record(prior, prior_raw, plan, intent, git=None):
    """Derive a projection; add final upgrade metadata when a Git receipt exists.

    A projection without a receipt is not a candidate and cannot be activated.
    """
    from .proof_memo import reuse
    return reuse('layout_binding',
                 lambda: (digest(prior), digest(prior_raw), digest(plan), digest(intent), digest(git)),
                 lambda: _derive_record(prior, prior_raw, plan, intent, git))


def _derive_record(prior, prior_raw, plan, intent, git):
    from .installation import _verify_inventory, OVERSEER_REFS
    from .view import HostAgenticView
    if json.loads(prior_raw, object_pairs_hook=_unique) != prior:
        raise ValueError('Prior binding bytes differ from supplied record')
    if (not isinstance(intent, dict) or intent.get('receipt_sha256') != digest({k: v for k, v in intent.items() if k != 'receipt_sha256'})
            or (git is not None and (not isinstance(git, dict) or git.get('receipt_sha256') != digest({k: v for k, v in git.items() if k != 'receipt_sha256'})))):
        raise ValueError('Layout receipt checksum mismatch')
    if (not isinstance(plan, dict) or set(plan) != {'schema_version', 'kind', 'layout_change', 'binding', 'export', 'selected_extensions', 'preservation', 'path_map', 'ref_map', 'rewrites', 'files', 'target_inventory', 'identity', 'state', 'git', 'legacy_authored_files', 'existing_authored_files', 'preference_seed', 'conflicts', 'complete', 'plan_digest'} or plan.get('schema_version') != 2 or plan.get('kind') != 'agentic_layout_v2'
            or plan.get('layout_change') is not True or plan.get('complete') is not True or plan.get('conflicts') != []
            or not isinstance(plan.get('plan_digest'), str) or not _SHA.fullmatch(plan['plan_digest'])
            or digest({k: v for k, v in plan.items() if k != 'plan_digest'}) != plan['plan_digest']
            or intent['plan'] != plan or intent['prior_binding_sha256'] != digest(prior_raw)):
        raise ValueError('Invalid layout plan or prior binding link')
    normalize_attribution(intent['attribution'], trust='host_installer')
    if (not isinstance(prior.get('instance_id'), str) or not re.fullmatch(r'inst_[0-9a-f]{32}', prior['instance_id'])
            or not isinstance(prior.get('root'), str) or not isinstance(prior.get('state'), str)
            or not isinstance(prior.get('initial_commit'), str) or not _OID.fullmatch(prior['initial_commit'])
            or not isinstance(prior.get('source_manifest_sha256'), str) or not _SHA.fullmatch(prior['source_manifest_sha256'])
            or not isinstance(prior.get('selected_extensions'), list)
            or not all(isinstance(v, str) and v for v in prior['selected_extensions'])
            or prior['selected_extensions'] != sorted(set(prior['selected_extensions']))):
        raise ValueError('Malformed prior binding identity or selection')
    binding = plan['binding']
    if (binding != {k: prior.get(k) for k in ('version', 'instance_id', 'root', 'state', 'source_manifest_sha256')} | {'record_sha256': digest(prior_raw)}
            or plan['selected_extensions'] != prior['selected_extensions']
            or prior['inventory_sha256'] != digest(prior['inventory'])
            or plan['preservation']['original_import_manifest'] != prior['import_manifest']
            or plan['preservation']['original_seed_provenance'] != {k: prior[k] for k in ('management_seed', 'overseer_seed', 'preferences_seed') if k in prior}):
        raise ValueError('Prior binding does not match reviewed plan')
    _validate_manifest(prior['import_manifest'])
    validate_legacy_provenance(prior)
    if git is not None and (git['phase'] != 'git_published' or git['plan_digest'] != plan['plan_digest']
            or git['intent_sha256'] != intent['receipt_sha256'] or git['attribution'] != intent['attribution']
            or git['parent'] != intent['head'] or git['original_index_sha256'] != intent['index']['sha256']
            or not _OID.fullmatch(git['commit']) or not _SHA.fullmatch(git['relocation_sha256'])):
        raise ValueError('Completed Git receipt does not match intent')
    if git is not None:
        identity = layout_git._IDENTITY
        body = (f"tree {git['tree']}\nparent {git['parent']}\n"
                f"author {identity['name']} <{identity['email']}> {git['timestamp']} +0000\n"
                f"committer {identity['name']} <{identity['email']}> {git['timestamp']} +0000\n\n"
                + git['message']).encode()
        oid = hashlib.sha1(b'commit ' + str(len(body)).encode() + b'\0' + body).hexdigest()
        if (oid != git['commit'] or git['identity'] != identity
                or git['message'] != 'Agentic layout migration\n\nAgentic-layout-plan: ' + plan['plan_digest'] + '\n'
                or {k: git['root'][k] for k in ('device', 'inode')} != plan['identity']['root']):
            raise ValueError('Completed Git commit identity mismatch')
    authored = {p: base64.b64decode(v['content_base64'], validate=True) for p, v in intent['authored'].items()}
    metadata = {**plan['legacy_authored_files'], **plan['existing_authored_files']}
    if set(authored) != set(metadata):
        raise ValueError('Incomplete retained authored evidence')
    for p, v in intent['authored'].items():
        relative(p)
        if (digest(authored[p]) != v['sha256'] or metadata[p] != {'sha256': v['sha256'], 'mode': v['mode'], 'bytes': len(authored[p])}):
            raise ValueError('Retained authored evidence mismatch')
    legacy = {p: b for p, b in authored.items() if p.startswith('packages/')}
    mapping = transform_legacy(legacy)
    target, errors = transform_inventory(prior['inventory'], mapping)
    if (mapping['conflicts'] or errors or any(mapping[k] != plan[k] for k in ('path_map', 'ref_map', 'rewrites'))
            or target != plan['target_inventory']):
        raise ValueError('Layout mapping or target inventory differs from prior')
    files = {p: base64.b64decode(v['content_base64'], validate=True) for p, v in plan['files'].items()}
    if any(set(v) != {'sha256', 'content_base64'} or v['sha256'] != digest(files[p]) for p, v in plan['files'].items()):
        raise ValueError('Target file bytes mismatch')
    origin = plan['preservation']['preference_origin']
    if origin == 'verified_export_seed':
        seed = plan['preference_seed']
        if intent['schema_version'] != 2:
            raise ValueError('Missing retained export manifest evidence for verified preferences')
        manifest_bytes = base64.b64decode(intent['export_manifest']['content_base64'], validate=True)
        if intent['export_manifest']['sha256'] != digest(manifest_bytes):
            raise ValueError('Retained export manifest checksum mismatch')
        layout_intent._manifest(manifest_bytes, plan)
        if (not isinstance(seed, dict) or set(seed) != {'source_manifest_sha256', 'resource', 'supplier_commit', 'supplier_tree', 'files'}
                or seed['resource'] != _RESOURCE or not isinstance(seed['files'], dict)
                or not {'agentic.yaml', 'SKILL.md'} <= seed['files'].keys()):
            raise ValueError('Invalid verified preference provenance')
        for p, sha in seed['files'].items():
            relative(p)
            if not isinstance(sha, str) or not _SHA.fullmatch(sha) or files.get('user-preferences/' + p) is None or digest(files['user-preferences/' + p]) != sha:
                raise ValueError('Verified preference seed bytes mismatch')
        seeded = {'user-preferences/' + p: files['user-preferences/' + p] for p in seed['files']}
        preferences = {'kind': 'layout_verified_export_seed', **deepcopy(seed)}
    elif origin == 'preserved_existing':
        if plan['preference_seed'] is not None:
            raise ValueError('Unexpected preference seed')
        old = {p.removeprefix('packages/user-preferences/'): digest(b) for p, b in authored.items() if p.startswith('packages/user-preferences/')}
        current = {p.removeprefix('user-preferences/'): digest(b) for p, b in authored.items() if p.startswith('user-preferences/')}
        if bool(old) == bool(current):
            raise ValueError('Ambiguous preserved preferences')
        preserved = old or current
        if not {'agentic.yaml', 'SKILL.md'} <= preserved.keys():
            raise ValueError('Missing preserved preferences singleton')
        if old and any(plan['path_map'].get('packages/user-preferences/' + p) != ['user-preferences/' + p] for p in old):
            raise ValueError('Invalid preserved preferences mapping')
        preferences = {'kind': 'preserved_existing', 'prior_binding_sha256': digest(prior_raw), 'files': preserved}
        seeded = {}
    else:
        raise ValueError('Unknown preference origin')
    if files != {**mapping['files'], **seeded}:
        raise ValueError('Target bytes differ from reviewed transformation')
    projected = {**{p: b for p, b in authored.items() if not p.startswith('packages/')}, **files}
    required = {'agentic-management/bootstrap': ('instruction', None), 'agentic-management/manage': ('skill', None),
                'user-preferences/preferences': ('skill', 'user-preferences/SKILL.md')}
    required.update({ref: ('workflow' if ref.rsplit('/', 1)[1] in ('extract', 'review', 'apply') else 'instruction', None) for ref in OVERSEER_REFS})
    for binding_row in target['bindings']:
        if binding_row['enabled']:
            for ref in binding_row['entry_refs']:
                required.setdefault(ref, (next(e['kind'] for p in target['packages'] if p['id'] == ref.split('/')[0] for e in p['entries'] if e['id'] == ref.split('/')[1]), None))
    if validate_catalog(projected, required_refs=required):
        raise ValueError('Projected catalogue or required native bindings invalid')
    for asset in target['assets']:
        if (asset.get('enabled') and asset.get('kind') in ('instruction', 'skill', 'manifest', 'metadata')
                and asset.get('destination') not in projected):
            raise ValueError('Missing required projected native asset')
    HostAgenticView(None, target)
    _verify_inventory(target, prior['selected_extensions'], prior['source_manifest_sha256'])
    supplier = build_import_plan(target)
    imported = {'schema_version': 1, 'source_digest': target['source_digest'], 'plan_digest': plan['plan_digest'],
                'previous_import_digest': prior['import_manifest']['manifest_digest'],
                'existing_digest': digest({p: intent['authored'][p]['sha256'] for p in sorted(authored)}),
                'files': deepcopy(supplier['desired_files']), 'bindings': deepcopy(target['bindings'])}
    imported['manifest_digest'] = digest(imported)
    record = {k: deepcopy(prior[k]) for k in ('instance_id', 'root', 'state', 'source_manifest_sha256', 'selected_extensions', 'initial_commit', 'management_seed', 'overseer_seed')}
    record.update(version=2, inventory=deepcopy(target), inventory_sha256=digest(target), import_manifest=imported,
                  preferences_seed=preferences)
    if git is not None:
        record['layout_upgrade'] = {'format_version': 1, 'plan_digest': plan['plan_digest'],
            'prior_binding_sha256': digest(prior_raw), 'intent_sha256': intent['receipt_sha256'],
            'git_receipt_sha256': git['receipt_sha256'], 'resulting_commit': git['commit'], 'preference_origin': origin}
    return record


def build_candidate(paths, plan):
    """Read immutable receipts and return (record, canonical bytes, SHA-256)."""
    pd = plan.get('plan_digest') if isinstance(plan, dict) else None
    if not isinstance(pd, str) or not _SHA.fullmatch(pd):
        raise ValueError('Invalid layout plan digest')
    directory = history_directory(paths)
    layout_intent._safe_ancestors(directory)
    intent = layout_intent._read_intent(directory / ('layout-' + pd + '.json'))
    prior_raw = read_private_file(history_snapshot(paths, intent['prior_binding_sha256']), max_bytes=MAX_RECORD)
    prior = json.loads(prior_raw, object_pairs_hook=_unique)
    git = layout_git._read_journal(directory / ('git-layout-' + pd + '.json'))
    if prior.get('root') != str(paths.agentic_root) or prior.get('state') != str(paths.agentic_runtime):
        raise ValueError('Prior binding paths differ from instance')
    record = derive_record(prior, prior_raw, plan, intent, git)
    raw = canonical(record) + b'\n'
    if len(raw) > MAX_RECORD:
        raise ValueError('Candidate binding exceeds size bound')
    return record, raw, digest(raw)
