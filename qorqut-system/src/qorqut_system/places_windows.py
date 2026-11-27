"""Where things live on Windows, and the service manager this version does not have.

A first version, not shipped and not hardened: the app owns the server on Windows, so no session host runs as a service,
and the folders are the ones a per-user installation of an app uses there.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from .errors import UnsupportedSystem

# Windows installs no systemd to name; the operations below refuse instead of using them.
SYSTEMCTL = SYSTEMD_RUN = ""
SYSTEMCTL_NAME = SYSTEMD_RUN_NAME = ""


def data_folder() -> Path:
    """The folder a per-user installation keeps its data in: ``%LOCALAPPDATA%``."""
    value = os.environ.get("LOCALAPPDATA")
    return Path(value) if value else Path.home() / "AppData" / "Local"


def run_folder() -> Path | None:
    """Windows has no short per-user runtime folder that is private by construction, so there is none to hand out."""
    return None


def temporary_folder() -> Path:
    """The shared temporary folder this process is told to use (``TEMP`` or ``TMP``)."""
    return Path(tempfile.gettempdir())


def channel_path_limit() -> None:
    """A named pipe has no address limit of the kind a Unix socket has, so the limit is ``None``."""
    return None


def channel_path_fits(path) -> bool:
    """Every path fits: the pipe's own name is derived from it, and names there are not length-bound."""
    return True


def service_manager_available() -> bool:
    """Windows has no per-user service manager to run a session host under; the app owns the server."""
    return False


def require_service_manager() -> None:
    raise UnsupportedSystem("Running a session host under a service manager is not available on Windows; the app owns the server")


def service_manager_environment() -> dict:
    raise UnsupportedSystem("There is no service manager environment on Windows; the app owns the server")


def session_host_command(**arguments) -> list:
    raise UnsupportedSystem("A session host does not run as a service unit on Windows; the app owns the server")


def run_session_host(**arguments):
    raise UnsupportedSystem("A session host does not run as a service unit on Windows; the app owns the server")


def session_host_state(unit, *, runner=None):
    raise UnsupportedSystem("A session host does not run as a service unit on Windows; the app owns the server")


def stop_session_host(unit, *, runner=None):
    raise UnsupportedSystem("A session host does not run as a service unit on Windows; the app owns the server")


def service_main_pid(service):
    raise UnsupportedSystem("Windows has no service manager to ask for a service's main process; the app owns the server")


def outside_private_mounts(path, table=None, same=None):
    """Windows gives a service no private bind mounts of its own, so every path is already valid outside them."""
    return Path(path)