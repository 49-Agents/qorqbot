"""Module-local inputs and evidence. None of these records is a durable receipt."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Provider = Literal["claude", "codex"]


class AdapterError(ValueError):
    """Invalid binding, unmanaged file, or unsupported adapter operation."""


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunBinding(Record):
    agent_id: UUID
    conversation_id: UUID
    recovery_epoch: UUID
    environment_generation: int = Field(ge=1, strict=True)
    boot_id: UUID
    run_id: UUID
    run_generation: int = Field(ge=1, strict=True)


class ManagedRoots(Record):
    home: Path
    workspace: Path

    @field_validator("home", "workspace")
    @classmethod
    def absolute(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts or value == Path("/"):
            raise ValueError("Managed roots must be explicit absolute directories")
        return value

    @model_validator(mode="after")
    def distinct(self):
        # Preparation also compares already-open root descriptors; lexical paths
        # alone cannot distinguish Linux aliases such as /tmp and //tmp.
        if self.home == self.workspace:
            raise ValueError("HOME and workspace must be distinct managed roots")
        return self


class MCPBinding(Record):
    """Caller supplies the complete authenticated endpoint binding; no inferred headers."""

    url: str
    headers: tuple[tuple[str, str], ...]

    @field_validator("url")
    @classmethod
    def http_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment
                or any(ord(c) < 32 for c in value)):
            raise ValueError("Expected an explicit HTTP MCP endpoint without userinfo")
        return value

    @field_validator("headers")
    @classmethod
    def explicit_headers(cls, value):
        names = [name.lower() for name, _ in value]
        if not value or len(names) != len(set(names)):
            raise ValueError("Supply unique explicit caller/target binding headers")
        for name, content in value:
            if not name or any(not (c.isascii() and (c.isalnum() or c in "!#$%&'*+-.^_`|~")) for c in name):
                raise ValueError("Invalid HTTP header name")
            if not content or any(ord(c) < 32 or ord(c) == 127 for c in content):
                raise ValueError("Invalid HTTP header value")
        return value


class PreparationInput(Record):
    roots: ManagedRoots
    run: RunBinding
    core: MCPBinding
    browser: MCPBinding
    # The later runner supplies its real durable sink. No placeholder executable.
    hook_sink_argv: tuple[str, ...] = ()

    @model_validator(mode="after")
    def bindings(self):
        if self.core.url == self.browser.url:
            raise ValueError("Core and browser require separate MCP endpoints")
        if self.hook_sink_argv:
            if not Path(self.hook_sink_argv[0]).is_absolute():
                raise ValueError("Hook sink executable must be explicit and absolute")
            if any(not arg or "\0" in arg for arg in self.hook_sink_argv):
                raise ValueError("Invalid hook sink argv")
        return self


class Capability(Record):
    name: str
    evidence: Literal["unsupported", "synthetic", "native_help"]
    detail: str
    native_verified: bool = False


class Compatibility(Record):
    provider: Provider
    version: str
    known_preparation_contract: bool
    capabilities: tuple[Capability, ...]
    # Intentionally false in R1: no actual native interaction acceptance occurred.
    native_verified: bool = False


class PreparationResult(Record):
    provider: Provider
    run: RunBinding
    roots: ManagedRoots
    generated_paths: tuple[Path, ...]
    binding_path: Path
    gaps: tuple[str, ...]
    authentication: Literal["required_setup"] = "required_setup"


class NativeBinding(Record):
    """Exact native identity supplied by the bound runner, never selected by cwd.

    The caller verifies the native process belongs to this run. Discovery checks
    file metadata against that prior binding; it does not attest a live process.
    """

    run: RunBinding
    native_session_id: UUID
    resumed_from_run_id: UUID | None = None

    @model_validator(mode="after")
    def previous_run(self):
        if self.resumed_from_run_id == self.run.run_id:
            raise ValueError("Resume must link a distinct prior run")
        return self


class LaunchSpec(Record):
    provider: Provider
    run: RunBinding
    argv: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...]
    native_session_id: UUID | None
    resumed_from_run_id: UUID | None
    setup_requirements: tuple[str, ...] = (
        "native_authentication", "workspace_and_hook_trust", "recorder_integration",
        "native_interaction_acceptance",
    )
    # An argv recipe, never a launch/readiness receipt.
    executed: Literal[False] = False


class SourceEvidence(Record):
    kind: str
    offset: int
    length: int
    sha256: str


class NativeSource(Record):
    provider: Provider
    binding: NativeBinding
    path: Path
    relative_path: str
    device: int
    inode: int
    observed_size: int
    ownership: tuple[SourceEvidence, ...]
    format_hint: str
    # Recorder assigns durable source IDs/epochs and deduplicates resume links.
    archived: Literal[False] = False


class AttachmentReference(Record):
    source_path: Path
    record: SourceEvidence
    kind: Literal["embedded", "external"]
    location: str | None = None
    # Embedded bytes are already in the raw source; external access is not attempted.
    captured: Literal[False] = False


class DiscoveryGap(Record):
    code: str
    relative_path: str | None = None


class DiscoveryResult(Record):
    sources: tuple[NativeSource, ...] = ()
    attachments: tuple[AttachmentReference, ...] = ()
    gaps: tuple[DiscoveryGap, ...] = ()


class HookObservation(Record):
    provider: Provider
    binding: NativeBinding
    source: NativeSource
    event: str
    activity: Literal["idle", "working", "waiting_user", "unknown"]
    sequence: int
    observed_at: str
    payload_sha256: str
    # The runner must commit before forwarding. SessionEnd is not verified process exit.
    requires_persistence: Literal[True] = True
    durable: Literal[False] = False


class ComposerVerdict(Record):
    state: Literal["empty", "busy", "blocked", "unknown"]
    reason: str
    fingerprint: str
    evidence: Literal["synthetic", "native_snapshot", "unsupported"]
    native_verified: Literal[False] = False

    @property
    def permits_paste(self) -> bool:
        # A synthetic layout cannot certify the safety of a running native client.
        return False
