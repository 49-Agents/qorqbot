"""Small private state files beside the history repository, outside its tracked tree."""
from __future__ import annotations

import json
import os
from uuid import uuid4

from qorqut_system import private_files


def read_json(runtime, name):
    """The saved mapping, or ``None`` when absent or unreadable (callers rebuild or ignore)."""
    try:
        fd = private_files.open_read(runtime, name)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        try:
            value = json.loads(stream.read())
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def write_json(runtime, name, value):
    """Atomically replace ``name``; readers see the old or the new file, never a partial one."""
    temporary = f'.{name}.{uuid4().hex}'
    fd = private_files.create_file(runtime, temporary)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        private_files.rename_entry(runtime, temporary, name)
        private_files.sync_folder(runtime)
    finally:
        private_files.remove_entry(runtime, temporary, missing_ok=True)
