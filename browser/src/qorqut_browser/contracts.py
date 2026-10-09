"""Module-local models; these are not the future MCP/HTTP wire contract."""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BrowserError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Identity(FrozenModel):
    agent_id: UUID
    recovery_epoch: UUID
    environment_generation: Annotated[int, Field(strict=True, ge=1)]
    boot_id: UUID
    browser_instance: UUID
    protocol: Literal[1]


class Binding(FrozenModel):
    identity: Identity
    control_epoch: Annotated[int, Field(strict=True, ge=0)]


class Mount(FrozenModel):
    """Expected Linux mountinfo identity, supplied by provisioning, never guessed."""

    path: Path
    mount_id: Annotated[int, Field(strict=True, gt=0)]
    device: str = Field(pattern=r"^\d+:\d+$")
    filesystem: str = Field(min_length=1, max_length=64)

    @field_validator("path")
    @classmethod
    def absolute(cls, value: Path) -> Path:
        return absolute_path(value)


def absolute_path(value: Path) -> Path:
    if not value.is_absolute() or ".." in value.parts or "\0" in str(value):
        raise ValueError("an explicit absolute path without '..' is required")
    return value


class BrowserConfig(FrozenModel):
    binding: Binding
    uid: Annotated[int, Field(strict=True, ge=0)]
    database: Path
    state_file: Path
    home: Path
    profile: Path
    downloads: Path
    artifacts: Path
    mounts: tuple[Mount, ...] = Field(min_length=1)
    cdp_host: Literal["127.0.0.1"]
    cdp_port: Annotated[int, Field(strict=True, ge=1024, le=65535)]
    vnc_port: Annotated[int, Field(strict=True, ge=1024, le=65535)]
    service_port: Annotated[int, Field(strict=True, ge=1024, le=65535)]
    display: Annotated[int, Field(strict=True, ge=1, le=999)]

    @field_validator("database", "state_file", "home", "profile", "downloads", "artifacts")
    @classmethod
    def absolute(cls, value: Path) -> Path:
        return absolute_path(value)

    @model_validator(mode="after")
    def layout(self):
        if self.database.name != "browser.sqlite3":
            raise ValueError("the receipt file must be browser.sqlite3")
        if self.state_file.parent != self.database.parent or self.state_file == self.database:
            raise ValueError("readiness and receipts need distinct files in the owned runtime directory")
        dirs = self.owned_directories
        if len(set(dirs)) != len(dirs) or Path("/") in dirs:
            raise ValueError("managed directories must be distinct and cannot be filesystem root")
        if len({self.cdp_port, self.vnc_port, self.service_port}) != 3:
            raise ValueError("CDP, VNC and service ports must differ")
        if any(not any(path.is_relative_to(m.path) for m in self.mounts) for path in dirs):
            raise ValueError("every managed path requires an explicit mount binding")
        return self

    @property
    def owned_directories(self) -> tuple[Path, ...]:
        return self.database.parent, self.home, self.profile, self.downloads, self.artifacts

    @property
    def resource_hash(self) -> str:
        # Boot/recovery/control can be explicitly rebound. Resources cannot silently move.
        value = self.model_dump(mode="json", exclude={"binding", "mounts"})
        value["agent_id"] = str(self.binding.identity.agent_id)
        return digest(value)

    @property
    def cdp_endpoint(self) -> str:
        return f"http://{self.cdp_host}:{self.cdp_port}"


class Snapshot(FrozenModel):
    kind: Literal["snapshot"]
    interactive: bool = False
    depth: Annotated[int, Field(strict=True, ge=1, le=100)] | None = None


class Navigate(FrozenModel):
    kind: Literal["navigate"]
    url: str = Field(min_length=1, max_length=16384)

    @field_validator("url")
    @classmethod
    def operand(cls, value: str) -> str:
        return helper_operand(value)


class Click(FrozenModel):
    kind: Literal["click"]
    selector: str = Field(min_length=1, max_length=4096)

    @field_validator("selector")
    @classmethod
    def operand(cls, value: str) -> str:
        return helper_operand(value)


class Fill(Click):
    kind: Literal["fill"]
    text: str = Field(max_length=65536)

    @field_validator("text")
    @classmethod
    def operand(cls, value: str) -> str:
        return helper_operand(value)


def helper_operand(value: str) -> str:
    # agent-browser parses options after command operands too. Do not let a value
    # become a global --cdp/--profile/--session option; no shell is involved.
    if value.startswith("-") or "\0" in value:
        raise ValueError("option-shaped or NUL-containing helper operands are unsupported")
    return value


class Screenshot(FrozenModel):
    kind: Literal["screenshot"]
    full_page: bool = False


Action = Annotated[Snapshot | Navigate | Click | Fill | Screenshot, Field(discriminator="kind")]


class Command(FrozenModel):
    command_id: UUID
    binding: Binding
    action: Action


class Artifact(FrozenModel):
    path: Path
    size: Annotated[int, Field(strict=True, ge=0)]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    media_type: str


class EffectResult(FrozenModel):
    text: str = Field(default="", max_length=8192)
    artifacts: tuple[Artifact, ...] = Field(default=(), max_length=4)


class Receipt(FrozenModel):
    command_id: UUID
    kind: Literal["browser", "control"]
    binding: Binding
    actor_id: UUID
    payload_hash: str
    outcome: Literal["intent", "pending", "completed", "failed", "uncertain"]
    result: EffectResult = Field(default_factory=EffectResult)
    error: str | None = None


class ControlStatus(FrozenModel):
    binding: Binding
    mode: Literal["automation", "yielding", "human", "reconciling"]
    ready: bool


class Reconcile(FrozenModel):
    """Trusted application's explicit local control direction, not model tool input.

    settled_commands attests those uncertain effects are no longer in flight. It
    never asserts that an external action succeeded and never replays an effect.
    """

    command_id: UUID
    expected: Binding
    desired: Binding
    mode: Literal["automation", "human"]
    settled_commands: tuple[UUID, ...] = Field(default=(), max_length=256)
    evidence: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def epochs(self):
        if self.desired.control_epoch <= self.expected.control_epoch:
            raise ValueError("control reconciliation must advance the epoch")
        if self.desired.identity.agent_id != self.expected.identity.agent_id:
            raise ValueError("control cannot change the agent")
        if len(set(self.settled_commands)) != len(self.settled_commands):
            raise ValueError("settled command IDs must be distinct")
        return self


def canonical(value) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()
