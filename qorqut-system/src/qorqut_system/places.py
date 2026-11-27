"""Where things live on this system, and how a session host runs under its service manager.

The operations are the ones the managed runtime, the browser supervisor and the release tools need today: the folder a
per-user installation keeps its data in, the short private run folder, the shared temporary folder, the byte limit of a
local channel's path, the path a service's own private bind mounts hide, and the transient user service a session host
runs as.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from .places_windows import (SYSTEMCTL, SYSTEMCTL_NAME, SYSTEMD_RUN, SYSTEMD_RUN_NAME, channel_path_fits,
                                 channel_path_limit, data_folder, outside_private_mounts, require_service_manager,
                                 run_folder, run_session_host, service_main_pid, service_manager_available,
                                 service_manager_environment, session_host_command, session_host_state,
                                 stop_session_host, temporary_folder)
else:
    from .places_unix import (SYSTEMCTL, SYSTEMCTL_NAME, SYSTEMD_RUN, SYSTEMD_RUN_NAME, channel_path_fits,
                              channel_path_limit, data_folder, outside_private_mounts, require_service_manager,
                              run_folder, run_session_host, service_main_pid, service_manager_available,
                              service_manager_environment, session_host_command, session_host_state,
                              stop_session_host, temporary_folder)

__all__ = ["SYSTEMCTL", "SYSTEMCTL_NAME", "SYSTEMD_RUN", "SYSTEMD_RUN_NAME", "channel_path_fits", "channel_path_limit",
           "data_folder", "outside_private_mounts", "require_service_manager", "run_folder", "run_session_host",
           "service_main_pid", "service_manager_available", "service_manager_environment", "session_host_command",
           "session_host_state", "stop_session_host", "temporary_folder"]