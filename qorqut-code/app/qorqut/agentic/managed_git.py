"""Deterministic reviewed-path Git projection and publication."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import tempfile

from . import layout_git, managed_files
from .layout_intent import _conflict
from .support import digest


def _entries(raw):
    result = {}
    for record in filter(None, raw.split(b'\0')):
        header, path = record.split(b'\t', 1)
        mode, kind, oid = header.split(b' ')
        name = path.decode('utf-8')
        object_id = oid.decode('ascii')
        if name in result or kind not in (b'blob', b'commit') or not re.fullmatch(r'[0-9a-f]{40}', object_id):
            raise _conflict('Invalid or duplicate managed Git tree entry')
        result[name] = (int(mode, 8), object_id)
    return result


def _project(intent):
    original = base64.b64decode(intent['index']['content_base64'], validate=True)
    entries = layout_git._index_entries(original)
    expected = dict(entries)
    files = managed_files.target(intent)
    for change in intent['plan']['changes']:
        path = change['path']
        if change['op'] == 'delete':
            expected.pop(path, None)
        else:
            body, mode = files[path]
            oid = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
            expected[path] = (layout_git._git_mode(mode), oid, False)
    return original, entries, expected


def prepare(paths, intent, directory):
    runner = layout_git._runner(paths.agentic_root, directory)
    layout_git._reject_filters(runner)
    original, entries, expected = _project(intent)
    with tempfile.TemporaryDirectory(prefix='.managed-index-', dir=directory) as temp:
        index = Path(temp) / 'index'
        fd = os.open(index, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(original)
        records = []
        files = managed_files.target(intent)
        for change in intent['plan']['changes']:
            path = change['path']
            if change['op'] == 'delete':
                records.append(b'0 ' + b'0' * 40 + b'\t' + path.encode() + b'\0')
            else:
                body, mode = files[path]
                oid = layout_git._run(runner, ['hash-object', '-w', '--stdin'], data=body).strip().decode('ascii')
                if oid != expected[path][1]:
                    raise _conflict('Managed Git blob identity differs')
                records.append(format(layout_git._git_mode(mode), 'o').encode() + b' ' + oid.encode() + b'\t' + path.encode() + b'\0')
        layout_git._run(runner, ['update-index', '-z', '--index-info'], index=index, data=b''.join(records))
        raw = index.read_bytes()
        if layout_git._index_entries(raw) != expected:
            raise _conflict('Managed Git index contains unreviewed changes')
        tree = layout_git._run(runner, ['write-tree'], index=index).strip().decode('ascii')
        fixed = intent['fixed']
        env = {'GIT_AUTHOR_NAME': fixed['identity']['name'], 'GIT_AUTHOR_EMAIL': fixed['identity']['email'],
               'GIT_COMMITTER_NAME': fixed['identity']['name'], 'GIT_COMMITTER_EMAIL': fixed['identity']['email'],
               'GIT_AUTHOR_DATE': f"@{fixed['timestamp']} +0000", 'GIT_COMMITTER_DATE': f"@{fixed['timestamp']} +0000"}
        commit = layout_git._run(runner, ['commit-tree', tree, '-p', intent['head']], data=fixed['message'].encode(), env=env).strip().decode('ascii')
    result = {'tree': tree, 'commit': commit, 'final_index': {'sha256': digest(raw),
              'content_base64': base64.b64encode(raw).decode('ascii')}}
    verify(paths, intent, result, directory)
    return result


def verify(paths, intent, result, directory):
    if not isinstance(result, dict) or set(result) != {'tree', 'commit', 'final_index'}:
        raise _conflict('Malformed managed Git proof')
    if any(not isinstance(result[k], str) or not re.fullmatch(r'[0-9a-f]{40}', result[k]) for k in ('tree', 'commit')):
        raise _conflict('Invalid managed Git object id')
    runner = layout_git._runner(paths.agentic_root, directory)
    original, initial, expected = _project(intent)
    item = result['final_index']
    if not isinstance(item, dict) or set(item) != {'sha256', 'content_base64'}:
        raise _conflict('Malformed managed final index')
    raw = base64.b64decode(item['content_base64'], validate=True)
    if digest(raw) != item['sha256'] or layout_git._index_entries(raw) != expected:
        raise _conflict('Managed final index differs')
    parent = _entries(layout_git._run(runner, ['ls-tree', '-r', '-z', '--full-tree', intent['head']]))
    if parent != {p: (v[0], v[1]) for p, v in initial.items()}:
        raise _conflict('Managed original index differs from parent commit')
    tree = _entries(layout_git._run(runner, ['ls-tree', '-r', '-z', '--full-tree', result['tree']]))
    if tree != {p: (v[0], v[1]) for p, v in expected.items()}:
        raise _conflict('Managed commit tree differs from reviewed paths')
    fixed = intent['fixed']
    expected_commit = (f"tree {result['tree']}\nparent {intent['head']}\n"
                       f"author {fixed['identity']['name']} <{fixed['identity']['email']}> {fixed['timestamp']} +0000\n"
                       f"committer {fixed['identity']['name']} <{fixed['identity']['email']}> {fixed['timestamp']} +0000\n\n"
                       + fixed['message']).encode()
    if layout_git._run(runner, ['cat-file', 'commit', result['commit']]) != expected_commit:
        raise _conflict('Managed commit identity differs')
    if hashlib.sha1(b'commit ' + str(len(expected_commit)).encode() + b'\0' + expected_commit).hexdigest() != result['commit']:
        raise _conflict('Managed commit object id differs')
    return raw
