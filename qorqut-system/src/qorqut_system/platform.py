"""Which system this is: the family of the machine, its own name, its machine name and the kind of host.

Feature code asks these questions here instead of testing the system itself. ``family`` is the comparison call
sites make today (``platform.system() == "Linux"``, ``sys.platform == "darwin"``); ``machine`` is the value they
read from ``platform.machine()`` unchanged, so a table lookup keeps its key; ``system_name`` is the system's own
name, for a call site whose table is keyed by those names today; and ``host_kind`` is the dashboard's own answer
(``darwin``, ``wsl`` or ``linux``), whose three strings a view shows as they are.
"""

from __future__ import annotations

import os
import platform as _system
import sys

FAMILIES = ("linux", "mac", "windows", "other")
# The file the kernel's own version text is read from, which is also the default a call site passes on.
KERNEL_RELEASE = "/proc/version"


def family() -> str:
    """The system family: ``linux``, ``mac``, ``windows`` or ``other``."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "mac"
    if sys.platform in ("win32", "cygwin", "msys"):
        return "windows"
    return "other"


def machine() -> str:
    """The machine name, exactly the value ``platform.machine()`` returns on this system."""
    return _system.machine()


def system_name() -> str:
    """The system's own name as it gives it (``Darwin``, ``Linux``, ``Windows``).

    For a call site whose table is keyed by those names and whose arguments are checked against them; a new table
    uses ``family``.
    """
    return _system.system()


def host_kind(kernel_release=KERNEL_RELEASE) -> str:
    """``darwin``, ``wsl`` (Windows through WSL2) or ``linux``, as the views that show it name the system.

    ``kernel_release`` is the file the kernel's version text is read from, and is the call site's parameter, so a test
    can point it at a file of its own. Windows answers ``windows``, which the three views do not know yet.
    """
    if family() == "windows":
        return "windows"
    if os.uname().sysname == "Darwin":
        return "darwin"
    try:
        with open(kernel_release) as source:
            return "wsl" if "microsoft" in source.read().lower() else "linux"
    except OSError:
        return "linux"
