"""Explicit offline activation of a retained layout migration."""
from __future__ import annotations

import base64
import ctypes
import json
import os
from pathlib import Path
import re
import tempfile

from qorqut_agentic.changes import normalize_attribution
from qorqut_agentic.files import split_path
from qorqut_agentic.errors import UnsafePathError
from qorqut_agentic.store import Store
from qorqut_operations.contracts import DomainError
from bazarlyk.private_files import fsync_directory, read_private_file, require_private_directory
from . import layout_binding, layout_files, layout_git, layout_intent, layout_runtime
from .layout_intent import _conflict
from .support import canonical, digest, relative
from .upgrade import MAX_RECORD, RECORD, _unique, _write_private_temp, history_directory, history_snapshot, read_binding_bytes

_NAME = re.compile(r'activation-layout-([0-9a-f]{64})\.json\Z')
_LIMIT = 1024 * 1024


def _read(path):
    raw = read_private_file(path, max_bytes=_LIMIT)
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'phase', 'inputs', 'result', 'receipt_sha256'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['phase'] not in ('binding_prepared', 'complete')
                or (value['phase'] == 'binding_prepared') != (value['result'] is None)
                or raw != canonical(value) + b'\n'
                or value['receipt_sha256'] != digest({k: v for k, v in value.items() if k != 'receipt_sha256'})):
            raise ValueError('invalid activation journal')
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise _conflict('Invalid layout activation journal') from exc


def _evidence(paths, intent, *, offline_read_only=False):
    plan, attr = intent['plan'], intent['attribution']
    _, runtime_path, found, relocation, git, candidate, candidate_sha, _ = layout_runtime._evidence(
        paths, plan, attr, binding=False, current=False, retained=True)
    runtime = layout_runtime._read(runtime_path)
    store = Store(paths.agentic_runtime)
    with store.lock(exclusive=False, timeout=10):
        receipt = layout_runtime._read_verified_receipt(store, plan, attr, found,
                                                        relocation, git, candidate_sha, runtime,
                                                        offline_read_only=offline_read_only)
    if found != intent:
        raise _conflict('Activation intent changed')
    inputs = {'plan_digest': plan['plan_digest'], 'intent_sha256': intent['receipt_sha256'],
              'relocation_sha256': relocation['receipt_sha256'], 'git_receipt_sha256': git['receipt_sha256'],
              'runtime_journal_sha256': runtime['receipt_sha256'], 'database_receipt_sha256': receipt['receipt_sha256'],
              'original_binding_sha256': intent['prior_binding_sha256'], 'candidate_binding_sha256': candidate_sha,
              'attribution': attr}
    result = {'commit': git['commit'], 'binding_sha256': candidate_sha, 'change_id': receipt['change_id'],
              'sequence': receipt['sequence'], 'activation': True}
    return inputs, result, candidate


def _verify(paths, intent, journal, *, offline_read_only=False):
    inputs, result, candidate = _evidence(paths, intent, offline_read_only=offline_read_only)
    if journal['inputs'] != inputs or (journal['phase'] == 'complete' and journal['result'] != result):
        raise _conflict('Layout activation evidence changed')
    return result, candidate


def incomplete(paths, intents, *, _verified=None, offline_read_only=False):
    directory = history_directory(paths)
    by_digest = {item['plan']['plan_digest']: item for item in intents}
    completed = set()
    if os.path.lexists(directory):
        with os.scandir(directory) as entries:
            count = 0
            for entry in entries:
                count += 1
                if count > 10000:
                    raise _conflict('History directory exceeds inspection bound')
                if not entry.name.startswith('activation-layout'):
                    continue
                match = _NAME.fullmatch(entry.name)
                if not match or match.group(1) not in by_digest:
                    raise _conflict('Unknown layout activation journal')
                intent = by_digest[match.group(1)]
                journal = _read(directory / entry.name)
                verified = _verify(paths, intent, journal, offline_read_only=offline_read_only)
                if _verified is not None:
                    _verified[match.group(1)] = (journal, verified)
                if journal['phase'] == 'complete':
                    completed.add(match.group(1))
    return [i for i in intents if i['plan']['plan_digest'] not in completed]


def validate_completed(paths, record, *, _intents=None, _verified=None, offline_read_only=False):
    return _validate_completed_record(paths, record, read_binding_bytes(paths), _intents=_intents,
                                      _verified=_verified, offline_read_only=offline_read_only)


def _validate_completed_record(paths, record, raw, *, _intents=None, _verified=None, offline_read_only=False):
    """Verify a completed current or hash-addressed historical layout binding."""
    try:
        if json.loads(raw, object_pairs_hook=_unique) != record:
            raise ValueError('binding bytes differ')
    except (TypeError, ValueError) as exc:
        raise _conflict('Invalid completed layout binding bytes') from exc
    metadata = record.get('layout_upgrade')
    if metadata is None:
        if (_intents if _intents is not None else layout_intent.retained_intents(paths)):
            raise _conflict('Installed binding lacks completed layout identity')
        return
    pd = metadata.get('plan_digest') if isinstance(metadata, dict) else None
    all_intents = _intents if _intents is not None else layout_intent.retained_intents(paths)
    intents = [i for i in all_intents if i['plan']['plan_digest'] == pd]
    if len(all_intents) != 1:
        raise _conflict('Unexpected retained layout intent')
    if len(intents) != 1:
        raise _conflict('Matching completed layout intent required')
    if _verified is not None and pd in _verified:
        journal, (result, candidate) = _verified[pd]
    else:
        journal = _read(history_directory(paths) / ('activation-layout-' + pd + '.json'))
        result, candidate = _verify(paths, intents[0], journal, offline_read_only=offline_read_only)
    if journal['phase'] != 'complete' or record != candidate or digest(raw) != result['binding_sha256']:
        raise _conflict('Completed layout activation does not match binding')
    return candidate


def _probe(parent):
    """Exercise the exact publication primitives in an isolated owned directory."""
    directory = Path(tempfile.mkdtemp(prefix='.layout-probe-', dir=parent))
    os.chmod(directory, 0o700)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        temp = os.open('.', os.O_RDWR | os.O_TMPFILE | os.O_CLOEXEC, 0o600, dir_fd=fd)
        try:
            os.write(temp, b'probe')
            os.fsync(temp)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.linkat(-100, os.fsencode('/proc/self/fd/' + str(temp)), fd, b'one', 0x400) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
        finally:
            os.close(temp)
        try:
            os.link('one', 'two', src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            try:
                os.link('one', 'two', src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                pass
            else:
                raise _conflict('Exclusive publication is unavailable')
            os.unlink('two', dir_fd=fd)
            target = os.open('two', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
            try:
                os.write(target, b'distinct target')
                os.fsync(target)
            finally:
                os.close(target)
            source_info = os.stat('one', dir_fd=fd, follow_symlinks=False)
            target_info = os.stat('two', dir_fd=fd, follow_symlinks=False)
            if (source_info.st_dev, source_info.st_ino) == (target_info.st_dev, target_info.st_ino):
                raise _conflict('Replacement probe operands are not distinct')
            os.replace('one', 'two', src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
            try:
                os.stat('one', dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _conflict('Replacement probe did not remove its source')
            replaced = os.open('two', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                info = os.fstat(replaced)
                if ((info.st_dev, info.st_ino) != (source_info.st_dev, source_info.st_ino)
                        or os.read(replaced, 32) != b'probe'):
                    raise _conflict('Replacement probe destination differs from its source')
            finally:
                os.close(replaced)
        finally:
            for name in ('one', 'two'):
                try:
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass
    finally:
        os.close(fd)
        os.rmdir(directory)
        fsync_directory(parent)
    if os.path.lexists(directory):
        raise _conflict('Layout filesystem probe was not cleaned')


def _probe_paths(paths, plan):
    """Validate every supplied authored path before any filesystem probe."""
    mapping, files = plan.get('path_map'), plan.get('files')
    if not isinstance(mapping, dict) or not isinstance(files, dict):
        raise _conflict('Invalid authored probe path collection')
    if len(mapping) > 100000 or len(files) > 100000:
        raise _conflict('Authored probe paths exceed inspection bound')
    root = Path(paths.agentic_root)
    layout_intent._safe_ancestors(root)
    resolved_root = root.resolve(strict=True)
    names = []
    count = 0
    def validate(name):
        try:
            relative(name)
            parts = split_path(name)
            if len(parts) > 64 or parts[0] not in layout_files._AREAS or len(parts) < 2:
                raise ValueError('unsupported authored path')
            candidate = root.joinpath(*parts)
            layout_intent._safe_ancestors(candidate)
            if not candidate.resolve(strict=False).is_relative_to(resolved_root):
                raise ValueError('authored path escapes root')
        except (ValueError, TypeError, OSError, UnsafePathError) as exc:
            raise _conflict('Unsafe authored probe path') from exc
        return name
    for name, destinations in mapping.items():
        names.append(validate(name))
        if not isinstance(destinations, list):
            raise _conflict('Invalid authored probe destinations')
        for destination in destinations:
            count += 1
            if count > 100000:
                raise _conflict('Authored probe paths exceed inspection bound')
            names.append(validate(destination))
    for name in files:
        names.append(validate(name))
    return names


def _probe_filesystems(paths, plan):
    names = _probe_paths(paths, plan)
    parents = {Path(paths.agentic_root), Path(paths.agentic_root) / '.git', Path(paths.instance_root) / 'config'}
    rootfd = layout_files._open_root(paths.agentic_root)
    try:
        _, encoded = layout_files._head_identity(rootfd)
    finally:
        os.close(rootfd)
    head = base64.b64decode(encoded, validate=True)
    if head.startswith(b'ref: refs/heads/') and head.endswith(b'\n'):
        ref = os.fsdecode(head[5:-1])
        parts = ref.split('/')
        if any(part in ('', '.', '..') for part in parts):
            raise _conflict('Unsafe Git reference destination')
        ref_parent = Path(paths.agentic_root) / '.git' / ref
        ref_parent = ref_parent.parent
        while not ref_parent.exists():
            ref_parent = ref_parent.parent
        layout_intent._safe_ancestors(ref_parent)
        parents.add(ref_parent)
    history = history_directory(paths)
    if history.exists():
        parents.add(history)
    for name in names:
        parent = Path(paths.agentic_root) / name
        parent = parent.parent
        while not parent.exists():
            parent = parent.parent
        layout_intent._safe_ancestors(parent)
        parents.add(parent)
    try:
        for parent in sorted(parents):
            layout_intent._safe_ancestors(parent)
            _probe(parent)
    except OSError as exc:
        raise _conflict('Filesystem cannot support offline layout publication: ' + str(exc)) from exc


def _project(paths, plan, intent):
    raw = read_private_file(history_snapshot(paths, intent['prior_binding_sha256']), max_bytes=MAX_RECORD)
    try:
        layout_binding.derive_record(json.loads(raw, object_pairs_hook=_unique), raw, plan, intent)
    except (ValueError, KeyError, TypeError) as exc:
        raise _conflict('Unsupported projected binding: ' + str(exc)) from exc


def apply(paths, plan, *, attribution):
    from bazarlyk.posix_files import require_anonymous_publication
    require_anonymous_publication()
    from .installation import _open_record
    normalize_attribution(attribution, trust='host_installer')
    keys = {'schema_version', 'kind', 'layout_change', 'binding', 'export', 'selected_extensions', 'preservation', 'path_map', 'ref_map', 'rewrites', 'files', 'target_inventory', 'identity', 'state', 'git', 'legacy_authored_files', 'existing_authored_files', 'preference_seed', 'conflicts', 'complete', 'plan_digest'}
    if (not isinstance(plan, dict) or set(plan) != keys or plan.get('kind') != 'agentic_layout_v2' or type(plan.get('schema_version')) is not int or plan.get('schema_version') != 2
            or plan.get('layout_change') is not True or plan.get('complete') is not True or plan.get('conflicts') != []
            or not isinstance(plan.get('plan_digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', plan['plan_digest'])
            or digest({k: v for k, v in plan.items() if k != 'plan_digest'}) != plan['plan_digest']):
        raise DomainError('invalid_plan', 'A complete fingerprinted layout plan is required')
    pd = plan['plan_digest']
    with Store(paths.agentic_runtime).lock(exclusive=True, timeout=10):
        intents = layout_intent.retained_intents(paths)
        pending = layout_intent.incomplete_intents(paths)
        matches = [i for i in intents if i['plan']['plan_digest'] == pd]
        if any(i['plan'] != plan or i['attribution'] != attribution for i in matches) or any(i['plan'] != plan for i in pending):
            raise _conflict('Another layout intent or attribution is incomplete')
        directory = history_directory(paths)
        path = directory / ('activation-layout-' + pd + '.json')
        if matches and not pending:
            journal = _read(path)
            result, candidate = _verify(paths, matches[0], journal)
            if journal['phase'] != 'complete' or read_binding_bytes(paths) != canonical(candidate) + b'\n':
                raise _conflict('Completed activation binding changed')
            _open_record(paths, candidate, tuple(candidate['selected_extensions']))
            return {'schema_version': 2, 'kind': 'agentic_layout_v2', 'plan_digest': pd, 'no_change': False, **result, 'worked': False, 'replayed': True}
        if matches:
            intent = matches[0]
            _project(paths, plan, intent)
        else:
            from .upgrade import prepare_upgrade
            try:
                current = prepare_upgrade(paths, plan['export']['root'], plan['selected_extensions'])
            except (KeyError, TypeError) as exc:
                raise DomainError('invalid_plan', 'The offline layout plan is malformed') from exc
            if current != plan:
                raise _conflict('The instance changed since layout preparation')
        _probe_filesystems(paths, plan)
        if not matches:
            layout_intent.begin(paths, plan, attribution=attribution)
            matches = [i for i in layout_intent.retained_intents(paths) if i['plan'] == plan]
            intent = matches[0]
            _project(paths, plan, intent)
        rp = directory / ('relocation-' + pd + '.json')
        gp = directory / ('git-layout-' + pd + '.json')
        tp = directory / ('runtime-layout-' + pd + '.json')
        if not os.path.lexists(gp):
            layout_files.relocate(paths, plan, attribution=attribution)
        if not os.path.lexists(tp):
            layout_git.publish(paths, plan, attribution=attribution)
        if not os.path.lexists(path):
            layout_runtime.apply(paths, plan, attribution=attribution)
            layout_runtime.verify_published(paths, plan, attribution=attribution)
            candidate, raw, sha = layout_binding.build_candidate(paths, plan)
            old_raw = read_private_file(history_snapshot(paths, intent['prior_binding_sha256']), max_bytes=MAX_RECORD)
            if read_binding_bytes(paths) != old_raw:
                raise _conflict('Original active binding changed')
            _open_record(paths, candidate, tuple(candidate['selected_extensions']))
            inputs, result, _ = _evidence(paths, intent)
            journal = {'schema_version': 1, 'phase': 'binding_prepared', 'inputs': inputs, 'result': None}
            layout_files._journal_write(path, journal)
        else:
            journal = _read(path)
            result, candidate = _verify(paths, intent, journal)
            if journal['phase'] != 'binding_prepared':
                raise _conflict('Unexpected activation phase')
            raw = canonical(candidate) + b'\n'
        old = read_private_file(history_snapshot(paths, intent['prior_binding_sha256']), max_bytes=MAX_RECORD)
        active = read_binding_bytes(paths)
        if active not in (old, raw):
            raise _conflict('Active binding is neither original nor candidate')
        layout_runtime.verify_published(paths, plan, attribution=attribution)
        _open_record(paths, candidate, tuple(candidate['selected_extensions']))
        if active == old:
            layout_runtime.verify_published(paths, plan, attribution=attribution)
            fresh_result, fresh_candidate = _verify(paths, intent, journal)
            if (fresh_result != result or fresh_candidate != candidate
                    or read_binding_bytes(paths) != old or _read(path) != journal):
                raise _conflict('Binding or activation evidence changed')
            config = require_private_directory(Path(paths.instance_root) / 'config')
            temp = _write_private_temp(config, raw)
            try:
                os.replace(temp, config / RECORD)
                fsync_directory(config)
            finally:
                temp.unlink(missing_ok=True)
        if read_binding_bytes(paths) != raw:
            raise _conflict('Candidate binding changed')
        layout_runtime.verify_published(paths, plan, attribution=attribution)
        _open_record(paths, candidate, tuple(candidate['selected_extensions']))
        result, _ = _verify(paths, intent, journal)
        if _read(path) != journal:
            raise _conflict('Activation evidence changed')
        journal.update(phase='complete', result=result)
        layout_files._journal_write(path, journal)
        return {'schema_version': 2, 'kind': 'agentic_layout_v2', 'plan_digest': pd, 'no_change': False, **result, 'worked': True, 'replayed': False}
