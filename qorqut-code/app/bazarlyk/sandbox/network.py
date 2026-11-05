"""Compile trusted business routing into a pinned sing-box gateway configuration.

Workloads have no network capabilities. The provisioner routes their isolated
interfaces to the gateway; firewall rules, rather than environment variables,
enforce that path. No workload-supplied configuration is accepted here.
"""

from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
import re
from urllib.parse import unquote, urlsplit


SING_BOX_IMAGE = "ghcr.io/sagernet/sing-box@sha256:4bed9332a0013fef72c31200a84e8fc0ed91a5ab2fe373a69f0acbbbbfbef3c5"
SING_BOX_VERSION = "1.14.0"
TCP_AGENT, TCP_BROWSER = 15001, 15002
DNS_AGENT, DNS_BROWSER = 15053, 15054
ACCOUNT_PROXY = 18080
FAKE_IP_RANGE = "198.18.0.0/15"
NON_PUBLIC_V4 = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4",
)


def public_ipv4(value: str) -> str:
    address = IPv4Address(value)
    if not address.is_global or any(address in IPv4Network(network) for network in NON_PUBLIC_V4):
        raise ValueError("Assigned proxy must use a public unicast IPv4 address")
    return str(address)


def domain(value: str) -> str:
    if not isinstance(value, str) or len(value) > 253:
        raise ValueError("Invalid development-service domain")
    value = value.lower().rstrip(".")
    labels = value.split(".")
    if len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise ValueError("Development-service rules require exact DNS names")
    try:
        IPv4Address(value)
    except ValueError:
        return value
    raise ValueError("IP literals cannot select a direct development route")


@dataclass(frozen=True)
class ProxyEndpoint:
    """Trusted upstream resolved before startup; secrets never appear in repr/logs."""

    protocol: str
    address: str
    port: int
    server_name: str = ""
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)

    def __post_init__(self):
        if self.protocol not in {"http", "https", "socks5"}:
            raise ValueError("Unsupported assigned proxy protocol")
        public_ipv4(self.address)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Invalid assigned proxy port")
        if self.protocol == "https" and not self.server_name:
            raise ValueError("HTTPS proxy requires its verified TLS server name")
        if bool(self.username) != bool(self.password) or any(ord(c) < 32 or ord(c) == 127 for c in self.username + self.password):
            raise ValueError("Invalid assigned proxy credentials")

    @classmethod
    def from_url(cls, url: str, *, resolved_address: str):
        """URL is provided by the existing protected browser credential resolver."""
        try:
            parsed = urlsplit(url)
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or not parsed.hostname:
                raise ValueError
            return cls(parsed.scheme, resolved_address, parsed.port, parsed.hostname,
                       unquote(parsed.username or ""), unquote(parsed.password or ""))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Invalid assigned proxy configuration") from None

    def outbound(self):
        value = {"type": "socks" if self.protocol == "socks5" else "http", "tag": "business-proxy",
                 "server": self.address, "server_port": self.port}
        if self.protocol == "socks5":
            value["version"] = "5"
        if self.protocol == "https":
            value["tls"] = {"enabled": True, "server_name": self.server_name}
        if self.username:
            value.update(username=self.username, password=self.password)
        return value


@dataclass(frozen=True)
class RoutingPolicy:
    """Exact domain + port grants; browsers and account helpers never inherit them."""

    direct_domains: tuple[str, ...] = ()
    direct_ports: tuple[int, ...] = (80, 443, 22)
    # Only the explicitly registered agent listener is an allowed host service.
    agent_api_address: str | None = None
    agent_api_port: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "direct_domains", tuple(sorted({domain(v) for v in self.direct_domains})))
        if any(type(p) is not int or not 1 <= p <= 65535 for p in self.direct_ports):
            raise ValueError("Invalid development-service port")
        if self.direct_domains and not self.direct_ports:
            raise ValueError("Development-service rules require explicit ports")
        if (self.agent_api_address is None) != (self.agent_api_port is None):
            raise ValueError("Agent listener needs both an address and port")
        if self.agent_api_address is not None:
            address = IPv4Address(self.agent_api_address)
            if (not address.is_private or address.is_loopback or address.is_unspecified
                    or address.is_link_local or address.is_reserved or address.is_multicast):
                raise ValueError("Agent listener must use a private bridge address")
            if type(self.agent_api_port) is not int or not 1 <= self.agent_api_port <= 65535:
                raise ValueError("Invalid agent listener port")


def gateway_config(policy: RoutingPolicy, proxy: ProxyEndpoint) -> dict:
    """A private configuration file, never a public DTO or diagnostic payload.

    Synthetic DNS addresses retain the requested name through raw TCP (including
    SSH). Only an approved DNS destination can enter the development class.
    Within that class, inspect HTTP/TLS names so a different virtual host on a
    shared address cannot borrow the grant. Sniffing never promotes an unknown
    destination or literal IP into direct access. Resolve names before checking
    private addresses and before dialing the selected IP.
    """
    inbounds = [
        {"type": "redirect", "tag": tag, "listen": "0.0.0.0", "listen_port": port}
        for tag, port in (("agent", TCP_AGENT), ("browser", TCP_BROWSER))
    ] + [
        {"type": "direct", "tag": tag, "listen": "0.0.0.0", "listen_port": port}
        for tag, port in (("dns-agent", DNS_AGENT), ("dns-browser", DNS_BROWSER))
    ] + [{"type": "http", "tag": "account", "listen": "0.0.0.0", "listen_port": ACCOUNT_PROXY}]
    dns = {
        "servers": [
            {"type": "fakeip", "tag": "names", "inet4_range": FAKE_IP_RANGE},
            {"type": "https", "tag": "proxy-dns", "server": "1.1.1.1", "server_port": 443,
             "path": "/dns-query", "tls": {"enabled": True, "server_name": "cloudflare-dns.com"}, "detour": "business-proxy"},
            {"type": "https", "tag": "direct-dns", "server": "1.1.1.1", "server_port": 443,
             "path": "/dns-query", "tls": {"enabled": True, "server_name": "cloudflare-dns.com"}},
        ],
        "rules": [
            {"inbound": ["dns-agent", "dns-browser"], "query_type": "AAAA", "action": "predefined", "rcode": "NOERROR"},
            {"inbound": ["dns-agent", "dns-browser"], "query_type": "A", "action": "route", "server": "names"},
        ],
        "final": "proxy-dns", "strategy": "ipv4_only", "reverse_mapping": False,
    }
    rules = [{"inbound": ["dns-agent", "dns-browser"], "action": "hijack-dns"},
             {"network": "udp", "action": "reject"}, {"ip_version": 6, "action": "reject"}]
    if policy.agent_api_address:
        rules.append({"inbound": "agent", "ip_cidr": policy.agent_api_address + "/32",
                      "port": policy.agent_api_port, "action": "route", "outbound": "direct"})
    if policy.direct_domains:
        direct_match = {"inbound": "agent", "domain": list(policy.direct_domains), "port": list(policy.direct_ports)}
        # Do this only after the original DNS destination matched. Sniffing raw
        # IP connections would allow forged SNI/Host to grant new direct access.
        # No destination override: the gateway still dials the DNS-bound target.
        rules.append({**direct_match, "action": "sniff", "sniffer": ["http", "tls"], "timeout": "1s"})
        rules.append({**direct_match, "action": "resolve", "server": "direct-dns", "strategy": "ipv4_only"})
        # Resolve actions can re-resolve an already-resolved request. Explicitly
        # exclude the direct class so it never depends on a healthy proxy DNS.
        rules.append({"type": "logical", "mode": "and", "rules": [direct_match], "invert": True,
                      "action": "resolve", "server": "proxy-dns", "strategy": "ipv4_only"})
    else:
        rules.append({"action": "resolve", "server": "proxy-dns", "strategy": "ipv4_only"})
    rules.append({"ip_cidr": list(NON_PUBLIC_V4), "action": "reject"})
    if policy.direct_domains:
        rules.append({"inbound": "agent", "domain": list(policy.direct_domains),
                      "port": list(policy.direct_ports), "action": "route", "outbound": "direct"})
    return {
        "log": {"disabled": True},
        "dns": dns,
        "inbounds": inbounds,
        "outbounds": [proxy.outbound(), {"type": "direct", "tag": "direct"}],
        "route": {"rules": rules, "final": "business-proxy", "default_domain_resolver": "proxy-dns"},
    }
