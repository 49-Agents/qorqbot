"""Reconcile a registered business's persistent Docker workstation.

Only SandboxLifecycle calls this driver, under its host lock and committed
generation lease. Neither MCP arguments nor business-authored files supply
container options. Readiness is an explicit Qorqut integration port, not an
assumption that a successful `docker start` means business provisioning finished.
"""

from dataclasses import dataclass
import hashlib
import json
import re
import socket
import stat
from urllib.parse import urlsplit

from bazarlyk.bootstrap.client import path_has_symlink
from bazarlyk.browser.config import legacy_target as configured_target
from bazarlyk.browser.service import upstream_url
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage import safe_io
from qorqut_operations.storage.safe_io import atomic_write
from .terminal import environment_fingerprint
from .docker import Docker, Ownership
from .lifecycle import EnvironmentResult
from .linux import LinuxNetwork
from .network import ProxyEndpoint, RoutingPolicy, SING_BOX_IMAGE, gateway_config


@dataclass(frozen=True)
class RunningEnvironment:
    ownership: Ownership
    generation: int
    workstation_id: str
    browser_id: str
    gateway_id: str
    agent_address: str
    browser_address: str
    agent_gateway: str
    browser_gateway: str
    agent_api_url: str


_PROXY_PIN_VERSION = 1
_PROXY_PIN_LIMIT = 4096


def _proxy_assignment(business, paths):
    """Hash the configured, credential-free routing identity for one business.

    The digest covers proxy URL, credential reference, and browser identity but
    never credential-file bytes. It is an integrity binding, not a secret store.
    """
    if not business.get("config_json", {}).get("browser", {}).get("enabled"):
        raise DomainError("configuration_missing", "Business proxy assignment is required")
    target = configured_target(business, paths.instance)
    assignment = {"business_id": paths.business_id,
                  "proxy_server": target["proxy_server"],
                  "credentials_ref": target["credentials_ref"],
                  "browser_generation": target["generation"]}
    digest = hashlib.sha256(json.dumps(assignment, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return target, digest


def _proxy_pin_path(paths):
    return paths.control / "proxy-assignment.json"


def _read_proxy_pin(paths):
    """Read one descriptor-safe host control record; never resolve DNS here."""
    path = _proxy_pin_path(paths)
    try:
        raw = safe_io.read_bytes(path, limit=_PROXY_PIN_LIMIT, private=True)
    except FileNotFoundError:
        return None
    except (OSError, DomainError):
        raise DomainError("environment_conflict", "Business proxy assignment record is unavailable") from None
    try:
        value = json.loads(raw.decode("utf-8"))
        if set(value) != {"schema_version", "generation", "assignment_sha256", "endpoint"}:
            raise ValueError
        if value["schema_version"] != _PROXY_PIN_VERSION or type(value["generation"]) is not int or value["generation"] < 1:
            raise ValueError
        if not isinstance(value["assignment_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["assignment_sha256"]):
            raise ValueError
        endpoint = value["endpoint"]
        if not isinstance(endpoint, dict) or set(endpoint) != {"protocol", "address", "port", "server_name"}:
            raise ValueError
        # Validate only public, credential-free endpoint fields before returning.
        ProxyEndpoint(endpoint["protocol"], endpoint["address"], endpoint["port"], endpoint["server_name"])
    except (UnicodeDecodeError, ValueError, TypeError, KeyError):
        raise DomainError("environment_conflict", "Business proxy assignment record is invalid") from None
    return value


def _pinned_proxy(business, paths, *, generation=None):
    target, assignment_hash = _proxy_assignment(business, paths)
    pin = _read_proxy_pin(paths)
    if pin is None:
        raise DomainError("environment_conflict", "Business proxy assignment is not pinned for this generation")
    if pin["assignment_sha256"] != assignment_hash or generation is not None and pin["generation"] != generation:
        raise DomainError("environment_conflict", "Business proxy assignment requires a replacement generation")
    # Credentials stay in their dedicated mode-0600 reference.  The pinned
    # record contains only the DNS-selected public endpoint, never secrets.
    url = upstream_url(paths.instance, paths.business_id, target)
    try:
        endpoint = pin["endpoint"]
        proxy = ProxyEndpoint.from_url(url, resolved_address=endpoint["address"])
        if (proxy.protocol, proxy.address, proxy.port, proxy.server_name) != (
                endpoint["protocol"], endpoint["address"], endpoint["port"], endpoint["server_name"]):
            raise ValueError
        return proxy
    except (ValueError, TypeError):
        raise DomainError("environment_conflict", "Business proxy assignment no longer matches its pinned endpoint") from None


def provision_assigned_proxy(business, paths, *, generation):
    """Resolve and persist one endpoint only for an initial/replacement generation.

    SandboxLifecycle holds its installation shared lock and per-business lifecycle
    lock around this call.  Same-generation retries read the existing record;
    they never let a changed DNS answer alter a running gateway's route.
    """
    if type(generation) is not int or generation < 1:
        raise DomainError("environment_conflict", "Business proxy generation is invalid")
    pin = _read_proxy_pin(paths)
    if pin is not None:
        if pin["generation"] == generation:
            return _pinned_proxy(business, paths, generation=generation)
        if pin["generation"] > generation:
            raise DomainError("environment_conflict", "Business proxy assignment belongs to a newer generation")
    target, assignment_hash = _proxy_assignment(business, paths)
    url = upstream_url(paths.instance, paths.business_id, target)
    try:
        hostname = urlsplit(url).hostname
        address = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        proxy = ProxyEndpoint.from_url(url, resolved_address=address)
    except (OSError, ValueError, TypeError):
        raise DomainError("environment_unavailable", "Business proxy assignment could not be resolved") from None
    record = {"schema_version": _PROXY_PIN_VERSION, "generation": generation,
              "assignment_sha256": assignment_hash,
              "endpoint": {"protocol": proxy.protocol, "address": proxy.address,
                           "port": proxy.port, "server_name": proxy.server_name}}
    try:
        safe_io.atomic_write(_proxy_pin_path(paths), json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n",
                             mode=0o600, enforce_mode=True)
    except (OSError, DomainError):
        raise DomainError("environment_conflict", "Business proxy assignment record could not be written") from None
    return proxy


def assigned_proxy(business, paths, *, generation=None):
    """Read a validated immutable assignment; this read path never calls DNS."""
    return _pinned_proxy(business, paths, generation=generation)


class DockerEnvironmentDriver:
    def __init__(self, *, readiness, docker=None, network=None, proxy_loader=assigned_proxy,
                 browser_runtime=None, before_stop=None):
        if not callable(readiness):
            raise TypeError("Registered agent/browser readiness verification is required")
        self.docker = docker or Docker()
        self.network = network or LinuxNetwork(self.docker)
        self.proxy_loader, self.readiness = proxy_loader, readiness
        self.browser_runtime, self.before_stop = browser_runtime, before_stop

    @staticmethod
    def ownership(paths):
        return Ownership(hashlib.sha256(str(paths.root).encode()).hexdigest()[:20], paths.business_id)

    def _inventory(self, kind, ownership):
        arguments = [kind, "ls", "--quiet", "--no-trunc"]
        if kind == "container":
            arguments.append("--all")
        for key, value in ownership.labels.items():
            arguments.extend(("--filter", "label=" + key + "=" + value))
        return [self.docker.owned(kind, identity, ownership) for identity in self.docker.call(*arguments).splitlines()]

    def _stop(self, ownership, containers, *, heartbeat=lambda: None, business=None, paths=None):
        # Stop every writer before removing even one old container. A failed or
        # timed-out stop preserves all containment and forbids a second writer.
        for current in containers:
            heartbeat()
            if self.before_stop and current and current["Config"]["Labels"].get("qorqut.role") == "browser":
                # An orderly profile flush is required for every stop path,
                # including old images and interrupted network reconstruction.
                if business is None or paths is None:
                    raise DomainError("environment_conflict", "Browser stop requires its registered workspace")
                self.before_stop(business, paths, current)
            if current and current["State"]["Running"]:
                self.docker.call("stop", "--time", "20", current["Id"], timeout=180)
        for current in containers:
            heartbeat()
            if current:
                observed = self.docker.owned("container", current["Id"], ownership)
                if observed and (observed["State"]["Running"] or observed["State"].get("Restarting")):
                    raise DomainError("environment_conflict", "Old business writers have not stopped")

    def _drain(self, ownership, containers, *, heartbeat=lambda: None, business=None, paths=None):
        self._stop(ownership, containers, heartbeat=heartbeat, business=business, paths=paths)
        for current in containers:
            heartbeat()
            if current and self.docker.owned("container", current["Id"], ownership):
                self.docker.call("rm", current["Id"], timeout=180)

    def stop(self, business, paths, *, heartbeat):
        """Stop exact owned writers, retaining volumes, containers and containment.

        Configuration may be missing during an emergency stop. No image, proxy,
        DNS lookup or replacement prerequisite is needed to stop existing writers.
        """
        ownership = self.ownership(paths)
        self._stop(ownership, self._inventory("container", ownership), heartbeat=heartbeat,
                   business=business, paths=paths)

    @staticmethod
    def _stopped(containers) -> bool:
        """Accept only fully stopped, non-paused inspected owned containers."""
        for current in containers:
            state = (current or {}).get("State", {})
            if (not isinstance(state, dict) or state.get("Running") is not False
                    or state.get("Paused") is not False or state.get("Restarting") is not False):
                return False
        return True

    def rollback_to_host(self, business, paths, *, stop_listener, profile_lock_unheld, heartbeat=lambda: None):
        """Remove one already-stopped exact environment for an explicit host rollback.

        This deliberately never invokes ``_drain`` or ``_stop``: rollback does
        not turn a running/paused writer into a stopped one.  Its caller holds
        installation EX, so a reconciler cannot recreate resources between the
        stopped proof and the final absence proof.
        """
        if not callable(stop_listener) or not callable(profile_lock_unheld):
            raise DomainError("environment_conflict", "Sandbox rollback boundary is incomplete")
        ownership = self.ownership(paths)
        containers = self._inventory("container", ownership)
        if not self._stopped(containers):
            raise DomainError("environment_conflict", "Sandbox writers must be stopped before host rollback")
        if profile_lock_unheld(paths) is not True:
            raise DomainError("environment_conflict", "Sandbox browser profile writer must be drained before host rollback")
        # A listener can still serve a stale generation even after Docker has
        # stopped it.  Its selected registry entry must drain before deletion.
        stop_listener(business["id"], timeout=20)
        # Re-read after listener shutdown; no cached Docker observation is a
        # proof of absence or stopped state.
        containers = self._inventory("container", ownership)
        if not self._stopped(containers) or profile_lock_unheld(paths) is not True:
            raise DomainError("environment_conflict", "Sandbox rollback stopped proof changed")
        for current in containers:
            heartbeat()
            self.docker.call("rm", current["Id"], timeout=180)
        for current in self._inventory("network", ownership):
            heartbeat()
            self.network.remove_bridge(current["Id"])
            self.docker.call("network", "rm", current["Id"], timeout=180)
        if self._inventory("container", ownership) or self._inventory("network", ownership):
            raise DomainError("environment_conflict", "Sandbox resources remain after host rollback")

    def _network(self, name, ownership, *, internal, api_port=None):
        current = self.docker.owned("network", name, ownership)
        if current is None:
            args = ["network", "create", "--driver", "bridge"]
            if internal:
                args.append("--internal")
            for key, value in ownership.labels.items():
                args.extend(("--label", key + "=" + value))
            self.docker.call(*args, name)
            current = self.docker.owned("network", name, ownership)
        if (current is None or current.get("Driver") != "bridge" or current.get("Internal") is not internal
                or current.get("EnableIPv6") or current.get("Options")):
            raise DomainError("environment_conflict", "Business network configuration conflicts")
        for identity in current.get("Containers", {}):
            member = self.docker.owned("container", identity, ownership)
            role = member and member["Config"]["Labels"].get("qorqut.role")
            allowed = {"gateway"} if not internal else {"gateway", "agent" if api_port is not None else "browser"}
            if role not in allowed:
                raise DomainError("environment_conflict", "Business network contains an unexpected workload")
        self.network.protect_bridge(current["Id"], internal=internal, agent_api_port=api_port)
        return current

    @staticmethod
    def _address(container, network):
        try:
            address = container["NetworkSettings"]["Networks"][network]["IPAddress"]
            socket.inet_pton(socket.AF_INET, address)
            return address
        except (KeyError, ValueError, OSError):
            raise DomainError("environment_conflict", "Business network address is unavailable") from None

    @staticmethod
    def _private_file(path, value):
        if path_has_symlink(path):
            raise DomainError("environment_conflict", "Sandbox control file crosses a symbolic link")
        atomic_write(path, value.encode(), mode=0o600)

    @staticmethod
    def _directory(path):
        if path_has_symlink(path):
            raise DomainError("environment_conflict", "Persistent sandbox directory crosses a symbolic link")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 1000 or info.st_gid != 1000:
            raise DomainError("configuration_missing", "Persistent sandbox files require the configured workload identity")

    def _container(self, *, name, role, image, network, ownership, generation, fingerprint, mounts,
                   memory, cpus, pids, runtime, workdir=None, browser_runtime=None):
        current = self.docker.owned("container", name, ownership)
        labels = {**ownership.labels, "qorqut.generation": str(generation), "qorqut.role": role,
                  "qorqut.plan": fingerprint}
        if current is None:
            args = ["create", "--name", name, "--network", network, "--init", "--restart", "no",
                    "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--user", "1000:1000", "--memory", str(memory) + "m", "--memory-swap", str(memory) + "m",
                    "--cpus", str(cpus), "--pids-limit", str(pids), "--runtime", runtime,
                    "--sysctl", "net.ipv6.conf.all.disable_ipv6=1", "--sysctl", "net.ipv6.conf.default.disable_ipv6=1",
                    "--sysctl", "net.ipv4.ip_forward=0", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
                    "--tmpfs", "/run:rw,nosuid,nodev,size=16m,mode=755", "--log-driver", "none",
                    "--entrypoint", "/bin/sleep"]
            if workdir:
                args.extend(("--workdir", str(workdir)))
            if browser_runtime is not None:
                args.extend(browser_runtime.container_options())
            for key, value in labels.items():
                args.extend(("--label", key + "=" + value))
            for source, destination, writable in mounts:
                # Mount option grammar must not allow a path to add Docker flags.
                if any(c in str(source) + str(destination) for c in ",\n\r\0"):
                    raise DomainError("invalid_configuration", "Registered mount path is not supported by Docker")
                args.extend(("--mount", f"type=bind,src={source},dst={destination}" + ("" if writable else ",readonly")))
            self.docker.call(*args, image, "infinity", timeout=180)
            current = self.docker.owned("container", name, ownership)
        if current is None or any(current["Config"]["Labels"].get(k) != v for k, v in labels.items()):
            raise DomainError("environment_conflict", "Business container generation or configuration conflicts")
        host, stored = current["HostConfig"], current["Config"]
        actual_mounts = {(m["Source"], m["Destination"], m["RW"]) for m in current["Mounts"] if m["Type"] == "bind"}
        expected_mounts = {(str(source), str(target), writable) for source, target, writable in mounts}
        if (stored["Image"] != image or stored["User"] != "1000:1000" or host["Privileged"]
                or not host["ReadonlyRootfs"] or host["CapDrop"] != ["ALL"] or host.get("CapAdd")
                or "no-new-privileges" not in host["SecurityOpt"] or host["NetworkMode"] != network
                or host["RestartPolicy"]["Name"] != "no" or host["Runtime"] != runtime
                or host["PidMode"] == "host" or host["IpcMode"] == "host" or host.get("Devices")
                or host["Memory"] != memory * 1024 * 1024 or host["MemorySwap"] != memory * 1024 * 1024
                or host["NanoCpus"] != round(cpus * 1_000_000_000)
                or host["PidsLimit"] != pids or actual_mounts != expected_mounts):
            raise DomainError("environment_conflict", "Business container isolation or persistent mounts conflict")
        if browser_runtime is not None:
            try:
                profiles = [json.loads(value.removeprefix("seccomp=")) for value in host["SecurityOpt"]
                            if value.startswith("seccomp=")]
            except (TypeError, ValueError):
                profiles = []
            if profiles != [browser_runtime.profile_document()] or host.get("ShmSize") != 256 * 1024 * 1024:
                raise DomainError("environment_conflict", "Browser sandbox configuration conflicts")
        return current

    def _proxy(self, business, paths, generation):
        # Existing fixture/adapter loaders are deliberately two-argument ports.
        # Only the production loader owns durable DNS pin publication.
        if self.proxy_loader is assigned_proxy:
            return provision_assigned_proxy(business, paths, generation=generation)
        return self.proxy_loader(business, paths)

    def reconcile(self, business, paths, config, generation, *, heartbeat):
        limits = config.limits
        if limits.pids < 96 or limits.cpus < .1:
            raise DomainError("configuration_missing", "Whole-business resource budget is too small for three services")
        ownership = self.ownership(paths)
        browser_runtime = self.browser_runtime(config) if self.browser_runtime is not None else None
        if browser_runtime is not None:
            browser_runtime.profile_document()  # Validate before any old writer is stopped.
        # Missing replacement prerequisites must not destroy a working old
        # generation. All three image identities are trusted, immutable inputs.
        for image in (config.workstation_image, config.browser_image, SING_BOX_IMAGE):
            if self.docker.inspect("image", image) is None:
                raise DomainError("configuration_missing", "Pinned sandbox images must be installed by the operator")
        containers = self._inventory("container", ownership)
        if self.proxy_loader is assigned_proxy:
            # A missing pin beside an already-running same generation is
            # ambiguous: never retroactively bind it to a fresh DNS response.
            known_generations = {c["Config"]["Labels"].get("qorqut.generation") for c in containers if c}
            if not _read_proxy_pin(paths) and str(generation) in known_generations:
                raise DomainError("environment_conflict", "Running sandbox generation has no proxy assignment record")
        proxy = self._proxy(business, paths, generation)
        fingerprint = environment_fingerprint(config, business, paths, proxy=proxy)
        generations = {c["Config"]["Labels"].get("qorqut.generation") for c in containers if c}
        if any(value != str(generation) for value in generations):
            if str(generation) in generations:
                raise DomainError("environment_conflict", "Multiple business generations need operator reconciliation")
            heartbeat()
            self._drain(ownership, containers, heartbeat=heartbeat, business=business, paths=paths)
            containers = []
        if any(c["Config"]["Labels"].get("qorqut.plan") != fingerprint for c in containers if c):
            raise DomainError("environment_conflict", "Changed sandbox configuration requires a replacement generation")
        expected_names = {paths.resource_prefix + "-g" + str(generation) + "-" + role for role in ("agent", "browser", "gateway")}
        if any(c["Name"].lstrip("/") not in expected_names for c in containers if c):
            raise DomainError("environment_conflict", "Unexpected business writers require operator reconciliation")
        paths.private_directory()
        self._directory(paths.home)
        self._directory(paths.workspace / "code")
        self._directory(paths.browser / "profile")
        self._directory(paths.browser / "downloads")
        generation_path = paths.control / ("generation-" + str(generation))
        self._directory(generation_path)
        marker = generation_path / "network-ready.json"
        names = {role: paths.resource_prefix + "-" + role for role in ("agent", "browser", "egress")}
        networks = {}
        for role, name in names.items():
            heartbeat()
            networks[role] = self._network(name, ownership, internal=role != "egress",
                                           api_port=config.agent_api_port if role == "agent" else None)
        api_address = networks["agent"]["IPAM"]["Config"][0]["Gateway"]
        policy = RoutingPolicy(config.direct_domains, config.direct_ports, api_address, config.agent_api_port)
        gateway_file = generation_path / "gateway.json"
        # Do not replace a bind-mounted inode of a running gateway on retry.
        value = json.dumps(gateway_config(policy, proxy), sort_keys=True)
        if not gateway_file.exists():
            self._private_file(gateway_file, value)
        elif path_has_symlink(gateway_file) or gateway_file.read_text() != value:
            raise DomainError("environment_conflict", "Gateway configuration differs from its recorded generation")
        gateway = self._container(name=paths.resource_prefix + "-g" + str(generation) + "-gateway", role="gateway",
            image=SING_BOX_IMAGE, network=names["egress"], ownership=ownership, generation=generation, fingerprint=fingerprint,
            mounts=[(gateway_file, "/etc/sing-box/config.json", False)], memory=limits.gateway_memory_mb,
            cpus=round(limits.cpus * .1, 4), pids=32, runtime="runc")
        for name in (names["agent"], names["browser"]):
            if name not in gateway["NetworkSettings"]["Networks"]:
                self.docker.call("network", "connect", name, gateway["Id"])
        heartbeat()
        gateway = self.docker.owned("container", gateway["Id"], ownership)
        if not gateway["State"]["Running"]:
            self.docker.call("start", gateway["Id"], timeout=180)
            gateway = self.docker.owned("container", gateway["Id"], ownership)
        agent_gateway, browser_gateway = (self._address(gateway, names[role]) for role in ("agent", "browser"))
        workloads = {}
        for role, address, image, memory in (("agent", agent_gateway, config.workstation_image, limits.agent_memory_mb),
                                           ("browser", browser_gateway, config.browser_image, limits.browser_memory_mb)):
            heartbeat()
            resolver = generation_path / (role + ".resolv.conf")
            resolver_value = f"nameserver {address}\noptions timeout:2 attempts:1\n"
            if not resolver.exists():
                self._private_file(resolver, resolver_value)
                resolver.chmod(0o644)
            elif path_has_symlink(resolver) or resolver.read_text() != resolver_value:
                raise DomainError("environment_conflict", "Gateway addresses changed within a generation")
            mounts = [(resolver, "/etc/resolv.conf", False)]
            if role == "agent":
                mounts += [(paths.workspace, paths.workspace, True), (paths.home, "/home/agent", True)]
            else:
                mounts += [(paths.browser / "profile", paths.browser / "profile", True),
                           (paths.browser / "downloads", paths.browser / "downloads", True)]
            workloads[role] = self._container(name=paths.resource_prefix + "-g" + str(generation) + "-" + role,
                role=role, image=image, network=names[role], ownership=ownership, generation=generation, fingerprint=fingerprint,
                mounts=mounts, memory=memory, cpus=round(limits.cpus * (.65 if role == "agent" else .25), 4),
                pids=(limits.pids - 32) // 2, runtime=config.runtime, workdir=paths.workspace if role == "agent" else None,
                browser_runtime=browser_runtime if role == "browser" else None)
        namespace = gateway["NetworkSettings"]["SandboxKey"]
        expected = {"gateway": gateway["Id"], "namespace": namespace, "plan": fingerprint,
                    "started_at": gateway["State"]["StartedAt"]}
        ready = False
        if marker.exists():
            if path_has_symlink(marker):
                raise DomainError("environment_conflict", "Network readiness record is invalid")
            ready = json.loads(marker.read_text()) == expected
        gateway_process = self.docker.call("exec", gateway["Id"], "/bin/sh", "-c", "pidof sing-box || :")
        ready = ready and bool(gateway_process)
        if not ready:
            # No network marker means a crash may have interrupted setup. Drain
            # both writers before rebuilding containment, even on the same image.
            self._stop(ownership, workloads.values(), heartbeat=heartbeat, business=business, paths=paths)
            if gateway_process:
                # A crash after process start but before marker publication
                # cannot create a second gateway process on reconciliation.
                self.docker.call("exec", gateway["Id"], "/bin/sh", "-c", "kill $(pidof sing-box)")
            self.network.configure_gateway(gateway["Id"], ownership, agent_address=agent_gateway,
                browser_address=browser_gateway, proxy=proxy, agent_api=(api_address, config.agent_api_port))
            # PID 1 stays idle; a host/daemon restart cannot launch the gateway
            # before rules are rebuilt. sing-box itself receives no capabilities.
            self.docker.call("exec", "-d", gateway["Id"], "sing-box", "run", "-c", "/etc/sing-box/config.json")
            self._private_file(marker, json.dumps(expected, sort_keys=True))
        for role, current in workloads.items():
            heartbeat()
            current = self.docker.owned("container", current["Id"], ownership)
            if not current["State"]["Running"]:
                self.docker.call("start", current["Id"], timeout=180)
            self.network.route_workload(current["Id"], ownership, agent_gateway if role == "agent" else browser_gateway)
            workloads[role] = self.docker.owned("container", current["Id"], ownership)
        result = RunningEnvironment(ownership, generation, workloads["agent"]["Id"], workloads["browser"]["Id"],
            gateway["Id"], self._address(workloads["agent"], names["agent"]), self._address(workloads["browser"], names["browser"]),
            agent_gateway, browser_gateway, f"http://{api_address}:{config.agent_api_port}")
        heartbeat()
        if self.readiness(business, paths, config, result) is not True:
            raise DomainError("environment_unhealthy", "Business integration readiness has not passed")
        return EnvironmentResult(result.workstation_id)
