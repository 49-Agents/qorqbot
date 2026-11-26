"""Optional owner-installed personal reader, bound to one global Ordubashy UUID.

This prepares a read-only client. It never reads session keys, pairs a phone, contacts
WhatsApp, or grants another role access because it has a similar name; ``linked`` only asks
the session store whether a device row exists.
A version 2 binding additionally records the managed-send transport (its private
socket, operator-created secret file and frozen opaque account reference). That
transport is never part of the agent's MCP launch: the connector's MCP stays
`whatsapp_read` only, and sending exists solely as the owner-approved
`whatsapp_send` Signoff operation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import quote
from uuid import UUID

from qorqut_operations.contracts import DomainError

NAME = "whatsapp-personal"
TOOLS = ["whatsapp_read"]



def runtime_name(instance: Path) -> str:
    return "qorqut-wa-" + hashlib.sha256(str(instance).encode()).hexdigest()[:12]


def socket_path(instance: Path) -> Path:
    return instance / "run" / "wa" / "read.sock"


def send_socket_path(instance: Path) -> Path:
    return instance / "run" / "wa" / "send.sock"


def send_secret_path(instance: Path) -> Path:
    return instance / "private" / (NAME + "-send.key")


def root_binding(instance: Path) -> Path:
    return instance / "config" / (NAME + ".json")


def setup_marker_path(instance: Path) -> Path:
    """Present while the app itself is finishing a setup it started (``setup.py``); never written by hand."""
    return instance / "private" / "whatsapp-setup.json"


def data_path(instance: Path) -> Path:
    return instance / "private" / "whatsapp"


def bundled_helper() -> Path:
    """The helper built into this package for this platform; absent where no build ships."""
    return Path(__file__).parent / "bin" / NAME


def supported() -> bool:
    helper = bundled_helper()
    return helper.is_file() and os.access(helper, os.X_OK)


def socket_limit() -> int:
    """sockaddr_un.sun_path: 108 bytes on Linux, 104 on macOS (both include the final NUL)."""
    return 104 if sys.platform == "darwin" else 108


def socket_fits(path) -> bool:
    return len(os.fsencode(str(path))) < socket_limit()


def linked(instance: Path) -> bool:
    """The helper's session store holds a linked device. Reads one row count; never reads keys or contacts WhatsApp."""
    store = data_path(instance) / "session.sqlite3"
    if not store.is_file() or store.is_symlink():
        return False
    try:
        connection = sqlite3.connect("file:" + quote(str(store)) + "?mode=rw", uri=True, timeout=5)
        try:
            return connection.execute("SELECT 1 FROM whatsmeow_device LIMIT 1").fetchone() is not None
        finally:
            connection.close()
    except sqlite3.OperationalError as problem:
        # A store its helper holds locked is in use, not unlinked: a busy moment must never read as "not set up", which
        # would stop a working receiver or let a cancel remove a working binding.
        return "locked" in str(problem) or "busy" in str(problem)
    except sqlite3.Error:
        return False


def pending_send(value: dict, instance: Path) -> dict | None:
    """The managed-send transport of a read-only binding the app is finishing (marker and secret present), before its
    version 2 binding can record the account reference. None otherwise."""
    if value["version"] != 1 or not setup_marker_path(instance).is_file() or not send_secret_path(instance).is_file():
        return None
    return {"socket": str(Path(value["socket"]).parent / "send.sock"), "secret_file": str(send_secret_path(instance))}


def regular_path(path: Path) -> bool:
    return path.is_absolute() and ".." not in path.parts and not any(
        p.is_symlink() for p in (path, *path.parents))


BINDING_V1 = {"version", "agent_id", "binary", "sha256", "socket"}
SEND_KEYS = {"socket", "secret_file", "account_ref"}


def validate_binding(value, instance: Path) -> dict:
    """Version 1 (read-only) or version 2 (read-only plus the managed-send transport) of the installation binding."""
    try:
        version = value["version"]
        if version == 1 and set(value) != BINDING_V1 or version == 2 and set(value) != BINDING_V1 | {"managed_send"}:
            raise ValueError()
        if version not in (1, 2) or str(UUID(value["agent_id"])) != value["agent_id"]:
            raise ValueError()
        binary = Path(value["binary"])
        base = instance / "runtime/connectors" / NAME
        if (not regular_path(binary) or binary.name != NAME or binary.parent.parent != base
                or re.fullmatch(r"[0-9a-f]{40}", binary.parent.name) is None
                or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
                or value["socket"] not in {str(socket_path(instance)), str(Path("/run") / runtime_name(instance) / "read.sock")}):
            raise ValueError()
        if version == 2:
            send = value["managed_send"]
            if (set(send) != SEND_KEYS or send["socket"] not in {str(send_socket_path(instance)), str(Path("/run") / runtime_name(instance) / "send.sock")}
                    or send["secret_file"] != str(send_secret_path(instance))
                    or re.fullmatch(r"[0-9a-f]{64}", send["account_ref"]) is None):
                raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise DomainError("invalid_configuration", "Invalid personal WhatsApp installation binding") from None
    return value


def load_binding(instance: Path) -> dict | None:
    """The validated installation binding, or None when the reader is not installed. Resolved from the runtime's
    instance root, never from the mutable ``current`` release pointer."""
    binding = root_binding(instance)
    if not regular_path(binding):
        raise DomainError("invalid_configuration", "Personal WhatsApp binding may not cross a symlink")
    if not binding.exists():
        return None
    try:
        if binding.stat().st_mode & 0o077:
            raise ValueError()
        return validate_binding(json.loads(binding.read_text()), instance)
    except (OSError, ValueError, TypeError):
        raise DomainError("invalid_configuration", "Personal WhatsApp binding must be private, valid JSON") from None


def managed_send(instance: Path) -> dict | None:
    """The managed-send settings of a version 2 binding for its exact agent, else None (read-only installations)."""
    value = load_binding(instance)
    if value is None or value["version"] != 2:
        return None
    return {"agent_id": value["agent_id"], **value["managed_send"]}


def launch(paths, data) -> tuple[dict | None, dict | None]:
    if data["role"]["id"] != "ordubashy" or data["scope"]["kind"] != "ecosystem":
        return None, None
    value = load_binding(paths.instance_root)
    if value is None:
        return None, None
    if value["agent_id"] != data["agent_id"]:
        return None, None
    binary = Path(value["binary"])
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return {"prepared": False, "reason": "reader_not_installed"}, None
    if not linked(paths.instance_root):
        return {"prepared": False, "reason": "not_linked"}, None
    with binary.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != value["sha256"]:
            raise DomainError("invalid_configuration", "Personal WhatsApp binary failed its installation integrity check")
    # A stopped/unpaired receiver does not prepare any send path: the MCP launch is the read client only.
    return {"prepared": True}, {"command": str(binary), "args": ["mcp", "--socket", value["socket"]]}
