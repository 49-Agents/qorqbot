"""D57 core views and the protocol-1 browser wire values; no browser imports."""

import base64
import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AfterValidator, Field, TypeAdapter

from ..contracts import Counter, Model, Mutation, Revision, Timestamp, _utf8


def nonblank(value):
    if not value.strip():
        raise ValueError("A nonblank prompt is required")
    return _utf8(value)


def service_origin(value):
    url = urlsplit(value)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.port is None or not 1 <= url.port <= 65535 or url.path or url.query or url.fragment
            or url.username is not None or url.password is not None
            or value != f"http://{url.netloc}" or any(c.isspace() for c in value)):
        raise ValueError("An HTTP loopback origin with an explicit port is required")
    return value


class Identity(Model):
    agent_id: UUID
    recovery_epoch: UUID
    environment_generation: Revision
    boot_id: UUID
    browser_instance: UUID
    protocol: Literal[1]


class Binding(Model):
    identity: Identity
    control_epoch: Counter


class Control(Model):
    mode: Literal["automation", "yielding", "human", "reconciling"]
    ready: bool


class ControlStatus(Control):
    binding: Binding


class Observation(Model):
    protocol: Literal[1]
    identity: Identity
    control: ControlStatus
    actions: list[str]
    handoff_enabled: bool


class ObservedTab(Model):
    binding: Binding
    tab_id: Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_utf8)]


class Reconcile(Model):
    command_id: UUID
    expected: Binding
    desired: Binding
    mode: Literal["automation", "human"]
    settled_commands: tuple[UUID, ...] = ()
    evidence: Annotated[str, Field(min_length=1, max_length=2048)]


class EffectResult(Model):
    text: Annotated[str, Field(max_length=8192)] = ""
    artifacts: list[dict] = Field(default_factory=list, max_length=4)


class Receipt(Model):
    command_id: UUID
    kind: Literal["browser", "control"]
    binding: Binding
    actor_id: UUID
    payload_hash: str
    outcome: Literal["intent", "pending", "completed", "failed", "uncertain"]
    result: EffectResult = Field(default_factory=EffectResult)
    error: str | None = None


class RequestError(Model):
    code: str
    message: str


class InputRequestView(Model):
    request_id: UUID
    agent_id: UUID
    conversation_id: UUID
    surface_id: UUID
    requester_actor_id: UUID
    prompt: str
    state: Literal["requested", "ready", "completed", "cancelled", "interrupted"]
    revision: Revision
    created_at: Timestamp
    updated_at: Timestamp
    note: str | None
    browser_binding: Binding | None
    control_pending: bool
    error: RequestError | None


class BrowserSurface(Model):
    surface_id: UUID
    agent_id: UUID
    kind: Literal["browser"] = "browser"
    status: Literal["available", "unavailable"]
    reason: str | None
    binding: Binding | None
    control: Control | None


class BrowserView(Model):
    agent_id: UUID
    recovery_epoch: UUID
    surface: BrowserSurface | None
    active_request: InputRequestView | None


class BrowserGet(Model):
    agent_id: UUID


class BrowsersAttach(Mutation):
    agent_id: UUID
    service_url: Annotated[str, Field(max_length=256), AfterValidator(service_origin)]


class InputRequestsCreate(Mutation):
    target_agent_id: UUID
    prompt: Annotated[str, Field(min_length=1, max_length=4096), AfterValidator(nonblank)]
    observed_tab: ObservedTab | None = None


class Handoff(InputRequestsCreate):
    protocol: Literal[1]
    actor_id: UUID


class InputRequestsComplete(Mutation):
    request_id: UUID
    expected_revision: Revision
    note: Annotated[str, Field(max_length=4096), AfterValidator(_utf8)] | None = None


class InputRequestsResume(Mutation):
    request_id: UUID
    expected_revision: Revision


def decode_cursor(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", value):
        raise ValueError("Invalid request cursor")
    data = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    raw = json.loads(data.decode("utf-8"))
    if not isinstance(raw, list) or len(raw) != 2 or not all(isinstance(v, str) for v in raw):
        raise ValueError("Invalid request cursor")
    timestamp = TypeAdapter(Timestamp).validate_python(raw[0])
    request_id = str(UUID(raw[1]))
    if timestamp != raw[0] or request_id != raw[1]:
        raise ValueError("Invalid request cursor coordinates")
    return timestamp, request_id


def cursor(value):
    decode_cursor(value)
    return value


class InputRequestsList(Model):
    agent_id: UUID
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50
    before_created_at: Annotated[str, Field(max_length=512), AfterValidator(cursor)] | None = None


class InputRequestPage(Model):
    items: list[InputRequestView]
    has_more: bool


class ControlJob(Model):
    command_id: UUID
