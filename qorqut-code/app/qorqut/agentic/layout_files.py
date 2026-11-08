"""Offline, journalled publication of a prepared authored layout.

New directories use mode 0755, subject to no process umask (set explicitly).
An interrupted mkdir before its identity is journalled is deliberately ambiguous.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat

from qorqut_agentic.changes import normalize_attribution
from qorqut_agentic.store import Store
from qorqut_operations.contracts import DomainError
from qorqut.private_files import fsync_directory, read_private_file, require_private_directory
from .layout_inspection import inspect_git, inspect_runtime, read_legacy_tree, validate_catalog
from .layout_intent import _conflict, _read_index, _safe_ancestors, incomplete_intents
from .support import canonical, digest, relative
from .upgrade import _unique, _write_private_temp, history_directory, prepare_upgrade, read_binding_bytes

_AREAS = ('packages', 'roles', 'operations', 'overseer', 'plugins', 'user-preferences')
_LIMIT = 512 * 1024 * 1024


def _identity(info):
    return {'device': info.st_dev, 'inode': info.st_ino, 'mode': stat.S_IMODE(info.st_mode)}


def _open_root(root):
    _safe_ancestors(root)
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _open_dir(rootfd, path, dirs):
    fd = os.dup(rootfd)
    try:
        current = ''
        for part in path.split('/') if path else ():
            current = current + '/' + part if current else part
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            if current not in dirs or _identity(os.fstat(fd)) != dirs[current]:
                raise _conflict('Authored directory identity changed: ' + current)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _directories(rootfd):
    result = {}
    def walk(fd, prefix):
        with os.scandir(fd) as entries:
            names = sorted(e.name for e in entries)
        for name in names:
            path = prefix + '/' + name if prefix else name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if stat.S_IMODE(info.st_mode) & ~0o777:
                    raise _conflict('Special directory mode is unsupported: ' + path)
                relative(path)
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _identity(os.fstat(child)) != _identity(info):
                        raise _conflict('Directory changed during inspection: ' + path)
                    result[path] = _identity(info)
                    if len(result) > 100000:
                        raise _conflict('Directory inspection limit exceeded')
                    walk(child, path)
                finally:
                    os.close(child)
    for area in _AREAS:
        try:
            info = os.stat(area, dir_fd=rootfd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise _conflict('Unsafe authored directory: ' + area)
        if stat.S_IMODE(info.st_mode) & ~0o777:
            raise _conflict('Special directory mode is unsupported: ' + area)
        fd = os.open(area, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
        try:
            if _identity(os.fstat(fd)) != _identity(info):
                raise _conflict('Directory changed during inspection: ' + area)
            result[area] = _identity(info)
            walk(fd, area)
        finally:
            os.close(fd)
    return result


def _authored(root):
    files, meta = {}, {}
    for area in _AREAS:
        if os.path.lexists(Path(root) / area):
            found, issues = read_legacy_tree(root, area, metadata=meta)
            if issues:
                raise _conflict('Unsafe authored tree: ' + str(issues[0]))
            files.update(found)
    return files, meta


def _journal_write(path, journal):
    body = {k: v for k, v in journal.items() if k != 'receipt_sha256'}
    journal['receipt_sha256'] = digest(body)
    raw = canonical(journal) + b'\n'
    if len(raw) > _LIMIT:
        raise _conflict('Relocation journal exceeds bound')
    temp = _write_private_temp(path.parent, raw)
    try:
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _journal_read(path):
    raw = read_private_file(path, max_bytes=_LIMIT)
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        keys = {'schema_version', 'phase', 'plan_digest', 'intent_sha256', 'attribution', 'root', 'gitdir',
                'head_identity', 'initial_dirs', 'created_dirs', 'effects', 'next_effect', 'receipt_sha256'}
        if (not isinstance(value, dict) or set(value) != keys or value['schema_version'] != 1
                or value['phase'] not in ('relocating', 'files_published') or raw != canonical(value) + b'\n'
                or value['receipt_sha256'] != digest({k: v for k, v in value.items() if k != 'receipt_sha256'})
                or type(value['next_effect']) is not int or not 0 <= value['next_effect'] <= len(value['effects'])
                or not isinstance(value['initial_dirs'], dict) or not isinstance(value['created_dirs'], dict)
                or (value['phase'] == 'files_published') != (value['next_effect'] == len(value['effects']))):
            raise ValueError('invalid journal')
        def valid_identity(item):
            return (isinstance(item, dict) and set(item) == {'device', 'inode', 'mode'}
                    and all(type(item[k]) is int and item[k] >= 0 for k in item)
                    and item['mode'] <= 0o7777)
        if (not valid_identity(value['root']) or not valid_identity(value['gitdir'])
                or not isinstance(value['effects'], list) or len(value['effects']) > 300000
                or len(value['initial_dirs']) + len(value['created_dirs']) > 100000
                or set(value['initial_dirs']) & set(value['created_dirs'])
                or not all(valid_identity(v) for v in [*value['initial_dirs'].values(), *value['created_dirs'].values()])
                or len(base64.b64decode(value['head_identity'], validate=True)) > 4096):
            raise ValueError('invalid journal evidence')
        for name in [*value['initial_dirs'], *value['created_dirs']]:
            relative(name)
            if name.split('/')[0] not in _AREAS:
                raise ValueError('invalid directory path')
        return value
    except (TypeError, ValueError, KeyError) as exc:
        raise _conflict('Invalid relocation journal') from exc


def _head_identity(rootfd):
    gitfd = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
    try:
        fd = os.open('HEAD', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=gitfd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                raise _conflict('Unsafe Git HEAD')
            body = os.read(fd, 4097)
            return _identity(os.fstat(gitfd)), base64.b64encode(body).decode('ascii')
        finally:
            os.close(fd)
    finally:
        os.close(gitfd)


def _environment_facts(paths, plan, journal, rootfd):
    _safe_ancestors(paths.agentic_runtime)
    runtime_info = os.stat(paths.agentic_runtime, follow_symlinks=False)
    if {'device': runtime_info.st_dev, 'inode': runtime_info.st_ino} != plan['identity']['state']:
        raise _conflict('Runtime directory identity changed')
    if {'device': journal['root']['device'], 'inode': journal['root']['inode']} != plan['identity']['root']:
        raise _conflict('Root differs from prepared identity')
    if _identity(os.fstat(rootfd)) != journal['root'] or _identity(os.stat(paths.agentic_root, follow_symlinks=False)) != journal['root']:
        raise _conflict('Root directory identity changed')
    gitdir, head = _head_identity(rootfd)
    if gitdir != journal['gitdir']:
        raise _conflict('Git directory identity changed')
    if digest(read_binding_bytes(paths)) != plan['binding']['record_sha256']:
        raise _conflict('Binding changed')
    state = inspect_runtime(paths.agentic_runtime, paths.agentic_root, plan['binding']['instance_id'])
    if state != plan['state']:
        raise _conflict('Runtime evidence changed')
    return head


def _facts(paths, plan, journal, rootfd):
    head = _environment_facts(paths, plan, journal, rootfd)
    _read_index(paths.agentic_root, plan['git']['index_sha256'])
    if head != journal['head_identity']:
        raise _conflict('Git directory or HEAD identity changed')
    git = inspect_git(paths.agentic_root)
    if git['conflicts'] or git['head'] != plan['git']['head'] or git['index_sha256'] != plan['git']['index_sha256']:
        raise _conflict('Git evidence changed')


def _derive(plan, intent, initial_dirs):
    originals = {p: (base64.b64decode(v['content_base64'], validate=True), v['mode']) for p, v in intent['authored'].items()}
    if any(type(mode) is not int or mode & ~0o777 for _, mode in originals.values()):
        raise _conflict('Special authored file mode is unsupported')
    modes = {}
    for old, targets in plan['path_map'].items():
        relative(old)
        if old not in originals or not isinstance(targets, list) or not targets:
            raise _conflict('Invalid path mapping')
        for target in targets:
            relative(target)
            if target in modes:
                raise _conflict('Duplicate target mapping')
            modes[target] = originals[old][1]
    targets = {}
    for path, entry in plan['files'].items():
        relative(path)
        body = base64.b64decode(entry['content_base64'], validate=True)
        if digest(body) != entry['sha256'] or path in originals or path in initial_dirs:
            raise _conflict('Invalid or occupied target: ' + path)
        if path not in modes and not path.startswith('user-preferences/'):
            raise _conflict('Unmapped target: ' + path)
        targets[path] = (body, modes.get(path, 0o644))
    if set(modes) - targets.keys() or set(plan['path_map']) != {p for p in originals if p.startswith('packages/')}:
        raise _conflict('Incomplete target mapping')
    existing = {p: v for p, v in originals.items() if not p.startswith('packages/')}
    projected = {p: b for p, (b, _) in {**existing, **targets}.items()}
    if validate_catalog(projected):
        raise _conflict('Projected catalogue invalid')
    needed = set()
    for path in targets:
        parts = path.split('/')[:-1]
        needed.update('/'.join(parts[:i]) for i in range(1, len(parts) + 1))
    if needed & originals.keys():
        raise _conflict('A target parent is an authored file')
    mkdirs = sorted(needed - initial_dirs.keys(), key=lambda p: (p.count('/'), p))
    effects = ([{'kind': 'mkdir', 'path': p} for p in mkdirs] +
               [{'kind': 'publish', 'path': p} for p in sorted(targets)] +
               [{'kind': 'remove', 'path': p, 'targets': sorted(plan['path_map'][p])}
                for p in sorted(plan['path_map'])])
    return originals, targets, effects


def _verify_tree(paths, rootfd, journal, originals, targets):
    _safe_ancestors(paths.agentic_root)
    if _identity(os.fstat(rootfd)) != journal['root'] or _identity(os.stat(paths.agentic_root, follow_symlinks=False)) != journal['root']:
        raise _conflict('Root directory identity changed')
    index = journal['next_effect']
    effects = journal['effects']
    dirs = {**journal['initial_dirs'], **journal['created_dirs']}
    actual_dirs = _directories(rootfd)
    # The current mkdir may have happened before its identity was persisted.
    if index < len(effects) and effects[index]['kind'] == 'mkdir':
        path = effects[index]['path']
        if path in actual_dirs and path not in dirs:
            raise _conflict('Ambiguous directory creation without recorded identity: ' + path)
    if actual_dirs != dirs:
        raise _conflict('Authored directory set or identity changed')
    expected = dict(originals)
    for effect in effects[:index]:
        if effect['kind'] == 'publish':
            expected[effect['path']] = targets[effect['path']]
        elif effect['kind'] == 'remove':
            expected.pop(effect['path'])
    files, meta = _authored(paths.agentic_root)
    actual = {p: (b, meta[p]['mode']) for p, b in files.items()}
    current = effects[index] if index < len(effects) else None
    # An interrupted atomic effect can be in its exact post-state.
    if current and current['kind'] == 'publish' and actual.get(current['path']) == targets[current['path']]:
        expected[current['path']] = targets[current['path']]
    elif current and current['kind'] == 'remove' and current['path'] not in actual:
        expected.pop(current['path'])
    if actual != expected:
        raise _conflict('Authored files differ from recorded intermediate state')
    return actual


def _expected_file(parent, name, expected, path, *, keep_open=False):
    """Verify a named regular file through a no-follow handle, or report absence."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _conflict('Cannot safely open expected file: ' + path) from exc
    retained = False
    try:
        before = os.fstat(fd)
        body, mode = expected
        if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != mode or before.st_size != len(body):
            raise _conflict('File type, mode or size changed: ' + path)
        offset = 0
        while offset < len(body):
            chunk = os.read(fd, min(65536, len(body) - offset))
            if not chunk or chunk != body[offset:offset + len(chunk)]:
                raise _conflict('File bytes changed: ' + path)
            offset += len(chunk)
        if os.read(fd, 1):
            raise _conflict('File bytes changed: ' + path)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(named):
            raise _conflict('File identity changed during verification: ' + path)
        if keep_open:
            retained = True
            return fd, identity(after)
        return identity(after)
    except FileNotFoundError as exc:
        raise _conflict('File disappeared during verification: ' + path) from exc
    finally:
        if not retained:
            os.close(fd)


def _apply_file(rootfd, journal, effect, originals, targets):
    path = effect['path']
    parent, name = path.rsplit('/', 1)
    dirs = {**journal['initial_dirs'], **journal['created_dirs']}
    fd = _open_dir(rootfd, parent, dirs)
    try:
        if effect['kind'] == 'publish':
            body, mode = targets[path]
            if _expected_file(fd, name, (body, mode), path) is not None:
                os.fsync(fd)
                return
            # Never a named partial file at the destination to adopt or delete after interruption (posix_files).
            from qorqut import posix_files
            pending = posix_files.anonymous_file(fd)
            try:
                view = memoryview(body)
                while view:
                    view = view[os.write(pending.fd, view):]
                os.fchmod(pending.fd, mode)
                os.fsync(pending.fd)
                posix_files.publish(pending, fd, name)
                os.fsync(fd)
            finally:
                posix_files.discard(pending)
        else:
            # The immutable plan fixes all destination copies, including split manifests.
            for target in effect['targets']:
                target_parent, target_name = target.rsplit('/', 1)
                target_fd = _open_dir(rootfd, target_parent, dirs)
                try:
                    if _expected_file(target_fd, target_name, targets[target], target) is None:
                        raise _conflict('Mapped destination is missing: ' + target)
                finally:
                    os.close(target_fd)
            verified = _expected_file(fd, name, originals[path], path, keep_open=True)
            if verified is None:
                os.fsync(fd)
                return
            source_fd, identity = verified
            try:
                try:
                    named = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise _conflict('Source disappeared before removal: ' + path) from exc
                opened = os.fstat(source_fd)
                identity_of = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
                if identity_of(named) != identity or identity_of(opened) != identity:
                    raise _conflict('Source identity changed before removal: ' + path)
                os.unlink(name, dir_fd=fd)
                os.fsync(fd)
            finally:
                os.close(source_fd)
    finally:
        os.close(fd)


def relocate(paths, plan, *, attribution):
    """Publish only authored files under the initialized authoring lock."""
    normalize_attribution(attribution, trust='host_installer')
    if not isinstance(plan, dict) or not isinstance(plan.get('plan_digest'), str) or digest({k: v for k, v in plan.items() if k != 'plan_digest'}) != plan['plan_digest']:
        raise DomainError('invalid_plan', 'A complete prepared layout plan is required')
    _safe_ancestors(paths.agentic_runtime)
    _safe_ancestors(paths.agentic_root)
    with Store(paths.agentic_runtime).lock(exclusive=True, timeout=10):
        intents = incomplete_intents(paths)
        if len(intents) != 1 or intents[0]['plan'] != plan or intents[0]['attribution'] != attribution:
            raise _conflict('Matching prepared intent and attribution required')
        intent = intents[0]
        directory = require_private_directory(history_directory(paths))
        path = directory / ('relocation-' + plan['plan_digest'] + '.json')
        for item in directory.iterdir():
            if item.name.startswith('relocation-') and item != path:
                raise _conflict('Another or unknown relocation journal exists')
        rootfd = _open_root(paths.agentic_root)
        try:
            if os.path.lexists(path):
                journal = _journal_read(path)
                originals, targets, effects = _derive(plan, intent, journal['initial_dirs'])
                if (journal['plan_digest'] != plan['plan_digest'] or journal['intent_sha256'] != intent['receipt_sha256']
                        or journal['attribution'] != attribution or journal['effects'] != effects):
                    raise _conflict('Relocation journal does not match prepared intent')
                done_mkdirs = {e['path'] for e in effects[:journal['next_effect']] if e['kind'] == 'mkdir'}
                if set(journal['created_dirs']) != done_mkdirs:
                    raise _conflict('Invalid recorded directory identities')
            else:
                initial_dirs = _directories(rootfd)
                originals, targets, effects = _derive(plan, intent, initial_dirs)
                gitdir, head = _head_identity(rootfd)
                journal = {'schema_version': 1, 'phase': 'relocating', 'plan_digest': plan['plan_digest'],
                           'intent_sha256': intent['receipt_sha256'], 'attribution': attribution,
                           'root': _identity(os.fstat(rootfd)), 'gitdir': gitdir, 'head_identity': head,
                           'initial_dirs': initial_dirs, 'created_dirs': {}, 'effects': effects, 'next_effect': 0}
                _facts(paths, plan, journal, rootfd)
                _verify_tree(paths, rootfd, journal, originals, targets)
                _journal_write(path, journal)
            _facts(paths, plan, journal, rootfd)
            _verify_tree(paths, rootfd, journal, originals, targets)
            if journal['phase'] == 'files_published':
                return {'phase': 'files_published', 'plan_digest': plan['plan_digest'], 'result_sha256': digest({p: {'sha256': digest(b), 'mode': mode} for p, (b, mode) in sorted({**{p: v for p, v in originals.items() if not p.startswith('packages/')}, **targets}.items())}), 'worked': False}
            while journal['next_effect'] < len(effects):
                _verify_tree(paths, rootfd, journal, originals, targets)
                effect = effects[journal['next_effect']]
                if effect['kind'] == 'mkdir':
                    rel = effect['path']
                    parent, _, name = rel.rpartition('/')
                    fd = _open_dir(rootfd, parent, {**journal['initial_dirs'], **journal['created_dirs']})
                    try:
                        os.mkdir(name, 0o755, dir_fd=fd)
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            os.fchmod(child, 0o755)
                            os.fsync(child)
                            identity = _identity(os.fstat(child))
                        finally:
                            os.close(child)
                        os.fsync(fd)
                        journal['created_dirs'][rel] = identity
                    finally:
                        os.close(fd)
                else:
                    _apply_file(rootfd, journal, effect, originals, targets)
                journal['next_effect'] += 1
                if journal['next_effect'] == len(effects):
                    # Validate before the final phase transition.
                    actual = _verify_tree(paths, rootfd, journal, originals, targets)
                    if validate_catalog({p: b for p, (b, _) in actual.items()}):
                        raise _conflict('Published catalogue invalid')
                    _facts(paths, plan, journal, rootfd)
                    journal['phase'] = 'files_published'
                _journal_write(path, journal)
            return {'phase': 'files_published', 'plan_digest': plan['plan_digest'], 'result_sha256': digest({p: {'sha256': digest(b), 'mode': mode} for p, (b, mode) in sorted({**{p: v for p, v in originals.items() if not p.startswith('packages/')}, **targets}.items())}), 'worked': True}
        finally:
            os.close(rootfd)
