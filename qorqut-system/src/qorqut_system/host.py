"""This machine's own facts: which file systems are mounted where, and the addresses this host answers on.

Feature code asks here instead of reading the kernel's tables itself. The Unix version holds today's Linux lines,
moved; the first Windows version refuses both operations with ``UnsupportedSystem`` rather than returning a guess.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from .host_windows import local_addresses, mounts
else:
    from .host_unix import local_addresses, mounts

__all__ = ["local_addresses", "mounts"]