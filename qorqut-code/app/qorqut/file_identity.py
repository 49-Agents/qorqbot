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

The operations live in the system layer (``qorqut_system.identity``).
"""

from qorqut_system.identity import fd_parent_volume, matches, parent_volume, records_match, tree_matches

__all__ = ["records_match", "matches", "parent_volume", "fd_parent_volume", "tree_matches"]