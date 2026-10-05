"""Local runner values. No process discovery, launch, or adapter imports."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

Positive = Annotated[StrictInt, Field(gt=0)]
Counter = Annotated[StrictInt, Field(ge=0)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
MAX_BODY_BYTES = 64 * 1024


class RunnerError(Exception):
    def __init__(self, code: str, message: str, *, reason: str | None = None):
        self.code = code
        self.reason = reason
        super().__init__(message)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProcessFingerprint(FrozenModel):
    boot_id: UUID
    pid: Positive
    start_ticks: Positive
    uid: Counter
    pid_namespace: Positive


class TmuxBinding(FrozenModel):
    socket_path: str
    socket_device: Counter
    socket_inode: Positive
    server: ProcessFingerprint
    session_id: Annotated[str, Field(pattern=r"^\$[0-9]+$")]
    window_id: Annotated[str, Field(pattern=r"^@[0-9]+$")]
    pane_id: Annotated[str, Field(pattern=r"^%[0-9]+$")]
    pane: ProcessFingerprint

    @field_validator("socket_path")
    @classmethod
    def absolute_socket(cls, value: str) -> str:
        if not Path(value).is_absolute() or "\0" in value:
            raise ValueError("socket_path must be absolute")
        return value


class RunBinding(FrozenModel):
    agent_id: UUID
    recovery_epoch: UUID
    environment_generation: Positive
    boot_id: UUID
    run_id: UUID
    run_generation: Positive
    tmux: TmuxBinding
    recorder: ProcessFingerprint
    native: ProcessFingerprint

    @model_validator(mode="after")
    def same_boot(self) -> RunBinding:
        if any(p.boot_id != self.boot_id for p in
               (self.tmux.server, self.tmux.pane, self.recorder, self.native)):
            raise ValueError("processes must belong to the bound boot")
        if self.tmux.pane != self.recorder:
            raise ValueError("the bound recorder must own the tmux pane")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class TerminalSnapshot(FrozenModel):
    columns: Positive
    rows: Positive
    cursor_x: Counter
    cursor_y: Counter
    cursor_visible: bool
    in_mode: bool
    input_off: bool
    synchronized: bool
    screen: str


ComposerVerdict = Literal["empty", "busy", "blocked", "unknown"]


class AttemptReceipt(FrozenModel):
    attempt_id: UUID
    binding: RunBinding
    payload_sha256: Digest
    payload_bytes: Counter
    control_epoch: Counter
    state: Literal["pending", "dispatching", "deferred", "submitted", "uncertain", "failed"]
    phase: Literal["prepared", "paste", "enter", "finished"]
    reason: str
    created_at: str
    updated_at: str


class ControlState(FrozenModel):
    mode: Literal["automation", "human"]
    epoch: Counter
    viewer_id: UUID | None
    last_sequence: Counter


class ControlReceipt(FrozenModel):
    control_id: UUID
    binding: RunBinding
    action: Literal["take", "release"]
    state: ControlState
    outcome: Literal["completed", "deferred"]
    reason: str


class FrameReceipt(FrozenModel):
    epoch: Counter
    viewer_id: UUID
    sequence: Positive
    outcome: Literal["submitted", "uncertain"]


def encode_body(body: str) -> bytes:
    if not isinstance(body, str):
        raise RunnerError("invalid_argument", "body must be text")
    try:
        encoded = body.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise RunnerError("invalid_argument", "body must be valid UTF-8", reason="unsafe_terminal_text") from exc
    if any((ord(char) < 0x20 and char not in "\n\t") or 0x7f <= ord(char) <= 0x9f for char in body):
        raise RunnerError("invalid_argument", "body contains unsafe terminal control characters",
                          reason="unsafe_terminal_text")
    if len(encoded) > MAX_BODY_BYTES:
        raise RunnerError("invalid_argument", "body exceeds 64 KiB UTF-8")
    return encoded


def validate_dimensions(columns: int, rows: int) -> None:
    if (type(columns) is not int or type(rows) is not int
            or not 20 <= columns <= 500 or not 5 <= rows <= 250):
        raise RunnerError("invalid_argument", "dimensions must be 20-500 by 5-250")
