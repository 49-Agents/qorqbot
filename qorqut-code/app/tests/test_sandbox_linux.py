import subprocess

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.sandbox.docker import Ownership
from bazarlyk.sandbox.linux import LinuxNetwork


OWNER = Ownership("a" * 20, "00000000-0000-4000-8000-000000000001")
IDENTITY = "b" * 64


def test_namespace_uses_exact_docker_namespace_without_a_recyclable_pid():
    calls = []
    class Inventory:
        def owned(self, kind, identity, owner):
            assert (kind, identity, owner) == ("container", IDENTITY, OWNER)
            return {"State": {"Running": True, "Pid": 99}, "NetworkSettings": {"SandboxKey": "/var/run/docker/netns/abcdef"}}
    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "ok", "")
    network = LinuxNetwork(Inventory(), command=("sudo", "-n"), runner=runner)
    assert network.namespace(IDENTITY, OWNER, "ip", "route") == "ok"
    assert calls == [("sudo", "-n", "nsenter", "--net=/var/run/docker/netns/abcdef", "--", "ip", "route")]


@pytest.mark.parametrize("path", ["/proc/1/ns/net", "/var/run/docker/netns/../../host", "", "/run/netns/default"])
def test_namespace_refuses_a_host_or_unregistered_namespace(path):
    class Inventory:
        def owned(self, *args):
            return {"State": {"Running": True, "Pid": 1}, "NetworkSettings": {"SandboxKey": path}}
    network = LinuxNetwork(Inventory(), runner=lambda *args, **kwargs: pytest.fail("Must not enter this namespace"))
    with pytest.raises(DomainError, match="namespace"):
        network.namespace(IDENTITY, OWNER, "iptables", "-F")


def test_bridge_reconciliation_refuses_to_overwrite_an_unrelated_chain():
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "-A collision -i tailscale0 -j ACCEPT\n", "")
    network = LinuxNetwork(None, runner=runner)
    with pytest.raises(DomainError, match="ownership"):
        network.protect_bridge(IDENTITY, internal=True)
    assert len(calls) == 1


def test_bridge_removal_resumes_after_one_chain_was_already_deleted():
    calls = []
    chains = LinuxNetwork._chains(IDENTITY)
    def runner(args, **kwargs):
        calls.append(args)
        if args[2] == "-S":
            if args[3] == chains["INPUT"]:
                return subprocess.CompletedProcess(args, 1, "", "iptables: No chain/target/match by that name.\n")
            return subprocess.CompletedProcess(args, 0, f"-A {args[3]} -i br-{IDENTITY[:12]} -j DROP\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")
    LinuxNetwork(None, runner=runner).remove_bridge(IDENTITY)
    assert sum(chains["INPUT"] in args for args in calls) == 1
    assert [args[2] for args in calls] == ["-S", "-S", "-C", "-D", "-F", "-X"]


@pytest.mark.parametrize("status,stdout,stderr", [
    (4, "", "Resource temporarily unavailable"),
    (1, "", "Permission denied"),
    (0, "-A unrelated -i tailscale0 -j ACCEPT\n", ""),
])
def test_bridge_removal_preserves_rules_on_unknown_inventory_or_ownership(status, stdout, stderr):
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, status, stdout, stderr)
    with pytest.raises(DomainError):
        LinuxNetwork(None, runner=runner).remove_bridge(IDENTITY)
    assert len(calls) == 1 and calls[0][2] == "-S"
