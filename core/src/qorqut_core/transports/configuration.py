"""Bounded, explicit installation and operator input."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from ..app import load_settings
from ..config import InstallationSettings

MAX_INPUT_BYTES = 1024 * 1024


class InputError(Exception):
    """Only fixed diagnostics belong here, never paths, values or validation errors."""


def read_file(path: Path) -> bytes:
    """Read at most 1 MiB; no symlink component, device or FIFO."""
    fd = None
    try:
        # Preserve '..' while walking descriptors: lexical normalization could
        # otherwise erase a symlink component before we reject it.
        parts = Path(path).absolute().parts
        fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        os.close(fd)
        fd = child
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InputError("Input must be a regular file without symbolic links")
        if info.st_size > MAX_INPUT_BYTES:
            raise InputError("Input file exceeds 1 MiB")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            value = stream.read(MAX_INPUT_BYTES + 1)
        if len(value) > MAX_INPUT_BYTES:
            raise InputError("Input file exceeds 1 MiB")
        return value
    except (OSError, ValueError):
        raise InputError("Cannot read the explicit input file safely") from None
    finally:
        if fd is not None:
            os.close(fd)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Nonfinite JSON value")


def json_object(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        if not isinstance(value, dict):
            raise ValueError("Object required")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise InputError("Expected a valid JSON object") from None


def read_configuration(installation: Path) -> InstallationSettings:
    try:
        # Preflight rejects unsafe file types before the foundation's bounded reader.
        read_file(installation)
        settings = load_settings(installation)
        if str(settings.listen_address) != "127.0.0.1":
            raise InputError("This increment requires listen_address 127.0.0.1")
        return settings
    except InputError:
        raise
    except Exception:
        raise InputError("Invalid installation configuration") from None
