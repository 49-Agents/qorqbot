"""Shared, transport-independent values. No runtime assembly at import time."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


Counter = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Revision = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]


def _utf8(value: str) -> str:
    value.encode("utf-8")
    return value


Key = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_utf8)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("Timestamp must include UTC timezone")
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


Timestamp = Annotated[str, AfterValidator(_timestamp)]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id() -> UUID:
    return uuid4()


def canonical(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "invalid_argument"
    NOT_FOUND = "not_found"
    REVISION_CONFLICT = "revision_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_TRANSITION = "invalid_transition"
    NOT_READY = "not_ready"
    TARGET_CHANGED = "target_changed"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    STORAGE_BLOCKED = "storage_blocked"
    UNCERTAIN_EFFECT = "uncertain_effect"
    UNAVAILABLE = "unavailable"


class Error(Model):
    code: ErrorCode
    message: Annotated[str, Field(max_length=2000)]
    retryable: bool = False


class CoreError(Exception):
    def __init__(self, code: ErrorCode | str, message: str, *, retryable: bool = False):
        self.error = Error(code=code, message=message, retryable=retryable)
        super().__init__(message)

    @property
    def code(self) -> ErrorCode:
        return self.error.code


class Actor(Model):
    actor_id: UUID
    kind: Literal["owner", "agent"]
    agent_id: UUID | None = None

    @model_validator(mode="after")
    def agent_binding(self):
        if (self.kind == "agent") != (self.agent_id is not None):
            raise ValueError("Only an agent actor requires an agent_id")
        return self


class Invocation(Model):
    """Constructed by a trusted adapter from its authenticated registration."""

    actor_id: UUID


class Mutation(Model):
    idempotency_key: Key


class OperationResult(Model):
    ok: bool
    operation_id: UUID | None = None
    outcome: Literal["completed", "accepted"] | None = None
    data: dict[str, JsonValue] | None = None
    event_seq: Counter | None = None
    error: Error | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.ok:
            if self.error is not None or self.outcome is None or self.data is None:
                raise ValueError("Successful results require an outcome/data and no error")
        elif self.error is None or self.outcome is not None or self.data is not None:
            raise ValueError("Failed results require an error and no outcome/data")
        return self


class JobState(StrEnum):
    QUEUED = "queued"
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class WaitReason(StrEnum):
    RETRY_DELAY = "retry_delay"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    RECOVERY_REVIEW = "recovery_review"


class JobProgress(Model):
    phase: Annotated[str, Field(min_length=1, max_length=100)]
    completed: Counter = 0
    total: Counter | None = None

    @model_validator(mode="after")
    def bounds(self):
        if self.total is not None and self.completed > self.total:
            raise ValueError("Progress exceeds its total")
        return self


class JobResolution(Model):
    state: Literal["completed", "waiting", "failed", "uncertain", "cancelled"]
    progress: JobProgress
    reason: WaitReason | None = None
    next_attempt_at: Timestamp | None = None
    error: Error | None = None

    @model_validator(mode="after")
    def waiting(self):
        if self.state == "waiting" and (self.reason is None or self.next_attempt_at is None):
            raise ValueError("Waiting needs a reason and next inspection time")
        return self


class JobRecord(Model):
    job_id: UUID
    operation_id: UUID
    kind: str
    args: dict[str, JsonValue]
    target_id: UUID | None
    expected_generation: Counter | None
    external_effect: bool
    recovery_epoch: UUID
    state: JobState
    attempt_count: Counter
    progress: JobProgress
    wait_reason: WaitReason | None
    next_attempt_at: Timestamp | None
    last_error: Error | None
    lease_owner: UUID | None
    lease_token: UUID | None
    lease_until: Timestamp | None
    created_at: Timestamp
    updated_at: Timestamp


class OperationView(Model):
    operation_id: UUID
    name: str
    actor_id: UUID
    created_at: Timestamp
    recovery_epoch: UUID
    original_result: OperationResult
    jobs: list[JobRecord]
