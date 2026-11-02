"""Known operation metadata and installed, typed domain implementations."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import cached_property
import json
import threading
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from qorqut_operations.contracts import DomainError, OPERATION_NAME, canonical_bytes, freeze, plain

# Schemas that already passed the Draft 2020-12 metaschema, least recently used first. Only successes are kept.
SCHEMA_CHECK_CACHE_LIMIT = 2048
_CHECKED_SCHEMAS: OrderedDict[bytes, None] = OrderedDict()
_CHECKED_SCHEMAS_LOCK = threading.Lock()


def check_schema_cached(schema) -> None:
    """Raise unless ``schema`` is a valid Draft 2020-12 schema, checking each distinct schema once per process.

    The key is the schema's complete canonical JSON, never its identity or an operation name, so a changed schema is
    a different key and an equivalent one built elsewhere is a hit. A miss validates a value decoded from the key
    itself; an invalid schema is checked, and rejected, every time.
    """
    key = canonical_bytes(schema)
    with _CHECKED_SCHEMAS_LOCK:
        if key in _CHECKED_SCHEMAS:
            _CHECKED_SCHEMAS.move_to_end(key)
            return
    Draft202012Validator.check_schema(json.loads(key))
    with _CHECKED_SCHEMAS_LOCK:
        _CHECKED_SCHEMAS[key] = None
        _CHECKED_SCHEMAS.move_to_end(key)
        while len(_CHECKED_SCHEMAS) > SCHEMA_CHECK_CACHE_LIMIT:
            _CHECKED_SCHEMAS.popitem(last=False)


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    supports_signoff: bool
    effect_kind: str
    exposure: frozenset[str] = field(default_factory=lambda: frozenset({"mcp", "dashboard", "worker"}))
    owner_only: bool = False

    def __post_init__(self):
        if not OPERATION_NAME.fullmatch(self.name):
            raise ValueError("Operation names must be snake_case")
        if self.effect_kind not in ("read", "records", "files", "job", "compound"):
            raise ValueError("Unknown operation effect kind")
        if self.effect_kind == "read" and self.supports_signoff:
            raise ValueError("Read operations cannot require signoff")
        if self.owner_only and "mcp" in self.exposure:
            raise ValueError("Owner-only operations cannot be agent tools")
        object.__setattr__(self, "exposure", frozenset(self.exposure))


@dataclass(frozen=True)
class OperationDescriptor:
    name: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any] = field(default_factory=lambda: {"type": "object"})
    version: int = 1
    aliases: tuple[str, ...] = ()
    plugin_aliases: Mapping[str, str] = field(default_factory=dict)
    alias_bindings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    effect_kind: str = "records"
    supports_signoff: bool = True
    scope_kinds: frozenset[str] = field(default_factory=lambda: frozenset({"business"}))
    exposure: frozenset[str] = field(default_factory=lambda: frozenset({"mcp", "dashboard", "worker"}))
    owner_only: bool = False
    # Selected dashboard/worker routes may be callable through the
    # authenticated owner route without becoming ordinary agent MCP tools.
    owner_session: bool = False
    plugin_requirement: str | None = None
    global_capability: str | None = None
    derived_effects: tuple[str, ...] = ()
    signoff_category: str = "internal_change"
    description: str = ""
    read: Callable | None = None
    prepare: Callable | None = None
    apply: Callable | None = None
    validate_targets: Callable | None = None
    # Provider calls run after authorization, outside any transaction, before apply's short write transaction.
    external: Callable | None = None
    # Identity rows this operation re-derives in full inside validate_targets, so
    # the engine must not also compare their revision counters.  Declaring a type
    # here is a statement that approval re-checks the same facts from current
    # state; an operation that only reads such a row while deciding must not.
    rederived_basis: frozenset[str] = field(default_factory=frozenset)
    resolve_namespace: Callable | None = None
    # Optional current-authority check run before a saved idempotency receipt is returned: (context, arguments, result, uow).
    # It may only refuse; a replay never reapplies the mutation. Absent for operations whose receipts carry no private data.
    replay_validator: Callable | None = None
    external_read: bool = False
    transport_only: bool = False
    # Read-only widening of the Global gate. A caller in the global namespace is admitted when it holds
    # ``global_capability`` OR one capability named here. The primary stays required of every declaration (it is what
    # business policy, catalogs and bindings continue to name); an empty set, the default, leaves a descriptor exactly as
    # before. Allowed only on a plain read with no composed effects, never on a mutation or platform action.
    global_read_capability_alternatives: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        CatalogEntry(self.name, self.supports_signoff, self.effect_kind, self.exposure, self.owner_only)
        if type(self.version) is not int or self.version < 1:
            raise ValueError("Operation version must be positive")
        if self.external is not None and self.supports_signoff:
            raise ValueError("External provider phases cannot be staged for signoff")
        if self.transport_only:
            if (self.exposure != {"mcp"} or self.supports_signoff or self.effect_kind != "compound"
                    or any(handler is not None for handler in (self.read, self.prepare, self.apply))):
                raise ValueError("Transport-only descriptors require MCP-only compound metadata without domain handlers or blanket signoff")
        elif self.effect_kind == "read":
            if self.read is None or self.prepare is not None or self.apply is not None:
                raise ValueError("Read descriptors require only a read handler")
        elif self.prepare is None or self.apply is None or self.read is not None:
            raise ValueError("Mutation descriptors require prepare and apply handlers")
        if self.signoff_category not in {"internal_change", "platform_action"}:
            raise ValueError("Unknown Signoff category")
        if self.signoff_category == "platform_action" and (not self.supports_signoff or self.effect_kind == "read"):
            raise ValueError("Platform proposals require exact-change Signoff support")
        schema = plain(self.input_schema)
        if schema.get("type") != "object":
            raise ValueError("Operation inputs must be objects")
        if schema.get("additionalProperties", False) is not False:
            raise ValueError("Operation inputs must reject unknown top-level fields")
        schema["additionalProperties"] = False
        check_schema_cached(schema)
        check_schema_cached(plain(self.output_schema))
        object.__setattr__(self, "input_schema", freeze(schema))
        object.__setattr__(self, "output_schema", freeze(self.output_schema))
        for name in ("scope_kinds", "exposure"):
            object.__setattr__(self, name, frozenset(getattr(self, name)))
        for name in ("aliases", "derived_effects"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if set(self.plugin_aliases) - set(self.aliases) or set(self.alias_bindings) - set(self.aliases):
            raise ValueError("Alias grants and bindings must refer to declared aliases")
        object.__setattr__(self, "plugin_aliases", freeze(self.plugin_aliases))
        object.__setattr__(self, "alias_bindings", freeze(self.alias_bindings))
        if not self.scope_kinds or not self.scope_kinds <= {"business", "global", "agent_workspace"}:
            raise ValueError("Unknown operation scope kind")
        alternatives = self.global_read_capability_alternatives
        if isinstance(alternatives, (str, bytes)):
            raise ValueError("Alternative global capabilities are a set of names")
        alternatives = frozenset(alternatives)
        if alternatives:
            if (self.effect_kind != "read" or self.read is None or self.supports_signoff or self.derived_effects
                    or self.signoff_category != "internal_change" or self.owner_only or self.transport_only):
                raise ValueError("Alternative global capabilities are allowed only on plain read operations")
            if not self.global_capability or "global" not in self.scope_kinds:
                raise ValueError("Alternative global capabilities require the primary capability on a Global-capable operation")
            if any(not isinstance(item, str) or not item or item == self.global_capability for item in alternatives):
                raise ValueError("Alternative global capabilities must be other, nonempty names")
        object.__setattr__(self, "global_read_capability_alternatives", alternatives)

    @property
    def metadata(self) -> CatalogEntry:
        return CatalogEntry(self.name, self.supports_signoff, self.effect_kind, self.exposure, self.owner_only)

    def global_capability_selected(self, granted) -> str | None:
        """The one declared capability that admits a caller holding ``granted``: the primary when held, otherwise the
        first (by name) held alternative, else ``None``. The single eligibility rule shared by namespace validation,
        policy resolution and discovery; composed effects and mutations only ever honour the primary."""
        if self.global_capability in granted:
            return self.global_capability
        return next((item for item in sorted(self.global_read_capability_alternatives) if item in granted), None)

    def global_capability_evidence(self, granted) -> dict[str, Any]:
        """Policy-binding evidence of the capability gate; alternatives appear only on descriptors that declare them."""
        evidence: dict[str, Any] = {"global_capability": self.global_capability}
        if self.global_read_capability_alternatives:
            evidence["global_capability_alternatives"] = sorted(self.global_read_capability_alternatives)
            evidence["global_capability_selected"] = self.global_capability_selected(granted)
        return evidence

    @cached_property
    def business_policy(self):
        """Reuse immutable operation metadata; authority is still resolved per call."""
        return (replace(self, global_capability=None, global_read_capability_alternatives=frozenset())
                if self.global_capability else self)

    @cached_property
    def account_policy(self):
        """Only account email replaces its business plugin gate with account authority."""
        return (replace(self, plugin_requirement=None)
                if self.plugin_requirement == "email-management" and self.global_capability == "manage_fastmail"
                else self)

    def validate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            arguments = plain(arguments)
        except ValueError as exc:
            raise DomainError("validation_error", str(exc)) from exc
        errors = sorted(Draft202012Validator(plain(self.input_schema), format_checker=FormatChecker()).iter_errors(arguments),
                        key=lambda e: tuple(str(x) for x in e.absolute_path))
        if errors:
            fields = {}
            for error in errors:
                path = '.'.join(str(x) for x in error.absolute_path) or '$'
                fields.setdefault(path, []).append(error.message)
            # Required/extra-field errors share the same object path. A dict
            # comprehension silently kept only the final missing field.
            raise DomainError("validation_error", "Operation arguments are invalid", field_errors={
                path: '; '.join(messages) for path, messages in fields.items()})
        return arguments

    def validate_output(self, result):
        if result.status not in ("ok", "accepted") or result.data is None:
            return result
        errors = list(Draft202012Validator(plain(self.output_schema), format_checker=FormatChecker()).iter_errors(plain(result.data)))
        if errors:
            raise DomainError("invalid_result", "Operation result violates its registered output contract")
        return result


class OperationRegistry:
    """Trusted operation metadata and handlers. Registration belongs to assembly code, never to a caller:
    ``freeze`` ends it, so nothing can be declared or installed once the application serves traffic."""

    def __init__(self, entries=()):
        self._entries: dict[str, CatalogEntry] = {}
        self._descriptors: dict[str, OperationDescriptor] = {}
        self._aliases: dict[str, str] = {}
        self._frozen = False
        for entry in entries:
            self.declare(entry)

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> OperationRegistry:
        self._frozen = True
        return self

    def _require_open(self) -> None:
        if self._frozen:
            raise ValueError("Operation registry is frozen; registration belongs to trusted assembly")

    def declare(self, entry: CatalogEntry) -> None:
        self._require_open()
        old = self._entries.get(entry.name)
        if old is not None and old != entry:
            raise ValueError(f"Operation metadata conflict: {entry.name}")
        self._entries[entry.name] = entry

    def register(self, descriptor: OperationDescriptor) -> None:
        self._require_open()
        if descriptor.name in self._descriptors:
            raise ValueError(f"Operation already installed: {descriptor.name}")
        if descriptor.name in self._aliases:
            raise ValueError("Operation name conflicts with an alias")
        old = self._entries.get(descriptor.name)
        if old is not None and (old.supports_signoff != descriptor.supports_signoff
                                or old.owner_only != descriptor.owner_only
                                or not descriptor.exposure <= old.exposure):
            raise ValueError(f"Operation registration changes its authority metadata: {descriptor.name}")
        for alias in descriptor.aliases:
            if not OPERATION_NAME.fullmatch(alias) or alias in self._aliases or alias in self._entries:
                raise ValueError(f"Invalid or duplicate operation alias: {alias}")
        self._entries[descriptor.name] = descriptor.metadata
        self._descriptors[descriptor.name] = descriptor
        self._aliases.update({alias: descriptor.name for alias in descriptor.aliases})

    def canonical_name(self, name: str) -> str:
        canonical = self._aliases.get(name, name)
        if canonical not in self._entries:
            raise DomainError("operation_unavailable", "Unknown operation")
        return canonical

    def metadata(self, name: str) -> CatalogEntry:
        return self._entries[self.canonical_name(name)]

    def known(self, name: str) -> bool:
        return name in self._entries or name in self._aliases

    def get(self, name: str) -> OperationDescriptor:
        name = self.canonical_name(name)
        if name not in self._descriptors:
            raise DomainError("operation_unavailable", "Operation is not installed")
        return self._descriptors[name]

    def required_plugin_for(self, name: str) -> str | None:
        descriptor = self.get(name)
        return descriptor.plugin_aliases.get(name, descriptor.plugin_requirement)

    def bind_arguments(self, name: str, arguments):
        descriptor = self.get(name)
        if not isinstance(arguments, Mapping):
            return descriptor.validate(arguments)
        arguments = plain(arguments)
        for key, value in descriptor.alias_bindings.get(name, {}).items():
            if key in arguments and arguments[key] != plain(value):
                raise DomainError("validation_error", "Alias has a fixed operation binding", field_errors={key: "fixed by alias"})
            arguments[key] = plain(value)
        return descriptor.validate(arguments)

    def schema_for(self, name: str):
        descriptor = self.get(name)
        schema = plain(descriptor.input_schema)
        fixed = descriptor.alias_bindings.get(name, {})
        for key in fixed:
            schema.get("properties", {}).pop(key, None)
        if "required" in schema:
            schema["required"] = [key for key in schema["required"] if key not in fixed]
        return schema

    def entries(self) -> tuple[CatalogEntry, ...]:
        return tuple(self._entries[name] for name in sorted(self._entries))

    def installed(self) -> tuple[OperationDescriptor, ...]:
        return tuple(self._descriptors[name] for name in sorted(self._descriptors))


Registry = OperationRegistry
