"""Trusted first-hop selection for host-side business provider calls."""

from __future__ import annotations

from urllib.parse import urlsplit

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.account_proxy import RequiredAccountProxy, account_proxy
from bazarlyk.sandbox.config import HostConfig
from .local import IDENTITY_KEYS


def canonical_base(value):
    """Return a conservative canonical base, or ``None`` for a malformed URL."""
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment):
            return None
        port = parts.port
        if port is None:
            port = 443 if parts.scheme == "https" else 80
        if not 1 <= port <= 65535:
            return None
        path = parts.path.rstrip("/") or "/"
        return parts.scheme.lower(), parts.hostname.lower(), port, path
    except (TypeError, ValueError):
        return None


class BusinessEgress:
    """Select direct host routing or a mandatory verified sandbox gateway."""

    def __init__(self, local_files, sandbox):
        self.local_files, self.sandbox = local_files, sandbox

    def _selected(self, business_id) -> bool:
        if not business_id:
            return False
        with self.local_files.runtime.database.read() as uow:
            return uow.repository("business_sandboxes").get(business_id) is not None

    def proxy_for(self, business_id, *, destination, trusted_destination, own_values, token_key,
                  require_https=False):
        """Route a changed selected-business destination through its gateway.

        The gateway, rather than this host process, owns destination DNS and
        private/unknown-address enforcement.  This method deliberately invokes
        it before checking the override credential, so a stale generation fails
        closed before any client can construct a direct route.
        """
        if not self._selected(business_id):
            return None
        destination_base, trusted_base = canonical_base(destination), canonical_base(trusted_destination)
        if destination_base is not None and trusted_base is not None and destination_base == trusted_base:
            return None
        if require_https and (destination_base is None or destination_base[0] != "https"):
            raise DomainError("invalid_configuration", "Custom sandbox model provider URL must use HTTPS")
        proxy = account_proxy(self.sandbox.account_proxy(business_id))
        if not isinstance(own_values.get(token_key), str) or not own_values[token_key].strip():
            raise DomainError("environment_unavailable", "Custom sandbox provider destination needs its own credential")
        return proxy

    def provider_environment(self, business_id):
        """Return provider credentials for host-side collection without lending arbitrary Global settings.

        Existing host and global collection retain the historical merged view.
        A selected sandbox row instead requires the same enabled HostConfig and
        root key allowlist used by its terminal environment.
        """
        if not self._selected(business_id):
            return self.local_files.environment(business_id)
        config = HostConfig.read(self.local_files.runtime.paths)
        if not isinstance(config, HostConfig) or not config.enabled:
            raise DomainError("configuration_missing", "Sandbox provider environment is unavailable")
        values = {row["key"]: row["value"] for row in self.local_files.environment_pairs()
                  if row["key"] in config.shared_environment_keys and row["key"] not in IDENTITY_KEYS}
        values.update({row["key"]: row["value"] for row in self.local_files.environment_pairs(business_id)
                       if row["key"] not in IDENTITY_KEYS})
        return values
