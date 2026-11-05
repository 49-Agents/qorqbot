"""Bounded host operations on explicitly owned Docker network namespaces."""

import hashlib
import json
import re
import subprocess

from qorqut_operations.contracts import DomainError
from .firewall import gateway_rules, host_bridge_rules


class LinuxNetwork:
    def __init__(self, docker, *, command=(), runner=subprocess.run):
        self.docker, self.command, self.runner = docker, tuple(command), runner

    def run(self, *args, input=None, check=True):
        try:
            result = self.runner((*self.command, *args), input=input, text=True,
                                 capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            raise DomainError("environment_unavailable", "Sandbox network administration is unavailable") from None
        if check and result.returncode:
            raise DomainError("environment_unavailable", "Sandbox network configuration failed")
        return result

    def _namespace(self, identity, ownership):
        current = self.docker.owned("container", identity, ownership)
        namespace = current and current.get("NetworkSettings", {}).get("SandboxKey", "")
        if (not re.fullmatch(r"[0-9a-f]{64}", identity) or not namespace
                or not re.fullmatch(r"/(?:var/)?run/docker/netns/[0-9a-f]+", namespace)
                or not current.get("State", {}).get("Running")):
            raise DomainError("environment_unavailable", "Sandbox namespace is not running")
        # The exact Docker-owned namespace path cannot become an unrelated host
        # process after PID reuse. A removed namespace fails instead of falling
        # back to the caller's host namespace.
        return namespace

    def namespace(self, identity, ownership, *args):
        return self.run("nsenter", "--net=" + self._namespace(identity, ownership), "--", *args).stdout.strip()

    @staticmethod
    def _chains(network_id):
        if not re.fullmatch(r"[0-9a-f]{64}", network_id):
            raise DomainError("environment_conflict", "Network needs its exact Docker identity")
        stem = "QBS" + hashlib.sha256(network_id.encode()).hexdigest()[:20]
        return {"INPUT": stem + "I", "DOCKER-USER": stem + "F"}

    def protect_bridge(self, network_id, *, internal, agent_api_port=None):
        """Replace only our two chains atomically, before starting any workload.

        Every chain rule is scoped to the bridge with this immutable Docker ID.
        The host's policies, Docker chains and unrelated firewall rules stay intact.
        A crash before the jump is installed leaves no started workload to expose.
        """
        chains = self._chains(network_id)
        bridge = "br-" + network_id[:12]
        plans = host_bridge_rules(bridge, internal=internal, agent_api_port=agent_api_port)
        for parent, chain in chains.items():
            found = self.run("iptables", "-w", "-S", chain, check=False)
            if found.returncode == 0:
                # Do not replace an unexpected chain merely because its name
                # happens to match. Prior complete/partial plans bind this bridge.
                lines = [line.split() for line in found.stdout.splitlines() if line.startswith("-A ")]
                if any(bridge not in line for line in lines):
                    raise DomainError("environment_conflict", "Sandbox firewall chain ownership conflicts")
            elif "No chain/target/match by that name" not in found.stderr and "does not exist" not in found.stderr:
                raise DomainError("environment_unavailable", "Sandbox firewall inventory is unavailable")
            commands = ["*filter", f":{chain} - [0:0]", f"-F {chain}"]
            commands.extend(" ".join(["-A", chain, *rule]) for rule in plans[parent])
            self.run("iptables-restore", "--noflush", "--wait", input="\n".join([*commands, "COMMIT", ""]))
            jump = (parent, "-m", "comment", "--comment", "qorqut:" + network_id, "-j", chain)
            exists = self.run("iptables", "-w", "-C", *jump, check=False)
            if exists.returncode == 1:
                self.run("iptables", "-w", "-I", parent, "1", *jump[1:])
            elif exists.returncode:
                raise DomainError("environment_unavailable", "Sandbox firewall attachment could not be verified")

    def remove_bridge(self, network_id):
        """Caller has already verified that every owned container is stopped."""
        for parent, chain in self._chains(network_id).items():
            found = self.run("iptables", "-w", "-S", chain, check=False)
            if found.returncode:
                # A prior interrupted removal may already have deleted this
                # chain. nftables -C then returns 2, not the absent-rule code 1.
                # Prove exact chain absence first; other inventory failures deny.
                if (found.returncode == 1 and found.stderr.strip()
                        == "iptables: No chain/target/match by that name."):
                    continue
                raise DomainError("environment_unavailable", "Sandbox firewall inventory is unavailable")
            bridge = "br-" + network_id[:12]
            if any(bridge not in line.split() for line in found.stdout.splitlines() if line.startswith("-A ")):
                raise DomainError("environment_conflict", "Sandbox firewall chain ownership conflicts")
            jump = (parent, "-m", "comment", "--comment", "qorqut:" + network_id, "-j", chain)
            exists = self.run("iptables", "-w", "-C", *jump, check=False)
            if exists.returncode == 0:
                self.run("iptables", "-w", "-D", *jump)
            elif exists.returncode != 1:
                raise DomainError("environment_unavailable", "Sandbox firewall removal could not be verified")
            self.run("iptables", "-w", "-F", chain)
            self.run("iptables", "-w", "-X", chain)

    def configure_gateway(self, identity, ownership, *, agent_address, browser_address, proxy, agent_api):
        addresses = json.loads(self.namespace(identity, ownership, "ip", "-j", "address"))
        def device(address):
            matches = [row["ifname"] for row in addresses if any(a.get("local") == address for a in row["addr_info"])]
            if len(matches) != 1:
                raise DomainError("environment_conflict", "Gateway network attachment is invalid")
            return matches[0]
        rules = gateway_rules(agent_interface=device(agent_address), browser_interface=device(browser_address),
                              proxy_address=proxy.address, proxy_port=proxy.port, agent_api=agent_api)
        # This is a dedicated, capability-free gateway, never the host netns.
        # A single atomic restore per table makes retries safe without growing
        # duplicate rules. The gateway userspace process has not been started yet.
        tables = {"filter": ["*filter", ":INPUT DROP [0:0]", ":FORWARD DROP [0:0]", ":OUTPUT DROP [0:0]"],
                  "mangle": ["*mangle", ":PREROUTING ACCEPT [0:0]"],
                  "nat": ["*nat", ":PREROUTING ACCEPT [0:0]"]}
        for rule in rules:
            if rule[0] == "-P":
                continue
            table, args = (rule[1], rule[2:]) if rule[0] == "-t" else ("filter", rule)
            tables[table].append(" ".join(args))
        self.run("nsenter", "--net=" + self._namespace(identity, ownership), "--", "iptables-restore", "--wait",
                 input="\n".join(line for value in tables.values() for line in [*value, "COMMIT", ""]))

    def route_workload(self, identity, ownership, gateway):
        self.namespace(identity, ownership, "ip", "route", "replace", "default", "via", gateway)
