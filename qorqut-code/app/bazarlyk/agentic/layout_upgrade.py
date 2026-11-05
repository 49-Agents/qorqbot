"""Read-only preparation for the explicit legacy authored-layout upgrade.

The returned contract is reviewable and includes the installed bytes, original
supplier inventory and source hashes.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import re

from qorqut_operations.contracts import DomainError

from .layout_inspection import (inspect_git, inspect_runtime, read_legacy_tree,
                                transform_inventory, transform_legacy, validate_catalog, legacy_destination,
                                load_memory_catalog, retained_layout_inventory)
from .support import digest, ImportFailure


def prepare_layout(paths, record, record_sha256, prepared):
    inventory = record.get('inventory')
    conflicts = []
    plan = {'schema_version': 2, 'kind': 'agentic_layout_v2',
            'layout_change': isinstance(inventory, dict) and inventory.get('schema_version') == 1,
            'binding': {'version': record['version'], 'instance_id': record.get('instance_id'),
                        'root': record.get('root'), 'state': record.get('state'), 'record_sha256': record_sha256,
                        'source_manifest_sha256': record.get('source_manifest_sha256')},
            'export': {'root': prepared['export_root'], 'source_manifest_sha256': prepared['source_manifest_sha256']},
            'selected_extensions': prepared['selected_extensions'],
            'preservation': {'historical_records': 'unchanged', 'profiles_credentials_sessions': 'unchanged',
                             'approved_legacy_plans': 'pre-merge plans require owner replanning under current rules',
                             'merged_applications': 'unsettled merged applications must settle before relocation',
                             'effects': []},
            'path_map': {}, 'ref_map': {}, 'rewrites': [], 'files': {}, 'target_inventory': None}
    if not isinstance(inventory, dict) or inventory.get('schema_version') not in (1, 2):
        conflicts.append({'code': 'unsupported_inventory_schema'})
    elif record.get('inventory_sha256') != digest(inventory):
        conflicts.append({'code': 'inventory_digest_mismatch'})
    if not isinstance(record.get('instance_id'), str) or not re.fullmatch(r'inst_[0-9a-f]{32}', record['instance_id']):
        conflicts.append({'code': 'invalid_instance_id'})
    plan['identity'] = {}
    for name, path in (('root', paths.agentic_root), ('state', paths.agentic_runtime)):
        try:
            path = Path(path)
            if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
                raise OSError('unsafe instance path')
            info = path.stat(follow_symlinks=False)
            plan['identity'][name] = {'device': info.st_dev, 'inode': info.st_ino}
        except OSError as exc:
            conflicts.append({'code': 'unsafe_instance_identity', 'path': str(path), 'reason': str(exc)})
    state = inspect_runtime(paths.agentic_runtime, paths.agentic_root, record.get('instance_id'))
    git = inspect_git(paths.agentic_root)
    plan['state'], plan['git'] = state, git
    if state['migration_required']:
        plan['preservation']['effects'].append('required runtime schema migration')
    if plan['layout_change']:
        plan['preservation']['effects'].append('future current inventory and change-feed refresh')
    conflicts.extend(state['conflicts'])
    conflicts.extend(git['conflicts'])
    if not plan['layout_change'] and os.path.lexists(Path(paths.agentic_root) / 'packages'):
        legacy_files, issues = read_legacy_tree(paths.agentic_root)
        conflicts.extend(issues)
        if legacy_files:
            conflicts.append({'code': 'legacy_packages_with_current_inventory', 'path': 'packages'})
    if isinstance(inventory, dict) and inventory.get('schema_version') == 2:
        from .installation import _validate_binding, _active_inventory, OVERSEER_REFS
        from .view import HostAgenticView
        try:
            _validate_binding(paths, record)
            HostAgenticView(None, inventory)
        except Exception as exc:
            conflicts.append({'code': 'invalid_current_binding', 'reason': str(exc)})
        current_files = {}
        for area in ('roles', 'operations', 'overseer', 'plugins', 'user-preferences'):
            if os.path.lexists(Path(paths.agentic_root) / area):
                found, issues = read_legacy_tree(paths.agentic_root, area)
                current_files.update(found)
                conflicts.extend(issues)
        required = {'agentic-management/bootstrap': ('instruction', None),
                    'agentic-management/manage': ('skill', None),
                    'user-preferences/preferences': ('skill', 'user-preferences/SKILL.md')}
        for ref in OVERSEER_REFS:
            required[ref] = ('workflow' if ref.rsplit('/', 1)[1] in ('extract', 'review', 'apply') else 'instruction', None)
        catalog_issues = validate_catalog(current_files, required_refs=required)
        conflicts.extend({'code': 'catalogue_' + issue['code'], 'detail': issue} for issue in catalog_issues)
        try:
            catalog = load_memory_catalog(current_files)
            class CatalogView:
                def validate(self, *, package_ids):
                    return {'valid': not catalog_issues,
                            'packages': [{'id': key} for key in package_ids if key in catalog.packages]}
            class LayerView:
                catalog = CatalogView()
            active = _active_inventory(LayerView(), inventory, set(prepared['selected_extensions']))
            HostAgenticView(None, active)
            for asset in active['assets']:
                path = asset.get('destination')
                if (asset.get('enabled') and asset.get('kind') in ('instruction', 'skill', 'manifest', 'metadata')
                        and isinstance(path, str) and path not in current_files):
                    conflicts.append({'code': 'missing_required_current_asset', 'path': path})
        except Exception as exc:
            conflicts.append({'code': 'invalid_current_activation', 'reason': str(exc)})
    if plan['layout_change']:
        legacy_metadata = {}
        legacy, issues = read_legacy_tree(paths.agentic_root, metadata=legacy_metadata)
        plan['legacy_authored_files'] = dict(sorted(legacy_metadata.items()))
        conflicts.extend(issues)
        mapped = transform_legacy(legacy)
        conflicts.extend(mapped['conflicts'])
        target_inventory, inv_conflicts = transform_inventory(inventory, mapped)
        conflicts.extend(inv_conflicts)
        plan['target_inventory'] = target_inventory
        try:
            from .plans import _validate_manifest
            from .view import HostAgenticView
            _validate_manifest(record.get('import_manifest'))
            HostAgenticView(None, target_inventory)
        except (DomainError, ValueError, KeyError, TypeError) as exc:
            conflicts.append({'code': 'invalid_target_inventory_or_receipt', 'reason': str(exc)})
        required_packages = {'agentic-management', 'overseer', 'imported-roles', 'imported-host-builtins',
                             'imported-email-management', 'imported-terminal-control'}
        installed_packages = {path.split('/')[1] for path in legacy if len(path.split('/')) >= 3}
        for package in sorted(required_packages - installed_packages):
            conflicts.append({'code': 'missing_required_package', 'path': 'packages/' + package})
        native_roles = {binding.get('native_id') for binding in inventory.get('bindings', [])
                        if binding.get('kind') == 'role' and binding.get('enabled')}
        for role in sorted({'global-worker', 'learning-applier', 'ordubashy'} - native_roles):
            conflicts.append({'code': 'missing_required_native_role', 'native_id': role})
        for binding in inventory.get('bindings', []):
            if binding.get('kind') == 'role' and binding.get('enabled'):
                source_path = binding.get('manifest_path')
                package = binding.get('package_id')
                path = f'packages/{package}/native/{source_path}' if isinstance(source_path, str) and isinstance(package, str) else ''
                if path not in legacy:
                    conflicts.append({'code': 'missing_required_native_manifest', 'path': path,
                                      'native_id': binding.get('native_id')})
        for asset in retained_layout_inventory(inventory, mapped).get('assets', []):
            path = asset.get('destination')
            if (asset.get('enabled') and asset.get('kind') in ('instruction', 'skill', 'manifest', 'metadata')
                    and isinstance(path, str) and path not in legacy):
                conflicts.append({'code': 'missing_required_installed_asset', 'path': path})
        plan['path_map'], plan['ref_map'], plan['rewrites'] = mapped['path_map'], mapped['ref_map'], mapped['rewrites']
        files = mapped['files']
        existing = {}
        existing_metadata = {}
        for area in ('roles', 'operations', 'overseer', 'plugins', 'user-preferences'):
            if os.path.lexists(Path(paths.agentic_root) / area):
                found, issues = read_legacy_tree(paths.agentic_root, area, metadata=existing_metadata)
                existing.update(found)
                conflicts.extend(issues)
        plan['existing_authored_files'] = dict(sorted(existing_metadata.items()))
        if sum(item['bytes'] for item in existing_metadata.values()) > 96 * 1024 * 1024:
            conflicts.append({'code': 'existing_authored_limit_exceeded'})
        existing_containers = {('/'.join(path.split('/')[:2]) if not path.startswith('user-preferences/') else 'user-preferences') for path in existing}
        mapped_containers = {('/'.join(path.split('/')[:2]) if not path.startswith('user-preferences/') else 'user-preferences') for path in files}
        for container in sorted(existing_containers & mapped_containers):
            conflicts.append({'code': 'destination_conflict', 'path': container})
        seed = {}
        if 'user-preferences' not in mapped_containers and 'user-preferences' not in existing_containers:
            for rel, body in sorted(prepared['preferences_seed'].items()):
                path = 'user-preferences/' + rel
                files[path] = body
                seed[rel] = digest(body)
        seed_source = next((source for source in prepared.get('inventory', {}).get('sources', [])
                            if source.get('id') == 'roles'), {})
        plan['preference_seed'] = None if not seed else {'source_manifest_sha256': prepared['source_manifest_sha256'],
            'resource': 'qorqut-agentic/src/qorqut_agentic/resources/defaults/user-preferences',
            'supplier_commit': seed_source.get('commit'), 'supplier_tree': seed_source.get('tree'), 'files': seed}
        plan['preservation']['preference_origin'] = 'verified_export_seed' if seed else 'preserved_existing'
        projected = {**existing, **files}
        conflicts.extend({'code': 'catalogue_' + issue['code'], 'detail': issue} for issue in validate_catalog(projected))
        plan['files'] = {path: {'sha256': digest(body), 'content_base64': base64.b64encode(body).decode('ascii')}
                         for path, body in sorted(files.items())}
        plan['preservation']['legacy_file_count'] = len(legacy)
        plan['preservation']['original_import_manifest'] = record.get('import_manifest')
        plan['preservation']['original_seed_provenance'] = {key: record.get(key) for key in ('management_seed', 'overseer_seed', 'preferences_seed') if key in record}
        previous = (record.get('import_manifest') or {}).get('files', {})
        if isinstance(previous, dict):
            previous = dict(previous)
            for package, key in (('agentic-management', 'management_seed'), ('overseer', 'overseer_seed')):
                seed_record = record.get(key)
                if seed_record is None:
                    continue
                if not isinstance(seed_record, dict) or not isinstance(seed_record.get('files'), dict):
                    conflicts.append({'code': 'malformed_seed_provenance', 'package_id': package})
                    continue
                for rel, sha in seed_record['files'].items():
                    if not isinstance(rel, str) or not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
                        conflicts.append({'code': 'malformed_seed_provenance', 'package_id': package})
                        continue
                    previous[f'packages/{package}/{rel}'] = sha
            absent = []
            for path in sorted(path for path in previous if path not in legacy):
                parts = path.split('/')
                try:
                    if len(parts) < 3 or parts[0] != 'packages':
                        raise ValueError('not a legacy package path')
                    target = legacy_destination(parts[1], '/'.join(parts[2:]))
                    absent.append({'old_path': path, 'new_path': target, 'recorded_sha256': previous[path]})
                except (ImportFailure, ValueError) as exc:
                    conflicts.append({'code': 'unmappable_deleted_path', 'path': path, 'reason': str(exc)})
            plan['preservation']['locally_absent_files'] = absent
        else:
            conflicts.append({'code': 'malformed_import_manifest'})
    plan['conflicts'] = conflicts
    plan['complete'] = not conflicts
    plan['plan_digest'] = digest(plan)
    return plan
