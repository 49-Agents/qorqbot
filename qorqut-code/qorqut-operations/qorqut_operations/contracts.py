"""Transport-independent values shared by application, domains, and adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Literal
from uuid import UUID, uuid4

Mode = Literal["allow", "signoff", "deny"]
PrincipalKind = Literal["human", "agent", "system", "integration"]
Source = Literal["mcp", "dashboard", "worker"]
OPERATION_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
HASH = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid4())


def plain(value: Any) -> Any:
    """Make a fresh JSON value; reject values whose canonical meaning is unclear."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value) if not f.name.startswith("_")}
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value):
            raise ValueError("JSON object keys must be strings")
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError(f"Unsupported JSON value: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(plain(value), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def freeze(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return MappingProxyType({f.name: freeze(getattr(value, f.name)) for f in fields(value)
                                 if not f.name.startswith("_")})
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value):
            raise ValueError("JSON object keys must be strings")
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(freeze(v) for v in value)
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError(f"Unsupported JSON value: {type(value).__name__}")


def _uuid(value: str | None, name: str) -> None:
    if value is None:
        return
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{name} must be a canonical UUID") from exc


class DomainError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 field_errors: Mapping[str, Any] | None = None,
                 current_revision: int | None = None, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.field_errors = freeze(field_errors) if field_errors is not None else None
        self.current_revision = current_revision
        self.details = freeze(details) if details is not None else None

    def to_dict(self) -> dict[str, Any]:
        result = {"code": self.code, "message": self.message, "retryable": self.retryable}
        for key in ("field_errors", "current_revision", "details"):
            value = getattr(self, key)
            if value is not None:
                result[key] = plain(value)
        return result


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    version: str
    digest: str
    kind: str = "role"

    def __post_init__(self):
        if not self.id or not self.version or not HASH.fullmatch(self.digest):
            raise ValueError("Artifact references require ID, version, and SHA-256 digest")
        if self.kind not in ("role", "plugin", "foundation", "scope"):
            raise ValueError("Unknown artifact kind")


@dataclass(frozen=True)
class Principal:
    id: str
    kind: PrincipalKind
    display_name: str = ""
    active: bool = True

    def __post_init__(self):
        if not isinstance(self.id, str):
            raise ValueError("principal.id is required")
        _uuid(self.id, "principal.id")
        if self.kind not in ("human", "agent", "system", "integration") or type(self.active) is not bool:
            raise ValueError("Invalid principal")


@dataclass(frozen=True)
class InvocationContext:
    principal: Principal
    business_id: str | None = None
    home_business_id: str | None = None
    allowed_business_ids: frozenset[str] = field(default_factory=frozenset)
    global_capabilities: frozenset[str] = field(default_factory=frozenset)
    profile_revision: int | None = None
    policy_revision: int | None = None
    role_artifact_ref: ArtifactRef | None = None
    enabled_plugin_refs: tuple[ArtifactRef, ...] = ()
    source: Source = "mcp"
    request_id: str = field(default_factory=new_id)
    session_registration_id: str | None = None
    inbox_session_id: str | None = None
    inbox_generation: int | None = None
    initiated_by_principal_id: str | None = None
    review_business_id: str | None = None
    configuration_revision: str | None = None
    context_refresh_required: bool = False
    job_id: str | None = None
    # Trusted provenance set by the owner-session transport from credential
    # metadata. It is never populated from operation arguments or options.
    owner_session_business_id: str | None = None
    # Trusted subordinate identity, populated only from an authenticated
    # server-side assignment binding. Caller arguments never set these fields.
    delegation_binding_id: str | None = None
    delegation_assignment_id: str | None = None
    delegation_generation: int | None = None
    # Trusted run binding set only by the host automation bridge for a script's own Ordubashy calls. It is never read from
    # arguments or options and is distinct from the system-only ``job_id``; refresh and nested calls preserve it.
    automation_run_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.principal, Principal):
            raise ValueError("Context requires an authenticated Principal")
        for name in ("business_id", "home_business_id", "review_business_id", "initiated_by_principal_id", "job_id",
                     "owner_session_business_id",
                     "delegation_binding_id", "delegation_assignment_id", "automation_run_id"):
            _uuid(getattr(self, name), name)
        object.__setattr__(self, "allowed_business_ids", frozenset(self.allowed_business_ids))
        object.__setattr__(self, "global_capabilities", frozenset(self.global_capabilities))
        object.__setattr__(self, "enabled_plugin_refs", tuple(self.enabled_plugin_refs))
        for value in self.allowed_business_ids:
            _uuid(value, "allowed_business_ids")
        if self.source not in ("mcp", "dashboard", "worker"):
            raise ValueError("Unknown invocation source")
        if self.owner_session_business_id is not None and (
                self.principal.kind != "human" or self.source != "mcp"):
            raise ValueError("Owner-session provenance requires the authenticated human MCP route")
        if self.job_id is not None and (self.source != "worker" or self.principal.kind != "system"):
            raise ValueError("Only authenticated system worker invocations may carry a registered job ID")
        delegation_values = (self.delegation_binding_id, self.delegation_assignment_id, self.delegation_generation)
        if any(value is not None for value in delegation_values):
            if (self.principal.kind != "agent" or self.source not in ("mcp", "worker")
                    or any(value is None for value in delegation_values)
                    or type(self.delegation_generation) is not int or self.delegation_generation < 1):
                raise ValueError("Delegation context requires a bound agent, assignment, and positive generation")
        if self.automation_run_id is not None and (self.principal.kind != "agent" or self.source != "mcp"
                                                   or self.job_id is not None or self.delegation_binding_id is not None):
            raise ValueError("An automation run binding requires the bridge's agent context without a job or delegation")
        for name in ("profile_revision", "policy_revision"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer")

    @property
    def principal_id(self) -> str:
        return self.principal.id

    @property
    def principal_kind(self) -> PrincipalKind:
        return self.principal.kind

    @property
    def agent_id(self) -> str | None:
        return self.principal.id if self.principal.kind == "agent" else None

    def select_business(self, business_id: str | None) -> InvocationContext:
        target = business_id if business_id is not None else self.business_id
        if target is not None and target not in self.allowed_business_ids:
            raise DomainError("forbidden", "Project is outside the principal's scope")
        return replace(self, business_id=target)


@dataclass(frozen=True)
class InvocationOptions:
    expected_revision: int | None = None
    idempotency_key: str | None = None
    business_id: str | None = None
    review_business_id: str | None = None
    supersedes_signoff_id: str | None = None
    expected_superseded_status_revision: int | None = None
    # Transport-internal read projection hint. It is deliberately absent from
    # public option schemas: callers select it through MCP response_detail.
    omit_work_briefing: bool = False

    def __post_init__(self):
        for name in ("expected_revision", "expected_superseded_status_revision"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise DomainError("validation_error", f"{name} must be a positive integer")
        if type(self.omit_work_briefing) is not bool:
            raise DomainError("validation_error", "omit_work_briefing must be a boolean")
        if self.idempotency_key is not None and (
                not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip()
                or len(self.idempotency_key) > 200 or "\0" in self.idempotency_key):
            raise DomainError("validation_error", "idempotency_key must contain 1-200 characters")
        if bool(self.supersedes_signoff_id) != (self.expected_superseded_status_revision is not None):
            raise DomainError("validation_error", "Signoff replacement requires its ID and current status revision")
        for name in ("business_id", "review_business_id", "supersedes_signoff_id"):
            try:
                _uuid(getattr(self, name), name)
            except ValueError as exc:
                raise DomainError("validation_error", str(exc)) from exc

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> InvocationOptions:
        values = dict(values or {})
        unknown = values.keys() - {f.name for f in fields(cls)}
        if unknown:
            raise DomainError("validation_error", "Unknown invocation options",
                              field_errors={key: "unknown field" for key in sorted(unknown)})
        return cls(**values)


@dataclass(frozen=True)
class ResourceRef:
    resource_type: str
    resource_id: str
    business_id: str | None = None
    namespace: str | None = None

    def __post_init__(self):
        if (not isinstance(self.resource_type, str) or not OPERATION_NAME.fullmatch(self.resource_type)
                or not isinstance(self.resource_id, str) or not self.resource_id):
            raise ValueError("Invalid resource reference")
        _uuid(self.business_id, "resource.business_id")


@dataclass(frozen=True)
class ExpectedTarget:
    resource: ResourceRef
    revision: int | None = None
    content_hash: str | None = None
    exists: bool = True

    def __post_init__(self):
        if not isinstance(self.resource, ResourceRef):
            raise ValueError("Expected target requires a resource reference")
        if type(self.exists) is not bool:
            raise ValueError("Expected existence must be a boolean")
        if self.revision is not None and (type(self.revision) is not int or self.revision < 1):
            raise ValueError("Expected revision must be positive")
        if self.content_hash is not None and not HASH.fullmatch(self.content_hash):
            raise ValueError("Expected content hash must be SHA-256")
        if not self.exists and (self.revision is not None or self.content_hash is not None):
            raise ValueError("An absent resource cannot have an expected revision or hash")


@dataclass(frozen=True)
class PreparedChange:
    kind: Literal["record", "file", "job"]
    resource: ResourceRef
    action: str
    values: Mapping[str, Any]
    operation_name: str | None = None

    def __post_init__(self):
        if not isinstance(self.resource, ResourceRef):
            raise ValueError("Prepared change requires a resource reference")
        if self.kind not in ("record", "file", "job") or not self.action:
            raise ValueError("Invalid prepared change")
        if self.operation_name is not None and not OPERATION_NAME.fullmatch(self.operation_name):
            raise ValueError("Invalid effect operation name")
        object.__setattr__(self, "values", freeze(self.values))


@dataclass(frozen=True)
class PreviewResource:
    resource_type: str
    resource_id: str
    label: str


@dataclass(frozen=True)
class FieldChange:
    path: str
    before: Any
    after: Any

    def __post_init__(self):
        object.__setattr__(self, "before", freeze(self.before))
        object.__setattr__(self, "after", freeze(self.after))


@dataclass(frozen=True)
class StructuredPreview:
    resource: PreviewResource
    fields: tuple[FieldChange, ...]
    kind: Literal["structured"] = field(default="structured", init=False)

    def __post_init__(self):
        object.__setattr__(self, "fields", tuple(sorted(self.fields, key=lambda item: item.path)))


@dataclass(frozen=True)
class TextPreview:
    resource: PreviewResource
    before: str
    after: str
    diff: str
    kind: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True)
class ChangePreview:
    title: str
    rationale: str
    affected_scope: Mapping[str, Any]
    resources: tuple[PreviewResource, ...]
    changes: tuple[StructuredPreview | TextPreview, ...]
    effects: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "affected_scope", freeze(self.affected_scope))
        for name in ("resources", "changes", "effects"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class ChangeSet:
    operation_name: str
    operation_version: int
    principal_id: str
    target_namespace: str
    arguments: Mapping[str, Any]
    affected_scope: Mapping[str, Any]
    expected_targets: tuple[ExpectedTarget, ...]
    changes: tuple[PreparedChange, ...]
    preview: ChangePreview | Mapping[str, Any]
    policy_binding: Mapping[str, Any] = field(default_factory=dict)
    review_business_id: str | None = None
    proposer_agent_id: str | None = None
    initiated_by_principal_id: str | None = None
    review_scope: Mapping[str, Any] | None = None
    encoding_version: int = 1
    _canonical: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        if not OPERATION_NAME.fullmatch(self.operation_name):
            raise ValueError("Invalid operation name")
        if not isinstance(self.principal_id, str):
            raise ValueError("Prepared changes require a principal ID")
        if type(self.operation_version) is not int or self.operation_version < 1 or not self.target_namespace:
            raise ValueError("Invalid operation version/namespace")
        for name in ("principal_id", "review_business_id", "proposer_agent_id", "initiated_by_principal_id"):
            _uuid(getattr(self, name), name)
        for name in ("arguments", "affected_scope", "policy_binding", "preview"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        object.__setattr__(self, "expected_targets", tuple(self.expected_targets))
        object.__setattr__(self, "changes", tuple(self.changes))
        if any(not isinstance(value, ExpectedTarget) for value in self.expected_targets):
            raise ValueError("Expected targets must use the typed target contract")
        if any(not isinstance(value, PreparedChange) for value in self.changes):
            raise ValueError("Changes must use the typed effect contract")
        value = plain(self)
        if self.encoding_version == 1:
            if self.review_scope is not None:
                raise ValueError("Legacy plans cannot carry a review scope")
            value.pop("review_scope")
            value.pop("encoding_version")
        elif self.encoding_version == 2:
            expected = ({"kind": "business", "business_id": self.review_business_id}
                        if self.review_business_id else {"kind": "global"})
            if self.review_scope != expected:
                raise ValueError("Prepared review scope is invalid")
            object.__setattr__(self, "review_scope", freeze(self.review_scope))
        else:
            raise ValueError("Unsupported prepared plan encoding")
        object.__setattr__(self, "_canonical", canonical_bytes(value))

    @property
    def canonical(self) -> bytes:
        return self._canonical

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(self._canonical).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    @classmethod
    def from_bytes(cls, value: bytes, *, expected_hash: str | None = None) -> ChangeSet:
        try:
            row = json.loads(value)
            expected = tuple(ExpectedTarget(resource=ResourceRef(**x["resource"]),
                                           **{k: v for k, v in x.items() if k != "resource"})
                             for x in row.pop("expected_targets"))
            changes = tuple(PreparedChange(resource=ResourceRef(**x["resource"]),
                                           **{k: v for k, v in x.items() if k != "resource"})
                            for x in row.pop("changes"))
            result = cls(**row, expected_targets=expected, changes=changes)
            if result.canonical != value:
                raise ValueError("Stored plan is not canonical")
            if expected_hash is not None and result.payload_hash != expected_hash.removeprefix("sha256:"):
                raise ValueError("Stored plan hash does not match")
            return result
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise DomainError("invalid_plan", "Stored prepared change is invalid") from exc


class TargetSet(tuple):
    """Trusted marker for the resources an automation run-bound invocation is about to touch, created only by the engine.

    A plain tuple - including the empty one every preflight passes - is *unresolved*: a resource-limited automation grant never applies
    to it. ``stage="call"`` is the preflight of a bound call: the items are empty and ``arguments`` carries the bound call, so a
    supported single-record call can be tied to its exact record (reads end there). ``stage="prepared"`` is the complete prepared target
    set of a mutation (every expected target and every changed resource); a resource-limited grant applies only when that whole set is
    exactly the one granted record."""

    stage: str
    arguments: Mapping[str, Any]

    def __new__(cls, items=(), stage="call", arguments=None):
        self = super().__new__(cls, items)
        self.stage, self.arguments = stage, {} if arguments is None else arguments
        return self


@dataclass(frozen=True)
class PolicyDecision:
    mode: Mode
    provenance: str = "fallback"
    binding: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.mode not in ("allow", "signoff", "deny"):
            raise ValueError("Invalid policy mode")
        object.__setattr__(self, "binding", freeze(self.binding))


@dataclass(frozen=True)
class OperationResult:
    request_id: str
    status: Literal["ok", "signoff_required", "accepted", "error"]
    data: Any = None
    signoff: Mapping[str, Any] | None = None
    job: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    warnings: tuple[str, ...] = ()
    configuration_revision: str | None = None
    context_refresh_required: bool | None = None

    def __post_init__(self):
        if self.status not in ("ok", "signoff_required", "accepted", "error"):
            raise ValueError("Invalid operation result status")
        if (self.status == "error") != (self.error is not None):
            raise ValueError("Error status requires an error, success forbids one")
        if self.status == "signoff_required" and self.signoff is None:
            raise ValueError("Signoff result requires a proposal reference")
        for name in ("data", "signoff", "job", "error"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def to_dict(self) -> dict[str, Any]:
        result = {"request_id": self.request_id, "status": self.status}
        for name in ("data", "signoff", "job", "error", "configuration_revision", "context_refresh_required"):
            value = getattr(self, name)
            if value is not None:
                result[name] = plain(value)
        if self.status != "error":
            result["warnings"] = list(self.warnings)
        return result

    @classmethod
    def ok(cls, data: Any, *, request_id: str = "", warnings=()) -> OperationResult:
        return cls(request_id=request_id, status="ok", data=data, warnings=tuple(warnings))

    @classmethod
    def failure(cls, error: DomainError, *, request_id: str = "") -> OperationResult:
        return cls(request_id=request_id, status="error", error=error.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> OperationResult:
        return cls(**value)
