"""Directory schemas, independent metadata/status revisions and fixed job inputs."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field, model_validator

from ..config import CLIDefaults, ICON_KEYS, ResourceDefaults
from ..contracts import CoreError, Counter, Model, Mutation, Revision, Timestamp, _utf8

DEFAULT_PERSONA = (
    "This is your editable persona and role. Describe your purpose and instructions here.\n"
    "Keep persistent notes and research in the workspace, and current handoff notes in memory.md.\n"
)


def _name(value: str) -> str:
    value = value.strip()
    _utf8(value)
    if not 1 <= len(value) <= 120:
        raise ValueError("Display names require 1–120 characters after trimming")
    return value


def _icon(value: str) -> str:
    if value not in ICON_KEYS:
        raise ValueError("Unknown icon key")
    return value


def _persona(value: str) -> str:
    if len(value.encode("utf-8")) > 65536:
        raise ValueError("Persona exceeds 64 KiB")
    return value


DisplayName = Annotated[str, AfterValidator(_name)]
IconKey = Annotated[str, Field(json_schema_extra={"enum": list(ICON_KEYS)}), AfterValidator(_icon)]
Persona = Annotated[str, AfterValidator(_persona)]


class AgentsCreate(Mutation):
    display_name: DisplayName
    # Legacy registration metadata only: callers omit both. They never select,
    # configure or start a CLI. A committed historical request that carried
    # start=true still validates so Application.execute can replay its receipt;
    # the handler refuses a fresh one.
    cli: Annotated[CLIDefaults | None, Field(json_schema_extra={"deprecated": True},
        description="Deprecated legacy registration metadata; omit. It does not select or configure a CLI.")] = None
    resources: ResourceDefaults | None = None
    icon_key: IconKey = "circle"
    persona: Persona = DEFAULT_PERSONA
    start: Annotated[bool, Field(strict=True, json_schema_extra={"deprecated": True},
        description="Deprecated; omit. true is refused: start the CLI in Terminal after creation.")] = False


class AgentsGet(Model):
    agent_id: UUID


class AgentsList(Model):
    limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 100
    after_id: UUID | None = None
    recovery_epoch: UUID | None = None

    @model_validator(mode="after")
    def cursor(self):
        if self.after_id is not None and self.recovery_epoch is None:
            raise ValueError("A continuation requires its recovery epoch")
        return self


class AgentsUpdate(Mutation):
    agent_id: UUID
    expected_revision: Revision
    display_name: DisplayName | None = None
    icon_key: IconKey | None = None

    @model_validator(mode="after")
    def presentation(self):
        supplied = self.model_fields_set & {"display_name", "icon_key"}
        if not supplied or any(getattr(self, name) is None for name in supplied):
            raise ValueError("Supply at least one nonnull presentation value")
        return self


class AgentsStatusSet(Mutation):
    agent_id: UUID
    expected_revision: Revision
    # UTF-8 validation precedes the shared canonical hash. Semantic bounds are
    # checked by the handler after the current status revision is verified.
    text: Annotated[str, AfterValidator(_utf8)]


def status_text(text: str) -> str:
    words = text.split()
    normalized = " ".join(words)
    if not 1 <= len(words) <= 100 or len(normalized) > 4096:
        raise CoreError("invalid_argument", "Status requires 1–100 words and at most 4,096 characters")
    return normalized


class AgentStatus(Model):
    text: str
    revision: Revision
    updated_at: Timestamp
    reviewed_at: Timestamp
    actor_id: UUID


class AgentView(Model):
    id: UUID
    actor_id: UUID
    conversation_id: UUID
    display_name: DisplayName
    icon_key: IconKey
    kind: Literal["host", "vm"]
    workspace_path: str
    cli: CLIDefaults
    resources: ResourceDefaults
    revision: Revision
    lifecycle_revision: Revision
    lifecycle: Literal["provisioning"]
    start_requested: bool
    created_by: UUID
    created_at: Timestamp
    updated_at: Timestamp
    workspace_initialized_at: Timestamp | None
    projection_revision: Counter
    status: AgentStatus
    activity: Literal["unknown"] = "unknown"
    activity_observed_at: None = None
    last_contact_at: None = None
    current_run: None = None
    capture_health: Literal["unknown"] = "unknown"
    needs_user_input: bool = False


class AgentCreated(Model):
    agent: AgentView
    provisioning_job_id: UUID


class AgentPage(Model):
    items: list[AgentView]
    next_after_id: UUID | None
    has_more: bool
    recovery_epoch: UUID
    onbash_agent_id: UUID


class AgentJob(Model):
    agent_id: UUID
    expected_lifecycle_revision: Revision
