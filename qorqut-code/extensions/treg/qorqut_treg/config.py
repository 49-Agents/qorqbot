"""The extension's configuration and the owner's treg key.

The selection entry in the installation configuration is::

    {"factory": "qorqut_treg.extension:create", "config": {}}

``lookup_ceiling_usd`` is the one optional setting: the most treg may charge for a single lookup that runs without the
owner's approval (default 0.10, at most 1.00). The service sends it to treg with every lookup and treg refuses a call
that would cost more.

The key is never part of this configuration. The owner pastes it in the dashboard and it is kept in the private file
``config/treg-key`` of the instance (mode 0600). It is read when a call needs it, never logged and never returned."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import uuid

from qorqut_operations.contracts import DomainError

from qorqut.private_files import fsync_directory, read_private_file, write_private_file

KEYS = frozenset({"lookup_ceiling_usd"})
DEFAULT_CEILING_USD = 0.10
MAX_CEILING_USD = 1.00
KEY_FILE = "treg-key"
# A treg key is one opaque token: printable, no spaces. Nothing else about its shape is assumed.
KEY = re.compile(r"[A-Za-z0-9._~+/=:-]{16,2048}")


@dataclass(frozen=True)
class TregConfig:
    lookup_ceiling_usd: float = DEFAULT_CEILING_USD


def from_config(config) -> TregConfig:
    if config is None:
        config = {}
    if not isinstance(config, dict) or set(config) - KEYS:
        raise DomainError("invalid_installation", "treg configuration allows only: " + ", ".join(sorted(KEYS)))
    ceiling = config.get("lookup_ceiling_usd", DEFAULT_CEILING_USD)
    if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not 0 < ceiling <= MAX_CEILING_USD:
        raise DomainError("invalid_installation", f"treg configuration: lookup_ceiling_usd is a number above 0 and at most {MAX_CEILING_USD:.2f}")
    return TregConfig(float(ceiling))


def key_path(instance_root) -> Path:
    return Path(instance_root) / "config" / KEY_FILE


def has_key(instance_root) -> bool:
    return key_path(instance_root).is_file()


def read_key(instance_root) -> str | None:
    """The saved key, or None when the owner has not added one. A file that is not private is refused, never used."""
    path = key_path(instance_root)
    if not os.path.lexists(path):
        return None
    key = read_private_file(path, max_bytes=4096).decode("ascii", "replace").strip()
    if not KEY.fullmatch(key):
        raise DomainError("operation_unavailable", "The saved treg key is not valid; the owner adds it again in Settings, Connectors")
    return key


def check_key(value) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if not KEY.fullmatch(key):
        raise DomainError("validation_error", "That does not look like a treg key. Paste the key exactly as treg shows it.")
    return key


def save_key(instance_root, key) -> None:
    """Replace the saved key with a new private file; the old one is never edited in place."""
    path = key_path(instance_root)
    temporary = write_private_file(path.parent, f".{KEY_FILE}-{uuid.uuid4().hex}", check_key(key).encode("ascii") + b"\n")
    try:
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def remove_key(instance_root) -> None:
    path = key_path(instance_root)
    path.unlink(missing_ok=True)
    fsync_directory(path.parent)
