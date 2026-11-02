"""Trusted bounded programs and typed durable job preparation."""

from dataclasses import dataclass, field
from collections.abc import Mapping
from jsonschema import Draft202012Validator, FormatChecker

from qorqut_operations.contracts import DomainError, canonical_hash, freeze, new_id, plain


@dataclass(frozen=True)
class Program:
    id: str
    version: int
    input_schema: Mapping
    run: object
    permissions: Mapping = field(default_factory=dict)
    outputs: tuple[str, ...] = ()
    resource_types: tuple[str, ...] = ()
    discretionary: tuple[str, ...] = ()
    global_capabilities: tuple[str, ...] = ()
    unavailable_reason: str | None = None
    timeout_seconds: int = 900
    scope_kinds: tuple[str, ...] = ("business",)
    external: bool = False
    recover: object = None
    # Host-owned programs that only an approved domain operation may queue (for example an owner-approved send) are
    # not schedulable as routines. Not part of the frozen definition, so every existing digest is unchanged.
    routine_allowed: bool = True

    def __post_init__(self):
        if not self.id or type(self.version) is not int or self.version < 1:
            raise ValueError("Programs require a stable ID and positive definition version")
        if self.run is None and not self.unavailable_reason:
            raise ValueError("Unavailable programs require an explicit reason")
        if self.external and not callable(self.recover):
            raise ValueError("External programs require an explicit recovery implementation")
        Draft202012Validator.check_schema(plain(self.input_schema))
        object.__setattr__(self, "input_schema", freeze(self.input_schema))
        object.__setattr__(self, "permissions", freeze(self.permissions))

    @property
    def definition(self):
        value = {"id": self.id, "version": self.version, "input_schema": plain(self.input_schema),
            "permissions": plain(self.permissions), "outputs": list(self.outputs),
            "resource_types": list(self.resource_types), "discretionary": list(self.discretionary),
            "global_capabilities": list(self.global_capabilities), "timeout_seconds": self.timeout_seconds,
            "scope_kinds": list(self.scope_kinds), "external": self.external,
            "unavailable_reason": self.unavailable_reason}
        return {**value, "digest": canonical_hash(value)}

    def validate(self, arguments):
        errors = sorted(Draft202012Validator(plain(self.input_schema), format_checker=FormatChecker()).iter_errors(plain(arguments)), key=lambda e: str(e.path))
        if errors:
            raise DomainError("validation_error", errors[0].message)
        if self.unavailable_reason:
            raise DomainError("operation_unavailable", self.unavailable_reason)


class ProgramRegistry:
    """Trusted program definitions. Registration belongs to assembly code: ``freeze`` ends it (the host freezes
    the registry before serving), and a program ID can be registered only once."""

    def __init__(self):
        self._programs = {}
        self._authority = None
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> "ProgramRegistry":
        self._frozen = True
        return self

    @property
    def authority(self):
        return self._authority

    def bind_authority(self, authority):
        """The one ``BackgroundAuthority`` that derives worker identities for these programs. Binding is not
        registration, so it stays possible after ``freeze``, but only once."""
        if self._authority is not None and self._authority is not authority:
            raise ValueError("Programs are already bound to a background authority")
        self._authority = authority

    def register(self, program):
        if self._frozen:
            raise ValueError("Program registry is frozen; registration belongs to trusted assembly")
        if not isinstance(program, Program) or program.id in self._programs:
            raise ValueError("Program registration must be unique")
        self._programs[program.id] = program
        return program

    def __iter__(self):
        return iter(self._programs.values())

    def get(self, kind):
        program = self._programs.get(kind)
        return ({**program.definition, "validate": program.validate} if program else None)

    def require(self, kind, *, available=True):
        program = self._programs.get(kind)
        if program is None:
            raise DomainError("operation_unavailable", "Named background program is not installed")
        if available and program.unavailable_reason:
            raise DomainError("operation_unavailable", program.unavailable_reason)
        return program

    def job_values(self, context, kind, arguments, uow, *, job_id=None, dedupe_key=None, definition_extra=None):
        program = self.require(kind)
        program.validate(arguments)
        if ("business" if context.business_id else "global") not in program.scope_kinds:
            raise DomainError("forbidden", "Registered program does not support this target scope")
        if self.authority is None:
            raise DomainError("operation_unavailable", "Background authority is not installed")
        worker = self.authority.worker_id(kind, uow)
        self.authority.require_worker(worker, context.business_id, uow)
        identity = job_id or new_id()
        if set(definition_extra or {}) & set(program.definition):
            raise DomainError("invalid_plan", "Job attribution cannot replace a registered definition")
        definition = {**program.definition, **(definition_extra or {})}
        return {"id": identity, "business_id": context.business_id,
            "target_namespace": "business:" + context.business_id if context.business_id else "global",
            "kind": kind, "principal_id": worker, "initiated_by_principal_id": (context.initiated_by_principal_id
                if context.principal_kind == "system" else context.principal_id),
            "arguments_json": plain(arguments), "definition_ref_json": definition,
            "dedupe_key": dedupe_key or identity, "state": "queued"}

    def prepare_job(self, builder, uow, kind, arguments, *, definition_extra=None):
        values = self.job_values(builder.ctx, kind, arguments, uow, definition_extra=definition_extra)
        builder.record("jobs", values["id"], None, values)
        builder.effects.append("Queues a registered background program; never launches a project agent.")
        return values["id"]


@dataclass(frozen=True)
class ProgramResult:
    outputs: tuple = ()
    data: Mapping = field(default_factory=dict)
    usage: Mapping = field(default_factory=dict)
    cost_usd: float = 0.0
    completion_error: Mapping | None = None

    def to_dict(self):
        result = {"outputs": plain(self.outputs), "data": plain(self.data), "usage": plain(self.usage), "cost_usd": self.cost_usd}
        if self.completion_error is not None:
            result["completion_error"] = plain(self.completion_error)
        return result


@dataclass(frozen=True)
class ProgramPending:
    retry_after: int = 30
