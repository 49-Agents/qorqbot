"""Whole-file locks, on the version this system uses.

A lock file is opened once as a ``LockFile`` when the caller must check it, take the lock later or hold it in a
registry; the simple callers use ``lock()``.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from .locks_windows import LockFile, acquire, held, lock, open_descriptor, open_lock, release
else:
    from .locks_unix import LockFile, acquire, held, lock, open_descriptor, open_lock, release

__all__ = ["LockFile", "acquire", "held", "lock", "open_descriptor", "open_lock", "release"]