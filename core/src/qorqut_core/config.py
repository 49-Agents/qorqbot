"""Explicit installation input and revisioned defaults, never personal CLI config."""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Network, IPv6Address
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .adapters.capabilities import require_profile
from .adapters.manual_hooks import NativeProgram
from .contracts import Model, Mutation, Revision
from .runner.shell_models import ShellSpec, absolute_path

ICON_KEYS = (
    "circle", "square", "triangle", "diamond", "star", "compass", "globe", "leaf",
    "feather", "book", "notebook", "flask", "microscope", "telescope", "code",
    "terminal", "wrench", "gear", "lightbulb", "anchor",
)


class ResourceDefaults(Model):
    # Representability bounds, not measured VM capacity or a scheduling policy.
    cpu: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
    memory_mib: Annotated[int, Field(strict=True, ge=1, le=(2**63 - 1) // 2**20)]
    disk_gib: Annotated[int, Field(strict=True, ge=1, le=(2**63 - 1) // 2**30)]


class CLIDefaults(Model):
    provider: Literal["claude", "codex"]
    model: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("model")
    @classmethod
    def model_name(cls, value):
        value = value.strip()
        value.encode("utf-8")
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("A nonblank model identifier without control characters is required")
        return value


class NewAgentDefaults(Model):
    cli: CLIDefaults
    resources: ResourceDefaults


class LocalTerminalSettings(Model):
    runtime_root: Path
    python_executable: Path
    tmux_executable: Path
    bash_executable: Path
    path: Annotated[str, Field(strict=True, min_length=1, max_length=32768)]
    native_programs: tuple[NativeProgram, ...] = ()

    @field_validator("native_programs")
    @classmethod
    def pinned_native_programs(cls, value):
        if len(value) > 2 or len({item.provider for item in value}) != len(value):
            raise ValueError("Supply at most two distinct native providers")
        for item in value:
            require_profile(item.provider, item.cli_version)
        return value

    @field_validator("runtime_root", "python_executable", "tmux_executable", "bash_executable")
    @classmethod
    def absolute(cls, value):
        return absolute_path(value)

    @field_validator("path")
    @classmethod
    def explicit_path(cls, value):
        return ShellSpec.path_string(value)

    @model_validator(mode="after")
    def socket_bound(self):
        if len(bytes(self.runtime_root / ("0" * 32) / "tmux.sock")) > 100:
            raise ValueError("Terminal socket path exceeds 100 bytes")
        return self


class InstallationSettings(Model):
    installation_id: UUID
    source_root: Path
    release_root: Path
    data_root: Path
    listen_address: IPv4Address | IPv6Address
    listen_port: Annotated[int, Field(strict=True, ge=1, le=65535)]
    private_network: IPv4Network | None = None
    busy_timeout_ms: Annotated[int, Field(strict=True, ge=1, le=60000)] = 1000
    policy: Literal["allow_all"] = "allow_all"
    local_terminal: LocalTerminalSettings | None = None

    @field_validator("source_root", "release_root", "data_root")
    @classmethod
    def absolute_root(cls, value):
        if not value.is_absolute() or ".." in value.parts or "\0" in str(value):
            raise ValueError("Roots must be explicit absolute paths without '..'")
        return value

    @model_validator(mode="after")
    def separate_data(self):
        for code_root in (self.source_root, self.release_root):
            if self.data_root.is_relative_to(code_root) or code_root.is_relative_to(self.data_root):
                raise ValueError("Data and source/release roots must be separate")
        return self


class ConfigGet(Model):
    pass


class ConfigUpdate(Mutation):
    expected_revision: Revision
    defaults: NewAgentDefaults


class ConfigView(Model):
    installation_id: UUID
    recovery_epoch: UUID
    revision: Revision
    defaults: NewAgentDefaults
    policy: Literal["allow_all"] = "allow_all"
    icon_keys: tuple[str, ...] = ICON_KEYS
    api_versions: tuple[int, ...] = (1,)
    event_payload_versions: tuple[int, ...] = (1,)
    # Transports and independent browser integration have not been implemented.
    runner_protocol_versions: tuple[int, ...] = ()
    browser_protocol_versions: tuple[int, ...] = ()
