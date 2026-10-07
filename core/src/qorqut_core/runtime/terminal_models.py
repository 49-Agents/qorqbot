"""Bounded Terminal presentation and protocol-1 client frames."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, TypeAdapter, field_validator
import base64
import binascii

from ..contracts import Counter, Model, Revision, Timestamp
from ..runner.shell_models import ShellControl


class TerminalGet(Model):
    agent_id: UUID


class Recording(Model):
    state: Literal["starting", "healthy", "blocked", "incomplete", "unknown"]
    local_captured_offset: Counter
    host_archived_offset: Counter


class TerminalView(Model):
    agent_id: UUID
    recovery_epoch: UUID
    status: Literal["disabled", "waiting_workspace", "available", "stopped", "unavailable", "unsupported_target"]
    terminal_id: UUID | None = None
    observed_at: Timestamp | None = None
    reason: Annotated[str, Field(max_length=100, pattern=r"^[a-z0-9_]+$")]
    control: ShellControl | None = None
    recording: Recording | None = None


class ControlMessage(Model):
    type: Literal["control"]
    action: Literal["take", "release"]
    expected_epoch: Counter


class InputMessage(Model):
    type: Literal["input"]
    epoch: Counter
    sequence: Revision
    data: Annotated[str, Field(strict=True, min_length=4, max_length=87384)]

    @field_validator("data")
    @classmethod
    def strict_base64(cls, value):
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Input requires strict base64") from None
        if not 1 <= len(raw) <= 65536 or base64.b64encode(raw).decode("ascii") != value:
            raise ValueError("Input requires 1-65536 bytes of canonical base64")
        return value


class ResizeMessage(Model):
    type: Literal["resize"]
    epoch: Counter
    sequence: Revision
    columns: Annotated[int, Field(strict=True, ge=20, le=500)]
    rows: Annotated[int, Field(strict=True, ge=5, le=250)]


CLIENT_MESSAGE = TypeAdapter(Annotated[ControlMessage | InputMessage | ResizeMessage, Field(discriminator="type")])
