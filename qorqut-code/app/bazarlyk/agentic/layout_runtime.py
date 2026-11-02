"""Internal offline reconciliation of the runtime after layout Git publication."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import struct
import stat
from contextlib import contextmanager
from pathlib import Path

from bazarlyk.telegram.private_files import read_private_file, require_private_directory
from qorqut_agentic.changes import Changes, event_dict, normalize_attribution
from qorqut_agentic.core import LayerContext
from qorqut_agentic.store import Store, _verify_layout
from qorqut_agentic.util import new_id

from . import layout_binding, layout_files as files, layout_git as git
from .layout_inspection import inspect_runtime, unfinished_work, validate_catalog
from .layout_intent import _conflict, incomplete_intents, retained_intents
from .support import canonical, digest
from .upgrade import _unique, history_directory, read_binding_bytes

_LIMIT = 1024 * 1024


def _read(path):
    raw = read_private_file(path, max_bytes=_LIMIT)
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'phase', 'inputs', 'result', 'receipt_sha256'}
                or value['schema_version'] != 1 or value['phase'] not in ('prepared', 'runtime_published')
                or raw != canonical(value) + b'\n' or value['receipt_sha256'] != digest({k: v for k, v in value.items() if k != 'receipt_sha256'})
                or (value['phase'] == 'prepared') != (value['result'] is None)):
            raise ValueError('invalid runtime journal')
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise _conflict('Invalid runtime layout journal') from exc


def _write(path, value):
    if len(canonical(value)) > _LIMIT - 256:
        raise _conflict('Runtime layout journal exceeds bound')
    files._journal_write(path, value)


def _digest_database(conn, excluded_key):
    """Hash logical schema and typed, rowid-ordered contents without exporting rows."""
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024 * 1024)
    h = hashlib.sha256()
    def cell(value):
        if value is None:
            tag, data = b'n', b''
        elif isinstance(value, int):
            tag, data = b'i', str(value).encode('ascii')
        elif isinstance(value, float):
            tag, data = b'f', struct.pack('>d', value)
        elif isinstance(value, str):
            tag, data = b't', value.encode('utf-8')
        elif isinstance(value, bytes):
            tag, data = b'b', value
        else:
            raise _conflict('Unsupported SQLite value')
        h.update(tag + len(data).to_bytes(8, 'big'))
        h.update(data)
    schema = list(conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
    for row in schema:
        cell('schema')
        for value in row:
            cell(value)
    for row in schema:
        if row[0] != 'table':
            continue
        name = row[1]
        if name.startswith('sqlite_') and name != 'sqlite_sequence':
            continue
        cell('table'); cell(name)
        quoted = '"' + name.replace('"', '""') + '"'
        query = 'SELECT rowid,* FROM ' + quoted
        params = ()
        if name == 'meta':
            query += ' WHERE key != ?'
            params = (excluded_key,)
        query += ' ORDER BY rowid'
        for item in conn.execute(query, params):
            cell('row')
            for value in item:
                cell(value)
    return h.hexdigest()


def _evidence(paths, plan, attribution, *, binding, current=True, retained=False):
    normalize_attribution(attribution, trust='host_installer')
    if (not isinstance(plan, dict) or not isinstance(plan.get('plan_digest'), str)
            or not re.fullmatch('[0-9a-f]{64}', plan['plan_digest'])
            or digest({k: v for k, v in plan.items() if k != 'plan_digest'}) != plan['plan_digest']):
        raise _conflict('Invalid layout plan')
    files._safe_ancestors(paths.agentic_root)
    files._safe_ancestors(paths.agentic_runtime)
    directory = require_private_directory(history_directory(paths))
    intents = retained_intents(paths) if retained else incomplete_intents(paths)
    intents = [item for item in intents if item['plan'] == plan] if retained else intents
    if len(intents) != 1 or intents[0]['plan'] != plan or intents[0]['attribution'] != attribution:
        raise _conflict('Matching intent and attribution required')
    intent = intents[0]
    rp = directory / ('relocation-' + plan['plan_digest'] + '.json')
    gp = directory / ('git-layout-' + plan['plan_digest'] + '.json')
    path = directory / ('runtime-layout-' + plan['plan_digest'] + '.json')
    for item in directory.iterdir():
        if any(item.name.startswith(prefix) and item != wanted for prefix, wanted in
               [('relocation-', rp), ('git-layout-', gp), ('runtime-layout-', path)]):
            raise _conflict('Unknown layout receipt')
    relocation = files._journal_read(rp)
    originals, targets, effects = files._derive(plan, intent, relocation['initial_dirs'])
    if (relocation['phase'] != 'files_published' or relocation['plan_digest'] != plan['plan_digest']
            or relocation['intent_sha256'] != intent['receipt_sha256'] or relocation['attribution'] != attribution
            or relocation['effects'] != effects
            or set(relocation['created_dirs']) != {e['path'] for e in effects if e['kind'] == 'mkdir'}):
        raise _conflict('Matching completed relocation required')
    journal = git._read_journal(gp)
    if (journal['phase'] != 'git_published' or journal['plan_digest'] != plan['plan_digest']
            or journal['intent_sha256'] != intent['receipt_sha256'] or journal['relocation_sha256'] != relocation['receipt_sha256']
            or journal['attribution'] != attribution or journal['root'] != relocation['root']
            or journal['gitdir'] != relocation['gitdir'] or journal['head_identity'] != relocation['head_identity']
            or journal['parent'] != intent['head'] or journal['original_index_sha256'] != intent['index']['sha256']):
        raise _conflict('Matching completed Git publication required')
    candidate, _, candidate_digest = layout_binding.build_candidate(paths, plan)
    if not current:
        return directory, path, intent, relocation, journal, candidate, candidate_digest, None
    if binding and digest(read_binding_bytes(paths)) != plan['binding']['record_sha256']:
        raise _conflict('Original active binding changed')
    state = os.stat(paths.agentic_runtime, follow_symlinks=False)
    if {'device': state.st_dev, 'inode': state.st_ino} != plan['identity']['state']:
        raise _conflict('Runtime directory identity changed')
    if {'device': relocation['root']['device'], 'inode': relocation['root']['inode']} != plan['identity']['root']:
        raise _conflict('Prepared root identity changed')
    rootfd = files._open_root(paths.agentic_root)
    try:
        if files._identity(os.fstat(rootfd)) != relocation['root'] or files._identity(os.stat(paths.agentic_root, follow_symlinks=False)) != relocation['root']:
            raise _conflict('Authored root changed')
        runner = git._runner(paths.agentic_root, directory)
        destination = git._destination(runner, journal)
        git._reject_filters(runner)
        git._check_operation_locks(paths.agentic_root, destination)
        if git._head_state(runner, rootfd, journal, destination) != 'candidate':
            raise _conflict('Published ref changed')
        gitfd = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
        try:
            if files._identity(os.fstat(gitfd)) != relocation['gitdir']:
                raise _conflict('Git directory changed')
            state, _, held = git._index_state(gitfd, journal, base64.b64decode(intent['index']['content_base64']), base64.b64decode(journal['final_index']['content_base64']))
            if held is not None:
                os.close(held)
            if state != 'final':
                raise _conflict('Published index changed')
        finally:
            os.close(gitfd)
        authored = files._verify_tree(paths, rootfd, relocation, originals, targets)
        if validate_catalog({p: b for p, (b, _) in authored.items()}):
            raise _conflict('Published catalogue invalid')
        git._verify_candidate(runner, journal, intent, authored)
    finally:
        os.close(rootfd)
    return directory, path, intent, relocation, journal, candidate, candidate_digest, authored


def _fixed(plan, attribution, intent, relocation, journal, candidate_digest, timestamp, change_id):
    return {'plan_digest': plan['plan_digest'], 'intent_sha256': intent['receipt_sha256'],
            'relocation_sha256': relocation['receipt_sha256'], 'git_receipt_sha256': journal['receipt_sha256'],
            'candidate_binding_sha256': candidate_digest, 'source_state': plan['state'],
            'attribution': attribution, 'timestamp': timestamp, 'change_id': change_id}


def _validate_private(journal, plan, attribution, intent, relocation, git_journal, candidate_digest):
    inputs = journal['inputs']
    if (not isinstance(inputs, dict) or set(inputs) != {'plan_digest', 'intent_sha256', 'relocation_sha256', 'git_receipt_sha256', 'candidate_binding_sha256', 'source_state', 'attribution', 'timestamp', 'change_id'}
            or not isinstance(inputs['timestamp'], str) or not inputs['timestamp']
            or not isinstance(inputs['change_id'], str) or not re.fullmatch(r'chg_[0-9a-f]{32}', inputs['change_id'])
            or inputs != _fixed(plan, attribution, intent, relocation, git_journal, candidate_digest, inputs['timestamp'], inputs['change_id'])):
        raise _conflict('Runtime preparation inputs changed')


def _receipt(conn, key, inputs, plan, git_journal):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    if row is None:
        return None
    try:
        raw = row[0]
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > _LIMIT:
            raise ValueError('oversized runtime receipt')
        value = json.loads(raw, object_pairs_hook=_unique)
        keys = {'format_version', 'input_sha256', 'schema_from', 'schema_to', 'candidate_commit', 'change_id',
                'sequence', 'counts', 'inventory_sha256', 'logical_sha256', 'receipt_sha256'}
        if (not isinstance(value, dict) or set(value) != keys or raw != canonical(value).decode('utf-8')
                or value['receipt_sha256'] != digest({k: v for k, v in value.items() if k != 'receipt_sha256'})
                or value['format_version'] != 1 or value['input_sha256'] != digest(inputs)
                or value['schema_from'] != plan['state']['schema_version'] or value['schema_to'] != '3'
                or value['candidate_commit'] != git_journal['commit']
                or value['change_id'] not in (None, inputs['change_id'])
                or (value['change_id'] is None) != (value['sequence'] is None)):
            raise ValueError('invalid receipt')
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise _conflict('Conflicting runtime database receipt') from exc


def _verify_database(conn, key, receipt, plan, authored, inputs):
    meta = {r['key']: r['value'] for r in conn.execute('SELECT key,value FROM meta')}
    if (meta.get('schema_version') != '3' or meta.get('root_path') != plan['binding']['root']
            or meta.get('instance_id') != plan['binding']['instance_id']):
        raise _conflict('Runtime identity changed')
    _verify_layout(conn, '3')
    if _digest_database(conn, key) != receipt['logical_sha256']:
        raise _conflict('Runtime logical state changed')
    inventory = [tuple(row) for row in conn.execute('SELECT path,sha256,size FROM inventory ORDER BY path')]
    expected = sorted((p, digest(body), len(body)) for p, (body, _) in authored.items())
    authored_inventory = [row for row in inventory if row[0].split('/', 1)[0] in files._AREAS]
    if authored_inventory != expected or digest(inventory) != receipt['inventory_sha256']:
        raise _conflict('Runtime inventory changed')
    if receipt['change_id'] is not None:
        rows = conn.execute('SELECT * FROM changes WHERE change_id=?', (receipt['change_id'],)).fetchall()
        if len(rows) != 1 or rows[0]['sequence'] != receipt['sequence']:
            raise _conflict('Runtime event changed')
        event = event_dict(conn, rows[0])
        counts = {'created': sum(f['action'] == 'create' for f in event['files']),
                  'modified': sum(f['action'] == 'edit' for f in event['files']),
                  'deleted': sum(f['action'] == 'delete' for f in event['files'])}
        if (not event['files'] or event['kind'] != 'external_scan' or event['timestamp'] != inputs['timestamp']
                or event['attribution'] != normalize_attribution(inputs['attribution'], trust='host_installer')
                or event['details'].get('layout_plan_digest') != plan['plan_digest']
                or counts != receipt['counts'] or any(f['commit'] != receipt['candidate_commit'] for f in event['files'])):
            raise _conflict('Runtime event attribution, counts or linkage changed')
    elif receipt['counts'] != {'created': 0, 'modified': 0, 'deleted': 0}:
        raise _conflict('Empty runtime event has nonzero counts')


def _result(plan, git_journal, candidate_digest, receipt, worked):
    return {'phase': 'runtime_published', 'plan_digest': plan['plan_digest'], 'commit': git_journal['commit'],
            'candidate_binding_sha256': candidate_digest, 'state_receipt': receipt,
            'original_binding_active': True, 'runtime_binding_activated': False, 'worked': worked}


def apply(paths, plan, *, attribution):
    store = Store(paths.agentic_runtime)
    with store.lock(exclusive=True, timeout=10):
        _, path, intent, relocation, git_journal, candidate, candidate_digest, authored = _evidence(paths, plan, attribution, binding=True)
        ctx = LayerContext(root=paths.agentic_root, store=store, git_executable=None, runtimes={}, clock=None, attribution_source='host_installer')
        changes = Changes(ctx)
        if os.path.lexists(path):
            journal = _read(path)
            _validate_private(journal, plan, attribution, intent, relocation, git_journal, candidate_digest)
        else:
            inputs = _fixed(plan, attribution, intent, relocation, git_journal, candidate_digest, ctx.now(), new_id('chg'))
            journal = {'schema_version': 1, 'phase': 'prepared', 'inputs': inputs, 'result': None}
            _write(path, journal)
        key = 'layout_upgrade:' + plan['plan_digest']
        with store.transaction() as conn:
            receipt = _receipt(conn, key, journal['inputs'], plan, git_journal)
            if receipt is None:
                if journal['phase'] != 'prepared':
                    raise _conflict('Published private receipt has no database receipt')
                if inspect_runtime(paths.agentic_runtime, paths.agentic_root, plan['binding']['instance_id']) != plan['state']:
                    raise _conflict('Original runtime evidence changed')
                meta = {r['key']: r['value'] for r in conn.execute('SELECT key,value FROM meta')}
                version = meta.get('schema_version')
                if (version not in ('1', '2', '3') or version != plan['state']['schema_version']
                        or meta.get('root_path') != str(paths.agentic_root) or meta.get('instance_id') != candidate['instance_id']
                        or not meta.get('initialized_at') or unfinished_work(conn)):
                    raise _conflict('Runtime identity, schema or unfinished work changed')
                _verify_layout(conn, version)
                if version in ('1', '2'):
                    store._migrate_to_current(conn, str(paths.agentic_root), journal['inputs']['timestamp'])
                scanned_paths = set(authored) | {r['path'] for r in conn.execute('SELECT path FROM inventory')
                                               if r['path'].split('/', 1)[0] in files._AREAS}
                scan = changes._scan_connection(conn, normalize_attribution(attribution, trust='host_installer'),
                        paths=scanned_paths, change_id=journal['inputs']['change_id'], created_at=journal['inputs']['timestamp'],
                        details={'layout_plan_digest': plan['plan_digest']})
                if scan['skipped']:
                    raise _conflict('Unsafe authored content during scan')
                event = scan['change']
                if event:
                    committed = {f['path']: f['after_sha256'] for f in event['files']}
                    if changes._link_commit_connection(conn, git_journal['commit'], committed, change_id=event['change_id']) != len(committed):
                        raise _conflict('Runtime event linkage incomplete')
                inventory = [tuple(row) for row in conn.execute('SELECT path,sha256,size FROM inventory ORDER BY path')]
                expected = sorted((p, digest(body), len(body)) for p, (body, _) in authored.items())
                if [row for row in inventory if row[0].split('/', 1)[0] in files._AREAS] != expected:
                    raise _conflict('Runtime inventory differs from verified authored tree')
                receipt = {'format_version': 1, 'input_sha256': digest(journal['inputs']), 'schema_from': version,
                           'schema_to': '3', 'candidate_commit': git_journal['commit'],
                           'change_id': event['change_id'] if event else None, 'sequence': event['sequence'] if event else None,
                           'counts': {k: scan[k] for k in ('created', 'modified', 'deleted')},
                           'inventory_sha256': digest(inventory), 'logical_sha256': _digest_database(conn, key)}
                receipt['receipt_sha256'] = digest(receipt)
                fresh = _evidence(paths, plan, attribution, binding=True)
                if (fresh[2:7] != (intent, relocation, git_journal, candidate, candidate_digest)
                        or fresh[7] != authored or _read(path) != journal):
                    raise _conflict('Layout evidence changed during runtime transaction')
                conn.execute('INSERT INTO meta(key,value) VALUES (?,?)', (key, canonical(receipt).decode('utf-8')))
            else:
                _verify_database(conn, key, receipt, plan, authored, journal['inputs'])
                if journal['result'] is not None and journal['result'] != receipt:
                    raise _conflict('Private and database receipts differ')
        worked = journal['phase'] != 'runtime_published'
        if worked:
            if _read(path) != journal:
                raise _conflict('Runtime preparation journal changed')
            journal.update(phase='runtime_published', result=receipt)
            _write(path, journal)
        return _result(plan, git_journal, candidate_digest, receipt, worked)


def read_receipt(paths, plan):
    store = Store(paths.agentic_runtime)
    with store.lock(exclusive=False, timeout=10):
        directory = history_directory(paths)
        intents = [item for item in retained_intents(paths) if item['plan'] == plan]
        if len(intents) != 1:
            raise _conflict('Matching intent required')
        attr = intents[0]['attribution']
        _, path, intent, relocation, gj, _, candidate_digest, _ = _evidence(paths, plan, attr, binding=False, current=False, retained=True)
        journal = _read(path)
        return _read_verified_receipt(store, plan, attr, intent, relocation, gj, candidate_digest, journal)


@contextmanager
def _offline_reader(store):
    """Inspect a quiescent runtime without SQLite creating or consuming sidecars."""
    path = Path(store.state_dir)
    if not path.is_absolute() or '..' in path.parts:
        raise _conflict('Unsafe offline runtime path')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    db = None
    conn = None
    parents = []
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    def state(name):
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise _conflict('Unsafe offline runtime database or sidecar')
        return identity(info)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            parents.append((component, info.st_dev, info.st_ino))
        before = {name: state(name) for name in ('state.sqlite3', 'state.sqlite3-wal', 'state.sqlite3-shm')}
        if before['state.sqlite3'] is None or before['state.sqlite3'][3] > 1024 * 1024 * 1024:
            raise _conflict('Missing or oversized offline runtime database')
        if before['state.sqlite3-wal'] is not None and before['state.sqlite3-wal'][3]:
            raise _conflict('Nonempty runtime WAL requires offline checkpoint before preparation')
        db = os.open('state.sqlite3', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if identity(os.fstat(db)) != before['state.sqlite3']:
            raise _conflict('Offline runtime database changed before inspection')
        conn = sqlite3.connect(f'file:/proc/self/fd/{db}?mode=ro&immutable=1', uri=True)
        conn.row_factory = sqlite3.Row
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024 * 1024)
        try:
            yield conn
        finally:
            if identity(os.fstat(db)) != before['state.sqlite3'] or any(state(name) != value for name, value in before.items()):
                raise _conflict('Offline runtime database or sidecar changed during inspection')
            check = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
            try:
                for component, device, inode in parents:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=check)
                    os.close(check)
                    check = child
                    info = os.fstat(check)
                    if (info.st_dev, info.st_ino) != (device, inode):
                        raise _conflict('Offline runtime path changed during inspection')
            finally:
                os.close(check)
    except (OSError, sqlite3.Error) as exc:
        raise _conflict('Cannot safely inspect offline runtime: ' + str(exc)) from exc
    finally:
        if conn is not None:
            conn.close()
        if db is not None:
            os.close(db)
        os.close(directory)


def _read_verified_receipt(store, plan, attr, intent, relocation, gj, candidate_digest, journal, *, offline_read_only=False):
    _validate_private(journal, plan, attr, intent, relocation, gj, candidate_digest)
    with (_offline_reader(store) if offline_read_only else store.reader()) as conn:
        conn.execute('BEGIN')
        meta = {row['key']: row['value'] for row in conn.execute('SELECT key,value FROM meta')}
        if (meta.get('schema_version') != '3' or meta.get('root_path') != plan['binding']['root']
                or meta.get('instance_id') != plan['binding']['instance_id']):
            raise _conflict('Runtime identity changed')
        _verify_layout(conn, '3')
        receipt = _receipt(conn, 'layout_upgrade:' + plan['plan_digest'], journal['inputs'], plan, gj)
        if receipt is None or journal['phase'] != 'runtime_published' or journal['result'] != receipt:
            raise _conflict('Published runtime receipt required')
    return receipt


def verify_published(paths, plan, *, attribution):
    store = Store(paths.agentic_runtime)
    with store.lock(exclusive=False, timeout=10):
        _, path, intent, relocation, gj, _, candidate_digest, authored = _evidence(paths, plan, attribution, binding=False)
        journal = _read(path)
        _validate_private(journal, plan, attribution, intent, relocation, gj, candidate_digest)
        key = 'layout_upgrade:' + plan['plan_digest']
        with store.reader() as conn:
            conn.execute('BEGIN')
            receipt = _receipt(conn, key, journal['inputs'], plan, gj)
            if receipt is None or journal['phase'] != 'runtime_published' or journal['result'] != receipt:
                raise _conflict('Published runtime receipt required')
            _verify_database(conn, key, receipt, plan, authored, journal['inputs'])
        result = _result(plan, gj, candidate_digest, receipt, False)
        result.pop('original_binding_active')
        return result
