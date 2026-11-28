"""Owner-private files, on the version this system uses.

A folder or file is private when it is a real object of this user, inside a real folder of this user, and holds no
group or other access. A folder opened once is passed to the later operations as a ``Folder`` object, so no
descriptor number crosses a function boundary and the Windows version can address the same folder by path.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from .private_files_windows import (DIRECTORY_MODE, FILE_MODE, Folder, _refuse, append_entry, create_entry, create_file, ensure_entry,
        fsync_directory, is_mine, is_private, is_private_and_mine, link_entry, list_entries, make_folder, make_folder_path,
        make_link, make_private_directory, mode_of, open_child_folder, open_entry, open_folder,
        open_private_file, open_read, owner_of, ownership_of, private_umask, read_link, read_private_file,
        remove_entry, remove_folder, remove_tree, rename_entry, rename_entry_exclusive, replace_entry,
        require_private_directory, set_private, stat_entry, stat_folder, sync_folder, user_key, walk_entries,
        write_private_file)
else:
    from .private_files_unix import (DIRECTORY_MODE, FILE_MODE, Folder, _refuse, append_entry, create_entry, create_file, ensure_entry,
        fsync_directory, is_mine, is_private, is_private_and_mine, link_entry, list_entries, make_folder, make_folder_path,
        make_link, make_private_directory, mode_of, open_child_folder, open_entry, open_folder,
        open_private_file, open_read, owner_of, ownership_of, private_umask, read_link, read_private_file,
        remove_entry, remove_folder, remove_tree, rename_entry, rename_entry_exclusive, replace_entry,
        require_private_directory, set_private, stat_entry, stat_folder, sync_folder, user_key, walk_entries,
        write_private_file)

__all__ = ["DIRECTORY_MODE", "FILE_MODE", "Folder", "_refuse", "append_entry", "create_entry", "create_file", "ensure_entry", "fsync_directory", "is_mine", "is_private", "is_private_and_mine", "link_entry", "list_entries", "make_folder", "make_folder_path", "make_link", "make_private_directory", "mode_of", "open_child_folder", "open_entry", "open_folder", "open_private_file", "open_read", "owner_of", "ownership_of", "private_umask", "read_link", "read_private_file", "remove_entry", "remove_folder", "remove_tree", "rename_entry", "rename_entry_exclusive", "replace_entry", "require_private_directory", "set_private", "stat_entry", "stat_folder", "sync_folder", "user_key", "walk_entries", "write_private_file"]
