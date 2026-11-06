"""Compare a saved file identity with what is on disk, in a way that survives a restart.

A device number (``st_dev``) names a volume only until the next restart. macOS renumbers
APFS volumes at boot, and Linux (including WSL2, where Windows installs run) can attach
disks in a different order. A ``device`` saved in a journal or receipt therefore proves
nothing on its own, and comparing it with a live ``st_dev`` rejects untouched data after
any restart.

These helpers ignore the saved device number. Every other saved field (inode, mode, owner,
size, ...) must still match, and the object must sit on the same volume as its parent
directory *right now*. Both device numbers in that second check are read live, so it holds
across restarts, and it still rejects a directory replaced by a mount or by an object from
another volume. A replacement made on the same volume gets a new inode, which the inode
comparison rejects, exactly as before.

Use these only where a saved identity may be older than the current boot. Comparisons
between two stats taken in the same run can keep comparing ``st_dev`` directly.
"""
import os

__all__ = ["records_match", "matches", "parent_volume", "fd_parent_volume", "tree_matches"]


def records_match(saved, other):
    """Whether two saved identities name the same object; each may hold a device number from a different boot."""
    return (isinstance(saved, dict) and isinstance(other, dict) and set(saved) == set(other)
            and all(saved[key] == other[key] for key in saved if key != "device"))


def matches(saved, current, volume):
    """Whether ``saved`` still names the live object described by ``current``.

    ``current`` is built from a live stat with the same fields as ``saved``. ``volume`` is the
    live ``st_dev`` of the object's parent directory.
    """
    return records_match(saved, current) and current.get("device") == volume


def parent_volume(path):
    """Live device number of the directory containing ``path`` (symlinks not followed)."""
    return os.stat(os.path.dirname(os.path.abspath(path)), follow_symlinks=False).st_dev


def fd_parent_volume(dirfd):
    """Live device number of the parent of the directory open as ``dirfd``."""
    return os.stat("..", dir_fd=dirfd, follow_symlinks=False).st_dev


def tree_matches(saved, live, root_volume):
    """Whether saved ``{relative path: identity}`` directory records still describe ``live``.

    Each directory must be on its parent's live volume; top-level entries must be on
    ``root_volume``, the live device of the directory the paths are relative to.
    """
    if not isinstance(saved, dict) or set(saved) != set(live):
        return False
    for path, current in live.items():
        parent = path.rpartition("/")[0]
        if parent and parent not in live:
            return False
        volume = live[parent]["device"] if parent else root_volume
        if not matches(saved[path], current, volume):
            return False
    return True
