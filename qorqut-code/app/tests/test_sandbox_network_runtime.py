"""Opt-in Linux/Docker acceptance: ordinary sockets behind the real gateway.

QORQUT_TEST_BUSINESS_NETWORK=1. Optional real assigned proxy compatibility uses
QORQUT_TEST_PROXY_BUSINESS=<slug>, QORQUT_TEST_PROXY_ROOT=<existing root>. Those
records are opened read-only; no browser, account, profile or service is changed.
Temporary firewall rules affect only bridges created by this test invocation.
"""

from contextlib import contextmanager
import json
from ipaddress import IPv4Address, IPv4Network
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import time

import pytest

from bazarlyk.browser.service import upstream_url
from bazarlyk.sandbox.firewall import gateway_rules, host_bridge_rules
from bazarlyk.sandbox.network import ProxyEndpoint, RoutingPolicy, SING_BOX_IMAGE, gateway_config


pytestmark = pytest.mark.skipif(os.environ.get("QORQUT_TEST_BUSINESS_NETWORK") != "1",
                              reason="Explicit disposable Docker/network test only")
WORKLOAD_IMAGE = os.environ.get("QORQUT_TEST_WORKSTATION_IMAGE", "qorqut-sandbox-agent-eval:github")


def run(*args, input=None, timeout=90, check=True):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        # argv and stdout may contain private configuration, never echo them.
        raise AssertionError(f"Network fixture command failed: {args[0]}, exit {result.returncode}; {result.stderr[-1500:]}")
    return result.stdout.strip() if check else result


def docker(*args, **kw):
    return run("sudo", "-n", "docker", *args, **kw)


class Environment:
    def __init__(self, directory):
        self.directory = directory
        self.name = "qbs-net-" + secrets.token_hex(5)
        self.networks, self.containers, self.chains = [], [], []
        self.gateway = None

    def network(self, suffix, *, internal):
        name = self.name + "-" + suffix
        args = ["network", "create", "--label", "qorqut.test=" + self.name]
        if internal:
            args.append("--internal")
        identity = docker(*args, name)
        self.networks.append(name)
        bridge = "br-" + identity[:12]
        network = json.loads(docker("network", "inspect", name))[0]
        host = network["IPAM"]["Config"][0]["Gateway"]
        for parent, rules in host_bridge_rules(bridge, internal=internal).items():
            chain = "QBS" + secrets.token_hex(7)
            run("sudo", "-n", "iptables", "-w", "-N", chain)
            # Save cleanup ownership immediately after creation.
            self.chains.append((parent, chain))
            for rule in rules:
                run("sudo", "-n", "iptables", "-w", "-A", chain, *rule)
            run("sudo", "-n", "iptables", "-w", "-I", parent, "1", "-j", chain)
        return name, host

    def container(self, image, network, *args, command=("sleep", "infinity")):
        name = self.name + "-" + secrets.token_hex(3)
        container = docker("create", "--name", name, "--label", "qorqut.test=" + self.name,
            "--network", network, "--memory", "256m", "--cpus", "1", "--pids-limit", "64",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "1000:1000",
            "--sysctl", "net.ipv6.conf.all.disable_ipv6=1", "--sysctl", "net.ipv6.conf.default.disable_ipv6=1",
            *args, image, *command)
        self.containers.append(container)
        docker("start", container)
        return container

    def address(self, container, network):
        return json.loads(docker("inspect", container))[0]["NetworkSettings"]["Networks"][network]["IPAddress"]

    def ns(self, container, *args):
        pid = json.loads(docker("inspect", container))[0]["State"]["Pid"]
        assert pid > 0
        return run("sudo", "-n", "nsenter", "-t", str(pid), "-n", "--", *args)

    def setup(self, proxy, policy, configure_fixture=None):
        self.agent_network, self.agent_host = self.network("agent", internal=True)
        self.browser_network, self.browser_host = self.network("browser", internal=True)
        self.external_network, _ = self.network("egress", internal=False)
        config = self.directory / "gateway.json"
        config.write_text(json.dumps(gateway_config(policy, proxy)))
        config.chmod(0o600)
        self.gateway = self.container(SING_BOX_IMAGE, self.external_network,
            "--entrypoint", "/bin/sleep", "--mount", f"type=bind,src={config},dst=/etc/sing-box/config.json,readonly",
            command=("infinity",))
        for network in (self.agent_network, self.browser_network):
            docker("network", "connect", network, self.gateway)
        agent_gateway = self.address(self.gateway, self.agent_network)
        browser_gateway = self.address(self.gateway, self.browser_network)
        self.agent_gateway, self.browser_gateway = agent_gateway, browser_gateway
        addresses = json.loads(self.ns(self.gateway, "ip", "-j", "address"))
        def device(address):
            return next(row["ifname"] for row in addresses if any(a.get("local") == address for a in row["addr_info"]))
        for rule in gateway_rules(agent_interface=device(agent_gateway), browser_interface=device(browser_gateway),
                                  proxy_address=proxy.address, proxy_port=proxy.port):
            self.ns(self.gateway, "iptables", "-w", *rule)
        self.agent = self.workload(self.agent_network, agent_gateway)
        self.browser = self.workload(self.browser_network, browser_gateway)
        if configure_fixture:
            value = json.loads(config.read_text())
            configure_fixture(self, value)
            config.write_text(json.dumps(value))
        docker("exec", "-d", self.gateway, "sing-box", "run", "-c", "/etc/sing-box/config.json")
        # Do not use a readiness connection that itself leaks DNS or uses host proxies.
        for _ in range(100):
            if ":3A99" in docker("exec", self.gateway, "cat", "/proc/net/tcp"):
                return self
            time.sleep(.05)
        pytest.fail("Gateway did not listen")

    def workload(self, network, gateway):
        resolver = self.directory / (network + ".resolv.conf")
        resolver.write_text(f"nameserver {gateway}\noptions timeout:2 attempts:1\n")
        container = self.container(WORKLOAD_IMAGE, network,
            "--mount", f"type=bind,src={resolver},dst=/etc/resolv.conf,readonly")
        self.ns(container, "ip", "route", "replace", "default", "via", gateway)
        return container

    def python(self, container, code, *args, check=True):
        return docker("exec", container, "python3", "-c", code, *args, check=check)

    def close(self):
        # Retain containment if a container could not be stopped.
        for container in reversed(self.containers):
            docker("rm", "-f", container, timeout=180)
        for parent, chain in reversed(self.chains):
            run("sudo", "-n", "iptables", "-w", "-D", parent, "-j", chain, check=False)
            run("sudo", "-n", "iptables", "-w", "-F", chain)
            run("sudo", "-n", "iptables", "-w", "-X", chain)
        for network in reversed(self.networks):
            docker("network", "rm", network)


@contextmanager
def environment(tmp_path, proxy, policy=RoutingPolicy(), *, configure_fixture=None):
    tmp_path.mkdir()
    env = Environment(tmp_path)
    try:
        yield env.setup(proxy, policy, configure_fixture)
    finally:
        env.close()


def assigned_proxy():
    slug = os.environ.get("QORQUT_TEST_PROXY_BUSINESS")
    if not slug:
        pytest.skip("Explicit existing proxy assignment required for compatibility test")
    root = Path(os.environ["QORQUT_TEST_PROXY_ROOT"])
    with sqlite3.connect(f"file:{root}/.bazarlyk/state.sqlite3?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT id, config_json FROM businesses WHERE slug=?", (slug,)).fetchone()
    assert row, "Selected business is not registered"
    browser = json.loads(row[1]).get("browser")
    assert browser and browser["enabled"], "Selected business has no enabled assignment"
    url = upstream_url(root, row[0], browser)
    from urllib.parse import urlsplit
    address = socket.getaddrinfo(urlsplit(url).hostname, None, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    return ProxyEndpoint.from_url(url, resolved_address=address)


FETCH = "from urllib.request import build_opener,ProxyHandler; import sys; print(build_opener(ProxyHandler({})).open(sys.argv[1],timeout=20).read().decode())"
CONNECT = "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex((sys.argv[1],int(sys.argv[2]))) else 1)"
FAIL_FETCH = FETCH.replace("timeout=20", "timeout=3")
RAW_FETCH = """
import socket,ssl,http.client,sys
raw=socket.create_connection((sys.argv[1],443),timeout=15)
s=ssl.create_default_context().wrap_socket(raw,server_hostname='api.ipify.org')
s.sendall(b'GET / HTTP/1.1\\r\\nHost: api.ipify.org\\r\\nConnection: close\\r\\n\\r\\n')
r=http.client.HTTPResponse(s); r.begin(); assert r.status==200; print(r.read().decode())
"""
DNS_QUERY = """
import socket,struct,sys
q=b'\\x01\\x03\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00'+b''.join(bytes([len(v)])+v.encode() for v in 'api.ipify.org'.split('.'))+b'\\x00\\x00\\x01\\x00\\x01'
s=socket.socket(socket.AF_INET, socket.SOCK_STREAM if sys.argv[1]=='tcp' else socket.SOCK_DGRAM); s.settimeout(3)
s.connect(('8.8.8.8',53))
if sys.argv[1]=='tcp':
    s.sendall(struct.pack('!H',len(q))+q); n=struct.unpack('!H',s.recv(2))[0]; data=b''
    while len(data)<n: data+=s.recv(n-len(data))
else:
    s.send(q); data=s.recv(2048)
assert data[:2]==q[:2]; print(socket.inet_ntoa(data[-4:]))
"""


def test_real_assignment_routes_and_fails_closed(tmp_path):
    proxy = assigned_proxy()
    policy = RoutingPolicy(direct_domains=("api.ipify.org", "example.com"))
    with environment(tmp_path / "business", proxy, policy) as env:
        proxied = env.python(env.browser, FETCH, "https://api.ipify.org").strip()
        direct = env.python(env.agent, FETCH, "https://api.ipify.org").strip()
        assert proxied != direct, "Assigned proxy did not produce a distinct exit"
        assert env.python(env.browser, FETCH, "https://api.ipify.org").strip() == proxied, "Proxy exit changed unexpectedly"
        raw_address = socket.getaddrinfo("api.ipify.org", 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        assert env.python(env.agent, RAW_FETCH, raw_address) == proxied, "A forged Host/SNI or raw IP selected the direct route"
        account_fetch = FETCH.replace("ProxyHandler({})", "ProxyHandler({'https':'http://" + env.agent_gateway + ":18080'})")
        assert env.python(env.agent, account_fetch, "https://api.ipify.org").strip() == proxied
        for workload in (env.agent, env.browser):
            for protocol in ("udp", "tcp"):
                result = env.python(workload, DNS_QUERY, protocol)
                assert IPv4Address(result) in IPv4Network("198.18.0.0/15"), "Alternate DNS resolver bypassed interception"
            assert env.ns(workload, "ip", "-6", "route") == ""
            assert env.python(workload, "import socket; s=socket.socket(socket.AF_INET6); s.settimeout(2); s.connect(('2606:4700:4700::1111',443))", check=False).returncode != 0
        before = env.ns(env.gateway, "iptables-save", "-c", "-t", "mangle")
        env.python(env.agent, "import socket; socket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto(b'blocked-udp',('1.1.1.1',443))")
        after = env.ns(env.gateway, "iptables-save", "-c", "-t", "mangle")
        def udp_drops(value):
            return sum(int(line.split(':')[0][1:]) for line in value.splitlines() if '-p udp -j DROP' in line)
        assert udp_drops(after) > udp_drops(before), "Unsupported UDP did not reach the mandatory drop rule"
        # Browser cannot opt into the agent redirect listener or account helper.
        for port in (15001, 15053, 18080):
            assert env.python(env.browser, CONNECT, env.browser_gateway, str(port), check=False).returncode == 0
        # A new gateway with another isolated workspace, no proxy credential mount.
        with environment(tmp_path / "foreign", proxy) as other:
            peer = other.address(other.agent, other.agent_network)
            docker("exec", "-d", other.agent, "python3", "-m", "http.server", "8123")
            for _ in range(20):
                ready = other.python(other.agent, CONNECT, "127.0.0.1", "8123", check=False)
                if ready.returncode == 1:
                    break
                time.sleep(.05)
            else:
                pytest.fail("Private peer positive control did not listen")
            assert env.python(env.agent, CONNECT, peer, "8123", check=False).returncode == 0
            assert env.python(env.agent, FAIL_FETCH, f"http://{peer}:8123", check=False).returncode != 0
        for host in (env.agent_host, env.browser_host, "169.254.169.254", "100.64.12.45"):
            assert env.python(env.agent, CONNECT, host, "1049", check=False).returncode == 0
        # Proxy credential and host control sockets are absent from both workloads.
        for workload in (env.agent, env.browser):
            docker("exec", workload, "test", "!", "-e", "/etc/sing-box/config.json")
            docker("exec", workload, "test", "!", "-e", "/var/run/docker.sock")
            assert env.python(workload, "import socket; socket.socket(socket.AF_INET,socket.SOCK_RAW,socket.IPPROTO_ICMP)", check=False).returncode != 0
        # The provider failing must not reroute browsing; direct development still works.
        env.ns(env.gateway, "iptables", "-I", "OUTPUT", "1", "-d", proxy.address, "-j", "REJECT")
        assert env.python(env.browser, FAIL_FETCH, "https://api.ipify.org", check=False).returncode != 0
        assert env.python(env.agent, FETCH, "https://api.ipify.org").strip() == direct
        # Uncached approved DNS must also work without any dependency on proxy DNS.
        assert "Example Domain" in env.python(env.agent, FETCH, "https://example.com")
        env.ns(env.gateway, "iptables", "-D", "OUTPUT", "-d", proxy.address, "-j", "REJECT")
        # A process crash must fail closed even though the gateway namespace stays up.
        docker("exec", env.gateway, "/bin/sh", "-c", "kill $(pidof sing-box)")
        for workload in (env.agent, env.browser):
            assert env.python(workload, RAW_FETCH.replace("timeout=15", "timeout=3"), raw_address, check=False).returncode != 0
        docker("stop", "-t", "2", env.gateway, timeout=180)
        for workload in (env.agent, env.browser):
            assert env.python(workload, FAIL_FETCH, "https://api.ipify.org", check=False).returncode != 0


DNS_SERVER = """
import socket,struct,pathlib
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(('0.0.0.0',1053))
pathlib.Path('/tmp/dns-ready').touch()
while True:
    packet,peer=s.recvfrom(2048)
    offset=12
    while packet[offset]: offset+=packet[offset]+1
    offset+=1
    question=packet[12:offset+4]; kind=struct.unpack('!H',packet[offset:offset+2])[0]
    answer=(b'\\xc0\\x0c'+struct.pack('!HHIH',1,1,0,4)+socket.inet_aton(pathlib.Path('/tmp/dns-answer').read_text())) if kind==1 else b''
    s.sendto(packet[:2]+struct.pack('!HHHHH',0x8180,1,int(bool(answer)),0,0)+question+answer,peer)
"""


def test_dns_rebinding_does_not_open_private_services(tmp_path):
    proxy = assigned_proxy()
    public = socket.getaddrinfo("api.ipify.org", 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]

    def configure(env, value):
        private = env.address(env.agent, env.agent_network)
        env.python(env.agent, "from pathlib import Path; import sys; Path('/tmp/dns-answer').write_text(sys.argv[1]); Path('/tmp/private-canary').write_text('private-service-canary')", public)
        docker("exec", "-d", env.agent, "python3", "-c", DNS_SERVER)
        docker("exec", "-d", env.agent, "python3", "-m", "http.server", "8123", "--directory", "/tmp")
        for _ in range(40):
            if docker("exec", env.agent, "test", "-e", "/tmp/dns-ready", check=False).returncode == 0:
                break
            time.sleep(.05)
        else:
            pytest.fail("Controlled DNS positive control did not start")
        for _ in range(40):
            if env.python(env.agent, CONNECT, "127.0.0.1", "8123", check=False).returncode == 1:
                break
            time.sleep(.1)
        else:
            pytest.fail("Private service positive control did not start")
        # Replace only the test's upstream DNS transport, with zero-TTL answers
        # under fixture control. All routing/private-address rules remain real.
        value["dns"]["disable_cache"] = True
        value["dns"]["servers"] = [value["dns"]["servers"][0],
            {"type": "udp", "tag": "proxy-dns", "server": private, "server_port": 1053},
            {"type": "udp", "tag": "direct-dns", "server": private, "server_port": 1053}]
        env.ns(env.gateway, "iptables", "-I", "OUTPUT", "1", "-d", private, "-p", "udp", "--dport", "1053", "-j", "ACCEPT")

    policy = RoutingPolicy(direct_domains=("rebind.sandbox.test",), direct_ports=(443, 8123))
    with environment(tmp_path / "rebinding", proxy, policy, configure_fixture=configure) as env:
        assert env.python(env.agent, FETCH, "http://127.0.0.1:8123/private-canary").strip() == "private-service-canary"
        # Start with a usable public destination, then rebind that same name.
        agent_echo = env.python(env.agent, RAW_FETCH, "rebind.sandbox.test")
        browser_echo = env.python(env.browser, RAW_FETCH, "rebind.sandbox.test")
        IPv4Address(agent_echo)
        # The fixture models two names hosted by one public endpoint: the
        # approved DNS name must not lend direct routing to a different SNI.
        assert agent_echo == browser_echo, "An unapproved virtual host borrowed an approved development DNS route"
        private = env.address(env.agent, env.agent_network)
        env.python(env.agent, "from pathlib import Path; import sys; Path('/tmp/dns-answer').write_text(sys.argv[1])", private)
        # A proxied private destination must not even be forwarded to the provider.
        env.ns(env.gateway, "iptables", "-I", "OUTPUT", "1", "-d", proxy.address, "-p", "tcp", "--dport", str(proxy.port), "-j", "ACCEPT")
        for workload in (env.agent, env.browser):
            for name in ("rebind.sandbox.test", "account.sandbox.test"):
                assert env.python(workload, FAIL_FETCH, f"http://{name}:8123/private-canary", check=False).returncode != 0
        rules = env.ns(env.gateway, "iptables-save", "-c")
        selected = [line for line in rules.splitlines() if f"-A OUTPUT -d {proxy.address}/32 -p tcp" in line and f"--dport {proxy.port} " in line]
        assert selected and selected[0].startswith("[0:0]"), "Private DNS answer was forwarded to upstream proxy"
