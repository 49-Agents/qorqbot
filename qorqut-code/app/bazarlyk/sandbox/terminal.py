"""Select a verified BusinessSandbox workstation for native agent terminals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
import socket

from qorqut_operations.contracts import DomainError
from bazarlyk.transport.terminals import TerminalExecution
from .config import BusinessPaths, HostConfig
from .docker import Docker, Ownership
from .tmux import DockerTmuxBroker, BOOT_COMMAND


def environment_fingerprint(config, business, paths, *, proxy_loader=None, proxy=None, generation=None):
    """Fingerprint trusted inputs, including the persisted proxy endpoint.

    Read-only callers never resolve DNS. Production reads require a durable proxy
    assignment; injected two-argument fixture loaders remain supported.
    """
    if proxy is None:
        if proxy_loader is None:
            from .runtime import assigned_proxy
            proxy = assigned_proxy(business, paths, generation=generation)
        else:
            proxy = proxy_loader(business, paths)
    return hashlib.sha256(json.dumps({"config": asdict(config), "workspace": str(paths.workspace),
        "proxy": asdict(proxy)}, sort_keys=True).encode()).hexdigest()


class SandboxTerminalProvider:
    """Resolve one immutable, ready workstation per business terminal request.

    A missing lifecycle row means this installation predates sandbox intent and
    deliberately returns ``None`` for the TerminalService host fallback. Once a
    row exists, every non-ready state fails closed.
    """

    def __init__(self, database, paths, docker, mirror, *, config_loader=HostConfig.read,
                 local_files=None, fingerprint_loader=None):
        self.database = database
        self.paths = paths
        self.config_loader = config_loader
        self.local_files = local_files
        self.mirror = mirror
        self.docker = docker
        self.fingerprint_loader = fingerprint_loader

    @staticmethod
    def _endpoint(network, config):
        try:
            gateway = network["IPAM"]["Config"][0]["Gateway"]
            socket.inet_pton(socket.AF_INET, gateway)
            if not 1024 <= config.agent_api_port <= 65535:
                raise ValueError
            return f"http://{gateway}:{config.agent_api_port}"
        except (KeyError, IndexError, TypeError, ValueError, OSError):
            raise DomainError("environment_conflict", "Sandbox agent endpoint is unavailable") from None

    def _environment(self, profile, business, config):
        if self.local_files is None:
            raise DomainError("configuration_missing", "Sandbox terminal environment bindings are not configured")
        identity = business["id"]
        from bazarlyk.service.local import IDENTITY_KEYS
        values = {row["key"]: row["value"] for row in self.local_files.environment_pairs()
                  if row["key"] in config.shared_environment_keys and row["key"] not in IDENTITY_KEYS}
        values.update({row["key"]: row["value"] for row in self.local_files.environment_pairs(identity)
                       if row["key"] not in IDENTITY_KEYS})
        values.update({key: value for key, value in self.local_files.private_environment(identity, role=profile.role.id).items()
                       if key not in IDENTITY_KEYS})
        return values

    def _fingerprint(self, config, business, paths, generation):
        if self.fingerprint_loader is not None:
            return self.fingerprint_loader(config, business, paths)
        return environment_fingerprint(config, business, paths, generation=generation)

    def __call__(self, business):
        business_id = business["id"]
        with self.database.read() as uow:
            row = uow.repository("business_sandboxes").get(business_id)
        if row is None:
            return None
        if (row.get("state") != "ready" or row.get("desired_state", "running") != "running"
                or row.get("observed_generation") != row.get("desired_generation")):
            raise DomainError("operation_unavailable", "Business terminal environment is not ready", retryable=True)
        try:
            generation = row["observed_generation"]
            environment_id = row["environment_id"]
            paths = BusinessPaths.registered(self.paths, business)
            config = self.config_loader(self.paths)
            if not isinstance(config, HostConfig):
                raise DomainError("configuration_missing", "Sandbox configuration is unavailable")
            if getattr(config, "enabled", True) is not True:
                raise DomainError("configuration_missing", "Business sandbox execution is disabled")
            ownership = Ownership(hashlib.sha256(str(paths.root).encode()).hexdigest()[:20], paths.business_id)
            name = f"{paths.resource_prefix}-g{generation}-agent"
            current = self.docker.owned("container", name, ownership)
            if current is None or current.get("Id") != environment_id:
                raise DomainError("environment_conflict", "Sandbox workstation identity is not current")
            labels = current.get("Config", {}).get("Labels", {})
            if labels.get("qorqut.plan") != self._fingerprint(config, business, paths, generation):
                raise DomainError("environment_conflict", "Sandbox workstation configuration is not current")
            network_name = paths.resource_prefix + "-agent"
            network = self.docker.owned("network", network_name, ownership)
            if network is None:
                raise DomainError("environment_unavailable", "Sandbox agent network is unavailable", retryable=True)
            attached = current.get("NetworkSettings", {}).get("Networks", {})
            members = network.get("Containers", {})
            if (network_name not in attached
                    or environment_id not in members):
                raise DomainError("environment_conflict", "Sandbox workstation is not attached to its agent network")
            if (labels.get("qorqut.role") != "agent"
                    or labels.get("qorqut.generation") != str(generation)
                    or not current.get("State", {}).get("Running")):
                raise DomainError("environment_conflict", "Sandbox workstation identity is not current")
            endpoint = self._endpoint(network, config)
            broker = DockerTmuxBroker(self.docker, ownership, environment_id, generation, self.mirror)
            broker._running()
            broker.verify_interpreter(BOOT_COMMAND[0])
            return TerminalExecution(broker, endpoint, BOOT_COMMAND[0], broker.installed_versions,
                lambda profile, selected: self._environment(profile, selected, config),
                (business_id, generation, environment_id), broker.verify_interpreter)
        except KeyError:
            raise DomainError("environment_conflict", "Sandbox workstation identity is incomplete") from None
