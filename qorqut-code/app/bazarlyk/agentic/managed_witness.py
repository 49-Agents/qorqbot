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

from bazarlyk import source_snapshot as snapshot
from .installation import CORE, SOURCES
from .inventory import Inventory, _compose_inventory
from .support import Source, ImportFailure, digest, private_resource, relative

_ROLES = 'src/qorqut_agentic/resources/defaults/'
_LIMIT = 96 * 1024 * 1024


def _inputs(standard, seeds):
    needed = {}
    for source in standard['sources']:
        sid = source['id']
        if sid not in SOURCES:
            raise ValueError('Unexpected standard source')
        prefix = _ROLES if sid == 'roles' else ''
        component = SOURCES[sid]
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


class _WitnessSource(Source):
    def __init__(self, source, component, records, directories, used, diagnostics, manifest_sha):
        self.id, self.owner, self.enabled = source['id'], source['owner'], source['enabled']
        self.root = Path(source['root'])
        self.diagnostics, self.used, self.classified = diagnostics, {}, set()
        self.commit, self.repo_prefix, self.clean = component['commit'], '', None
        self.export_component, self.manifest_digest = component, manifest_sha
        self._prefix = _ROLES if self.id == 'roles' else ''
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
        if not isinstance(witness, dict) or set(witness) != {'records', 'directories', 'used_files'}:
            raise ValueError('Invalid source witness')
        records = _records(components[name], manifest['components'], witness['records'], witness['directories'])
        total += len(snapshot.canonical(witness['records'])) + len(snapshot.canonical(witness['directories']))
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
        mappings[name] = records
    inv = Inventory()
    for source in standard['sources']:
        sid = source['id']
        component = components[SOURCES[sid]]
        owner = {'email': 'email-management', 'content-library': 'content'}.get(sid, sid)
        enabled = sid in {'host', 'roles'} or owner in (set(selected) | CORE)
        expected_root = Path(root) / component['destination']
        if sid == 'roles':
            expected_root /= _ROLES
            if _ROLES.rstrip('/') not in witnesses[component['name']]['directories']:
                raise ValueError('Missing witnessed source root')
        if (source['owner'] != owner or source['enabled'] is not enabled
                or source['root'] != str(expected_root) or source['commit'] != component['commit']
                or source['tree'] != component['tree'] or source['snapshot_sha256'] != component['snapshot_sha256']
                or source['source_manifest_sha256'] != manifest_sha):
            raise ValueError('Standard source provenance differs from component')
        inv.sources[sid] = _WitnessSource(source, component, mappings[component['name']], witnesses[component['name']]['directories'],
                                          witnesses[component['name']]['used_files'], inv.diagnostics, manifest_sha)
    actual = _compose_inventory(inv, set(selected) | CORE, (), None)
    if actual != standard:
        raise ValueError('Standard inventory differs from witnessed inputs')
    return manifest
