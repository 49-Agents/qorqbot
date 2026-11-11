"""Trusted schema, repository and hook assembly for one Operations store.

The engine owns identity, authority and receipt storage. Retained domains contribute their own table
specifications, DDL, same-transaction hooks and file/record lifecycle callbacks as small typed values, built by
trusted assembly code before the service starts. There is no dynamic plugin loading and no registration entry
point reachable from HTTP/MCP input: a contribution is a frozen dataclass of this module, any other value
(a mapping, JSON, a string) is refused, and ``AssemblyBuilder.freeze`` ends registration for good.

Same-transaction hooks (``TransactionContribution.finalizers``, the pre-commit half of a change publisher)
run before ``COMMIT`` and roll the whole unit back when they fail. Post-commit notification runs after the unit
is durable and can never undo or fail it.

A missing required hook is an error at ``freeze`` time, never a silent no-op: the engine cannot finish a file
receipt or apply prepared records without the domain's history callbacks being named explicitly, and a resource type
whose availability bookkeeping a contribution declares required (``required_lifecycles``) must have its lifecycle
provider. A file resource with no declared requirement has no lifecycle callbacks: that is explicit, not an omission.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import re
from types import MappingProxyType
from typing import Any, Protocol

from qorqut_operations.contracts import ChangeSet, ResourceRef, canonical_bytes
from qorqut_operations.storage.core_schema import CORE_SCHEMA
from qorqut_operations.storage.specs import CORE_IMMUTABLE_FIELDS, CORE_TABLES, TableSpec

OWNER = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
CORE_OWNER = "operations"
# Engine-owned storage names no contribution may declare or reuse (``change_sequences`` is not a repository entry,
# but it is part of the engine's schema and of the validated inventory).
RESERVED_TABLES = frozenset({"change_sequences", "target_identity"})


class AssemblyError(ValueError):
    """Trusted composition is invalid or incomplete. Never a request-time error."""


# -- typed hook ports ---------------------------------------------------------------------------------------------

class ChangePublisher(Protocol):
    """External change publication (the host's change feed): ``capture`` reads inside the writing transaction
    before commit; ``publish_commit`` runs once after a successful commit and must not raise."""

    def capture(self, connection) -> Any: ...

    def publish_commit(self, *, captured, operation: str, business_id, wrote: bool = True) -> None: ...


class ReceiptHistory(Protocol):
    """Domain history for verified file results, called in the finalising transaction before the receipt
    is marked applied. Required: the engine will not finish a file receipt without it."""

    def capture_receipt(self, uow, receipt: Mapping[str, Any], plan: ChangeSet) -> None: ...


class ResourceLifecycle(Protocol):
    """Availability bookkeeping for one resource type touched by a file receipt (for v1: ``documents``)."""

    def applying(self, uow, resource: ResourceRef) -> None: ...

    def conflicted(self, uow, resource: ResourceRef) -> None: ...

    def applied(self, uow, resource: ResourceRef) -> None: ...

    def released(self, uow, resource: ResourceRef) -> None: ...


class RecordEffectHooks(Protocol):
    """Domain behaviour around the generic prepared-record application.

    ``apply_effect`` is offered every record/job effect first and returns the rows it wrote, or ``None`` to
    leave the effect to the generic insert/update/delete path (Story event allocation, collection limits and
    legacy Action inserts live behind it). ``force_revision`` names updates that must advance a record's
    revision even when its own fields are unchanged. ``record_applied`` is the retained domain's history
    callback for every applied row and ``tracked_tables`` the tables whose before-image it needs."""

    tracked_tables: frozenset[str]

    def apply_effect(self, context, plan: ChangeSet, change, uow, repository, key, values) -> Sequence[Mapping[str, Any]] | None: ...

    def force_revision(self, plan: ChangeSet, change) -> bool: ...

    def record_applied(self, uow, plan: ChangeSet, table: str, before, after, *, actor_id=None) -> None: ...


def _require_methods(value: Any, kind: str, names: Sequence[str]) -> None:
    missing = [name for name in names if not callable(getattr(value, name, None))]
    if missing:
        raise AssemblyError(f"{kind} must provide {', '.join(missing)}")


def _owner(value: Any) -> str:
    if not isinstance(value, str) or not OWNER.fullmatch(value):
        raise AssemblyError("A contribution requires a lowercase owner name")
    return value


# -- contributions ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StorageContribution:
    """Tables, their immutable columns and the DDL statements that create them (tables, indexes, triggers and
    sequence seeds). ``validators`` receive a read-only connection when an existing store is opened.

    ``required_lifecycles`` names resource types whose availability bookkeeping this contribution cannot do
    without (v1: ``documents``); the assembly refuses to freeze unless a lifecycle provider is registered for each.

    ``required_tables`` names host-created tables the contribution's code reads or writes without owning them
    (for the engine's Signoffs: ``inbox_changes``; for program authority: ``documents``). Each must be owned by
    another contribution with a table specification (``freeze`` refuses otherwise) and must exist in the compiled
    target schema (checked at initialization and validation), so a composition that forgot one fails there
    instead of at the first request."""

    owner: str
    tables: Mapping[str, TableSpec] = field(default_factory=dict)
    immutable_fields: Mapping[str, frozenset[str]] = field(default_factory=dict)
    schema: tuple[str, ...] = ()
    validators: tuple[Callable[[Any], None], ...] = ()
    required_lifecycles: frozenset[str] = frozenset()
    required_tables: frozenset[str] = frozenset()

    def __post_init__(self):
        _owner(self.owner)
        tables = dict(self.tables)
        for name, spec in tables.items():
            if not isinstance(name, str) or not TABLE_NAME.fullmatch(name) or not isinstance(spec, TableSpec):
                raise AssemblyError("Tables require a plain name and a TableSpec")
        if self.owner != CORE_OWNER and RESERVED_TABLES & set(tables):
            raise AssemblyError(f"Table reserved by the engine: {', '.join(sorted(RESERVED_TABLES & set(tables)))}")
        required = frozenset(self.required_lifecycles)
        if isinstance(self.required_lifecycles, str) or any(
                not isinstance(name, str) or not TABLE_NAME.fullmatch(name) for name in required):
            raise AssemblyError("Required lifecycles are resource type names")
        object.__setattr__(self, "required_lifecycles", required)
        needed = frozenset(self.required_tables)
        if isinstance(self.required_tables, str) or any(
                not isinstance(name, str) or not TABLE_NAME.fullmatch(name) for name in needed):
            raise AssemblyError("Required tables are plain table names")
        if needed & set(tables):
            raise AssemblyError("A contribution cannot require a table it owns")
        object.__setattr__(self, "required_tables", needed)
        immutable = {name: frozenset(fields) for name, fields in dict(self.immutable_fields).items()}
        if set(immutable) - set(tables):
            raise AssemblyError("Immutable columns can only be declared by the table's own contribution")
        schema = tuple(self.schema)
        if any(not isinstance(statement, str) or not statement.strip() for statement in schema):
            raise AssemblyError("Schema statements must be non-empty SQL strings")
        validators = tuple(self.validators)
        if any(not callable(validator) for validator in validators):
            raise AssemblyError("Schema validators must be callable")
        object.__setattr__(self, "tables", MappingProxyType(tables))
        object.__setattr__(self, "immutable_fields", MappingProxyType(immutable))
        object.__setattr__(self, "schema", schema)
        object.__setattr__(self, "validators", validators)


@dataclass(frozen=True)
class TransactionContribution:
    """Same-transaction finalizers (run in registration order before COMMIT) and at most one post-commit
    change publisher for the whole assembly."""

    owner: str
    finalizers: tuple[Callable[[Any], None], ...] = ()
    change_publisher: ChangePublisher | None = None

    def __post_init__(self):
        _owner(self.owner)
        finalizers = tuple(self.finalizers)
        if any(not callable(finalizer) for finalizer in finalizers):
            raise AssemblyError("Finalizers must be callable")
        if len(set(map(id, finalizers))) != len(finalizers):
            raise AssemblyError("A finalizer can be registered only once")
        if self.change_publisher is not None:
            _require_methods(self.change_publisher, "Change publisher", ("capture", "publish_commit"))
        object.__setattr__(self, "finalizers", finalizers)


@dataclass(frozen=True)
class FileEffectContribution:
    """Resource availability lifecycles by resource type, and the domain history called when a file receipt
    is finished. ``receipt_history`` is required by the assembly, so exactly one contribution names it."""

    owner: str
    lifecycles: Mapping[str, ResourceLifecycle] = field(default_factory=dict)
    receipt_history: ReceiptHistory | None = None

    def __post_init__(self):
        _owner(self.owner)
        lifecycles = dict(self.lifecycles)
        for name, lifecycle in lifecycles.items():
            if not isinstance(name, str) or not TABLE_NAME.fullmatch(name):
                raise AssemblyError("Resource lifecycles are keyed by resource type")
            _require_methods(lifecycle, f"Lifecycle for {name}", ("applying", "conflicted", "applied", "released"))
        if self.receipt_history is not None:
            _require_methods(self.receipt_history, "Receipt history", ("capture_receipt",))
        object.__setattr__(self, "lifecycles", MappingProxyType(lifecycles))


@dataclass(frozen=True)
class RecordEffectContribution:
    owner: str
    hooks: RecordEffectHooks

    def __post_init__(self):
        _owner(self.owner)
        _require_methods(self.hooks, "Record effect hooks", ("apply_effect", "force_revision", "record_applied"))
        tracked = getattr(self.hooks, "tracked_tables", None)
        if not isinstance(tracked, (set, frozenset)) or any(not isinstance(name, str) for name in tracked):
            raise AssemblyError("Record effect hooks must name their tracked tables")


CONTRIBUTION_TYPES = (StorageContribution, TransactionContribution, FileEffectContribution, RecordEffectContribution)


# -- frozen result ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SchemaTarget:
    """The explicit identity of a composed target schema. The digest covers owners, DDL, table specs and
    immutable columns, so two compositions with different storage contracts cannot be mistaken for each other."""

    name: str
    version: int
    digest: str


_FREEZE_TOKEN = object()


@dataclass(frozen=True)
class Assembly:
    """An immutable, validated composition. Only ``AssemblyBuilder.freeze`` makes one."""

    target: SchemaTarget
    owners: tuple[str, ...]
    tables: Mapping[str, TableSpec]
    immutable_fields: Mapping[str, frozenset[str]]
    schema: tuple[tuple[str, tuple[str, ...]], ...]
    validators: tuple[Callable[[Any], None], ...]
    finalizers: tuple[Callable[[Any], None], ...]
    change_publisher: ChangePublisher | None
    lifecycles: Mapping[str, ResourceLifecycle]
    required_lifecycles: frozenset[str]
    required_tables: Mapping[str, tuple[str, ...]]
    receipt_history: ReceiptHistory
    record_hooks: RecordEffectHooks
    _origin: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._origin is not _FREEZE_TOKEN:
            raise AssemblyError("An Assembly can only be produced by AssemblyBuilder.freeze")


class AssemblyBuilder:
    """Single-threaded, trusted composition. The engine's own tables are always present first, so a domain
    cannot redefine them. ``core_schema`` is only for an offline upgrade that composes the engine's schema as
    an earlier release had it."""

    def __init__(self, *, target_name: str, target_version: int, core_schema: tuple[str, ...] = CORE_SCHEMA):
        if not isinstance(target_name, str) or not OWNER.fullmatch(target_name):
            raise AssemblyError("The target schema needs an explicit lowercase name")
        if type(target_version) is not int or target_version < 1:
            raise AssemblyError("The target schema needs an explicit positive version")
        self._target_name, self._target_version = target_name, target_version
        self._frozen = False
        self._storage: list[StorageContribution] = []
        self._transactions: list[TransactionContribution] = []
        self._files: list[FileEffectContribution] = []
        self._records: list[RecordEffectContribution] = []
        self.add(StorageContribution(CORE_OWNER, CORE_TABLES, CORE_IMMUTABLE_FIELDS, core_schema))

    @property
    def frozen(self) -> bool:
        return self._frozen

    def add(self, contribution) -> AssemblyBuilder:
        if self._frozen:
            raise AssemblyError("Assembly is frozen; contributions are registered before the service starts")
        kind = type(contribution)
        if kind not in CONTRIBUTION_TYPES:
            raise AssemblyError("Only typed contributions made by trusted assembly code can be registered")
        group = {StorageContribution: self._storage, TransactionContribution: self._transactions,
                 FileEffectContribution: self._files, RecordEffectContribution: self._records}[kind]
        if any(existing.owner == contribution.owner for existing in group):
            raise AssemblyError(f"{kind.__name__} already registered for owner {contribution.owner}")
        if kind is StorageContribution:
            known = {name for existing in self._storage for name in existing.tables}
            duplicate = sorted(known & set(contribution.tables))
            if duplicate:
                raise AssemblyError(f"Table already owned by another contribution: {', '.join(duplicate)}")
        elif kind is TransactionContribution:
            if contribution.change_publisher is not None and any(
                    existing.change_publisher is not None for existing in self._transactions):
                raise AssemblyError("Only one change publisher can be registered")
            registered = {id(finalizer) for existing in self._transactions for finalizer in existing.finalizers}
            if registered & set(map(id, contribution.finalizers)):
                raise AssemblyError("A finalizer can be registered only once")
        elif kind is RecordEffectContribution:
            if self._records:
                raise AssemblyError("Only one set of record effect hooks can be registered")
        elif kind is FileEffectContribution:
            duplicate = sorted({name for existing in self._files for name in existing.lifecycles}
                               & set(contribution.lifecycles))
            if duplicate:
                raise AssemblyError(f"Lifecycle already registered for resource type: {', '.join(duplicate)}")
            if contribution.receipt_history is not None and any(
                    existing.receipt_history is not None for existing in self._files):
                raise AssemblyError("Only one receipt history can be registered")
        group.append(contribution)
        return self

    def freeze(self) -> Assembly:
        if self._frozen:
            raise AssemblyError("Assembly is already frozen")
        histories = [item.receipt_history for item in self._files if item.receipt_history is not None]
        if not histories:
            raise AssemblyError("Required hook missing: file receipts need a domain receipt history")
        if not self._records:
            raise AssemblyError("Required hook missing: prepared-record application needs record effect hooks")
        provided = {name for item in self._files for name in item.lifecycles}
        required = frozenset(name for item in self._storage for name in item.required_lifecycles)
        if required - provided:
            raise AssemblyError("Required hook missing: resource lifecycle for "
                                + ", ".join(sorted(required - provided)))
        tables: dict[str, TableSpec] = {}
        immutable: dict[str, frozenset[str]] = {}
        for item in self._storage:
            tables.update(item.tables)
            immutable.update(item.immutable_fields)
        needed: dict[str, list[str]] = {}
        for item in self._storage:
            for name in sorted(item.required_tables):
                needed.setdefault(name, []).append(item.owner)
        unspecified = sorted((name, owners) for name, owners in needed.items() if name not in tables)
        if unspecified:
            raise AssemblyError("Required table has no table specification: " + "; ".join(
                f"{name} (needed by {', '.join(owners)})" for name, owners in unspecified))
        publishers = [item.change_publisher for item in self._transactions if item.change_publisher is not None]
        payload = {"name": self._target_name, "version": self._target_version, "owners": [
            {"owner": item.owner, "schema": list(item.schema)} for item in self._storage],
            "tables": {name: [list(spec.keys), spec.scope_column, spec.mutable, spec.revision_column]
                       for name, spec in sorted(tables.items())},
            "immutable_fields": {name: sorted(fields) for name, fields in sorted(immutable.items())}}
        target = SchemaTarget(self._target_name, self._target_version, hashlib.sha256(canonical_bytes(payload)).hexdigest())
        self._frozen = True
        return Assembly(
            target=target,
            owners=tuple(dict.fromkeys(item.owner for item in (*self._storage, *self._transactions, *self._files,
                                                                   *self._records))),
            tables=MappingProxyType(tables), immutable_fields=MappingProxyType(immutable),
            schema=tuple((item.owner, item.schema) for item in self._storage),
            validators=tuple(validator for item in self._storage for validator in item.validators),
            finalizers=tuple(finalizer for item in self._transactions for finalizer in item.finalizers),
            change_publisher=publishers[0] if publishers else None,
            lifecycles=MappingProxyType({name: lifecycle for item in self._files
                                         for name, lifecycle in item.lifecycles.items()}),
            required_lifecycles=required,
            required_tables=MappingProxyType({name: tuple(owners) for name, owners in sorted(needed.items())}),
            receipt_history=histories[0], record_hooks=self._records[0].hooks, _origin=_FREEZE_TOKEN)
