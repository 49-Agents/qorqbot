"""The mount table and the interface addresses on Windows: both refused in this first version.

Windows keeps its volumes and its addresses in forms this version does not read yet, so it says so instead of
returning a guess. Nothing here runs in a delivered installation.
"""

from __future__ import annotations

from .errors import UnsupportedSystem


def mounts():
    raise UnsupportedSystem("This first Windows version cannot read the mount table of the machine")


def local_addresses():
    raise UnsupportedSystem("This first Windows version cannot list the addresses of this machine")