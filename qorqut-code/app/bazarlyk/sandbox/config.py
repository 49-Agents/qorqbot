"""Host-owned sandbox configuration and registered path bindings.

This file is deliberately outside every business mount and is not part of
business_config_update. A model cannot choose mounts, images, routes or limits.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from bazarlyk.bootstrap.client import path_has_symlink
from bazarlyk.browser.config import browser_state_directory
from bazarlyk.composition.locations import business_hq, business_root
from qorqut_operations.contracts import DomainError
from .network import RoutingPolicy


DEFAULT_DIRECT_DOMAINS = (
    "api.anthropic.com", "api.openai.com", "github.com", "api.github.com", "ssh.github.com",
    "codeload.github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
    "registry.npmjs.org", "pypi.org", "files.pythonhosted.org", "nodejs.org",
    "deb.debian.org", "security.debian.org",
)


def business_identity(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise DomainError("invalid_configuration", "Sandbox requires a canonical registered business identity") from None
    return value


def pinned_image(value):
    if (not isinstance(value, str) or len(value) > 300
            or not re.fullmatch(r"(?:[a-z0-9][a-z0-9./:_-]*@)?sha256:[0-9a-f]{64}", value)):
        raise DomainError("invalid_configuration", "Sandbox images must use immutable SHA-256 identities")
    return value


@dataclass(frozen=True)
class ResourceLimits:
    agent_memory_mb: int
    browser_memory_mb: int
    gateway_memory_mb: int
    cpus: float
    pids: int
    disk_gb: int

    def __post_init__(self):
        for value in (self.agent_memory_mb, self.browser_memory_mb, self.gateway_memory_mb, self.pids, self.disk_gb):
            if type(value) is not int or value < 1:
                raise DomainError("invalid_configuration", "Sandbox resource budgets must be positive integers")
        if type(self.cpus) not in (int, float) or not 0 < self.cpus <= 1024:
            raise DomainError("invalid_configuration", "Sandbox CPU budget must be positive and bounded")

    @property
    def memory_mb(self):
        return self.agent_memory_mb + self.browser_memory_mb + self.gateway_memory_mb


@dataclass(frozen=True)
class HostConfig:
    workstation_image: str
    browser_image: str
    limits: ResourceLimits
    agent_api_port: int
    runtime: str = "runc"
    direct_domains: tuple[str, ...] = DEFAULT_DIRECT_DOMAINS
    direct_ports: tuple[int, ...] = (80, 443, 22)
    # Explicitly approved shared provider bindings only. Never copy host process
    # environment, personal CLI login stores, or every Global Settings value.
    shared_environment_keys: tuple[str, ...] = ()
    # Existing host configuration remains inert until the owner explicitly enables
    # sandbox adoption/provisioning for this installation.
    enabled: bool = False
    provision_new_businesses: bool = True

    def __post_init__(self):
        pinned_image(self.workstation_image)
        pinned_image(self.browser_image)
        if self.runtime not in {"runc", "runsc"}:
            raise DomainError("invalid_configuration", "Unsupported sandbox runtime")
        if type(self.agent_api_port) is not int or not 1024 <= self.agent_api_port <= 65535:
            raise DomainError("invalid_configuration", "Sandbox agent listener requires a dedicated unprivileged port")
        if type(self.enabled) is not bool:
            raise DomainError("invalid_configuration", "Sandbox enablement must be explicit")
        if type(self.provision_new_businesses) is not bool:
            raise DomainError("invalid_configuration", "New-business sandbox provisioning must be boolean")
        # The owner service must never be advertised as the sandbox agent API.
        if self.agent_api_port == 1049:
            raise DomainError("invalid_configuration", "Sandbox listener must not use the owner dashboard port")
        RoutingPolicy(self.direct_domains, self.direct_ports)
        if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,199}", key)
               for key in self.shared_environment_keys):
            raise DomainError("invalid_configuration", "Invalid shared environment binding name")

    @classmethod
    def read(cls, paths):
        path = paths.runtime / "sandbox-host.json"
        if path_has_symlink(path):
            raise DomainError("invalid_configuration", "Host sandbox configuration cannot cross symbolic links")
        if not path.exists():
            return None
        try:
            if path_has_symlink(path):
                raise ValueError
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 65536:
                    raise ValueError
                value = json.load(source)
            if not isinstance(value, dict) or value.pop("schema_version", None) != 1:
                raise ValueError
            allowed = {"workstation_image", "browser_image", "limits", "agent_api_port", "enabled", "runtime", "provision_new_businesses",
                       "direct_domains", "direct_ports", "shared_environment_keys"}
            if set(value) - allowed:
                raise ValueError
            value["limits"] = ResourceLimits(**value["limits"])
            for name in ("direct_domains", "direct_ports", "shared_environment_keys"):
                if name in value:
                    if not isinstance(value[name], list):
                        raise ValueError
                    value[name] = tuple(value[name])
            return cls(**value)
        except (OSError, TypeError, ValueError, KeyError):
            raise DomainError("invalid_configuration", "Host sandbox configuration is missing required fields, invalid or not private") from None


def creation_enabled(paths) -> bool:
    """Whether a newly committed business selects sandbox provisioning.

    An absent or explicitly disabled configuration retains legacy creation.
    A present but invalid configuration cannot silently downgrade new businesses
    to host execution; its operator must correct the configuration first.
    """
    config = HostConfig.read(paths)
    return config is not None and config.enabled and config.provision_new_businesses


@dataclass(frozen=True)
class BusinessPaths:
    business_id: str
    root: Path
    workspace: Path
    home: Path
    control: Path
    browser: Path
    resource_prefix: str
    instance: object = None  # the InstancePaths every location above was derived from

    @classmethod
    def registered(cls, instance, business, *, require_workspace=True):
        """The sandbox locations of a registered business, selected by its folder (slug) through ``instance``."""
        identity = business_identity(business["id"])
        root = instance.operations
        if not root.is_dir() or root.resolve() != root or path_has_symlink(root):
            raise DomainError("registration_conflict", "Sandbox installation root must be canonical")
        workspace, hq = business_root(instance, business), business_hq(instance, business)
        if ((require_workspace or hq.exists()) and not hq.is_dir()
                or workspace.resolve() != workspace or path_has_symlink(workspace) or path_has_symlink(hq)):
            raise DomainError("registration_conflict", "Sandbox workspace must be the registered business folder without symbolic links")
        control = instance.runtime / "sandboxes" / identity
        browser = browser_state_directory(instance, identity)
        if path_has_symlink(control) or path_has_symlink(browser):
            raise DomainError("registration_conflict", "Sandbox private paths cannot cross symbolic links")
        prefix = "qbs-" + hashlib.sha256((str(root) + "\0" + identity).encode()).hexdigest()[:20]
        return cls(identity, root, workspace, control / "home", control, browser, prefix, instance)

    def private_directory(self):
        """Create only private control state; no workspace or browser replacement."""
        self.control.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.control.chmod(0o700)
        return self.control
