"""Processes, on the version this system uses: start in the background, observe, identify and stop.

The Unix version holds today's Linux and macOS code, moved. The Windows version is a first version: not shipped, not
hardened and not run by the checks yet.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from .processes_windows import (CLAUDE_VERSIONED, OpenFile, _identified_alive, boot_id, boot_id_plain, cmdline, cmdline_contains,
        cmdline_parts, cwd, darwin, effective_user_id, environ, environ_strict, exe, fields, fields_as_given, identity, ignore_hangup, is_zombie, is_zombie_as_given,
        kill_pid, kill_process, kill_process_group, machine_id, main, name, namespace_id, namespace_pids,
        open_files, open_links, owned, owns_listener, parent_pid, pid_identity, pids, process_group, process_token,
        start_in_background, start_token, stop_identified_process, stop_process_group, terminate_pid,
        terminate_process, terminate_process_group, token_alive, uids, user_id, wait_for_exit_without_reaping)
else:
    from .processes_unix import (CLAUDE_VERSIONED, OpenFile, _identified_alive, boot_id, boot_id_plain, cmdline, cmdline_contains,
        cmdline_parts, cwd, darwin, effective_user_id, environ, environ_strict, exe, fields, fields_as_given, identity, ignore_hangup, is_zombie, is_zombie_as_given,
        kill_pid, kill_process, kill_process_group, machine_id, main, name, namespace_id, namespace_pids,
        open_files, open_links, owned, owns_listener, parent_pid, pid_identity, pids, process_group, process_token,
        start_in_background, start_token, stop_identified_process, stop_process_group, terminate_pid,
        terminate_process, terminate_process_group, token_alive, uids, user_id, wait_for_exit_without_reaping)

__all__ = ["CLAUDE_VERSIONED", "OpenFile", "_identified_alive", "boot_id", "boot_id_plain", "cmdline", "cmdline_contains", "cmdline_parts", "cwd", "darwin", "effective_user_id", "environ", "environ_strict", "exe", "fields", "fields_as_given", "identity", "ignore_hangup", "is_zombie", "is_zombie_as_given", "kill_pid", "kill_process", "kill_process_group", "machine_id", "main", "name", "namespace_id", "namespace_pids", "open_files", "open_links", "owned", "owns_listener", "parent_pid", "pid_identity", "pids", "process_group", "process_token", "start_in_background", "start_token", "stop_identified_process", "stop_process_group", "terminate_pid", "terminate_process", "terminate_process_group", "token_alive", "uids", "user_id", "wait_for_exit_without_reaping"]
