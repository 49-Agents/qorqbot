"""Bounded logical runtime fingerprints for offline managed preparation."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import struct

from .support import digest, relative
from .layout_intent import _conflict

# The existing authored/inventory inspectors use a 100,000 entry bound. Runtime
# bytes are bounded by the existing 1 GiB database inspection limit.
MAX_ROWS = 100000
MAX_BYTES = 1024 * 1024 * 1024
MAX_VALUE = 16 * 1024 * 1024


def _encoded(value):
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
        raise _conflict('Unsupported SQLite value in managed runtime evidence')
    if len(data) > MAX_VALUE:
        raise _conflict('Runtime value exceeds managed inspection bound')
    return tag + len(data).to_bytes(8, 'big') + data


def _feed(hash_, row):
    size = 0
    for value in ('row', *row):
        encoded = _encoded(value)
        size += len(encoded)
        hash_.update(encoded)
    return size


def snapshot(conn, paths, change_id, *, require_unused=True):
    """Fingerprint every table, plus the exact effects excluded from projection.

    Caller provides an immutable offline connection under the authoring lock.
    Unknown tables are fully fingerprinted, never excluded.
    """
    if not isinstance(paths, (list, tuple)) or len(paths) > MAX_ROWS:
        raise _conflict('Invalid managed runtime path set')
    try:
        for path in paths:
            relative(path)
    except (TypeError, ValueError) as exc:
        raise _conflict('Unsafe managed runtime path') from exc
    if (len(set(paths)) != len(paths) or bool(paths) != bool(change_id)
            or (change_id is not None and (not isinstance(change_id, str)
                                           or not re.fullmatch(r'chg_[0-9a-f]{32}', change_id)))):
        raise _conflict('Invalid managed runtime event identity')
    selected = set(paths)
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE)
    schema = [tuple(row) for row in conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name LIMIT 100001')]
    if len(schema) > MAX_ROWS:
        raise _conflict('Runtime schema exceeds managed inspection bound')
    names = {row[1] for row in schema if row[0] == 'table'}
    if not {'meta', 'changes', 'change_files', 'inventory', 'sqlite_sequence'} <= names:
        raise _conflict('Runtime schema lacks required managed evidence tables')
    if require_unused and change_id is not None and conn.execute('SELECT 1 FROM changes WHERE change_id=?', (change_id,)).fetchone():
        raise _conflict('Managed event identity already exists')
    if require_unused and change_id is not None and conn.execute('SELECT 1 FROM change_files WHERE change_id=?', (change_id,)).fetchone():
        raise _conflict('Managed event file identity already exists')
    tables, projection, old_inventory = {}, {}, {path: None for path in sorted(selected)}
    total_rows, total_bytes = 0, 0
    for name in sorted(names):
        if name.startswith('sqlite_') and name != 'sqlite_sequence':
            continue
        quoted = '"' + name.replace('"', '""') + '"'
        columns = [row[1] for row in conn.execute('PRAGMA table_info(' + quoted + ')')]
        positions = {column: index + 1 for index, column in enumerate(columns)}
        whole, protected = hashlib.sha256(), hashlib.sha256()
        count, kept = 0, 0
        for row in conn.execute('SELECT rowid,* FROM ' + quoted + ' ORDER BY rowid'):
            row = tuple(row)
            count += 1
            total_rows += 1
            if total_rows > MAX_ROWS:
                raise _conflict('Runtime rows exceed managed inspection bound')
            total_bytes += _feed(whole, row)
            if total_bytes > MAX_BYTES:
                raise _conflict('Runtime contents exceed managed inspection bound')
            excluded = False
            if selected and name == 'inventory' and row[positions['path']] in selected:
                path = row[positions['path']]
                old_inventory[path] = {'sha256': row[positions['sha256']], 'bytes': row[positions['size']]}
                excluded = True
            elif selected and name in ('changes', 'change_files') and row[positions['change_id']] == change_id:
                excluded = True
            elif selected and name == 'sqlite_sequence' and row[positions['name']] == 'changes':
                excluded = True
            if not excluded:
                _feed(protected, row)
                kept += 1
        tables[name] = {'rows': count, 'sha256': whole.hexdigest()}
        projection[name] = {'rows': kept, 'sha256': protected.hexdigest()}
    sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='changes'").fetchone()
    feed = conn.execute('SELECT COALESCE(MAX(sequence),0) FROM changes').fetchone()[0]
    return {'schema_sha256': digest(schema), 'tables': tables, 'protected': projection,
            'changed_inventory_before': dict(sorted(old_inventory.items())),
            'changes_sequence_before': sequence[0] if sequence else None, 'feed_head': feed,
            'inspection': {'rows': total_rows, 'bytes': total_bytes, 'row_limit': MAX_ROWS, 'byte_limit': MAX_BYTES}}
