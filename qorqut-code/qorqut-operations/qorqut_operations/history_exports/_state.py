"""Small private state files beside the history repository, outside its tracked tree."""
from __future__ import annotations

import json
import os
from uuid import uuid4


def read_json(runtime, name):
    """The saved mapping, or ``None`` when absent or unreadable (callers rebuild or ignore)."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=runtime)
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
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                 dir_fd=runtime)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=runtime, dst_dir_fd=runtime)
        os.fsync(runtime)
    finally:
        try:
            os.unlink(temporary, dir_fd=runtime)
        except FileNotFoundError:
            pass
