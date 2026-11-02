"""Validation of the provider's trusted configuration.

The selection entry in the installation configuration is::

    {"factory": "qorqut_fastmail.extension:create",
     "config": {"engine_url": "http://127.0.0.1:8787", "admin_token_file": "/absolute/private/engine-admin-token",
                "account_id": "u0123456"}}

``engine_url`` must name an explicit loopback or Tailscale IP. ``admin_token_file`` is an absolute private file (mode 0600) that
holds the email engine's account-administration token; it is read when the engine is first used, never when the declaration is
made, and never logged. ``account_id`` is optional: when set, the engine must report exactly that Fastmail account or every
operation is refused. No mailbox, Project, folder or alias belongs to this configuration."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from qorqut_operations.contracts import DomainError

KEYS = frozenset({"engine_url", "admin_token_file", "account_id"})
ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
MIN_TOKEN = 32


def _invalid(message: str) -> DomainError:
    return DomainError("invalid_installation", "Fastmail configuration: " + message)


def engine_url(value) -> str:
    """The engine URL: an http(s) origin with an explicit loopback or Tailscale IP, no credentials, path, query or fragment."""
    parsed = urlsplit(value) if isinstance(value, str) else None
    try:
        ip = ipaddress.ip_address(parsed.hostname) if parsed is not None and parsed.hostname else None
    except ValueError:
        ip = None
    if (parsed is None or ip is None or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or not (ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10"))):
        raise _invalid("engine_url needs an explicit loopback or Tailscale IP origin")
    return value.rstrip("/")


@dataclass(frozen=True)
class FastmailConfig:
    engine_url: str
    admin_token_file: Path
    account_id: str | None = None

    def admin_token(self) -> str:
        """The engine administration token, read now from its private file (a symlink or a group/world-readable file is refused)."""
        try:
            descriptor = os.open(self.admin_token_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise DomainError("operation_unavailable", "The Fastmail engine token file is unavailable") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise DomainError("operation_unavailable", "The Fastmail engine token file must be a private regular file")
            token = os.read(descriptor, 4097).decode("ascii", "replace").strip()
        finally:
            os.close(descriptor)
        if len(token) < MIN_TOKEN or not token.isascii() or len(token) > 4096:
            raise DomainError("operation_unavailable", "The Fastmail engine token is invalid")
        return token


def from_config(config) -> FastmailConfig:
    if not isinstance(config, dict) or set(config) - KEYS or not {"engine_url", "admin_token_file"} <= set(config):
        raise _invalid("requires engine_url and admin_token_file and allows only: " + ", ".join(sorted(KEYS)))
    token = config["admin_token_file"]
    if not isinstance(token, str) or not Path(token).is_absolute() or "\0" in token or ".." in Path(token).parts:
        raise _invalid("admin_token_file must be an absolute file path")
    account = config.get("account_id")
    if account is not None and (not isinstance(account, str) or not ACCOUNT_ID.fullmatch(account)):
        raise _invalid("account_id is a provider account identifier")
    return FastmailConfig(engine_url(config["engine_url"]), Path(token), account)
