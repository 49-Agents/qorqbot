"""Atomic replacement of one small file on Windows: a first version, written beside it and renamed over it.

Windows does not change permission bits the way Unix does, so the requested mode is applied on a best-effort basis.
Another program may hold the target open for a moment; the replacement is retried a few times before it gives up.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time

RETRIES = 10
FIRST_WAIT = 0.05


def atomic_write(path: Path, content: bytes, *, mode: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, mode)
        except OSError:
            pass
        for attempt in range(RETRIES):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt + 1 == RETRIES:
                    raise
                time.sleep(FIRST_WAIT * (attempt + 1))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)