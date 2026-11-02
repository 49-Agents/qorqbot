"""Read-only preparation of a managed update for a current installation."""
from __future__ import annotations

import base64
import hashlib
import stat
from copy import deepcopy
import os
from pathlib import Path
import re
import sqlite3

from qorqut_operations.contracts import DomainError

from .installation import OVERSEER_REFS, _validated_installed_binding, prepare_installation
from .layout_inspection import inspect_git, inspect_runtime, read_legacy_tree, validate_catalog, load_memory_catalog
from .plans import _desired, _validate_manifest
from .provisioning import plan as provision_plan
from bazarlyk.telegram.private_files import read_private_file
from .support import digest
from .upgrade import (read_binding, history_directory, history_snapshot, _installed_selection,
                      _requested_selection, _missing_selected_components, _profile_grants)

_AREAS = ('roles', 'operations', 'overseer', 'plugins', 'user-preferences')


def public_plan(plan):
    return {key: value for key, value in plan.items() if key != '_prepared'}


def plan_digest(plan):
    return digest({k: v for k, v in plan.items() if k not in ('plan_digest', '_prepared', 'grants', 'evidence')})


def validate_plan(paths, plan):
    """Validate both canonical identity and the derived, instance-bound evidence paths."""
    if not isinstance(plan, dict) or plan.get('kind') != 'agentic_managed_update_v2' or plan.get('schema_version') != 2:
        raise DomainError('invalid_plan', 'Invalid managed update plan')
    value = plan.get('plan_digest')
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) or plan_digest(plan) != value:
        raise DomainError('invalid_plan', 'Managed update plan digest changed')
    directory = history_directory(paths)
    expected = {'intent': str(directory / ('managed-intent-' + value + '.json')),
                'journal': str(directory / ('managed-journal-' + value + '.json'))}
    if plan.get('evidence') != expected:
        raise DomainError('invalid_plan', 'Managed update evidence paths changed')
    return True


def _compare(previous, desired, current, metadata):
    changes, preserved, conflicts = [], [], []
    for path in sorted(set(previous) | set(desired)):
        old = previous.get(path)
        body = current.get(path)
        target = desired.get(path)
        actual = None if body is None else digest(body)
        wanted = None if target is None else digest(target)
        if actual == wanted:
            continue
        if actual == old:
            item = {'op': 'delete' if target is None else 'create' if body is None else 'edit',
                    'path': path, 'expected_sha256': actual}
            if body is not None:
                item['mode'] = metadata[path]['mode']
            if target is not None:
                item['content_base64'] = base64.b64encode(target).decode('ascii')
                item['desired_sha256'] = wanted
            changes.append(item)
        elif wanted == old:
            preserved.append({'path': path, 'previous_sha256': old, 'current_sha256': actual})
        else:
            conflicts.append({'code': 'local_upstream_conflict', 'path': path, 'previous_sha256': old,
                              'current_sha256': actual, 'desired_sha256': wanted})
    return changes, preserved, conflicts


def _state_evidence(paths, state, files):
    if not state['inspected'] or state['conflicts'] or state['migration_required']:
        return
    # The database was already inspected with a no-follow descriptor, under the shared lock.
    fd = os.open(paths.agentic_runtime / 'state.sqlite3', os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024 * 1024:
            raise OSError('unsafe runtime database')
        hasher = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            hasher.update(chunk)
        if {'sha256': hasher.hexdigest(), 'bytes': before.st_size} != state['database_fingerprint']:
            raise OSError('runtime database changed during inspection')
        conn = sqlite3.connect(f'file:/proc/self/fd/{fd}?mode=ro&immutable=1', uri=True)
        try:
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024 * 1024)
            rows = conn.execute('SELECT substr(path,1,4097), substr(sha256,1,65), size FROM inventory ORDER BY path LIMIT 100001').fetchall()
            if len(rows) > 100000:
                state['conflicts'].append({'code': 'runtime_inventory_limit'})
                return
            if any(len(path) > 4096 or not re.fullmatch(r'[0-9a-f]{64}', sha) for path, sha, _ in rows):
                state['conflicts'].append({'code': 'invalid_runtime_inventory'})
                return
            observed = {path: {'sha256': sha, 'bytes': size} for path, sha, size in rows}
            actual = {path: {'sha256': digest(body), 'bytes': len(body)} for path, body in sorted(files.items())}
            state['inventory_sha256'] = digest(observed)
            state['authored_inventory_sha256'] = digest(actual)
            state['feed_sequence'] = conn.execute('SELECT COALESCE(MAX(sequence), 0) FROM changes').fetchone()[0]
            for path in sorted(set(observed) | set(actual)):
                if observed.get(path) != actual.get(path):
                    state['conflicts'].append({'code': 'unobserved_inventory_divergence', 'path': path})
        finally:
            conn.close()
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError('runtime database changed during inspection')
    finally:
        os.close(fd)


def prepare(paths, export_root, selected_extensions=None, *, acknowledge_grants=False):
    record, record_sha = read_binding(paths)
    installed = _installed_selection(record)
    selected = installed if selected_extensions is None else _requested_selection(selected_extensions)
    if selected != installed:
        raise DomainError('operation_conflict', 'Changing the installed extension selection requires a separate operation',
                          details={'selected_extensions': installed, 'requested_extensions': selected})
    try:
        prepared = prepare_installation(export_root, selected)
    except DomainError as exc:
        missing = _missing_selected_components(export_root, selected)
        if missing:
            raise DomainError('agentic_installation_invalid', 'The export lacks selected extension sources',
                              details={'missing_components': missing, 'selected_extensions': installed}) from exc
        raise
    if not isinstance(record.get('inventory'), dict) or record['inventory'].get('schema_version') != 2:
        raise DomainError('agentic_installation_invalid', 'A current schema-2 layout is a separate prerequisite')
    # The lock implementation opens an existing lock only; it never creates an invalid instance.
    try:
        with _validated_installed_binding(paths, timeout=10, offline_read_only=True) as bound:
            _, locked_sha = read_binding(paths)
            if bound != record or locked_sha != record_sha:
                raise ValueError('Binding changed during preparation')
            return _prepare_locked(paths, record, record_sha, prepared, acknowledge_grants)
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError('agentic_installation_invalid', 'Current installation is a separate prerequisite: ' + str(exc)) from exc


def _content_plan(record, prepared, target, comparison, files, metadata):
    """Derive reviewed supplier decisions and projected catalog from captured data."""
    conflicts = []
    previous = _validate_manifest(record['import_manifest'])
    desired = _desired(target)
    # Preferences are never supplier-managed by this updater.
    managed_previous = {p: sha for p, sha in previous.items() if not p.startswith('user-preferences/')}
    managed_previous.update({p: sha for p, sha in comparison['files'].items()
                             if p in managed_previous and not p.startswith('user-preferences/')})
    managed_desired = {p: body for p, body in desired.items() if not p.startswith('user-preferences/')}
    changes, preserved, issues = _compare(managed_previous, managed_desired, files, metadata)
    imported = {'previous_supplier_digest': record['import_manifest']['manifest_digest'],
                'previous_manifest': deepcopy(record['import_manifest']),
                'existing_digest': digest({p: digest(files[p]) for p in sorted(set(previous) | set(desired)) if p in files}),
                'desired_files': {p: digest(body) for p, body in sorted(desired.items())},
                'bindings': deepcopy(target['bindings']), 'source_digest': target['source_digest'],
                'changes': list(changes), 'preserved_local_paths': list(preserved), 'conflicts': issues}
    conflicts.extend(issues)
    blocks = {}
    source = next(s for s in prepared['inventory']['sources'] if s['id'] == 'roles')
    for key, prefix, seed in (('management_seed', 'operations/agentic-management/', prepared['seed']),
                              ('overseer_seed', 'overseer/overseer/', prepared['overseer_seed'])):
        prior = record[key]
        old = {prefix + p: sha for p, sha in prior['files'].items()}
        old.update({p: sha for p, sha in comparison['files'].items() if p in old})
        wanted = {prefix + p: body for p, body in seed.items()}
        edits, local, errors = _compare(old, wanted, files, metadata)
        provenance = {'source_id': 'roles', 'source_manifest_sha256': prepared['source_manifest_sha256'],
                      'supplier_commit': source.get('commit'), 'supplier_tree': source.get('tree'),
                      'resource': prefix.rstrip('/'), 'files': {p: digest(b) for p, b in sorted(seed.items())}}
        blocks[key] = {'previous_provenance': deepcopy(prior), 'desired': provenance, 'changes': edits,
                       'preserved_local_paths': local, 'conflicts': errors}
        changes += edits
        preserved += local
        conflicts += errors
    changes.sort(key=lambda item: item['path'])
    if len({item['path'] for item in changes}) != len(changes):
        conflicts.append({'code': 'overlapping_supplier_paths'})
    projected = dict(files)
    for item in changes:
        if item['op'] == 'delete':
            projected.pop(item['path'], None)
        else:
            projected[item['path']] = base64.b64decode(item['content_base64'])
    required = {'agentic-management/bootstrap': ('instruction', None), 'agentic-management/manage': ('skill', None),
                'user-preferences/preferences': ('skill', 'user-preferences/SKILL.md')}
    required.update({ref: ('workflow' if ref.rsplit('/', 1)[1] in ('extract', 'review', 'apply') else 'instruction', None) for ref in OVERSEER_REFS})
    try:
        from .installation import _active_inventory
        def active_inventory(inventory, bodies):
            catalog = load_memory_catalog(bodies)
            class CatalogView:
                def validate(self, *, package_ids):
                    return {'valid': True, 'packages': [{'id': p} for p in package_ids if p in catalog.packages]}
            class LayerView:
                catalog = CatalogView()
            return _active_inventory(LayerView(), inventory, set(prepared['selected_extensions']))

        def native_refs(inventory):
            entries = {package['id'] + '/' + entry['id']: entry
                       for package in inventory['packages'] for entry in package['entries']}
            result = {}
            for binding in inventory['bindings']:
                if not binding['enabled']:
                    continue
                for ref in binding['entry_refs']:
                    result[ref] = (entries[ref]['kind'], None)
                for skill in binding.get('skills', ()):
                    ref = skill['canonical_entry']
                    result[ref] = (entries[ref]['kind'], None)
            return result

        current_active = active_inventory(record['inventory'], files)
        target_active = active_inventory(target, projected)
        current_required = {**required, **native_refs(current_active)}
        target_required = {**required, **native_refs(target_active)}
        conflicts.extend({'code': 'current_catalogue_' + issue['code'], 'detail': issue}
                         for issue in validate_catalog(files, required_refs=current_required))
        conflicts.extend({'code': 'catalogue_' + issue['code'], 'detail': issue}
                         for issue in validate_catalog(projected, required_refs=target_required))
        for asset in target_active['assets']:
            path = asset.get('destination')
            if asset.get('enabled') and asset.get('kind') in ('instruction', 'skill', 'manifest', 'metadata') and isinstance(path, str) and path not in projected:
                conflicts.append({'code': 'missing_required_projected_asset', 'path': path})
    except Exception as exc:
        conflicts.append({'code': 'invalid_projected_catalog', 'reason': str(exc)})
    return previous, imported, blocks, changes, preserved, conflicts


def _prepare_locked(paths, record, record_sha, prepared, acknowledge_grants):
    conflicts = []
    from .managed_sources import comparison_baseline, target_inventory
    comparison = comparison_baseline(paths, record)
    try:
        target = target_inventory(record['inventory'], prepared['inventory'], prepared['selected_extensions'],
                                  prepared['source_manifest_sha256'])
    except (DomainError, ValueError, KeyError, TypeError) as exc:
        conflicts.append({'code': 'custom_inventory_conflict', 'reason': str(exc)})
        target = prepared['inventory']
    files, metadata = {}, {}
    for area in _AREAS:
        if os.path.lexists(paths.agentic_root / area):
            found, issues = read_legacy_tree(paths.agentic_root, area, metadata=metadata)
            files.update(found)
            conflicts.extend(issues)
    if os.path.lexists(paths.agentic_root / 'packages'):
        legacy, issues = read_legacy_tree(paths.agentic_root)
        conflicts.extend(issues)
        if legacy:
            conflicts.append({'code': 'legacy_packages_with_current_inventory', 'path': 'packages'})
    if sum(v['bytes'] for v in metadata.values()) > 96 * 1024 * 1024:
        conflicts.append({'code': 'authored_limit_exceeded'})
    identity = {}
    for name, path in (('root', paths.agentic_root), ('state', paths.agentic_runtime)):
        try:
            if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
                raise OSError('unsafe instance path')
            st = path.stat(follow_symlinks=False)
            identity[name] = {'device': st.st_dev, 'inode': st.st_ino}
        except OSError as exc:
            conflicts.append({'code': 'unsafe_instance_identity', 'path': str(path), 'reason': str(exc)})
    git = inspect_git(paths.agentic_root, entire_worktree=True)
    conflicts.extend(git['conflicts'])
    branch = git.get('branch')
    if not isinstance(branch, str) or not branch.startswith('refs/heads/') or branch == 'refs/heads/':
        conflicts.append({'code': 'selected_branch_required', 'branch': branch})
    if git['dirty']:
        conflicts.append({'code': 'dirty_git_worktree', 'paths': git['dirty']})
    if git.get('index_sha256') and not git['conflicts']:
        try:
            from .layout_git import _index_entries
            from .layout_intent import _read_index
            entries = _index_entries(_read_index(paths.agentic_root, git['index_sha256']))
            if any(entry[2] for entry in entries.values()):
                conflicts.append({'code': 'git_assume_unchanged_unsupported'})
        except Exception as exc:
            conflicts.append({'code': 'git_index_inspection_failed', 'reason': str(exc)})
    state = inspect_runtime(paths.agentic_runtime, paths.agentic_root, record['instance_id'])
    try:
        _state_evidence(paths, state, files)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        state['conflicts'].append({'code': 'runtime_inventory_inspection_failed', 'reason': str(exc)})
    if state['migration_required']:
        state['conflicts'].append({'code': 'runtime_migration_required'})
    conflicts.extend(state['conflicts'])
    previous, imported, blocks, changes, preserved, content_conflicts = _content_plan(
        record, prepared, target, comparison, files, metadata)
    conflicts.extend(content_conflicts)
    snapshot = history_snapshot(paths, record_sha)
    if os.path.lexists(snapshot):
        try:
            if digest(read_private_file(snapshot, max_bytes=96 * 1024 * 1024)) != record_sha:
                conflicts.append({'code': 'history_snapshot_mismatch', 'path': str(snapshot)})
        except Exception as exc:
            conflicts.append({'code': 'unsafe_history_snapshot', 'path': str(snapshot), 'reason': str(exc)})
    provisioning = provision_plan(paths)
    conflicts.extend(provisioning['conflicts'])
    supplier_same = (record['inventory'] == target and record['source_manifest_sha256'] == prepared['source_manifest_sha256']
                     and previous == imported['desired_files']
                     and all(record[key]['files'] == blocks[key]['desired']['files'] for key in blocks))
    no_change = supplier_same and not changes and not any(item['state'] == 'missing' for item in provisioning['items'])
    plan = {'schema_version': 2, 'kind': 'agentic_managed_update_v2',
            'binding': {'current_version': record['version'], 'target_version': 2, 'instance_id': record['instance_id'],
                        'record_sha256': record_sha, 'root': record['root'], 'state': record['state']},
            'history': {'directory': str(history_directory(paths)), 'snapshot': str(history_snapshot(paths, record_sha)), 'sha256': record_sha},
            'export': {'root': prepared['export_root'], 'source_manifest_sha256': prepared['source_manifest_sha256']},
            'selected_extensions': prepared['selected_extensions'], 'target_inventory': target, 'import': imported,
            'comparison_baseline': comparison,
            **blocks, 'preferences_seed': deepcopy(record['preferences_seed']), 'changes': changes,
            'preservation': {'files': {p: v for p, v in sorted(metadata.items()) if p not in {c['path'] for c in changes}},
                             'local_edits': preserved}, 'identity': identity, 'git': git, 'state': state,
            'provisioning': provisioning, 'grants': _profile_grants(paths, immutable=True), 'acknowledge_grants': bool(acknowledge_grants),
            'conflicts': conflicts, 'complete': not conflicts, 'no_change': no_change}
    plan['plan_digest'] = plan_digest(plan)
    d = history_directory(paths)
    plan['evidence'] = {'intent': str(d / ('managed-intent-' + plan['plan_digest'] + '.json')),
                        'journal': str(d / ('managed-journal-' + plan['plan_digest'] + '.json'))}
    plan['_prepared'] = prepared
    return plan
