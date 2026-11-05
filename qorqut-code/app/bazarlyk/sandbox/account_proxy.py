"""Mandatory HTTP CONNECT transport without importing service composition."""

from urllib.parse import urlsplit
from urllib.request import ProxyHandler

from qorqut_operations.contracts import DomainError


def account_proxy(proxy: str) -> str:
    """Validate one verified sandbox gateway URL without resolving a destination."""
    try:
        parts = urlsplit(proxy)
        valid = (parts.scheme == "http" and parts.hostname and parts.port
                 and not parts.username and not parts.password and not parts.path
                 and not parts.query and not parts.fragment)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise DomainError("environment_unavailable", "Business account gateway is invalid", retryable=True)
    return proxy


class RequiredAccountProxy(ProxyHandler):
    """Use the verified gateway even when the host has ``NO_PROXY`` configured."""

    def __init__(self, proxy):
        proxy = account_proxy(proxy)
        super().__init__({"https": proxy})
        self.gateway = urlsplit(proxy).netloc

    def proxy_open(self, req, proxy, type):
        # urllib's default handler honors ambient NO_PROXY even with an explicit
        # proxy. This route is mandatory; HTTPSHandler performs CONNECT and TLS.
        if req.type != "https":
            raise DomainError("environment_unavailable", "Business account route requires HTTPS")
        req.set_proxy(self.gateway, "http")
        return None
