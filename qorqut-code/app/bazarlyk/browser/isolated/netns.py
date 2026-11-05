"""Refuse to run anywhere but a namespace with loopback and no route out.

The probe reads this process's own netns state (``/proc/self/net``), then tries
addresses that only a routed namespace could reach. Connecting a datagram or
stream socket to a TEST-NET address must fail with an unreachable error; a
routed host would either connect (UDP) or hang/refuse (TCP), and both fail here.
"""

import socket
from pathlib import Path

from qorqut_operations.contracts import DomainError
from . import unreachable
from .processes import netns_identity

# RFC 5737 / RFC 3849 documentation addresses: never assigned to a real host.
UNROUTED = ((socket.AF_INET, socket.SOCK_STREAM, "192.0.2.1"), (socket.AF_INET, socket.SOCK_DGRAM, "192.0.2.1"),
            (socket.AF_INET6, socket.SOCK_STREAM, "2001:db8::1"), (socket.AF_INET6, socket.SOCK_DGRAM, "2001:db8::1"))


def _fault(reason):
    return DomainError("invalid_configuration",
                       "The isolated browser must run in a loopback-only network namespace (" + reason + ")")


def interfaces(net=Path("/proc/self/net")):
    lines = (net / "dev").read_text().splitlines()[2:]
    return sorted(line.split(":", 1)[0].strip() for line in lines if ":" in line)


def routes(net=Path("/proc/self/net")):
    """Non-loopback routes in IPv4 and IPv6 route tables."""
    found = [line.split()[0] for line in (net / "route").read_text().splitlines()[1:] if line.split()]
    table = net / "ipv6_route"
    if table.exists():
        for line in table.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 10 and fields[9] != "lo":
                found.append(fields[9])
    return found


def loopback_works():
    with socket.socket() as server, socket.socket() as client:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client.settimeout(2)
        client.connect(server.getsockname())
        return True


def probe(net=Path("/proc/self/net"), *, unrouted=UNROUTED):
    """Return the namespace identity, or raise when it is not loopback-only."""
    names = interfaces(net)
    if names != ["lo"]:
        raise _fault("interfaces: " + ", ".join(names))
    if routes(net):
        raise _fault("a route exists")
    try:
        loopback_works()
    except OSError:
        raise _fault("loopback is not usable") from None
    for family, kind, address in unrouted:
        try:
            with socket.socket(family, kind) as probe_socket:
                probe_socket.settimeout(1)
                probe_socket.connect((address, 9))
        except OSError as error:
            if unreachable(error):
                continue
            raise _fault("unexpected reply from an unrouted address") from None
        raise _fault("an unrouted address accepted a connection")
    return {"netns": netns_identity(), "interfaces": names}
