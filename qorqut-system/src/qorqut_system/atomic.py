"""Atomic replacement of one small file, on the version this system uses."""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from .atomic_windows import atomic_write
else:
    from .atomic_unix import atomic_write

__all__ = ["atomic_write"]