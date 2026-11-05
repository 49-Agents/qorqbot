"""Private, offline preparation and reference publication of a layout commit.

Preparation creates unreferenced objects and private evidence; ref publication
uses a guarded native Git transaction and publishes the live index. Activation is deferred.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import struct
import tempfile
import time

from qorqut_agentic.changes import normalize_attribution
from qorqut_agentic.history import GitRunner, _GIT_OPTIONS
from qorqut_agentic.store import Store
from qorqut_operations.contracts import DomainError
from bazarlyk.private_files import read_private_file, require_private_directory
from . import layout_files as files
from .layout_inspection import validate_catalog
from .layout_intent import _conflict, incomplete_intents
from .support import canonical, digest
from .upgrade import _unique, history_directory

_LIMIT = files._LIMIT
_AREAS = files._AREAS
_OID = re.compile(r'[0-9a-f]{40}\Z')
_KEYS = {'schema_version', 'phase', 'plan_digest', 'intent_sha256', 'relocation_sha256',
         'attribution', 'root', 'gitdir', 'head_identity', 'parent', 'original_index_sha256',
         'identity', 'timestamp', 'message', 'tree', 'commit', 'final_index', 'receipt_sha256'}
_IDENTITY = {'name': 'Agentic Layout Migration', 'email': 'agentic-layout@localhost'}


def _authored_path(path):
    return path.split('/', 1)[0] in _AREAS


def _index_entries(raw):
    """Parse supported SHA-1 index forms, refusing features we cannot preserve.

    Index v2/v3 regular stage-zero entries and the disposable TREE cache are
    supported. Extended flags, sparse/split indexes and other extensions refuse.
    Unrelated assume-unchanged bits are retained and checked explicitly.
    """
    try:
        if len(raw) < 32 or len(raw) > files._LIMIT or raw[:4] != b'DIRC':
            raise ValueError('header')
        if hashlib.sha1(raw[:-20]).digest() != raw[-20:]:
            raise ValueError('checksum')
        version, count = struct.unpack_from('>II', raw, 4)
        if version not in (2, 3) or count > 300000:
            raise ValueError('version or count')
        offset, result = 12, {}
        for _ in range(count):
            start = offset
            if start + 62 > len(raw) - 20:
                raise ValueError('truncated entry')
            mode = struct.unpack_from('>I', raw, start + 24)[0]
            oid = raw[start + 40:start + 60].hex()
            flags = struct.unpack_from('>H', raw, start + 60)[0]
            if flags & 0x7000:
                raise ValueError('unmerged or extended entry')
            end = raw.index(b'\0', start + 62, len(raw) - 20)
            name = raw[start + 62:end].decode('utf-8')
            if not name or name in result or (flags & 0xfff) != min(len(raw[start + 62:end]), 0xfff):
                raise ValueError('invalid entry path')
            result[name] = (mode, oid, bool(flags & 0x8000))
            offset = start + ((end + 1 - start + 7) & ~7)
            if offset > len(raw) - 20 or any(raw[end + 1:offset]):
                raise ValueError('invalid padding')
        while offset < len(raw) - 20:
            if offset + 8 > len(raw) - 20:
                raise ValueError('truncated extension')
            sig, length = struct.unpack_from('>4sI', raw, offset)
            offset += 8
            if sig != b'TREE' or offset + length > len(raw) - 20:
                raise ValueError('unsupported index extension')
            offset += length
        if offset != len(raw) - 20:
            raise ValueError('invalid index length')
        return result
    except (ValueError, UnicodeError, struct.error) as exc:
        raise _conflict('Unsupported or invalid Git index feature') from exc


def _git_mode(mode):
    """Match Git's regular-file mode conversion while retaining disk modes."""
    return 0o100755 if mode & 0o100 else 0o100644


def _index_delta(before, final, authored):
    unrelated = {p: v for p, v in before.items() if not _authored_path(p)}
    if unrelated != {p: v for p, v in final.items() if not _authored_path(p)}:
        raise _conflict('Unrelated staged entries or flags changed')
    expected = {p: (_git_mode(mode),
                    hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest(), False)
                for p, (body, mode) in authored.items()}
    if {p: v for p, v in final.items() if _authored_path(p)} != expected:
        raise _conflict('Prepared authored index does not match final files')


def _read_journal(path):
    raw = read_private_file(path, max_bytes=_LIMIT)
    try:
        j = json.loads(raw, object_pairs_hook=_unique)
        if (not isinstance(j, dict) or j.get('phase') not in ('preparing', 'prepared', 'ref_publishing', 'ref_published', 'index_publishing', 'git_published')
                or set(j) != (_KEYS | ({'index_publication'} if j.get('phase') in ('index_publishing', 'git_published') else set()))
                or j['schema_version'] != 1 or raw != canonical(j) + b'\n'
                or j['receipt_sha256'] != digest({k: v for k, v in j.items() if k != 'receipt_sha256'})
                or j['identity'] != _IDENTITY or type(j['timestamp']) is not int or j['timestamp'] < 0
                or not isinstance(j['plan_digest'], str) or not re.fullmatch(r'[0-9a-f]{64}', j['plan_digest'])
                or j['message'] != 'Agentic layout migration\n\nAgentic-layout-plan: ' + j['plan_digest'] + '\n'
                or not isinstance(j['parent'], str) or not _OID.fullmatch(j['parent'])):
            raise ValueError('invalid journal')
        if j['phase'] == 'preparing':
            if any(j[k] is not None for k in ('tree', 'commit', 'final_index')):
                raise ValueError('invalid preparing state')
        else:
            if not all(isinstance(j[k], str) and _OID.fullmatch(j[k]) for k in ('tree', 'commit')):
                raise ValueError('invalid object identity')
            item = j['final_index']
            if not isinstance(item, dict) or set(item) != {'sha256', 'content_base64'}:
                raise ValueError('invalid final index')
            raw_index = base64.b64decode(item['content_base64'], validate=True)
            if digest(raw_index) != item['sha256']:
                raise ValueError('invalid index digest')
            _index_entries(raw_index)
        if j['phase'] in ('index_publishing', 'git_published'):
            publication = j['index_publication']
            if not isinstance(publication, dict) or set(publication) != {'original', 'replacement'}:
                raise ValueError('invalid index publication')
            original, replacement = publication['original'], publication['replacement']
            if (not isinstance(original, dict) or set(original) != {'device', 'inode', 'mode', 'size', 'mtime_ns', 'ctime_ns'}
                    or not isinstance(replacement, dict) or set(replacement) != {'device', 'inode', 'mode', 'size', 'mtime_ns', 'sha256'}
                    or any(type(original[k]) is not int or original[k] < 0 for k in original)
                    or any(type(replacement[k]) is not int or replacement[k] < 0 for k in ('device', 'inode', 'mode', 'size', 'mtime_ns'))
                    or original['mode'] > 0o777 or replacement['mode'] != original['mode']
                    or original['size'] > 96 * 1024 * 1024 or replacement['size'] > 96 * 1024 * 1024
                    or replacement['size'] != len(raw_index) or replacement['sha256'] != digest(raw_index)):
                raise ValueError('invalid index identity')
        return j
    except (ValueError, TypeError, KeyError) as exc:
        raise _conflict('Invalid Git layout preparation journal') from exc


def _runner(root, home):
    from bazarlyk.native_paths import executable
    return GitRunner(executable('git'), root, home)


def _reject_filters(git):
    # T11's inspection invokes status. Refuse local filter drivers before that
    # inspection so it cannot execute a configured clean/process command.
    result = git.run(['config', '--local', '--includes', '--name-only', '--null',
                      '--get-regexp', r'^filter\..*\.(clean|smudge|process|required)$'],
                     check=False, env={'GIT_NO_REPLACE_OBJECTS': '1'}, timeout=30)
    if result.returncode not in (0, 1) or result.stdout:
        raise _conflict('Configured Git filter drivers are unsupported during preparation')


def _run(git, args, *, index=None, data=None, env=None):
    extra = {'GIT_NO_REPLACE_OBJECTS': '1'}
    if index is not None:
        extra['GIT_INDEX_FILE'] = str(index)
    extra.update(env or {})
    result = git.run(['-c', 'core.splitIndex=false', '-c', 'index.sparse=false', *args],
                     input=data, env=extra, timeout=30).stdout
    if len(result) > files._LIMIT:
        raise _conflict('Git output exceeds preparation bound')
    return result


def _replace_entries(git, index, original, authored):
    records = []
    for path in sorted(p for p in original if _authored_path(p)):
        records.append(b'0 ' + b'0' * 40 + b'\t' + path.encode() + b'\0')
    for path, (body, mode) in sorted(authored.items()):
        oid = _run(git, ['hash-object', '-w', '--stdin'], data=body).strip().decode('ascii')
        expected = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
        if oid != expected:
            raise _conflict('Git blob identity mismatch')
        gitmode = format(_git_mode(mode), 'o').encode('ascii')
        records.append(gitmode + b' ' + oid.encode() + b'\t' + path.encode() + b'\0')
    if records:
        _run(git, ['update-index', '-z', '--index-info'], index=index, data=b''.join(records))


def _unrelated_index_view(git, index):
    view = {}
    raw = _run(git, ['ls-files', '--stage', '-v', '-z'], index=index)
    for record in filter(None, raw.split(b'\0')):
        header, path = record.split(b'\t', 1)
        name = path.decode('utf-8')
        if not _authored_path(name):
            view[name] = header
    return view


def _make_candidates(git, directory, intent, journal, authored):
    raw = base64.b64decode(intent['index']['content_base64'], validate=True)
    original = _index_entries(raw)
    with tempfile.TemporaryDirectory(prefix='.git-layout-', dir=directory) as temporary:
        temp = Path(temporary)
        tree_index = temp / 'tree-index'
        live_index = temp / 'live-index'
        _run(git, ['read-tree', journal['parent']], index=tree_index)
        head_entries = _index_entries(tree_index.read_bytes())
        _replace_entries(git, tree_index, head_entries, authored)
        tree = _run(git, ['write-tree'], index=tree_index).strip().decode('ascii')
        live_index.write_bytes(raw)
        old_view = _unrelated_index_view(git, live_index)
        _replace_entries(git, live_index, original, authored)
        if _unrelated_index_view(git, live_index) != old_view:
            raise _conflict('Unrelated Git index view changed')
        final_raw = live_index.read_bytes()
        _index_delta(original, _index_entries(final_raw), authored)
        env = {'GIT_AUTHOR_NAME': _IDENTITY['name'], 'GIT_AUTHOR_EMAIL': _IDENTITY['email'],
               'GIT_COMMITTER_NAME': _IDENTITY['name'], 'GIT_COMMITTER_EMAIL': _IDENTITY['email'],
               'GIT_AUTHOR_DATE': f"@{journal['timestamp']} +0000",
               'GIT_COMMITTER_DATE': f"@{journal['timestamp']} +0000"}
        commit = _run(git, ['commit-tree', tree, '-p', journal['parent']],
                      data=journal['message'].encode(), env=env).strip().decode('ascii')
        if not _OID.fullmatch(tree) or not _OID.fullmatch(commit):
            raise _conflict('Invalid candidate Git object identity')
        return tree, commit, final_raw


def _verify_candidate(git, journal, intent, authored):
    expected = (f"tree {journal['tree']}\nparent {journal['parent']}\n"
                f"author {_IDENTITY['name']} <{_IDENTITY['email']}> {journal['timestamp']} +0000\n"
                f"committer {_IDENTITY['name']} <{_IDENTITY['email']}> {journal['timestamp']} +0000\n\n"
                + journal['message']).encode()
    if _run(git, ['cat-file', 'commit', journal['commit']]) != expected:
        raise _conflict('Candidate commit changed')
    raw = _run(git, ['ls-tree', '-r', '-z', '--full-tree', journal['tree']])
    tree_entries = {}
    for record in filter(None, raw.split(b'\0')):
        header, path = record.split(b'\t', 1)
        mode, kind, oid = header.split(b' ')
        tree_entries[path.decode('utf-8')] = (int(mode, 8), oid.decode(), kind)
    # Compare the complete candidate against HEAD with only authored entries replaced.
    parent_raw = _run(git, ['ls-tree', '-r', '-z', '--full-tree', journal['parent']])
    parent_entries = {}
    for record in filter(None, parent_raw.split(b'\0')):
        header, path = record.split(b'\t', 1)
        mode, kind, oid = header.split(b' ')
        parent_entries[path.decode('utf-8')] = (int(mode, 8), oid.decode(), kind)
    expected_tree = {p: v for p, v in parent_entries.items() if not _authored_path(p)}
    for p, (body, mode) in authored.items():
        expected_tree[p] = (_git_mode(mode),
                            hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest(), b'blob')
    if tree_entries != expected_tree:
        raise _conflict('Candidate tree differs from expected content')
    # Indexes cannot represent empty trees. Refuse if read-tree would silently
    # remove any unrelated tree, including an empty directory object.
    def unrelated_trees(revision):
        trees = {}
        listing = _run(git, ['ls-tree', '-r', '-t', '-z', '--full-tree', revision])
        for record in filter(None, listing.split(b'\0')):
            header, path = record.split(b'\t', 1)
            mode, kind, oid = header.split(b' ')
            name = path.decode('utf-8')
            if kind == b'tree' and not _authored_path(name):
                trees[name] = (mode, oid)
        return trees
    if unrelated_trees(journal['tree']) != unrelated_trees(journal['parent']):
        raise _conflict('Candidate would change unrelated tree objects')
    original = _index_entries(base64.b64decode(intent['index']['content_base64'], validate=True))
    final = _index_entries(base64.b64decode(journal['final_index']['content_base64'], validate=True))
    _index_delta(original, final, authored)


def prepare(paths, plan, *, attribution):
    """Prepare one immutable candidate without publishing Git or runtime state."""
    normalize_attribution(attribution, trust='host_installer')
    if (not isinstance(plan, dict) or not isinstance(plan.get('plan_digest'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', plan['plan_digest'])
            or digest({k: v for k, v in plan.items() if k != 'plan_digest'}) != plan['plan_digest']):
        raise DomainError('invalid_plan', 'A complete prepared layout plan is required')
    files._safe_ancestors(paths.agentic_runtime)
    files._safe_ancestors(paths.agentic_root)
    with Store(paths.agentic_runtime).lock(exclusive=True, timeout=10):
        intents = incomplete_intents(paths)
        if len(intents) != 1 or intents[0]['plan'] != plan or intents[0]['attribution'] != attribution:
            raise _conflict('Matching prepared intent and attribution required')
        intent = intents[0]
        directory = require_private_directory(history_directory(paths))
        relocation_path = directory / ('relocation-' + plan['plan_digest'] + '.json')
        if any(item.name.startswith('relocation-') and item != relocation_path for item in directory.iterdir()):
            raise _conflict('Another or unknown relocation journal exists')
        relocation = files._journal_read(relocation_path)
        originals, targets, effects = files._derive(plan, intent, relocation['initial_dirs'])
        if (relocation['phase'] != 'files_published' or relocation['plan_digest'] != plan['plan_digest']
                or relocation['intent_sha256'] != intent['receipt_sha256'] or relocation['attribution'] != attribution
                or relocation['effects'] != effects
                or set(relocation['created_dirs']) != {e['path'] for e in effects if e['kind'] == 'mkdir'}):
            raise _conflict('Matching completed relocation required')
        rootfd = files._open_root(paths.agentic_root)
        try:
            git = _runner(paths.agentic_root, directory)
            def facts():
                _reject_filters(git)
                if incomplete_intents(paths) != [intent] or files._journal_read(relocation_path) != relocation:
                    raise _conflict('Prepared intent or relocation receipt changed')
                files._facts(paths, plan, relocation, rootfd)
                actual = files._verify_tree(paths, rootfd, relocation, originals, targets)
                if validate_catalog({p: b for p, (b, _) in actual.items()}):
                    raise _conflict('Published catalogue invalid')
                return actual
            authored = facts()
            _index_entries(base64.b64decode(intent['index']['content_base64'], validate=True))
            path = directory / ('git-layout-' + plan['plan_digest'] + '.json')
            for item in directory.iterdir():
                if item.name.startswith('git-layout-') and item != path:
                    raise _conflict('Another or unknown Git layout journal exists')
            fixed = {'schema_version': 1, 'plan_digest': plan['plan_digest'],
                     'intent_sha256': intent['receipt_sha256'], 'relocation_sha256': relocation['receipt_sha256'],
                     'attribution': attribution, 'root': relocation['root'], 'gitdir': relocation['gitdir'],
                     'head_identity': relocation['head_identity'], 'parent': intent['head'],
                     'original_index_sha256': intent['index']['sha256'], 'identity': _IDENTITY,
                     'message': 'Agentic layout migration\n\nAgentic-layout-plan: ' + plan['plan_digest'] + '\n'}
            if os.path.lexists(path):
                journal = _read_journal(path)
                if any(journal[k] != v for k, v in fixed.items()):
                    raise _conflict('Git preparation journal does not match intent')
            else:
                journal = {**fixed, 'phase': 'preparing', 'timestamp': int(time.time()),
                           'tree': None, 'commit': None, 'final_index': None}
                files._journal_write(path, journal)
            if journal['phase'] not in ('preparing', 'prepared'):
                raise _conflict('Git reference publication already started')
            worked = journal['phase'] != 'prepared'
            if worked:
                preparing = dict(journal)
                tree, commit, raw_index = _make_candidates(git, directory, intent, journal, authored)
                journal.update(phase='prepared', tree=tree, commit=commit,
                               final_index={'sha256': digest(raw_index), 'content_base64': base64.b64encode(raw_index).decode('ascii')})
                _verify_candidate(git, journal, intent, authored)
                facts()
                if _read_journal(path) != preparing:
                    raise _conflict('Git preparation journal changed')
                files._journal_write(path, journal)
                if _read_journal(path) != journal:
                    raise _conflict('Git preparation receipt changed')
            else:
                _verify_candidate(git, journal, intent, authored)
                facts()
            return {'phase': 'prepared', 'plan_digest': plan['plan_digest'], 'parent': journal['parent'],
                    'commit': journal['commit'], 'tree': journal['tree'],
                    'index_sha256': journal['final_index']['sha256'], 'worked': worked}
        finally:
            os.close(rootfd)


def _destination(git, journal):
    try:
        raw = base64.b64decode(journal['head_identity'], validate=True)
        if raw == (journal['parent'] + '\n').encode('ascii'):
            return 'HEAD'
        if not raw.startswith(b'ref: refs/heads/') or not raw.endswith(b'\n') or raw.count(b'\n') != 1:
            raise ValueError('unsupported HEAD')
        ref = os.fsdecode(raw[5:-1])
        checked = git.run(['check-ref-format', ref], check=False, env={'GIT_NO_REPLACE_OBJECTS': '1'}, timeout=30)
        if checked.returncode != 0 or not ref.startswith('refs/heads/'):
            raise ValueError('invalid branch')
        return ref
    except (ValueError, UnicodeError) as exc:
        raise _conflict('Unsupported recorded Git HEAD destination') from exc


def _head_state(git, rootfd, journal, destination):
    gitdir, encoded = files._head_identity(rootfd)
    if gitdir != journal['gitdir']:
        raise _conflict('Git directory identity changed')
    raw = base64.b64decode(encoded, validate=True)
    if destination == 'HEAD':
        if raw not in ((journal['parent'] + '\n').encode(), (journal['commit'] + '\n').encode()):
            raise _conflict('Detached HEAD changed')
    else:
        if encoded != journal['head_identity']:
            raise _conflict('Git HEAD destination changed')
        resolved = git.run(['symbolic-ref', '--no-recurse', 'HEAD'], check=False,
                           env={'GIT_NO_REPLACE_OBJECTS': '1'}, timeout=30)
        nested = git.run(['symbolic-ref', '-q', '--no-recurse', destination], check=False,
                         env={'GIT_NO_REPLACE_OBJECTS': '1'}, timeout=30)
        if resolved.returncode or resolved.stdout != os.fsencode(destination) + b'\n' or nested.returncode != 1:
            raise _conflict('Git branch destination is not direct')
    oid = _run(git, ['rev-parse', '--verify', '--end-of-options', destination]).strip().decode('ascii')
    head = _run(git, ['rev-parse', '--verify', 'HEAD']).strip().decode('ascii')
    if oid != head or oid not in (journal['parent'], journal['commit']):
        raise _conflict('Git reference changed')
    if destination == 'HEAD' and raw != (oid + '\n').encode():
        raise _conflict('Detached HEAD identity changed')
    return 'original' if oid == journal['parent'] else 'candidate'


def _check_operation_locks(root, destination, *, transaction=False, owned_index=None):
    """Reject Git operations except this transaction's known native ref locks."""
    gitdir = Path(root) / '.git'
    allowed = {'HEAD.lock'}
    if destination != 'HEAD':
        allowed.add(destination + '.lock')
    seen = set()
    for name in ('index.lock', 'HEAD.lock', 'packed-refs.lock', 'config.lock', 'shallow.lock',
                 'ORIG_HEAD.lock', 'FETCH_HEAD.lock', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                 'REBASE_HEAD', 'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_LOG'):
        if os.path.lexists(gitdir / name):
            if name == 'index.lock' and owned_index is not None:
                info = os.lstat(gitdir / name)
                if stat.S_ISREG(info.st_mode) and _stable_index_identity(info) == owned_index:
                    continue
            if not transaction or name not in allowed or not stat.S_ISREG(os.lstat(gitdir / name).st_mode):
                raise _conflict('Unfinished Git operation: .git/' + name)
            seen.add(name)
    refs = gitdir / 'refs'
    if refs.is_symlink():
        raise _conflict('Unsafe Git refs directory')
    def inaccessible(_error):
        raise _conflict('Cannot inspect Git refs')
    for directory, subdirs, names in os.walk(refs, followlinks=False, onerror=inaccessible):
        if any((Path(directory) / name).is_symlink() for name in subdirs):
            raise _conflict('Unsafe Git refs directory')
        for name in names:
            path = Path(directory) / name
            if not stat.S_ISREG(os.lstat(path).st_mode):
                raise _conflict('Unsafe Git ref')
            if name.endswith('.lock'):
                relative = str(path.relative_to(gitdir))
                if not transaction or relative not in allowed or not stat.S_ISREG(os.lstat(path).st_mode):
                    raise _conflict('Unfinished Git operation: .git/' + relative)
                seen.add(relative)
    if transaction and seen != allowed:
        raise _conflict('Expected native Git transaction locks are missing')


def _transaction(git, parent, candidate, guard, *, timeout=30):
    """Bounded native update-ref transaction. Never remove Git's lock files."""
    env = git.env({'GIT_NO_REPLACE_OBJECTS': '1'})
    command = [git.executable, '-C', git.root, *_GIT_OPTIONS, '-c', 'core.splitIndex=false',
               '-c', 'index.sparse=false', 'update-ref', '--stdin']
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, bufsize=0)
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    output = bytearray()
    errors = bytearray()
    commit_sent = False
    try:
        for stream in (process.stdout, process.stderr, process.stdin):
            os.set_blocking(stream.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ, output)
        selector.register(process.stderr, selectors.EVENT_READ, errors)

        def pump():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _conflict('Git reference transaction timed out')
            events = selector.select(remaining)
            if not events:
                raise _conflict('Git reference transaction timed out')
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    key.data.extend(chunk)
                    if len(key.data) > 65536:
                        raise _conflict('Git reference transaction output exceeds bound')

        def send(data):
            view = memoryview(data)
            while view:
                if time.monotonic() >= deadline:
                    raise _conflict('Git reference transaction timed out')
                try:
                    count = os.write(process.stdin.fileno(), view)
                    view = view[count:]
                except BlockingIOError:
                    # Drain both output pipes while waiting for stdin to become writable.
                    selector.register(process.stdin, selectors.EVENT_WRITE, None)
                    try:
                        remaining = max(0, deadline - time.monotonic())
                        events = selector.select(remaining)
                        if not events:
                            raise _conflict('Git reference transaction timed out')
                        for key, _ in events:
                            if key.fileobj is not process.stdin:
                                chunk = os.read(key.fd, 65536)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                else:
                                    key.data.extend(chunk)
                                    if len(key.data) > 65536:
                                        raise _conflict('Git reference transaction output exceeds bound')
                    finally:
                        selector.unregister(process.stdin)

        def ack(expected):
            while b'\n' not in output:
                if process.poll() is not None and not selector.get_map():
                    raise _conflict('Git reference transaction ended before acknowledgement')
                pump()
            line, _, rest = output.partition(b'\n')
            output[:] = rest
            if line != expected:
                raise _conflict('Unexpected Git reference transaction acknowledgement')

        send(b'start\nupdate HEAD ' + candidate.encode() + b' ' + parent.encode() + b'\nprepare\n')
        ack(b'start: ok')
        ack(b'prepare: ok')
        guard()
        if time.monotonic() >= deadline:
            raise _conflict('Git reference transaction timed out before commit')
        send(b'commit\n')
        commit_sent = True
        ack(b'commit: ok')
        process.stdin.close()
        while selector.get_map():
            pump()
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0 or output or errors:
            raise _conflict('Git reference transaction did not finish cleanly')
    except BaseException:
        if not commit_sent and process.poll() is None:
            try:
                os.write(process.stdin.fileno(), b'abort\n')
            except (BrokenPipeError, OSError):
                pass
        raise
    finally:
        selector.close()
        if process.poll() is None:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


def publish_ref(paths, plan, *, attribution):
    """Publish a prepared candidate ref; leave the live index and runtime intact."""
    normalize_attribution(attribution, trust='host_installer')
    if (not isinstance(plan, dict) or not isinstance(plan.get('plan_digest'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', plan['plan_digest'])
            or digest({k: v for k, v in plan.items() if k != 'plan_digest'}) != plan['plan_digest']):
        raise DomainError('invalid_plan', 'A complete prepared layout plan is required')
    files._safe_ancestors(paths.agentic_runtime)
    files._safe_ancestors(paths.agentic_root)
    with Store(paths.agentic_runtime).lock(exclusive=True, timeout=10):
        intents = incomplete_intents(paths)
        if len(intents) != 1 or intents[0]['plan'] != plan or intents[0]['attribution'] != attribution:
            raise _conflict('Matching prepared intent and attribution required')
        intent = intents[0]
        directory = require_private_directory(history_directory(paths))
        relocation_path = directory / ('relocation-' + plan['plan_digest'] + '.json')
        path = directory / ('git-layout-' + plan['plan_digest'] + '.json')
        for item in directory.iterdir():
            if (item.name.startswith('relocation-') and item != relocation_path
                    or item.name.startswith('git-layout-') and item != path):
                raise _conflict('Another or unknown layout journal exists')
        relocation = files._journal_read(relocation_path)
        originals, targets, effects = files._derive(plan, intent, relocation['initial_dirs'])
        if (relocation['phase'] != 'files_published' or relocation['plan_digest'] != plan['plan_digest']
                or relocation['intent_sha256'] != intent['receipt_sha256'] or relocation['attribution'] != attribution
                or relocation['effects'] != effects
                or set(relocation['created_dirs']) != {e['path'] for e in effects if e['kind'] == 'mkdir'}):
            raise _conflict('Matching completed relocation required')
        journal = _read_journal(path)
        fixed = {'schema_version': 1, 'plan_digest': plan['plan_digest'],
                 'intent_sha256': intent['receipt_sha256'], 'relocation_sha256': relocation['receipt_sha256'],
                 'attribution': attribution, 'root': relocation['root'], 'gitdir': relocation['gitdir'],
                 'head_identity': relocation['head_identity'], 'parent': intent['head'],
                 'original_index_sha256': intent['index']['sha256'], 'identity': _IDENTITY,
                 'message': 'Agentic layout migration\n\nAgentic-layout-plan: ' + plan['plan_digest'] + '\n'}
        if journal['phase'] not in ('prepared', 'ref_publishing', 'ref_published') or any(
                journal[k] != v for k, v in fixed.items()):
            raise _conflict('Matching prepared Git journal required')
        git = _runner(paths.agentic_root, directory)
        destination = _destination(git, journal)
        rootfd = files._open_root(paths.agentic_root)
        try:
            def facts(*, transaction=False):
                _reject_filters(git)
                _check_operation_locks(paths.agentic_root, destination, transaction=transaction)
                if (incomplete_intents(paths) != [intent] or files._journal_read(relocation_path) != relocation
                        or _read_journal(path) != journal):
                    raise _conflict('Private layout receipts changed')
                files._environment_facts(paths, plan, relocation, rootfd)
                if files._read_index(paths.agentic_root, intent['index']['sha256']) != base64.b64decode(
                        intent['index']['content_base64'], validate=True):
                    raise _conflict('Original Git index changed')
                state = _head_state(git, rootfd, journal, destination)
                actual = files._verify_tree(paths, rootfd, relocation, originals, targets)
                if validate_catalog({p: b for p, (b, _) in actual.items()}):
                    raise _conflict('Published catalogue invalid')
                return state, actual
            state, authored = facts()
            _verify_candidate(git, journal, intent, authored)
            if (journal['phase'] == 'prepared' and state != 'original'
                    or journal['phase'] == 'ref_published' and state != 'candidate'):
                raise _conflict('Git reference does not match publication phase')
            worked = journal['phase'] != 'ref_published'
            if journal['phase'] == 'prepared':
                journal['phase'] = 'ref_publishing'
                files._journal_write(path, journal)
                if _read_journal(path) != journal:
                    raise _conflict('Git publication receipt changed')
            if journal['phase'] == 'ref_publishing':
                if state == 'original':
                    def guard():
                        guarded, _ = facts(transaction=True)
                        if guarded != 'original':
                            raise _conflict('Git reference changed before commit')
                    _transaction(git, journal['parent'], journal['commit'], guard)
                post, authored = facts()
                if post != 'candidate':
                    raise _conflict('Candidate reference was not published')
                _verify_candidate(git, journal, intent, authored)
                journal['phase'] = 'ref_published'
                files._journal_write(path, journal)
                if _read_journal(path) != journal:
                    raise _conflict('Git publication receipt changed')
                final_state, _ = facts()
                if final_state != 'candidate':
                    raise _conflict('Published Git reference changed')
            else:
                final_state, _ = facts()
                if final_state != 'candidate':
                    raise _conflict('Published Git reference changed')
            return {'phase': 'ref_published', 'plan_digest': plan['plan_digest'], 'destination': destination,
                    'parent': journal['parent'], 'commit': journal['commit'], 'tree': journal['tree'],
                    'original_index_sha256': journal['original_index_sha256'],
                    'final_index_sha256': journal['final_index']['sha256'], 'active_index': 'original',
                    'runtime_binding_activated': False, 'worked': worked}
        finally:
            os.close(rootfd)


def _stable_index_identity(info):
    return {'device': info.st_dev, 'inode': info.st_ino, 'mode': stat.S_IMODE(info.st_mode),
            'size': info.st_size, 'mtime_ns': info.st_mtime_ns}


def _original_index_identity(info):
    return {**_stable_index_identity(info), 'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns}


def _read_index_at(gitfd, name):
    """Read a bounded regular named index using a retained no-follow handle."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=gitfd)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) & ~0o777
                or before.st_size > 96 * 1024 * 1024):
            raise _conflict('Unsafe or oversized Git index file')
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > 96 * 1024 * 1024:
                raise _conflict('Git index exceeds capture bound')
            chunks.append(chunk)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=gitfd, follow_symlinks=False)
        if (_original_index_identity(before) != _original_index_identity(after)
                or _original_index_identity(after) != _original_index_identity(named)
                or not stat.S_ISREG(named.st_mode) or size != before.st_size):
            raise _conflict('Git index changed during inspection')
        return fd, _original_index_identity(after), b''.join(chunks)
    except BaseException:
        os.close(fd)
        raise


def _verify_open_index(gitfd, name, heldfd, expected, identity, *, original=False):
    fd, seen, data = _read_index_at(gitfd, name)
    try:
        held = os.fstat(heldfd)
        current = _original_index_identity(held) if original else _stable_index_identity(held)
        named = seen if original else {k: seen[k] for k in ('device', 'inode', 'mode', 'size', 'mtime_ns')}
        if current != identity or named != identity or data != expected:
            raise _conflict('Git index identity or contents changed: ' + name)
    finally:
        os.close(fd)


def _index_state(gitfd, journal, original, final):
    indexfd, identity, raw = _read_index_at(gitfd, 'index')
    lockfd = None
    try:
        try:
            lockfd, lock_identity, lock_raw = _read_index_at(gitfd, 'index.lock')
        except FileNotFoundError:
            lock_identity, lock_raw = None, None
        if journal['phase'] == 'ref_published':
            if raw != original or lockfd is not None:
                raise _conflict('Original index or index lock changed')
            return 'original', identity, None
        evidence = journal['index_publication']
        owned = {k: evidence['replacement'][k] for k in ('device', 'inode', 'mode', 'size', 'mtime_ns')}
        index_stable = {k: identity[k] for k in ('device', 'inode', 'mode', 'size', 'mtime_ns')}
        if identity == evidence['original'] and raw == original:
            if lockfd is None:
                return 'original', identity, None
            if ({k: lock_identity[k] for k in owned} != owned or lock_raw != final
                    or digest(lock_raw) != evidence['replacement']['sha256']):
                raise _conflict('Index lock is not the recorded owned candidate')
            retained = lockfd
            lockfd = None
            return 'locked', identity, retained
        if (index_stable == owned and raw == final and digest(raw) == evidence['replacement']['sha256']
                and lockfd is None):
            return 'final', identity, None
        raise _conflict('Unexpected index or index-lock state')
    finally:
        os.close(indexfd)
        if lockfd is not None:
            os.close(lockfd)


def _link_index(gitfd, pending):
    from bazarlyk import posix_files
    posix_files.publish(pending, gitfd, 'index.lock')


def _replace_index(gitfd):
    os.replace('index.lock', 'index', src_dir_fd=gitfd, dst_dir_fd=gitfd)


def _index_effect(gitfd, path, journal, original, final, check):
    """Finish one recorded index publication; ``check`` verifies outer facts."""
    state, original_identity, lockfd = _index_state(gitfd, journal, original, final)
    try:
        if journal['phase'] == 'git_published':
            if state != 'final':
                raise _conflict('Completed Git index publication changed')
            check(None, expected='final')
            return False
        if journal['phase'] == 'ref_published' and state != 'original':
            raise _conflict('Original Git index required')
        if state == 'final':
            check(None, expected='final')
            os.fsync(gitfd)
        else:
            if state == 'original':
                check(None, expected='original')
                from bazarlyk import posix_files
                pending = posix_files.anonymous_file(gitfd, read=True)
                fd = pending.fd
                try:
                    view = memoryview(final)
                    while view:
                        count = os.write(fd, view)
                        if count <= 0:
                            raise OSError('short anonymous index write')
                        view = view[count:]
                    os.fchmod(fd, original_identity['mode'])
                    os.fsync(fd)
                    replacement = {**_stable_index_identity(os.fstat(fd)), 'sha256': digest(final)}
                    if journal['phase'] == 'ref_published':
                        journal['index_publication'] = {'original': original_identity, 'replacement': replacement}
                    else:
                        journal['index_publication']['replacement'] = replacement
                    journal['phase'] = 'index_publishing'
                    files._journal_write(path, journal)
                    check(None, expected='original')
                    _link_index(gitfd, pending)
                    os.fsync(gitfd)
                    lockfd = fd
                    fd = None
                finally:
                    if fd is not None:
                        posix_files.discard(pending)
            os.fsync(gitfd)
            check(lockfd, expected='locked')
            oldfd, oldidentity, oldraw = _read_index_at(gitfd, 'index')
            try:
                expected_original = journal['index_publication']['original']
                expected_owned = {k: journal['index_publication']['replacement'][k]
                                  for k in ('device', 'inode', 'mode', 'size', 'mtime_ns')}
                if oldidentity != expected_original or oldraw != original:
                    raise _conflict('Original index changed before replacement')
                _verify_open_index(gitfd, 'index.lock', lockfd, final, expected_owned)
                _verify_open_index(gitfd, 'index', oldfd, original, expected_original, original=True)
                _verify_open_index(gitfd, 'index.lock', lockfd, final, expected_owned)
                _replace_index(gitfd)
            finally:
                os.close(oldfd)
            os.fsync(gitfd)
            check(None, expected='final')
        journal['phase'] = 'git_published'
        files._journal_write(path, journal)
        check(None, expected='final')
        return True
    finally:
        if lockfd is not None:
            os.close(lockfd)


def publish_index(paths, plan, *, attribution):
    """Atomically install the recorded final index after ref publication."""
    normalize_attribution(attribution, trust='host_installer')
    if (not isinstance(plan, dict) or not isinstance(plan.get('plan_digest'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', plan['plan_digest'])
            or digest({k: v for k, v in plan.items() if k != 'plan_digest'}) != plan['plan_digest']):
        raise DomainError('invalid_plan', 'A complete prepared layout plan is required')
    files._safe_ancestors(paths.agentic_runtime)
    files._safe_ancestors(paths.agentic_root)
    with Store(paths.agentic_runtime).lock(exclusive=True, timeout=10):
        intents = incomplete_intents(paths)
        if len(intents) != 1 or intents[0]['plan'] != plan or intents[0]['attribution'] != attribution:
            raise _conflict('Matching prepared intent and attribution required')
        intent = intents[0]
        directory = require_private_directory(history_directory(paths))
        relocation_path = directory / ('relocation-' + plan['plan_digest'] + '.json')
        path = directory / ('git-layout-' + plan['plan_digest'] + '.json')
        for item in directory.iterdir():
            if (item.name.startswith('relocation-') and item != relocation_path
                    or item.name.startswith('git-layout-') and item != path):
                raise _conflict('Another or unknown layout journal exists')
        relocation = files._journal_read(relocation_path)
        originals, targets, effects = files._derive(plan, intent, relocation['initial_dirs'])
        if (relocation['phase'] != 'files_published' or relocation['plan_digest'] != plan['plan_digest']
                or relocation['intent_sha256'] != intent['receipt_sha256'] or relocation['attribution'] != attribution
                or relocation['effects'] != effects
                or set(relocation['created_dirs']) != {e['path'] for e in effects if e['kind'] == 'mkdir'}):
            raise _conflict('Matching completed relocation required')
        journal = _read_journal(path)
        fixed = {'schema_version': 1, 'plan_digest': plan['plan_digest'],
                 'intent_sha256': intent['receipt_sha256'], 'relocation_sha256': relocation['receipt_sha256'],
                 'attribution': attribution, 'root': relocation['root'], 'gitdir': relocation['gitdir'],
                 'head_identity': relocation['head_identity'], 'parent': intent['head'],
                 'original_index_sha256': intent['index']['sha256'], 'identity': _IDENTITY,
                 'message': 'Agentic layout migration\n\nAgentic-layout-plan: ' + plan['plan_digest'] + '\n'}
        if journal['phase'] not in ('ref_published', 'index_publishing', 'git_published') or any(
                journal[k] != v for k, v in fixed.items()):
            raise _conflict('Matching published Git reference required')
        original = base64.b64decode(intent['index']['content_base64'], validate=True)
        final = base64.b64decode(journal['final_index']['content_base64'], validate=True)
        if journal['phase'] in ('index_publishing', 'git_published') and journal['index_publication']['original']['size'] != len(original):
            raise _conflict('Recorded original index size changed')
        git = _runner(paths.agentic_root, directory)
        destination = _destination(git, journal)
        rootfd = files._open_root(paths.agentic_root)
        try:
            gitfd = os.open('.git', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=rootfd)
            try:
                def check(held_lock, *, expected):
                    _reject_filters(git)
                    state, _, inspected_lock = _index_state(gitfd, journal, original, final)
                    try:
                        if state != expected or (held_lock is None) != (inspected_lock is None):
                            raise _conflict('Git index publication state changed')
                        owned = None
                        if held_lock is not None:
                            owned = {k: journal['index_publication']['replacement'][k]
                                     for k in ('device', 'inode', 'mode', 'size', 'mtime_ns')}
                            _verify_open_index(gitfd, 'index.lock', held_lock, final, owned)
                        _check_operation_locks(paths.agentic_root, destination, owned_index=owned)
                        if (incomplete_intents(paths) != [intent] or files._journal_read(relocation_path) != relocation
                                or _read_journal(path) != journal):
                            raise _conflict('Private layout receipts changed')
                        if files._identity(os.fstat(gitfd)) != relocation['gitdir']:
                            raise _conflict('Git directory identity changed')
                        files._environment_facts(paths, plan, relocation, rootfd)
                        if _head_state(git, rootfd, journal, destination) != 'candidate':
                            raise _conflict('Published Git reference changed')
                        actual = files._verify_tree(paths, rootfd, relocation, originals, targets)
                        if validate_catalog({p: b for p, (b, _) in actual.items()}):
                            raise _conflict('Published catalogue invalid')
                        return actual
                    finally:
                        if inspected_lock is not None:
                            os.close(inspected_lock)
                initial, _, initial_lock = _index_state(gitfd, journal, original, final)
                try:
                    if journal['phase'] == 'ref_published' and initial != 'original':
                        raise _conflict('Original Git index required')
                    if journal['phase'] == 'git_published' and initial != 'final':
                        raise _conflict('Completed Git index publication changed')
                    authored = check(initial_lock, expected=initial)
                    _verify_candidate(git, journal, intent, authored)
                finally:
                    if initial_lock is not None:
                        os.close(initial_lock)
                worked = _index_effect(gitfd, path, journal, original, final, check)
                return {'phase': 'git_published', 'plan_digest': plan['plan_digest'], 'destination': destination,
                        'parent': journal['parent'], 'commit': journal['commit'], 'tree': journal['tree'],
                        'original_index_sha256': journal['original_index_sha256'],
                        'final_index_sha256': journal['final_index']['sha256'],
                        'index_publication': journal['index_publication'], 'active_index': 'final',
                        'runtime_binding_activated': False, 'worked': worked}
            finally:
                os.close(gitfd)
        finally:
            os.close(rootfd)


def publish(paths, plan, *, attribution):
    """Complete the internal Git stage following a completed relocation."""
    normalize_attribution(attribution, trust='host_installer')
    if (not isinstance(plan, dict) or not isinstance(plan.get('plan_digest'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', plan['plan_digest'])
            or digest({k: v for k, v in plan.items() if k != 'plan_digest'}) != plan['plan_digest']):
        raise DomainError('invalid_plan', 'A complete prepared layout plan is required')
    files._safe_ancestors(paths.agentic_runtime)
    files._safe_ancestors(paths.agentic_root)
    with Store(paths.agentic_runtime).lock(exclusive=True, timeout=10):
        intents = incomplete_intents(paths)
        if len(intents) != 1 or intents[0]['plan'] != plan or intents[0]['attribution'] != attribution:
            raise _conflict('Matching prepared intent and attribution required')
        intent = intents[0]
        directory = require_private_directory(history_directory(paths))
        relocation_path = directory / ('relocation-' + plan['plan_digest'] + '.json')
        path = directory / ('git-layout-' + plan['plan_digest'] + '.json')
        for item in directory.iterdir():
            if (item.name.startswith('relocation-') and item != relocation_path
                    or item.name.startswith('git-layout-') and item != path):
                raise _conflict('Another or unknown layout journal exists')
        relocation = files._journal_read(relocation_path)
        _, _, effects = files._derive(plan, intent, relocation['initial_dirs'])
        if (relocation['phase'] != 'files_published' or relocation['plan_digest'] != plan['plan_digest']
                or relocation['intent_sha256'] != intent['receipt_sha256'] or relocation['attribution'] != attribution
                or relocation['effects'] != effects
                or set(relocation['created_dirs']) != {e['path'] for e in effects if e['kind'] == 'mkdir'}):
            raise _conflict('Matching completed relocation required')
        phase = None
        if os.path.lexists(path):
            journal = _read_journal(path)
            if (journal['plan_digest'] != plan['plan_digest'] or journal['intent_sha256'] != intent['receipt_sha256']
                    or journal['relocation_sha256'] != relocation['receipt_sha256'] or journal['attribution'] != attribution):
                raise _conflict('Matching Git preparation journal required')
            phase = journal['phase']
    worked = False
    if phase in (None, 'preparing'):
        prepared = prepare(paths, plan, attribution=attribution)
        worked = prepared['worked'] or worked
        phase = 'prepared'
    if phase in ('prepared', 'ref_publishing'):
        published = publish_ref(paths, plan, attribution=attribution)
        worked = published['worked'] or worked
        phase = 'ref_published'
    if phase not in ('ref_published', 'index_publishing', 'git_published'):
        raise _conflict('Unsupported Git publication phase')
    result = publish_index(paths, plan, attribution=attribution)
    return {**result, 'worked': worked or result['worked']}
