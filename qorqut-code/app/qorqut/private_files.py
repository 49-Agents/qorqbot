"""Owner-private files: 0600 regular files in 0700 directories owned by this process user.

Symlinks, non-regular files and group/world-accessible secrets are rejected. Nothing here logs or returns a path
or a value to an agent; callers keep the bytes in memory only for as long as they need them.

The operations live in the system layer (``qorqut_system.private_files``). This module keeps their names for the
files that import them during the move and translates the layer's error to the domain error callers expect.
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError
from qorqut_system import private_files as _layer
from qorqut_system.errors import PrivateFileError

DIRECTORY_MODE = _layer.DIRECTORY_MODE
FILE_MODE = _layer.FILE_MODE


def _translate(error):
    return DomainError("invalid_configuration", str(error))


def require_private_directory(path, *, create=False):
    try:
        return _layer.require_private_directory(path, create=create)
    except PrivateFileError as error:
        raise _translate(error) from error


def read_private_file(path, *, max_bytes=65536, directory_check=True):
    try:
        return _layer.read_private_file(path, max_bytes=max_bytes, directory_check=directory_check)
    except PrivateFileError as error:
        raise _translate(error) from error


def fsync_directory(path):
    try:
        return _layer.fsync_directory(path)
    except PrivateFileError as error:
        raise _translate(error) from error


def write_private_file(directory, name, data):
    try:
        return _layer.write_private_file(directory, name, data)
    except PrivateFileError as error:
        raise _translate(error) from error