"""Small, explicit firewall plans for dedicated business network namespaces.

The trusted host installs these before exposing an environment. No host-wide
policy or existing chain is flushed. Namespace policies apply only to the new
gateway, never the host. The Docker provisioner owns application and cleanup.
"""

from ipaddress import IPv4Address
import re

from .network import ACCOUNT_PROXY, DNS_AGENT, DNS_BROWSER, TCP_AGENT, TCP_BROWSER, NON_PUBLIC_V4, FAKE_IP_RANGE, public_ipv4


def interface(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", value):
        raise ValueError("Invalid network interface")
    return value


def gateway_rules(*, agent_interface, browser_interface, proxy_address, proxy_port, agent_api=None):
    """iptables argv suffixes, applied only within the dedicated gateway netns.

    Redirect consumes TCP locally; forwarding is disabled even while the proxy
    process is down. Distinct interfaces select immutable browser/agent classes.
    DNS to any IPv4 address is intercepted; all remaining UDP is rejected.
    """
    agent_interface, browser_interface = interface(agent_interface), interface(browser_interface)
    if agent_interface == browser_interface:
        raise ValueError("Browser and agent networks must have separate interfaces")
    proxy_address = public_ipv4(proxy_address)
    if type(proxy_port) is not int or not 1 <= proxy_port <= 65535:
        raise ValueError("Invalid proxy port")
    rules = [
        ["-P", "FORWARD", "DROP"],
        ["-P", "INPUT", "DROP"],
        # Replies to the gateway's explicitly permitted private services must
        # reach its sockets; destination checks below apply to new requests.
        ["-t", "mangle", "-A", "PREROUTING", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ["-A", "INPUT", "-i", "lo", "-j", "ACCEPT"],
        ["-A", "INPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
    ]
    for device, tcp, dns in ((agent_interface, TCP_AGENT, DNS_AGENT), (browser_interface, TCP_BROWSER, DNS_BROWSER)):
        # Reject forbidden literal destinations before a transparent TCP listener
        # could even complete their handshake. Synthetic DNS addresses must pass
        # through to sing-box for name resolution and the second private-IP check.
        for protocol in ("tcp", "udp"):
            rules.append(["-t", "mangle", "-A", "PREROUTING", "-i", device, "-p", protocol,
                          "--dport", "53", "-j", "ACCEPT"])
        rules.append(["-t", "mangle", "-A", "PREROUTING", "-i", device, "-p", "udp", "-j", "DROP"])
        if device == agent_interface:
            rules.append(["-t", "mangle", "-A", "PREROUTING", "-i", device, "-p", "tcp",
                          "--dport", str(ACCOUNT_PROXY), "-m", "addrtype", "--dst-type", "LOCAL", "-j", "ACCEPT"])
            if agent_api:
                rules.append(["-t", "mangle", "-A", "PREROUTING", "-i", device, "-p", "tcp",
                              "-d", str(IPv4Address(agent_api[0])), "--dport", str(agent_api[1]), "-j", "ACCEPT"])
        rules.extend(["-t", "mangle", "-A", "PREROUTING", "-i", device, "-d", network, "-j", "DROP"]
                     for network in NON_PUBLIC_V4 if network != FAKE_IP_RANGE)
        # Intercept attempts to use a different DNS resolver too, including TCP.
        for protocol in ("tcp", "udp"):
            rules.append(["-t", "nat", "-A", "PREROUTING", "-i", device, "-p", protocol,
                          "--dport", "53", "-j", "REDIRECT", "--to-ports", str(dns)])
        # An explicit local account proxy is convenient for trusted host helpers.
        if device == agent_interface:
            rules.append(["-t", "nat", "-A", "PREROUTING", "-i", device, "-p", "tcp",
                          "--dport", str(ACCOUNT_PROXY), "-m", "addrtype", "--dst-type", "LOCAL", "-j", "RETURN"])
        rules.append(["-t", "nat", "-A", "PREROUTING", "-i", device, "-p", "tcp",
                      "-j", "REDIRECT", "--to-ports", str(tcp)])
        rules.append(["-A", "INPUT", "-i", device, "-p", "tcp", "-m", "multiport", "--dports",
                      f"{tcp},{dns}" + (f",{ACCOUNT_PROXY}" if device == agent_interface else ""), "-j", "ACCEPT"])
        rules.append(["-A", "INPUT", "-i", device, "-p", "udp", "--dport", str(dns), "-j", "ACCEPT"])
    rules += [
        ["-A", "OUTPUT", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ["-A", "OUTPUT", "-o", "lo", "-j", "ACCEPT"],
        ["-A", "OUTPUT", "-d", proxy_address, "-p", "tcp", "--dport", str(proxy_port), "-j", "ACCEPT"],
    ]
    if agent_api:
        address, port = agent_api
        address_ip = IPv4Address(address)
        if (not address_ip.is_private or address_ip.is_loopback or address_ip.is_link_local
                or address_ip.is_unspecified or address_ip.is_reserved or address_ip.is_multicast):
            raise ValueError("Agent listener must use a private bridge address")
        address = str(address_ip)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid agent listener port")
        rules.append(["-A", "OUTPUT", "-d", address, "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"])
    rules.extend(["-A", "OUTPUT", "-d", network, "-j", "REJECT"] for network in NON_PUBLIC_V4)
    rules += [["-A", "OUTPUT", "-p", "tcp", "-j", "ACCEPT"], ["-P", "OUTPUT", "DROP"]]
    return rules


def host_bridge_rules(bridge, *, internal=True, agent_api_port=None):
    """Rules appended to task-owned chains reached before Docker's allow rules.

    Permit only same-bridge Ethernet delivery internally: the container's route
    targets the gateway's MAC, while its IP destination remains the internet IP.
    Anything routed via the Docker host to another interface is denied.
    """
    bridge = interface(bridge)
    inputs = [["-i", bridge, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"]]
    if agent_api_port is not None:
        if type(agent_api_port) is not int or not 1 <= agent_api_port <= 65535:
            raise ValueError("Invalid agent listener port")
        inputs.append(["-i", bridge, "-p", "tcp", "--dport", str(agent_api_port), "-j", "ACCEPT"])
    inputs.append(["-i", bridge, "-j", "DROP"])
    forwards = []
    if internal:
        forwards += [["-i", bridge, "-o", bridge, "-j", "ACCEPT"], ["-i", bridge, "-j", "DROP"],
                     ["-o", bridge, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
                     ["-o", bridge, "-j", "DROP"]]
    else:
        forwards.append(["-i", bridge, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])
        forwards += [["-i", bridge, "-d", network, "-j", "DROP"] for network in NON_PUBLIC_V4]
        # This bridge contains only the trusted gateway. Its userspace router
        # needs public TCP for approved direct services and the assigned proxy.
        # No workload may ever be connected to this network by the provisioner.
        forwards += [["-i", bridge, "-p", "tcp", "-j", "ACCEPT"], ["-i", bridge, "-j", "DROP"]]
        forwards += [["-o", bridge, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
                     ["-o", bridge, "-j", "DROP"]]
    return {"INPUT": inputs, "DOCKER-USER": forwards}
