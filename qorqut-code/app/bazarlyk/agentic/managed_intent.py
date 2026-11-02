"""Read-only capture and validation of a future managed update intent."""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from bazarlyk.telegram.private_files import read_private_file
from qorqut_agentic.changes import normalize_attribution
from qorqut_agentic.store import Store, _verify_layout
from qorqut_agentic.util import iso, new_id
from . import managed_plan, managed_sources, managed_witness, managed_runtime_evidence
from .installation import IDENTITY, _validated_installed_binding, prepare_installation
from .layout_inspection import inspect_git, inspect_runtime, read_legacy_tree
from .layout_intent import MAX_CAPTURE_BYTES, MAX_INTENT_BYTES, _capture_manifest, _conflict, _read_index
from .layout_runtime import _offline_reader
from .provisioning import plan as provision_plan
from .support import canonical, digest, relative
from .upgrade import MAX_RECORD, _unique, read_binding_bytes, history_directory, history_snapshot

_KEYS = {'schema_version', 'kind', 'phase', 'plan', 'attribution', 'prior_binding_sha256', 'export_manifest',
         'standard_inventory', 'source_witnesses', 'seeds', 'authored', 'absent', 'index', 'head',
         'runtime_before', 'fixed', 'receipt_sha256'}
_PLAN_KEYS = {'schema_version', 'kind', 'binding', 'history', 'export', 'selected_extensions', 'target_inventory',
              'import', 'management_seed', 'overseer_seed', 'preferences_seed', 'changes', 'preservation', 'identity',
              'git', 'state', 'provisioning', 'acknowledge_grants', 'conflicts', 'complete', 'no_change',
              'plan_digest', 'evidence', 'comparison_baseline'}


def _item(raw):
    return {'sha256': digest(raw), 'content_base64': base64.b64encode(raw).decode('ascii')}


def _decode(item):
    if not isinstance(item, dict) or set(item) != {'sha256', 'content_base64'}:
        raise ValueError('Invalid retained bytes')
    raw = base64.b64decode(item['content_base64'], validate=True)
    if digest(raw) != item['sha256'] or base64.b64encode(raw).decode('ascii') != item['content_base64']:
        raise ValueError('Retained bytes differ from digest')
    return raw


def _retained_plan(plan):
    return {k: deepcopy(v) for k, v in plan.items() if k not in ('_prepared', 'grants')}


def _scan(paths):
    files, metadata = {}, {}
    for area in managed_plan._AREAS:
        if os.path.lexists(paths.agentic_root / area):
            found, issues = read_legacy_tree(paths.agentic_root, area, metadata=metadata)
            if issues:
                raise ValueError('Unsafe authored capture')
            files.update(found)
    if sum(len(v) for v in files.values()) > MAX_CAPTURE_BYTES:
        raise ValueError('Authored capture exceeds limit')
    return files, metadata


def _absent(plan, files):
    paths = set(plan['import']['previous_manifest']['files']) | set(plan['import']['desired_files'])
    for key, prefix in (('management_seed', 'operations/agentic-management/'),
                        ('overseer_seed', 'overseer/overseer/')):
        paths.update(prefix + p for p in plan[key]['previous_provenance']['files'])
        paths.update(prefix + p for p in plan[key]['desired']['files'])
    paths.update(c['path'] for c in plan['changes'])
    paths.update(x['path'] for x in plan['preservation']['local_edits'])
    return sorted(p for p in paths if p not in files)


def capture(paths, plan, *, attribution):
    """Capture immutable in-memory inputs; never publish any instance file."""
    managed_plan.validate_plan(paths, plan)
    attr = normalize_attribution(attribution, trust='host_installer')
    wanted = _retained_plan(plan)
    if not wanted.get('complete') or wanted.get('conflicts'):
        raise _conflict('Managed plan has unresolved conflicts')
    try:
        with _validated_installed_binding(paths, timeout=10, offline_read_only=True) as record:
            raw = read_binding_bytes(paths)
            if digest(raw) != wanted['binding']['record_sha256']:
                raise ValueError('Prior binding changed')
            prepared = prepare_installation(wanted['export']['root'], wanted['selected_extensions'])
            current = managed_plan._prepare_locked(paths, record, digest(raw), prepared, wanted['acknowledge_grants'])
            if _retained_plan(current) != wanted:
                raise ValueError('Managed plan changed since preparation')
            if wanted['no_change']:
                fresh = prepare_installation(wanted['export']['root'], wanted['selected_extensions'])
                final = managed_plan._prepare_locked(paths, record, digest(raw), fresh, wanted['acknowledge_grants'])
                if _retained_plan(final) != wanted or read_binding_bytes(paths) != raw:
                    raise ValueError('Managed inputs changed during no-op capture')
                return None
            manifest = _capture_manifest(wanted)
            manifest_raw = _decode(manifest)
            seeds = {'management_seed': prepared['seed'], 'overseer_seed': prepared['overseer_seed']}
            seed_items = {key: {p: _item(b) for p, b in sorted(items.items())} for key, items in seeds.items()}
            witnesses = managed_witness.capture(wanted['export']['root'], json.loads(manifest_raw, object_pairs_hook=_unique),
                                                prepared['inventory'], seeds)
            files, metadata = _scan(paths)
            authored = {p: {**_item(body), 'mode': metadata[p]['mode'], 'bytes': len(body)}
                        for p, body in sorted(files.items())}
            index = _read_index(paths.agentic_root, wanted['git']['index_sha256'])
            raw_total = sum(map(len, files.values())) + len(index) + sum(len(b) for v in seeds.values() for b in v.values())
            raw_total += sum(len(base64.b64decode(v['content_base64'])) for w in witnesses.values() for v in w['used_files'].values())
            raw_total += sum(len(canonical(w['records'])) + len(canonical(w['directories'])) for w in witnesses.values())
            if raw_total > MAX_CAPTURE_BYTES:
                raise ValueError('Managed capture exceeds raw size bound')
            changed = [c['path'] for c in wanted['changes']]
            fixed = None
            if changed:
                now = datetime.now(timezone.utc)
                fixed = {'change_id': new_id('chg'), 'created_at': iso(now), 'identity': deepcopy(IDENTITY),
                         'timestamp': int(now.timestamp()),
                         'message': 'Agentic managed update\n\nAgentic-managed-plan: ' + wanted['plan_digest'] + '\n'}
            with _offline_reader(Store(paths.agentic_runtime)) as conn:
                conn.execute('BEGIN')
                _verify_layout(conn, '3')
                runtime = managed_runtime_evidence.snapshot(conn, changed, fixed['change_id'] if fixed else None)
            intent = {'schema_version': 1, 'kind': 'agentic_managed_intent_v1', 'phase': 'prepared',
                      'plan': wanted, 'attribution': attr, 'prior_binding_sha256': digest(raw),
                      'export_manifest': manifest, 'standard_inventory': deepcopy(prepared['inventory']),
                      'source_witnesses': witnesses, 'seeds': seed_items, 'authored': authored,
                      'absent': _absent(wanted, files), 'index': _item(index), 'head': wanted['git']['head'],
                      'runtime_before': runtime, 'fixed': fixed}
            intent['receipt_sha256'] = digest(intent)
            if len(canonical(intent)) + 1 > MAX_INTENT_BYTES:
                raise ValueError('Managed intent exceeds encoded size bound')
            _validate(paths, intent, raw, record)
            fresh = prepare_installation(wanted['export']['root'], wanted['selected_extensions'])
            final = managed_plan._prepare_locked(paths, record, digest(raw), fresh, wanted['acknowledge_grants'])
            managed_witness.recheck(wanted['export']['root'], json.loads(manifest_raw, object_pairs_hook=_unique), witnesses)
            if _retained_plan(final) != wanted or read_binding_bytes(paths) != raw:
                raise ValueError('Managed inputs changed during capture')
            return intent
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise _conflict('Cannot capture managed intent: ' + str(exc)) from exc


def _validate(paths, intent, prior_raw, prior):
    if (not isinstance(intent, dict) or set(intent) != _KEYS or type(intent['schema_version']) is not int
            or intent['schema_version'] != 1 or intent['kind'] != 'agentic_managed_intent_v1'
            or intent['phase'] != 'prepared' or intent['receipt_sha256'] != digest({k: v for k, v in intent.items() if k != 'receipt_sha256'})):
        raise ValueError('Invalid managed intent envelope')
    plan = intent['plan']
    if not isinstance(plan, dict) or set(plan) != _PLAN_KEYS:
        raise ValueError('Invalid retained plan')
    managed_plan.validate_plan(paths, plan)
    export = plan['export']
    if (not isinstance(export, dict) or set(export) != {'root', 'source_manifest_sha256'}
            or not isinstance(export['root'], str) or '\0' in export['root']
            or not Path(export['root']).is_absolute() or '..' in Path(export['root']).parts):
        raise ValueError('Invalid retained export path')
    binding = {'current_version': prior['version'], 'target_version': 2, 'instance_id': prior['instance_id'],
               'record_sha256': digest(prior_raw), 'root': prior['root'], 'state': prior['state']}
    history = {'directory': str(history_directory(paths)), 'snapshot': str(history_snapshot(paths, digest(prior_raw))),
               'sha256': digest(prior_raw)}
    if (plan['complete'] is not True or plan['conflicts'] != [] or plan['no_change'] is not False
            or type(plan['acknowledge_grants']) is not bool
            or plan['binding'] != binding or plan['history'] != history
            or plan['selected_extensions'] != prior['selected_extensions']
            or digest(prior_raw) != intent['prior_binding_sha256']
            or intent['prior_binding_sha256'] != plan['binding']['record_sha256']
            or not isinstance(intent['attribution'], dict)
            or intent['attribution'] != normalize_attribution({k: v for k, v in intent['attribution'].items() if k != 'trust'}, trust='host_installer')
            or intent['head'] != plan['git']['head'] or _decode(intent['index']) is None
            or digest(_decode(intent['index'])) != plan['git']['index_sha256']):
        raise ValueError('Managed intent identity mismatch')
    manifest_raw = _decode(intent['export_manifest'])
    if digest(manifest_raw) != plan['export']['source_manifest_sha256']:
        raise ValueError('Retained export manifest mismatch')
    if not isinstance(intent['seeds'], dict) or set(intent['seeds']) != {'management_seed', 'overseer_seed'}:
        raise ValueError('Invalid retained seeds')
    seeds = {key: {p: _decode(v) for p, v in items.items()} for key, items in intent['seeds'].items()}
    standard = intent['standard_inventory']
    managed_witness.validate(plan['export']['root'], manifest_raw, standard, seeds, intent['source_witnesses'], plan['selected_extensions'])
    target = managed_sources.target_inventory(prior['inventory'], standard, plan['selected_extensions'], plan['export']['source_manifest_sha256'])
    if target != plan['target_inventory']:
        raise ValueError('Managed target inventory differs')
    comparison = managed_sources.comparison_baseline(paths, prior)
    if comparison != plan['comparison_baseline']:
        raise ValueError('Managed comparison proof differs')
    files, metadata, size = {}, {}, 0
    if not isinstance(intent['authored'], dict):
        raise ValueError('Invalid authored evidence')
    for path, item in intent['authored'].items():
        relative(path)
        if path.split('/', 1)[0] not in managed_plan._AREAS or not isinstance(item, dict) or set(item) != {'sha256', 'content_base64', 'mode', 'bytes'}:
            raise ValueError('Invalid authored path')
        body = _decode({k: item[k] for k in ('sha256', 'content_base64')})
        if type(item['mode']) is not int or not 0 <= item['mode'] <= 0o7777 or item['bytes'] != len(body):
            raise ValueError('Invalid authored metadata')
        files[path] = body
        metadata[path] = {'sha256': item['sha256'], 'mode': item['mode'], 'bytes': len(body)}
        size += len(body)
    size += len(_decode(intent['index'])) + sum(len(b) for values in seeds.values() for b in values.values())
    size += sum(len(canonical(w['records'])) + len(canonical(w['directories'])) + sum(len(_decode(v)) for v in w['used_files'].values())
                for w in intent['source_witnesses'].values())
    if size > MAX_CAPTURE_BYTES or intent['absent'] != _absent(plan, files):
        raise ValueError('Invalid authored capture bounds or absent paths')
    observed = {p: {'sha256': digest(body), 'bytes': len(body)} for p, body in sorted(files.items())}
    if digest(observed) != plan['state']['inventory_sha256'] or digest(observed) != plan['state']['authored_inventory_sha256']:
        raise ValueError('Retained authored files differ from runtime inventory')
    prepared = {'inventory': standard, 'seed': seeds['management_seed'], 'overseer_seed': seeds['overseer_seed'],
                'source_manifest_sha256': plan['export']['source_manifest_sha256'], 'selected_extensions': plan['selected_extensions']}
    _, imported, blocks, changes, preserved, conflicts = managed_plan._content_plan(prior, prepared, target, comparison, files, metadata)
    if (conflicts or imported != plan['import'] or changes != plan['changes']
            or any(blocks[k] != plan[k] for k in blocks)
            or plan['preservation'] != {'files': {p: v for p, v in sorted(metadata.items()) if p not in {c['path'] for c in changes}}, 'local_edits': preserved}
            or prior['preferences_seed'] != plan['preferences_seed']):
        raise ValueError('Managed supplier decisions differ from retained evidence')
    fixed = intent['fixed']
    if changes:
        if (not isinstance(fixed, dict) or set(fixed) != {'change_id', 'created_at', 'identity', 'timestamp', 'message'}
                or not isinstance(fixed['change_id'], str) or not re.fullmatch(r'chg_[0-9a-f]{32}', fixed['change_id'])
                or not isinstance(fixed['created_at'], str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z', fixed['created_at'])
                or fixed['identity'] != IDENTITY or type(fixed['timestamp']) is not int or fixed['timestamp'] < 0
                or fixed['message'] != 'Agentic managed update\n\nAgentic-managed-plan: ' + plan['plan_digest'] + '\n'):
            raise ValueError('Invalid fixed managed event identity')
        if int(datetime.fromisoformat(fixed['created_at'].replace('Z', '+00:00')).timestamp()) != fixed['timestamp']:
            raise ValueError('Managed event and commit timestamps differ')
    elif fixed is not None:
        raise ValueError('Unexpected fixed metadata for binding-only update')
    runtime = intent['runtime_before']
    if (not isinstance(runtime, dict) or set(runtime) != {'schema_sha256', 'tables', 'protected',
            'changed_inventory_before', 'changes_sequence_before', 'feed_head', 'inspection'}
            or not isinstance(runtime['schema_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', runtime['schema_sha256'])
            or not isinstance(runtime['tables'], dict) or not isinstance(runtime['protected'], dict)
            or set(runtime['tables']) != set(runtime['protected'])
            or not {'meta', 'inventory', 'changes', 'change_files', 'sqlite_sequence'} <= runtime['tables'].keys()
            or runtime['feed_head'] != plan['state']['feed_sequence']):
        raise ValueError('Invalid managed runtime evidence')
    for name, item in runtime['tables'].items():
        for value in (item, runtime['protected'][name]):
            if (not isinstance(value, dict) or set(value) != {'rows', 'sha256'} or type(value['rows']) is not int
                    or value['rows'] < 0 or value['rows'] > managed_runtime_evidence.MAX_ROWS
                    or not isinstance(value['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['sha256'])):
                raise ValueError('Invalid runtime table fingerprint')
        if runtime['protected'][name]['rows'] > item['rows']:
            raise ValueError('Invalid runtime protected count')
    expected_old = {c['path']: observed.get(c['path']) for c in changes}
    if runtime['changed_inventory_before'] != dict(sorted(expected_old.items())):
        raise ValueError('Managed prior inventory evidence differs')
    seq = runtime['changes_sequence_before']
    inspection = runtime['inspection']
    if (seq is not None and (type(seq) is not int or seq < 0)) or not isinstance(inspection, dict) or set(inspection) != {'rows', 'bytes', 'row_limit', 'byte_limit'}:
        raise ValueError('Invalid runtime inspection bounds')
    if (inspection['row_limit'] != managed_runtime_evidence.MAX_ROWS or inspection['byte_limit'] != managed_runtime_evidence.MAX_BYTES
            or type(inspection['rows']) is not int or not 0 <= inspection['rows'] <= inspection['row_limit']
            or type(inspection['bytes']) is not int or not 0 <= inspection['bytes'] <= inspection['byte_limit']
            or inspection['rows'] != sum(v['rows'] for v in runtime['tables'].values())):
        raise ValueError('Invalid runtime inspection totals')
    return intent


def validate(paths, intent, prior_raw):
    """Validate retained inputs against the still-installed prior proof, read-only."""
    if len(prior_raw) > MAX_RECORD or len(canonical(intent)) + 1 > MAX_INTENT_BYTES:
        raise _conflict('Managed intent exceeds size bound')
    try:
        prior = json.loads(prior_raw, object_pairs_hook=_unique)
        with _validated_installed_binding(paths, timeout=10, offline_read_only=True) as record:
            if record != prior or read_binding_bytes(paths) != prior_raw:
                raise ValueError('Prior binding is not the installed binding')
            result = _validate(paths, intent, prior_raw, prior)
            plan = intent['plan']
            if inspect_git(paths.agentic_root, entire_worktree=True) != plan['git']:
                raise ValueError('Git changed since intent capture')
            files, metadata = _scan(paths)
            captured = {p: {**_item(body), 'mode': metadata[p]['mode'], 'bytes': len(body)}
                        for p, body in sorted(files.items())}
            if captured != intent['authored']:
                raise ValueError('Authored files changed since intent capture')
            identity = {}
            for name, path in (('root', paths.agentic_root), ('state', paths.agentic_runtime)):
                if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
                    raise ValueError('Unsafe managed instance identity')
                info = path.stat(follow_symlinks=False)
                identity[name] = {'device': info.st_dev, 'inode': info.st_ino}
            if identity != plan['identity'] or provision_plan(paths) != plan['provisioning']:
                raise ValueError('Managed instance identity or provisioning changed')
            current = inspect_runtime(paths.agentic_runtime, paths.agentic_root, prior['instance_id'])
            managed_plan._state_evidence(paths, current, files)
            if current != plan['state']:
                raise ValueError('Runtime changed since intent capture')
            fixed = intent['fixed']
            with _offline_reader(Store(paths.agentic_runtime)) as conn:
                conn.execute('BEGIN')
                actual = managed_runtime_evidence.snapshot(conn, [c['path'] for c in plan['changes']],
                                                           fixed['change_id'] if fixed else None)
            if actual != intent['runtime_before']:
                raise ValueError('Runtime logical evidence differs')
            return result
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise _conflict('Invalid managed intent: ' + str(exc)) from exc


def _read_retained(paths, raw, prior_raw):
    """Derive retained contents; caller must independently prove prior ancestry and phase."""
    if not isinstance(raw, bytes) or len(raw) > MAX_INTENT_BYTES or len(prior_raw) > MAX_RECORD:
        raise _conflict('Invalid retained managed evidence size')
    try:
        intent = json.loads(raw, object_pairs_hook=_unique)
        prior = json.loads(prior_raw, object_pairs_hook=_unique)
        if canonical(intent) + b'\n' != raw:
            raise ValueError('Noncanonical managed intent')
        return _validate(paths, intent, prior_raw, prior)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise _conflict('Invalid retained managed intent: ' + str(exc)) from exc


def encode(paths, intent, prior_raw):
    validate(paths, intent, prior_raw)
    raw = canonical(intent) + b'\n'
    if len(raw) > MAX_INTENT_BYTES:
        raise _conflict('Managed intent exceeds size bound')
    return raw


def decode(paths, raw, prior_raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_INTENT_BYTES:
        raise _conflict('Invalid managed intent bytes')
    try:
        intent = json.loads(raw, object_pairs_hook=_unique)
        if canonical(intent) + b'\n' != raw:
            raise ValueError('Noncanonical managed intent')
        return validate(paths, intent, prior_raw)
    except (ValueError, TypeError) as exc:
        raise _conflict('Invalid managed intent encoding: ' + str(exc)) from exc


def read(paths, path, prior_raw):
    return decode(paths, read_private_file(path, max_bytes=MAX_INTENT_BYTES), prior_raw)
