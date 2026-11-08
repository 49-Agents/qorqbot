"""Read-only evidence used by the explicit legacy layout preparation."""
from __future__ import annotations

from bazarlyk.posix_files import immutable_database_uri

import json
import sqlite3


def unfinished_work(connection):
    """Inspect saved receipts without dispatching or recovering any work.

    A running-control workflow can be idle; unfinished reservations and receipts,
    rather than the run's control flag alone, are the blocking evidence.
    """
    connection.row_factory = sqlite3.Row
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conflicts = []
    def bounded(query):
        rows = connection.execute(query + ' LIMIT 10001').fetchall()
        if len(rows) > 10000:
            conflicts.append({'code': 'runtime_evidence_limit'})
        return rows[:10000]
    if 'pending_changes' in tables:
        for row in bounded('SELECT change_id FROM pending_changes ORDER BY change_id'):
            conflicts.append({'code': 'unfinished_authoring', 'change_id': row['change_id']})
    if 'pending_files' in tables:
        for row in bounded('SELECT DISTINCT change_id FROM pending_files ORDER BY change_id'):
            conflicts.append({'code': 'unfinished_authoring_files', 'change_id': row['change_id']})
    if 'overseer_publications' in tables:
        for row in bounded("SELECT change_id,operation_id,stage FROM overseer_publications WHERE stage NOT IN ('prepared','merged','complete','superseded') ORDER BY operation_id"):
            conflicts.append({'code': 'unfinished_publication', **dict(row)})
    if 'delta_applications' in tables:
        for row in bounded("""SELECT substr(id,1,256) AS application_id,substr(delta_id,1,256) AS delta_id,
                               substr(phase,1,256) AS phase,merge IS NOT NULL AS merge_recorded,
                               CASE WHEN json_valid(checks) THEN substr(json_extract(checks, '$.state'),1,256) END AS check_state,
                               CASE WHEN json_valid(checks) THEN substr(json_extract(checks, '$.phase'),1,256) END AS check_phase,
                               json_valid(checks) AS checks_valid
                               FROM delta_applications
                               WHERE phase != 'complete' AND (phase = 'merged' OR merge IS NOT NULL)
                               ORDER BY id"""):
            conflicts.append({'code': 'unsettled_merged_application', **dict(row)})
    if 'invocations' in tables:
        for row in bounded("SELECT invocation_id,status,runner FROM invocations WHERE status='in_flight' ORDER BY invocation_id"):
            conflicts.append({'code': 'in_flight_invocation', **dict(row)})
    if {'run_attempts', 'invocations'} <= tables:
        for row in bounded("""SELECT a.run_id,a.step_id,a.operation_id,a.invocation_id,a.status AS attempt_status,
                                             i.status AS receipt_status
                                      FROM run_attempts a LEFT JOIN invocations i ON i.invocation_id=a.invocation_id
                                      WHERE a.status='verifying' ORDER BY a.run_id,a.operation_id"""):
            conflicts.append({'code': 'unresolved_verification', **dict(row)})
    if 'overseer_operations' in tables:
        # Only the receipt's validity and its top-level `_state` decide here, so SQLite reads just those: a large but valid
        # saved result (bounded by the connection's own length limit) is not mistaken for a malformed one.
        for row in bounded("""SELECT operation_id, json_valid(result) AS valid,
                                     CASE WHEN json_valid(result) AND json_type(result) = 'object'
                                               AND json_type(result, '$._state') = 'text'
                                          THEN substr(json_extract(result, '$._state'), 1, 256) END AS state
                              FROM overseer_operations ORDER BY operation_id"""):
            if not row['valid']:
                conflicts.append({'code': 'malformed_operation', 'operation_id': row['operation_id']})
                continue
            if row['state'] in ('check_running', 'candidate_reserved'):
                conflicts.append({'code': 'unfinished_check', 'operation_id': row['operation_id'],
                                  'state': row['state']})
    return conflicts


def legacy_destination(package_id, path):
    """Map a legacy package-relative resource to its fixed new container and path.

    Singleton validation and manifest partitioning happen before this mapping is
    accepted by a complete plan.
    """
    from .support import identifier, relative

    identifier(package_id)
    relative(path)
    if package_id == 'agentic-management':
        container = 'operations/agentic-management'
    elif package_id == 'overseer':
        container = 'overseer/overseer'
    elif package_id in ('imported-host-builtins', 'imported-host-builtins-templates', 'runtime-instructions', 'tool-instructions'):
        container = 'operations/' + package_id
    elif package_id == 'imported-roles':
        if path.startswith('native/skills/learning-application/'):
            container = 'overseer/overseer-role-skills'
        elif path.startswith('native/skills/'):
            container = 'operations/imported-role-skills'
        else:
            container = 'roles/imported-roles'
    elif package_id == 'user-preferences':
        container = 'user-preferences'
    else:
        container = 'plugins/' + package_id
    return container + '/' + path


def read_legacy_tree(root, area='packages', *, metadata=None):
    """Read every regular legacy package file through no-follow directory handles.

    Special files and paths outside the supported authored path grammar are
    reported individually; callers must treat each as a blocking conflict.
    """
    import os
    import stat
    from pathlib import Path
    from .support import ImportFailure, MAX_FILE_BYTES, relative

    root = Path(root)
    files, conflicts = {}, []
    total, seen = 0, 0
    if not root.is_absolute() or '..' in root.parts:
        return files, [{'code': 'unsafe_root', 'path': str(root)}]
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            if area not in ('packages', 'roles', 'operations', 'overseer', 'plugins', 'user-preferences'):
                raise OSError('invalid authored area')
            for part in (*root.parts[1:], area):
                next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        except OSError as exc:
            return files, [{'code': 'unsafe_or_missing_area', 'path': area, 'reason': type(exc).__name__}]

        def walk(parent, prefix):
            nonlocal total, seen
            entries = []
            with os.scandir(parent) as stream:
                for entry in stream:
                    seen += 1
                    if seen > 100000:
                        conflicts.append({'code': 'file_count_limit_exceeded', 'path': prefix})
                        return
                    entries.append(entry)
            for entry in sorted(entries, key=lambda item: item.name):
                path = prefix + '/' + entry.name
                try:
                    relative(path)
                    if len(path.split('/')) > 64:
                        conflicts.append({'code': 'path_depth_limit_exceeded', 'path': path})
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        try:
                            walk(child, path)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        if info.st_size > MAX_FILE_BYTES or total + info.st_size > 96 * 1024 * 1024:
                            conflicts.append({'code': 'file_limit_exceeded', 'path': path})
                            continue
                        child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                        try:
                            opened = os.fstat(child)
                            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                                raise OSError('file changed during inspection')
                            chunks, count = [], 0
                            while True:
                                chunk = os.read(child, min(65536, MAX_FILE_BYTES + 1 - count))
                                if not chunk:
                                    break
                                chunks.append(chunk)
                                count += len(chunk)
                                if count > MAX_FILE_BYTES or total + count > 96 * 1024 * 1024:
                                    raise OSError('file exceeds inspection bound')
                            finished = os.fstat(child)
                            if count != opened.st_size or (finished.st_size, finished.st_mtime_ns, finished.st_ctime_ns, finished.st_mode) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns, opened.st_mode):
                                raise OSError('file changed during inspection')
                            files[path] = b''.join(chunks)
                            if metadata is not None:
                                import hashlib
                                metadata[path] = {'sha256': hashlib.sha256(files[path]).hexdigest(),
                                                  'bytes': count, 'mode': stat.S_IMODE(opened.st_mode)}
                            total += count
                        finally:
                            os.close(child)
                    else:
                        conflicts.append({'code': 'unsafe_authored_file', 'path': path})
                except (OSError, ImportFailure) as exc:
                    conflicts.append({'code': 'unsafe_authored_file', 'path': path, 'reason': type(exc).__name__})
        walk(descriptor, area)
    finally:
        os.close(descriptor)
    return files, conflicts


def inspect_runtime(state_dir, root, instance_id):
    """Inspect an offline database without creating SQLite sidecars or checkpointing."""
    import hashlib
    import os
    import stat
    from pathlib import Path

    result = {'inspected': False, 'schema_version': None, 'migration_required': None,
              'database_fingerprint': None, 'conflicts': []}
    state_dir = Path(state_dir)
    if not state_dir.is_absolute() or '..' in state_dir.parts:
        result['conflicts'].append({'code': 'unsafe_state_path', 'path': str(state_dir)})
        return result
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    db = None
    try:
        for component in state_dir.parts[1:]:
            next_directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        def state(name):
            try:
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(info.st_mode):
                raise OSError('runtime path is not a regular file')
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        before = {name: state(name) for name in ('state.sqlite3', 'state.sqlite3-wal', 'state.sqlite3-shm')}
        if before['state.sqlite3'] is None:
            raise OSError('runtime database missing')
        if before['state.sqlite3-wal'] and before['state.sqlite3-wal'][2]:
            result['conflicts'].append({'code': 'runtime_wal_present', 'path': 'state.sqlite3-wal',
                'instruction': 'Complete normal offline shutdown, checkpoint or recovery outside preparation before replanning.'})
            return result
        db = os.open('state.sqlite3', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        opened = os.fstat(db)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != before['state.sqlite3']:
            raise OSError('runtime database changed before inspection')
        # Bound offline inspection to 1 GiB; hash the complete database through the
        # same no-follow descriptor used for the SQLite inspection.
        if opened.st_size > 1024 * 1024 * 1024:
            result['conflicts'].append({'code': 'runtime_database_limit_exceeded'})
            return result
        hasher = hashlib.sha256()
        size = 0
        while chunk := os.read(db, 1024 * 1024):
            size += len(chunk)
            if size > 1024 * 1024 * 1024:
                raise OSError('runtime database exceeded inspection bound')
            hasher.update(chunk)
        if size != opened.st_size:
            raise OSError('runtime database changed during hashing')
        result['database_fingerprint'] = {'sha256': hasher.hexdigest(), 'bytes': size}
        connection = sqlite3.connect(immutable_database_uri(db), uri=True)
        try:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024 * 1024)
            meta = dict(connection.execute('SELECT key,value FROM meta'))
            version = meta.get('schema_version')
            result['schema_version'] = version
            if version not in ('1', '2', '3'):
                result['conflicts'].append({'code': 'unsupported_runtime_schema', 'schema_version': version})
            else:
                from qorqut_agentic.store import _verify_layout
                from qorqut_agentic.errors import AgenticError
                try:
                    _verify_layout(connection, version)
                except AgenticError:
                    result['conflicts'].append({'code': 'invalid_runtime_schema'})
            if meta.get('root_path') != str(root) or meta.get('instance_id') != instance_id:
                result['conflicts'].append({'code': 'runtime_identity_mismatch'})
            if not meta.get('initialized_at'):
                result['conflicts'].append({'code': 'invalid_runtime_metadata'})
            result['migration_required'] = version in ('1', '2')
            result['conflicts'].extend(unfinished_work(connection))
            result['inspected'] = True
        finally:
            connection.close()
        after = {name: state(name) for name in before}
        opened = os.fstat(db)
        if after != before or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != before['state.sqlite3']:
            result['inspected'] = False
            result['conflicts'].append({'code': 'runtime_changed_during_inspection'})
    except (OSError, sqlite3.Error, ValueError) as exc:
        result['inspected'] = False
        result['conflicts'].append({'code': 'runtime_inspection_failed', 'reason': str(exc)})
    finally:
        if db is not None:
            os.close(db)
        os.close(directory)
    return result


def transform_legacy(files):
    """Partition installed manifests and map installed bytes without refreshing them."""
    from copy import deepcopy
    from qorqut_agentic.util import load_yaml, dump_yaml
    from .support import digest, identifier, relative, ImportFailure

    conflicts, outputs, path_map, ref_map, rewrites, manifests = [], {}, {}, {}, [], {}
    packages = set()
    for path in files:
        parts = path.split('/')
        if len(parts) < 3 or parts[0] != 'packages':
            conflicts.append({'code': 'unclassifiable_resource', 'path': path})
        else:
            packages.add(parts[1])
    for package in sorted(packages):
        path = f'packages/{package}/agentic.yaml'
        try:
            identifier(package)
            if path not in files:
                conflicts.append({'code': 'missing_manifest', 'path': path})
                continue
            manifest = load_yaml(files[path].decode('utf-8'))
            if not isinstance(manifest, dict) or manifest.get('id') != package or not isinstance(manifest.get('entries'), list):
                raise ValueError('manifest ID or entries invalid')
            if package == 'user-preferences':
                entries = manifest['entries']
                if len(entries) != 1 or not isinstance(entries[0], dict) or (entries[0].get('id'), entries[0].get('kind'), entries[0].get('path')) != ('preferences', 'skill', 'SKILL.md'):
                    raise ValueError('malformed preferences singleton')
            manifests[package] = manifest
            seen = set()
            for entry in manifest['entries']:
                if not isinstance(entry, dict):
                    raise ValueError('invalid entry')
                entry_id = identifier(entry.get('id'))
                path_in_package = relative(entry.get('path'))
                if entry_id in seen:
                    raise ValueError('duplicate entry ID')
                seen.add(entry_id)
                target = legacy_destination(package, path_in_package)
                target_package = target.split('/')[0] if target.startswith('user-preferences/') else target.split('/')[1]
                ref_map[package + '/' + entry_id] = target_package + '/' + entry_id
                if f'packages/{package}/{path_in_package}' not in files:
                    conflicts.append({'code': 'missing_installed_entry', 'path': f'packages/{package}/{path_in_package}'})
        except (UnicodeError, ValueError, ImportFailure, TypeError) as exc:
            conflicts.append({'code': 'malformed_manifest', 'path': path, 'reason': str(exc)})
            manifests.pop(package, None)
    def put(old, new, body):
        if new in outputs:
            conflicts.append({'code': 'destination_conflict', 'path': new, 'source': old})
            return
        outputs[new] = body
        path_map.setdefault(old, []).append(new)
    for path, body in sorted(files.items()):
        parts = path.split('/')
        if len(parts) < 3 or parts[0] != 'packages':
            continue
        package, rel = parts[1], '/'.join(parts[2:])
        if package not in manifests:
            conflicts.append({'code': 'unmappable_resource', 'path': path})
            continue
        if rel == 'agentic.yaml':
            continue
        try:
            target = legacy_destination(package, rel)
            if package == 'imported-roles' and rel.startswith('native/roles/') and rel.endswith('/role.yaml'):
                role = load_yaml(body.decode('utf-8'))
                if not isinstance(role, dict) or not isinstance(role.get('brief'), str) or not isinstance(role.get('skills', []), list):
                    raise ValueError('malformed native role manifest')
                for record, field in [(role, 'brief'), *((skill, 'path') for skill in role.get('skills', []))]:
                    if not isinstance(record, dict):
                        raise ValueError('malformed native role skill')
                    source_path = 'native/' + relative(record.get(field))
                    if f'packages/{package}/{source_path}' not in files:
                        conflicts.append({'code': 'missing_installed_asset', 'path': f'packages/{package}/{source_path}'})
                    record[field] = legacy_destination(package, source_path)
                rewritten = dump_yaml(role).encode('utf-8')
                if rewritten != body:
                    rewrites.append({'old_path': path, 'new_path': target,
                                     'old_sha256': digest(body), 'new_sha256': digest(rewritten)})
                    body = rewritten
            put(path, target, body)
        except (ImportFailure, ValueError, UnicodeError, TypeError) as exc:
            conflicts.append({'code': 'unmappable_resource', 'path': path, 'reason': str(exc)})
    for package, original in sorted(manifests.items()):
        old_path = f'packages/{package}/agentic.yaml'
        grouped = {}
        for entry in original['entries']:
            try:
                target = legacy_destination(package, entry['path'])
                container = target.rsplit('/' + entry['path'], 1)[0]
                copied = deepcopy(entry)
                if 'requires' in copied:
                    copied['requires'] = [ref_map.get(ref, ref) for ref in copied['requires']]
                grouped.setdefault(container, []).append(copied)
            except (KeyError, TypeError, ValueError, ImportFailure) as exc:
                conflicts.append({'code': 'unmappable_entry', 'path': old_path, 'reason': str(exc)})
        if package == 'imported-roles':
            for output in outputs:
                if output.startswith(('roles/imported-roles/', 'operations/imported-role-skills/', 'overseer/overseer-role-skills/')):
                    group = output.split('/')[:2]
                    grouped.setdefault('/'.join(group), [])
        if not grouped:
            grouped[legacy_destination(package, 'agentic.yaml').rsplit('/', 1)[0]] = []
        for container, entries in sorted(grouped.items()):
            manifest = deepcopy(original)
            manifest['id'] = container.rsplit('/', 1)[-1]
            manifest['entries'] = entries
            new_path = container + '/agentic.yaml'
            body = files[old_path] if manifest == original else dump_yaml(manifest).encode('utf-8')
            put(old_path, new_path, body)
            if body != files[old_path]:
                rewrites.append({'old_path': old_path, 'new_path': new_path,
                                 'old_sha256': digest(files[old_path]), 'new_sha256': digest(body)})
    ids = {}
    for path in sorted(outputs):
        if path.endswith('/agentic.yaml'):
            container = path.rsplit('/', 1)[0]
            package_id = container.rsplit('/', 1)[-1]
            if package_id in ids and ids[package_id] != container:
                conflicts.append({'code': 'duplicate_package_id', 'package_id': package_id,
                                  'paths': [ids[package_id], container]})
            ids[package_id] = container
    return {'files': outputs, 'path_map': path_map, 'ref_map': ref_map, 'rewrites': rewrites, 'conflicts': conflicts}


def inspect_git(root, *, entire_worktree=False):
    """Capture HEAD, index and authored dirty paths without refreshing the index."""
    import hashlib
    import os
    from pathlib import Path
    import stat
    import subprocess
    import selectors
    import time
    from .support import MAX_GIT_OUTPUT_BYTES

    root = Path(root)
    result = {'head': None, 'index_sha256': None, 'index_entries': [], 'dirty': [], 'conflicts': []}
    try:
        if not root.is_absolute() or any(part.is_symlink() for part in (root, *root.parents)):
            raise OSError('unsafe repository root')
        gitdir = root / '.git'
        if gitdir.is_symlink() or not gitdir.is_dir():
            raise OSError('repository .git must be a real directory')
        for name in ('index.lock', 'HEAD.lock', 'packed-refs.lock', 'config.lock', 'shallow.lock',
                     'ORIG_HEAD.lock', 'FETCH_HEAD.lock', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                     'REBASE_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_LOG'):
            if os.path.lexists(gitdir / name):
                result['conflicts'].append({'code': 'unfinished_git_operation', 'path': '.git/' + name})
        refs = gitdir / 'refs'
        if refs.is_symlink():
            raise OSError('Git refs directory is a symlink')
        def inaccessible(_error):
            raise OSError('Cannot inspect Git refs')
        for directory, subdirs, names in os.walk(refs, followlinks=False, onerror=inaccessible):
            if any((Path(directory) / name).is_symlink() for name in subdirs):
                raise OSError('Git refs contains a symlink')
            for name in names:
                path = Path(directory) / name
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    raise OSError('Git ref is not a regular file')
                if name.endswith('.lock'):
                    result['conflicts'].append({'code': 'unfinished_git_operation', 'path': str(path.relative_to(root))})
        if result['conflicts']:
            return result
        index_path = gitdir / 'index'
        index = os.open(index_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(index)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 96 * 1024 * 1024:
                raise OSError('unsafe or oversized Git index')
            hasher = hashlib.sha256()
            size = 0
            while chunk := os.read(index, min(65536, 96 * 1024 * 1024 + 1 - size)):
                size += len(chunk)
                if size > 96 * 1024 * 1024:
                    raise OSError('oversized Git index')
                hasher.update(chunk)
            result['index_sha256'] = hasher.hexdigest()
            before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            env = {'PATH': os.defpath, 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
                   'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
                   'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_TERMINAL_PROMPT': '0',
                   'GIT_ATTR_NOSYSTEM': '1', 'LC_ALL': 'C'}
            from bazarlyk.native_paths import environment, executable
            env = environment(env)
            base = [executable('git'), '--no-optional-locks', '-c', 'core.hooksPath=/dev/null',
                    '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false',
                    '-c', 'gc.auto=0', '-c', 'maintenance.auto=false', '-C', str(root)]
            def run(args, *, allow_missing=False):
                process = subprocess.Popen(base + args, env=env, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                selector = selectors.DefaultSelector()
                chunks, size, deadline = [], 0, time.monotonic() + 30
                try:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise OSError('Git inspection timed out')
                        chunk = os.read(process.stdout.fileno(), min(65536, MAX_GIT_OUTPUT_BYTES + 1 - size))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > MAX_GIT_OUTPUT_BYTES:
                            raise OSError('Git inspection output exceeds limit')
                    status = process.wait(timeout=max(0.01, deadline - time.monotonic()))
                    if status != 0 and not (allow_missing and status == 1):
                        raise OSError('Git inspection failed')
                    return b''.join(chunks)
                finally:
                    selector.close()
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    process.stdout.close()
            from .layout_preflight import check
            result['conflicts'].extend(check(root, run))
            if result['conflicts']:
                return result
            result['head'] = run(['rev-parse', '--verify', 'HEAD']).decode('ascii').strip()
            if entire_worktree:
                branch = run(['symbolic-ref', '-q', '--no-recurse', 'HEAD'], allow_missing=True).decode('utf-8').strip()
                result['branch'] = branch or None
            authored = ['packages', 'roles', 'operations', 'overseer', 'plugins', 'user-preferences']
            result['index_entries'] = [item.decode('utf-8') for item in run(['ls-files', '--stage', '-z', '--', *authored]).split(b'\0') if item]
            status_args = ['status', '--porcelain=v1', '-z', '--untracked-files=all']
            if not entire_worktree:
                status_args += ['--', *authored]
            result['dirty'] = [item.decode('utf-8') for item in run(status_args).split(b'\0') if item]
            if (run(['rev-parse', '--verify', 'HEAD']).decode('ascii').strip() != result['head']
                    or (entire_worktree and (run(['symbolic-ref', '-q', '--no-recurse', 'HEAD'], allow_missing=True).decode('utf-8').strip() or None) != result['branch'])):
                result['conflicts'].append({'code': 'git_changed_during_inspection'})
            after_info = os.fstat(index)
            after = (after_info.st_dev, after_info.st_ino, after_info.st_size, after_info.st_mtime_ns, after_info.st_ctime_ns)
            if before != after or os.stat(index_path, follow_symlinks=False).st_ino != info.st_ino:
                result['conflicts'].append({'code': 'git_changed_during_inspection'})
        finally:
            os.close(index)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        result['conflicts'].append({'code': 'git_inspection_failed', 'reason': str(exc)})
    return result


def retained_layout_inventory(inventory, mapping):
    """Exclude the retired learning-work entry only when its authored file and ref are absent.

    The original inventory/import receipt remains in the migration's retained provenance.
    Other missing entries still fail normal completeness validation.
    """
    from copy import deepcopy
    ref = 'imported-host-builtins/learning-work'
    relative_path = 'native/app/bazarlyk/agentic/assets/learning-work.md'
    path = 'packages/imported-host-builtins/' + relative_path
    if ref in mapping['ref_map'] or path in mapping['path_map']:
        return inventory
    result = deepcopy(inventory)
    for package in result.get('packages', []):
        if package['id'] == 'imported-host-builtins':
            package['entries'] = [entry for entry in package['entries']
                                  if not (entry['id'] == 'learning-work' and entry['path'] == relative_path)]
            package['files'].pop(relative_path, None)
    result['assets'] = [asset for asset in result['assets'] if asset['destination'] != path]
    for binding in result['bindings']:
        binding['entry_refs'] = [value for value in binding['entry_refs'] if value != ref]
        if 'skills' in binding:
            binding['skills'] = [skill for skill in binding['skills'] if skill['canonical_entry'] != ref]
    result['coverage']['asset_count'] = len(result['assets'])
    return result


def transform_inventory(inventory, mapping):
    """Relocate inventory destinations while retaining supplier records and hashes."""
    from copy import deepcopy
    from .support import relative

    inventory = retained_layout_inventory(inventory, mapping)
    result = deepcopy(inventory)
    conflicts = []
    packages, owners = {}, {}
    refs = mapping['ref_map']
    try:
        if result.get('schema_version') != 1:
            raise ValueError('expected legacy inventory schema 1')
        for original in inventory['packages']:
            old_id = original['id']
            def target_package(rel):
                destination = legacy_destination(old_id, rel)
                suffix = '/' + rel
                return destination[:-len(suffix)]
            def group(container):
                new_id = container.rsplit('/', 1)[-1]
                if new_id not in packages:
                    item = deepcopy(original)
                    item.update(id=new_id, path=container, entries=[], files={})
                    packages[new_id] = item
                    owners[new_id] = old_id
                elif packages[new_id]['path'] != container or owners[new_id] != old_id:
                    raise ValueError('duplicate package ID ' + new_id)
                return packages[new_id]
            for rel, item in original['files'].items():
                relative(rel)
                group(target_package(rel))['files'][rel] = deepcopy(item)
            for entry in original['entries']:
                new = deepcopy(entry)
                new['requires'] = [refs.get(ref, ref) for ref in entry.get('requires', [])] if 'requires' in entry else new.get('requires')
                if new.get('requires') is None:
                    new.pop('requires', None)
                old_ref = old_id + '/' + entry['id']
                if old_ref not in refs:
                    conflicts.append({'code': 'missing_inventory_entry', 'ref': old_ref})
                group(target_package(entry['path']))['entries'].append(new)
            if not original['files'] and not original['entries']:
                group(target_package('agentic.yaml'))
        result['packages'] = [packages[key] for key in sorted(packages)]
        for asset in result['assets']:
            original = asset['destination']
            prefix = 'packages/' + asset['package_id'] + '/'
            if not original.startswith(prefix):
                raise ValueError('asset destination does not match package')
            target = legacy_destination(asset['package_id'], original[len(prefix):])
            asset['destination'] = target
            asset['package_id'] = target.split('/')[0] if target.startswith('user-preferences/') else target.split('/')[1]
            for binding in asset.get('native_bindings', []):
                if not isinstance(binding, dict):
                    raise ValueError('invalid native binding')
        for binding in result['bindings']:
            old_refs = binding['entry_refs']
            for ref in old_refs:
                if ref not in refs:
                    conflicts.append({'code': 'missing_inventory_binding_ref', 'ref': ref})
            binding['entry_refs'] = [refs.get(ref, ref) for ref in old_refs]
            for skill in binding.get('skills', []):
                ref = skill['canonical_entry']
                if ref not in refs:
                    conflicts.append({'code': 'missing_inventory_binding_ref', 'ref': ref})
                skill['canonical_entry'] = refs.get(ref, ref)
            if binding['package_id'] == 'imported-roles' and binding['kind'] != 'role':
                package_ids = {ref.split('/')[0] for ref in binding['entry_refs']}
                if len(package_ids) != 1:
                    conflicts.append({'code': 'ambiguous_binding_package', 'native_id': binding['native_id']})
                else:
                    binding['package_id'] = package_ids.pop()
        result['schema_version'] = 2
        result['coverage']['package_count'] = len(result['packages'])
    except (KeyError, TypeError, ValueError) as exc:
        conflicts.append({'code': 'invalid_legacy_inventory', 'reason': str(exc)})
    return result, conflicts


def load_memory_catalog(files):
    """Load the projected authored catalogue in memory, without a scan."""
    from qorqut_agentic.catalog import load_catalog
    from qorqut_agentic.files import FileEntry
    from .support import digest

    class Source:
        prefix = None
        special = {}
        legacy_nonempty = False
        def __init__(self, bodies):
            self.bodies = bodies
            self.files = {path: FileEntry(digest(body), len(body)) for path, body in bodies.items()}
        def read(self, path):
            return self.bodies[path]
    return load_catalog(Source(files))


def validate_catalog(files, *, required_refs=None):
    """Return errors and missing canonical content from an in-memory catalogue."""
    return catalog_issues(load_memory_catalog(files), files, required_refs=required_refs)


def catalog_issues(catalogue, files, *, required_refs=None):
    """Check an already loaded projection and its exact captured file map."""
    issues = [issue.to_dict() for issue in catalogue.issues if issue.severity == 'error']
    for ref, (kind, path) in (required_refs or {}).items():
        entry = catalogue.entries.get(ref)
        if entry is None or entry.kind != kind or (path is not None and entry.root_path != path):
            issues.append({'code': 'missing_canonical_entry', 'severity': 'error', 'entry_ref': ref,
                           'path': path, 'message': 'Required canonical entry is missing or invalid'})
        elif ref == 'user-preferences/preferences':
            try:
                body = files[entry.root_path].decode('utf-8')
            except (KeyError, UnicodeError):
                continue  # The catalogue reports missing or invalid content.
            if not body.strip():
                issues.append({'code': 'empty_canonical_preferences', 'severity': 'error', 'entry_ref': ref,
                               'path': entry.root_path, 'message': 'Canonical preferences must not be empty'})
    return issues
