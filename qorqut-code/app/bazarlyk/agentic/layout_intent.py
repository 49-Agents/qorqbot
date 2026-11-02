"""Private preparation evidence for a future offline layout application.

This module does not publish authored content, update Git, or alter runtime state.
The offline application coordinator uses this as its first durable stage.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from qorqut_agentic.changes import normalize_attribution
from qorqut_agentic.store import Store
from qorqut_operations.contracts import DomainError
from bazarlyk import source_snapshot
from bazarlyk.private_files import fsync_directory, read_private_file, require_private_directory
from .layout_inspection import read_legacy_tree
from .support import canonical, digest
from .upgrade import (MAX_RECORD, _unique, _write_private_temp, history_directory,
                      history_snapshot, read_binding_bytes, retain_prior_binding, prepare_upgrade)

# A single bounded receipt contains the plan and all recoverable bytes, encoded.
MAX_INTENT_BYTES = 512 * 1024 * 1024
MAX_CAPTURE_BYTES = 96 * 1024 * 1024
_INTENT_RE = re.compile(r'layout-([0-9a-f]{64})\.json\Z')
_publish = os.link
_retained_read = ContextVar('agentic_retained_layout_read', default=None)


@contextmanager
def _retained_reads():
    """Reuse validation of one immutable receipt within one locked open.

    Every access still reads the private file and its prior binding. The memo
    holds only the last exact byte string and is discarded before the next open.
    """
    token = _retained_read.set({})
    try:
        yield
    finally:
        _retained_read.reset(token)


def _conflict(message):
    return DomainError('operation_conflict', message)


def _safe_ancestors(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise _conflict('Unsafe path in offline layout preparation')


def _manifest(raw, plan):
    if len(raw) > source_snapshot.MAX_MANIFEST:
        raise ValueError('retained export manifest exceeds bound')
    manifest = source_snapshot.validate_manifest(json.loads(raw, object_pairs_hook=source_snapshot.unique_object))
    if digest(raw) != plan['export']['source_manifest_sha256']:
        raise ValueError('retained export manifest digest mismatch')
    seed = plan.get('preference_seed')
    if seed is not None:
        components = [c for c in manifest['components'] if c['name'] == 'qorqut-agentic']
        if (len(components) != 1 or seed.get('source_manifest_sha256') != digest(raw)
                or seed.get('resource') != 'qorqut-agentic/src/qorqut_agentic/resources/defaults/user-preferences'
                or seed.get('supplier_commit') != components[0]['commit']
                or seed.get('supplier_tree') != components[0]['tree']):
            raise ValueError('retained export preference supplier mismatch')
    return manifest


def _capture_manifest(plan):
    directory = source_snapshot._directory(plan['export']['root'])
    try:
        fd = os.open('SOURCE-MANIFEST.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        with os.fdopen(fd, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError('export manifest is not a regular file')
            raw = handle.read(source_snapshot.MAX_MANIFEST + 1)
    finally:
        os.close(directory)
    _manifest(raw, plan)
    return {'sha256': digest(raw), 'content_base64': base64.b64encode(raw).decode('ascii')}


def _read_intent(path, expected=None):
    raw = read_private_file(path, max_bytes=MAX_INTENT_BYTES)
    memo = _retained_read.get()
    try:
        if memo is not None and memo.get('path') == path and memo.get('raw') == raw:
            # Parsing again is cheap and gives each caller an independent value.
            # Duplicate keys and all nested checks were verified for these bytes.
            value = json.loads(raw)
        else:
            value = _decode_intent(path, raw)
        if expected is not None and value != expected:
            raise ValueError('unexpected intent receipt')
        prior = read_private_file(path.parent / (value['prior_binding_sha256'] + '.json'), max_bytes=MAX_RECORD)
        if digest(prior) != value['prior_binding_sha256']:
            raise ValueError('prior binding mismatch')
        if memo is not None:
            memo.update(path=path, raw=raw)
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise _conflict('Invalid or changed layout intent') from exc


def _decode_intent(path, raw):
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value.get('schema_version') not in (1, 2) or set(value) != ({'schema_version', 'phase', 'plan', 'attribution', 'prior_binding_sha256', 'authored', 'absent', 'index', 'head', 'staged', 'receipt_sha256'} | ({'export_manifest'} if value.get('schema_version') == 2 else set())):
            raise ValueError('invalid intent shape')
        checksum = value['receipt_sha256']
        body = {k: v for k, v in value.items() if k != 'receipt_sha256'}
        if (value['phase'] != 'prepared' or checksum != digest(body)
                or raw != canonical(value) + b'\n' or not _INTENT_RE.fullmatch(path.name)
                or path.name != 'layout-' + value['plan']['plan_digest'] + '.json'
                or digest({k: v for k, v in value['plan'].items() if k != 'plan_digest'}) != value['plan']['plan_digest']):
            raise ValueError('invalid intent receipt')
        if value['schema_version'] == 2:
            item = value['export_manifest']
            if not isinstance(item, dict) or set(item) != {'sha256', 'content_base64'}:
                raise ValueError('invalid retained export manifest')
            raw_manifest = base64.b64decode(item['content_base64'], validate=True)
            if item['sha256'] != digest(raw_manifest):
                raise ValueError('retained export manifest checksum mismatch')
            _manifest(raw_manifest, value['plan'])
        for item in [value['index'], *value['authored'].values(), *value['staged']]:
            data = base64.b64decode(item['content_base64'], validate=True)
            if digest(data) != item['sha256']:
                raise ValueError('captured content mismatch')
            if 'object_id' in item and hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest() != item['object_id']:
                raise ValueError('captured Git object mismatch')
        for name, evidence in value['plan']['existing_authored_files'].items():
            item = value['authored'][name]
            if (item['sha256'] != evidence['sha256'] or item['mode'] != evidence['mode']
                    or len(base64.b64decode(item['content_base64'], validate=True)) != evidence['bytes']):
                raise ValueError('existing authored evidence mismatch')
        if (value['head'] != value['plan']['git']['head']
                or value['index']['sha256'] != value['plan']['git']['index_sha256']
                or value['prior_binding_sha256'] != value['plan']['binding']['record_sha256']
                or not isinstance(value['prior_binding_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', value['prior_binding_sha256'])):
            raise ValueError('captured identity mismatch')
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise _conflict('Invalid or changed layout intent') from exc


def retained_intents(paths):
    """Read all retained intents without interpreting completion evidence."""
    directory = history_directory(paths)
    _safe_ancestors(directory.parent)
    if not os.path.lexists(directory):
        return []
    _safe_ancestors(directory)
    require_private_directory(directory)
    result = []
    entries = []
    with os.scandir(directory) as stream:
        for entry in stream:
            if len(entries) >= 10000:
                raise _conflict('History directory exceeds inspection bound')
            entries.append(entry)
    for entry in sorted(entries, key=lambda e: e.name):
        if entry.name.startswith('layout-'):
            if not _INTENT_RE.fullmatch(entry.name):
                raise _conflict('Unknown layout intent path')
            result.append(_read_intent(directory / entry.name))
    return result



def incomplete_intents(paths):
    from . import layout_activation
    intents = retained_intents(paths)
    return layout_activation.incomplete(paths, intents)

def _read_index(root, expected):
    path = Path(root) / '.git' / 'index'
    _safe_ancestors(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CAPTURE_BYTES:
            raise _conflict('Unsafe or oversized Git index')
        chunks, count = [], 0
        while chunk := os.read(fd, 1024 * 1024):
            count += len(chunk)
            if count > MAX_CAPTURE_BYTES:
                raise _conflict('Git index exceeds capture bound')
            chunks.append(chunk)
        after = os.fstat(fd)
        named = os.stat(path, follow_symlinks=False)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        data = b''.join(chunks)
        if identity(info) != identity(after) or identity(info) != identity(named) or digest(data) != expected:
            raise _conflict('Git index changed during capture')
        return data
    finally:
        os.close(fd)


def _git(root, args, *, limit=MAX_CAPTURE_BYTES):
    env = {'PATH': os.defpath, 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'LC_ALL': 'C', 'GIT_NO_REPLACE_OBJECTS': '1'}
    from bazarlyk.native_paths import environment
    env = environment(env)
    # Size is checked before reading blobs, and this helper is used only for fixed,
    # bounded metadata commands or one known object.
    result = subprocess.run(['git', '--no-optional-locks', '-C', str(root), *args], env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode or len(result.stdout) > limit:
        raise _conflict('Cannot safely capture staged Git object')
    return result.stdout


def _capture(paths, plan, attribution):
    metadata = {}
    legacy, issues = read_legacy_tree(paths.agentic_root, metadata=metadata)
    if issues or set(legacy) != set(plan['path_map']) or metadata != plan['legacy_authored_files']:
        raise _conflict('Authored files changed during capture')
    existing, existing_metadata = {}, {}
    for area in ('roles', 'operations', 'overseer', 'plugins', 'user-preferences'):
        if os.path.lexists(Path(paths.agentic_root) / area):
            found, issues = read_legacy_tree(paths.agentic_root, area, metadata=existing_metadata)
            if issues:
                raise _conflict('Existing authored area changed during capture')
            existing.update(found)
    if existing_metadata != plan['existing_authored_files']:
        raise _conflict('Existing authored area changed since preparation')
    metadata.update(existing_metadata)
    authored = {**legacy, **existing}
    encoded = {p: {'sha256': digest(b), 'mode': metadata[p]['mode'], 'content_base64': base64.b64encode(b).decode('ascii')}
               for p, b in sorted(authored.items())}
    index = _read_index(paths.agentic_root, plan['git']['index_sha256'])
    staged, total = [], sum(map(len, authored.values())) + len(index)
    if total > MAX_CAPTURE_BYTES:
        raise _conflict('Layout capture exceeds bound')
    for entry in plan['git']['index_entries']:
        try:
            header, path = entry.split('\t', 1)
            mode, oid, stage = header.split(' ')
            if not re.fullmatch(r'[0-9a-f]{40}', oid) or mode not in ('100644', '100755') or stage != '0':
                raise ValueError('unsupported index entry')
            size = int(_git(paths.agentic_root, ['cat-file', '-s', oid], limit=128))
            if size < 0 or size > MAX_CAPTURE_BYTES or total + size > MAX_CAPTURE_BYTES:
                raise ValueError('capture bound')
            body = _git(paths.agentic_root, ['cat-file', 'blob', oid], limit=size)
            if len(body) != size or hashlib.sha1(b'blob ' + str(size).encode() + b'\0' + body).hexdigest() != oid:
                raise ValueError('object identity mismatch')
            total += size
            staged.append({'path': path, 'mode': mode, 'object_id': oid, 'sha256': digest(body),
                           'content_base64': base64.b64encode(body).decode('ascii')})
        except (ValueError, TypeError) as exc:
            raise _conflict('Invalid or oversized authored index entry') from exc
    tracked = _git(paths.agentic_root, ['ls-tree', '-r', '-z', '--name-only', plan['git']['head'], '--',
                                       'packages', 'roles', 'operations', 'overseer', 'plugins', 'user-preferences'])
    try:
        tracked_paths = {p.decode('utf-8') for p in tracked.split(b'\0') if p}
    except UnicodeError as exc:
        raise _conflict('Unsupported tracked Git path') from exc
    absent = sorted(({x['old_path'] for x in plan['preservation'].get('locally_absent_files', [])} |
                     {x['path'] for x in staged} | tracked_paths) - authored.keys())
    body = {'schema_version': 2, 'phase': 'prepared', 'plan': plan, 'attribution': attribution,
            'export_manifest': _capture_manifest(plan),
            'prior_binding_sha256': plan['binding']['record_sha256'], 'authored': encoded, 'absent': absent,
            'index': {'sha256': digest(index), 'content_base64': base64.b64encode(index).decode('ascii')},
            'head': plan['git']['head'], 'staged': staged}
    return {**body, 'receipt_sha256': digest(body)}


def begin(paths, plan, *, attribution):
    """Validate and retain a prepared intent under the initialized authoring lock."""
    normalize_attribution(attribution, trust='host_installer')
    if (not isinstance(plan, dict) or plan.get('kind') != 'agentic_layout_v2' or plan.get('schema_version') != 2
            or plan.get('layout_change') is not True or plan.get('complete') is not True or plan.get('conflicts') != []
            or not isinstance(plan.get('plan_digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', plan['plan_digest'])
            or digest({k: v for k, v in plan.items() if k != 'plan_digest'}) != plan['plan_digest']
            or not isinstance(plan.get('state'), dict) or not isinstance(plan['state'].get('database_fingerprint'), dict)
            or not isinstance(plan.get('existing_authored_files'), dict)
            or not isinstance(plan.get('legacy_authored_files'), dict)):
        raise DomainError('invalid_plan', 'A complete fingerprinted legacy layout plan is required')
    _safe_ancestors(paths.agentic_runtime)
    _safe_ancestors(paths.agentic_root)
    with Store(paths.agentic_runtime).lock(exclusive=True, timeout=10):
        current = prepare_upgrade(paths, plan['export']['root'], plan['selected_extensions'])
        if current != plan:
            raise _conflict('The instance changed since layout preparation')
        existing = incomplete_intents(paths)
        if any(item['plan']['plan_digest'] != plan['plan_digest'] for item in existing):
            raise _conflict('Another layout intent is incomplete')
        binding = read_binding_bytes(paths)
        if digest(binding) != plan['binding']['record_sha256']:
            raise _conflict('The prior binding changed')
        intent = _capture(paths, plan, attribution)
        from .layout_binding import derive_record
        try:
            derive_record(json.loads(binding, object_pairs_hook=_unique), binding, plan, intent)
        except (ValueError, KeyError, TypeError) as exc:
            raise _conflict('Unsupported projected binding: ' + str(exc)) from exc
        raw = canonical(intent) + b'\n'
        if len(raw) > MAX_INTENT_BYTES:
            raise _conflict('Layout intent exceeds capture bound')
        # Recheck after capturing; no private metadata has yet been created.
        if prepare_upgrade(paths, plan['export']['root'], plan['selected_extensions']) != plan:
            raise _conflict('The instance changed during layout capture')
        if existing:
            saved = _read_intent(history_directory(paths) / ('layout-' + plan['plan_digest'] + '.json'))
            comparable = dict(intent)
            if saved['schema_version'] == 1:
                comparable.pop('export_manifest')
                comparable['schema_version'] = 1
                comparable['receipt_sha256'] = digest({k: v for k, v in comparable.items() if k != 'receipt_sha256'})
            if saved != comparable:
                raise _conflict('Retained layout intent changed')
            if read_private_file(history_snapshot(paths, plan['binding']['record_sha256']), max_bytes=MAX_RECORD) != binding:
                raise _conflict('Retained prior binding changed')
            return {'phase': 'prepared', 'plan_digest': plan['plan_digest'], 'created': False}
        retain_prior_binding(paths, binding, plan['binding']['record_sha256'])
        directory = require_private_directory(history_directory(paths))
        target = directory / ('layout-' + plan['plan_digest'] + '.json')
        temp = _write_private_temp(directory, raw)
        try:
            _publish(temp, target, follow_symlinks=False)
            fsync_directory(directory)
        finally:
            temp.unlink(missing_ok=True)
        _read_intent(target, intent)
        return {'phase': 'prepared', 'plan_digest': plan['plan_digest'], 'created': True}
