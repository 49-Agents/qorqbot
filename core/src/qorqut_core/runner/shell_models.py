"""Local recorded-terminal values; Bash is not a connected native session."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StrictInt, field_validator, model_validator

from .models import FrozenModel, ProcessFingerprint, TmuxBinding
from ..transcripts.probe import ProbeResult

Counter = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
Positive = Annotated[StrictInt, Field(gt=0, le=2**63 - 1)]


def absolute_path(value: str | Path) -> Path:
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts or path == Path("/")
            or "\0" in str(path) or len(bytes(path)) >= 4096):
        raise ValueError("an explicit absolute, bounded path without '..' is required")
    return path


class ShellSpec(FrozenModel):
    installation_id: UUID
    agent_id: UUID
    recovery_epoch: UUID
    environment_generation: Positive
    home: Path
    workspace: Path
    path: Annotated[str, Field(strict=True, min_length=1, max_length=32768)]

    @field_validator("home", "workspace")
    @classmethod
    def roots(cls, value: Path) -> Path:
        return absolute_path(value)

    @field_validator("path")
    @classmethod
    def path_string(cls, value: str) -> str:
        if "\0" in value or len(value.encode()) > 32768:
            raise ValueError("PATH must be bounded and contain no NUL")
        return value

    @model_validator(mode="after")
    def distinct_roots(self):
        if self.home == self.workspace:
            raise ValueError("managed HOME and workspace must be distinct")
        return self


class ShellControl(FrozenModel):
    """Unclaimed is not automated-input readiness. Disconnect changes nothing."""

    mode: Literal["unclaimed", "human"]
    epoch: Counter
    viewer_id: UUID | None
    last_sequence: Counter


class ShellCapture(FrozenModel):
    """Historical spool facts, deliberately separate from the live probe."""

    health: Literal["starting", "healthy", "blocked", "incomplete"]
    local_captured_offset: Counter
    host_archived_offset: Counter
    recorder_finished_at: str | None
    child_exit_code: int | None
    drain_complete: bool


class ShellView(FrozenModel):
    spec: ShellSpec
    terminal_id: UUID
    state: Literal["running", "stopped", "unknown"]
    reason: str
    tmux: TmuxBinding | None = None
    recorder: ProcessFingerprint | None = None
    shell: ProcessFingerprint | None = None
    source_id: UUID
    source_epoch: UUID
    probe_address: str
    capture: ProbeResult
    saved_capture: ShellCapture | None = None
    control: ShellControl


class ShellFrame(FrozenModel):
    viewer_id: UUID
    epoch: Counter
    sequence: Positive
    outcome: Literal["submitted", "uncertain"]
    bytes_written: Counter = 0
