"""Retained format-1 component witnesses and deterministic inventory replay."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re

from qorqut import source_snapshot as snapshot
from .installation import CORE, SOURCES
from .inventory import Inventory, _compose_inventory
from .support import Source, ImportFailure, digest, private_resource, relative

_ROLES = 'src/qorqut_agentic/resources/defaults/'
_LIMIT = 96 * 1024 * 1024


def _location(source):
    sid = source['id']
    if source.get('provenance_kind') == 'installed_distribution':
        from .installed_sources import CONNECTORS
        if sid not in CONNECTORS:
            raise ValueError('Unknown installed source')
        return 'installed:' + sid, ''
    if sid in {'telegram', 'whatsapp'}:
        return 'qorqut-code', 'extensions/' + sid + '/'
    if sid not in SOURCES:
        raise ValueError('Unexpected standard source')
    return SOURCES[sid], _ROLES if sid == 'roles' else ''


def _distribution(source):
    return {key: source[key] for key in ('root', 'distribution', 'version', 'distribution_record_sha256', 'distribution_files')}


def _distribution_observations(distribution):
    records = {path: dict(path=path, kind='file', sha256=sha, executable=False)
               for path, sha in distribution['distribution_files'].items()}
    directories = sorted({parent.as_posix() for path in records for parent in Path(path).parents if parent.as_posix() != '.'})
    return records, directories


def _inputs(standard, seeds):
    needed = {}
    for source in standard['sources']:
        component, prefix = _location(source)
        files = needed.setdefault(component, {})
        for path, sha in source['files'].items():
            relative(path)
            name = prefix + path
            if name in files and files[name] != sha:
                raise ValueError('Conflicting source input hashes')
            files[name] = sha
    for key, prefix in (('management_seed', 'operations/agentic-management/'),
                        ('overseer_seed', 'overseer/overseer/')):
        for path, body in seeds[key].items():
            relative(path)
            name = _ROLES + prefix + path
            sha = digest(body)
            files = needed.setdefault(SOURCES['roles'], {})
            if name in files and files[name] != sha:
                raise ValueError('Conflicting seed input hashes')
            files[name] = sha
    return needed


def capture(root, manifest, standard, seeds):
    needed = _inputs(standard, seeds)
    components = {c['name']: c for c in manifest['components']}
    result, total = {}, 0
    for name in sorted(needed):
        if name.startswith('installed:'):
            from .installed_sources import installed_source
            sid = name.split(':', 1)[1]
            source = next(source for source in standard['sources'] if source['id'] == sid)
            distribution = installed_source(sid)
            if distribution != _distribution(source):
                raise ValueError('Installed distribution changed before witness capture')
            reader = Source(sid, distribution['root'], sid, True, [], installed_distribution=distribution)
            used = {}
            for path, expected in sorted(needed[name].items()):
                raw = reader.read(path)
                total += len(raw)
                if total > _LIMIT or digest(raw) != expected:
                    raise ValueError('Installed source changed or exceeds capture bound')
                used[path] = {'sha256': expected, 'content_base64': base64.b64encode(raw).decode('ascii')}
            total += len(snapshot.canonical(distribution))
            if total > _LIMIT:
                raise ValueError('Installed source witness exceeds capture bound')
            result[name] = {'distribution': distribution, 'used_files': used}
            continue
        component = components[name]
        records, directories = snapshot.component_observations(root, component, manifest['components'])
        if digest(snapshot.canonical(records)) != component['snapshot_sha256']:
            raise ValueError('Source component changed during witness capture')
        total += len(snapshot.canonical(records)) + len(snapshot.canonical(directories))
        if total > _LIMIT:
            raise ValueError('Source witness exceeds capture bound')
        reader = Source('witness', Path(root) / component['destination'], 'witness', True, [],
                        export_component=component, manifest_digest='')
        used = {}
        for path, expected in sorted(needed[name].items()):
            raw = reader.read(path)
            total += len(raw)
            if total > _LIMIT or digest(raw) != expected:
                raise ValueError('Source input changed or exceeds capture bound')
            used[path] = {'sha256': expected, 'content_base64': base64.b64encode(raw).decode('ascii')}
        result[name] = {'records': records, 'directories': directories, 'used_files': used}
    return result


def recheck(root, manifest, witnesses):
    """Refuse changes to directory observations as well as hashed file records."""
    components = {c['name']: c for c in manifest['components']}
    for name, witness in witnesses.items():
        if name.startswith('installed:'):
            from .installed_sources import installed_source
            sid = name.split(':', 1)[1]
            distribution = installed_source(sid)
            if distribution != witness['distribution']:
                raise ValueError('Installed distribution RECORD changed during capture')
            reader = Source(sid, distribution['root'], sid, True, [], installed_distribution=distribution)
            for path, item in witness['used_files'].items():
                if digest(reader.read(path)) != item['sha256']:
                    raise ValueError('Installed instruction source changed during capture')
            continue
        records, directories = snapshot.component_observations(root, components[name], manifest['components'])
        if records != witness['records'] or directories != witness['directories']:
            raise ValueError('Source component observations changed during capture')


def _records(component, all_components, value, directories):
    if not isinstance(value, list) or len(value) > snapshot.MAX_FILES:
        raise ValueError('Invalid component records')
    excluded = [c['destination'][len(component['destination']) + 1:] for c in all_components
                if c['destination'].startswith(component['destination'] + '/')]
    paths, prior = {}, None
    for record in value:
        if not isinstance(record, dict) or record.get('kind') not in ('file', 'symlink'):
            raise ValueError('Invalid component record')
        kind, path = record['kind'], snapshot.relative(record.get('path'))
        if len(path) > 4096 or len(path.split('/')) > 64 or (prior is not None and path <= prior):
            raise ValueError('Invalid component record ordering')
        prior = path
        if any(path == p or path.startswith(p + '/') or p.startswith(path + '/') for p in excluded):
            raise ValueError('Component record crosses ownership')
        parts = path.split('/')
        if any('/'.join(parts[:i]) in paths for i in range(1, len(parts))):
            raise ValueError('Component record crosses a file or symlink')
        if kind == 'file':
            if (set(record) != {'path', 'kind', 'sha256', 'executable'} or type(record['executable']) is not bool
                    or not isinstance(record['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])):
                raise ValueError('Invalid file witness')
        else:
            if set(record) != {'path', 'kind', 'target'} or not isinstance(record['target'], str):
                raise ValueError('Invalid symlink witness')
            target = record['target']
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            if (not target or '\0' in target or posixpath.isabs(target) or resolved == '..' or resolved.startswith('../')
                    or any(resolved == p or resolved.startswith(p + '/') for p in excluded)):
                raise ValueError('Unsafe symlink witness')
        paths[path] = record
    if hashlib.sha256(snapshot.canonical(value)).hexdigest() != component['snapshot_sha256']:
        raise ValueError('Component witness digest mismatch')
    if not isinstance(directories, list) or len(directories) + len(value) > snapshot.MAX_FILES:
        raise ValueError('Invalid component directories')
    directory_set, previous = set(), None
    for directory in directories:
        directory = snapshot.relative(directory)
        if (len(directory) > 4096 or len(directory.split('/')) > 64
                or (previous is not None and directory <= previous)
                or directory in paths
                or any(directory == p or directory.startswith(p + '/') for p in excluded)):
            raise ValueError('Invalid component directory')
        parts = directory.split('/')
        if any('/'.join(parts[:i]) not in directory_set for i in range(1, len(parts))):
            raise ValueError('Missing or unsafe component directory ancestor')
        directory_set.add(directory)
        previous = directory
    for path in paths:
        parts = path.split('/')
        if any('/'.join(parts[:i]) not in directory_set for i in range(1, len(parts))):
            raise ValueError('Missing component file ancestor')
    return paths


class _WitnessInventory(Inventory):
    """Replay aliases only when their supplier exists in the verified snapshot.

    The installed Agentic library can add aliases after an intent was sealed.
    Those later files must not enter the older intent's closed instruction tree.
    Files actually present in its witness still undergo the full inventory checks.
    """

    def supplier(self, source, path):
        supplier, target = super().supplier(source, path)
        if supplier is not source and not supplier.exists(target):
            return source, path
        return supplier, target

    def alias_paths(self, source_id, root):
        source = self.sources[source_id]
        result = []
        for path in super().alias_paths(source_id, root):
            supplier, target = super().supplier(source, path)
            if supplier is not source and supplier.exists(target):
                result.append(path)
        return tuple(result)


class _WitnessSource(Source):
    def __init__(self, source, component, records, directories, used, diagnostics, manifest_sha):
        self.id, self.owner, self.enabled = source['id'], source['owner'], source['enabled']
        self.root = Path(source['root'])
        self.diagnostics, self.used, self.classified = diagnostics, {}, set()
        self.installed_distribution = _distribution(source) if source.get('provenance_kind') == 'installed_distribution' else None
        self.commit, self.repo_prefix, self.clean = (component['commit'], '', None) if component else (None, None, None)
        self.export_component, self.manifest_digest = component, manifest_sha
        self._prefix = _location(source)[1]
        self._records, self._directories, self._inputs = records, set(directories), used

    def exists(self, rel):
        relative(rel)
        target = self._prefix + rel
        if target in self._records:
            if self._records[target]['kind'] == 'symlink':
                raise ImportFailure('unsafe_path', 'Source symlinks are not allowed', path=rel)
            return True
        return target in self._directories

    def read(self, rel):
        relative(rel)
        item = self._inputs.get(self._prefix + rel)
        if item is None:
            raise ImportFailure('missing_resource', 'Required source input is missing', path=rel)
        data = base64.b64decode(item['content_base64'], validate=True)
        self.used[rel] = digest(data)
        self.classified.add(rel)
        return data

    def tree(self, rel, *, root_resources=False):
        if rel:
            relative(rel)
        elif not root_resources:
            raise ImportFailure('unsafe_path', 'Empty source tree')
        root = self._prefix + (rel + '/' if rel else '')
        directory = root.rstrip('/')
        if directory and directory not in self._directories:
            raise ImportFailure('missing_resource', 'Source directory is missing', path=rel)
        found = []
        for path, item in self._records.items():
            if not path.startswith(root):
                continue
            remainder = path[len(root):]
            if root_resources and any(private_resource(part) for part in remainder.split('/')):
                continue
            if item['kind'] != 'file':
                raise ImportFailure('unsafe_path', 'Source assets must be ordinary files', path=path)
            found.append(path[len(self._prefix):])
        return sorted(found)


def validate(root, manifest_raw, standard, seeds, witnesses, selected):
    manifest = snapshot.validate_manifest(json.loads(manifest_raw, object_pairs_hook=snapshot.unique_object))
    manifest_sha = digest(manifest_raw)
    components = {c['name']: c for c in manifest['components']}
    needed = _inputs(standard, seeds)
    if not isinstance(witnesses, dict) or set(witnesses) != set(needed):
        raise ValueError('Source witness component set differs')
    mappings, total = {}, 0
    for name in sorted(needed):
        witness = witnesses[name]
        if name.startswith('installed:'):
            source = next(source for source in standard['sources'] if source['id'] == name.split(':', 1)[1])
            if not isinstance(witness, dict) or set(witness) != {'distribution', 'used_files'} or witness['distribution'] != _distribution(source):
                raise ValueError('Invalid installed distribution witness')
            records, directories = _distribution_observations(witness['distribution'])
            total += len(snapshot.canonical(witness['distribution']))
        else:
            if not isinstance(witness, dict) or set(witness) != {'records', 'directories', 'used_files'}:
                raise ValueError('Invalid source witness')
            records = _records(components[name], manifest['components'], witness['records'], witness['directories'])
            directories = witness['directories']
            total += len(snapshot.canonical(witness['records'])) + len(snapshot.canonical(directories))
        used = witness['used_files']
        if not isinstance(used, dict) or set(used) != set(needed[name]):
            raise ValueError('Source witness input set differs')
        for path, item in used.items():
            if not isinstance(item, dict) or set(item) != {'sha256', 'content_base64'}:
                raise ValueError('Invalid source witness input')
            raw = base64.b64decode(item['content_base64'], validate=True)
            total += len(raw)
            record = records.get(path)
            if (record is None or record['kind'] != 'file' or base64.b64encode(raw).decode('ascii') != item['content_base64'] or digest(raw) != item['sha256']
                    or item['sha256'] != needed[name][path] or item['sha256'] != record['sha256']):
                raise ValueError('Source input does not match component witness')
        if total > _LIMIT:
            raise ValueError('Source witness exceeds capture bound')
        mappings[name] = records, directories
    # Retained pre-consolidation evidence names the original physical sources.
    # Reproduce that inventory from its verified bytes, without resolving those
    # paths through today's Agentic aliases. The exact comparison below still
    # validates the complete inventory; new imports always use the current layout.
    legacy_host = any(source['id'] == 'host' and
                      'app/bazarlyk/agentic/assets/index.json' in source['files']
                      for source in standard['sources'])
    inv = _WitnessInventory(relocated_instructions=any(
        package['id'] == 'runtime-instructions' for package in standard['packages']),
        host_package='bazarlyk' if legacy_host else 'qorqut')
    for source in standard['sources']:
        sid = source['id']
        name, prefix = _location(source)
        component = components.get(name)
        owner = {'email': 'email-management', 'content-library': 'content'}.get(sid, sid)
        enabled = sid in {'host', 'roles'} or owner in (set(selected) | CORE)
        if component is None:
            if source['owner'] != owner or source['enabled'] is not enabled:
                raise ValueError('Installed source ownership differs')
            inv.sources[sid] = _WitnessSource(source, None, *mappings[name], witnesses[name]['used_files'], inv.diagnostics, manifest_sha)
            continue
        expected_root = Path(root) / component['destination']
        if prefix:
            expected_root /= prefix
            if prefix.rstrip('/') not in witnesses[name]['directories']:
                raise ValueError('Missing witnessed source root')
        if (source['owner'] != owner or source['enabled'] is not enabled
                or source['root'] != str(expected_root) or source['commit'] != component['commit']
                or source['tree'] != component['tree'] or source['snapshot_sha256'] != component['snapshot_sha256']
                or source['source_manifest_sha256'] != manifest_sha):
            raise ValueError('Standard source provenance differs from component')
        inv.sources[sid] = _WitnessSource(source, component, *mappings[name], witnesses[name]['used_files'], inv.diagnostics, manifest_sha)
    actual = _compose_inventory(inv, set(selected) | CORE, (), None)
    if actual != standard:
        raise ValueError('Standard inventory differs from witnessed inputs')
    return manifest
