"""Generic catalog registration: trusted contributions in, one frozen registry out.

The engine ships no operation names. Each retained domain contributes its authority metadata (``CatalogEntry``),
its installed ``OperationDescriptor`` handlers and any review-scope exceptions as one frozen
``CatalogContribution``; ``compose_catalog`` assembles them before the service starts and freezes the registry,
so nothing can be declared or installed from a request. The one generic operation the engine itself owns is the
MCP discovery contract ``operations_batch``."""

from __future__ import annotations

from dataclasses import dataclass

from qorqut_operations.storage.assembly import AssemblyError, OWNER
from . import batches
from .registry import CatalogEntry, OperationDescriptor, OperationRegistry
from .review import ReviewScopeException


@dataclass(frozen=True)
class CatalogContribution:
    owner: str
    entries: tuple[CatalogEntry, ...] = ()
    descriptors: tuple[OperationDescriptor, ...] = ()
    review_exceptions: tuple[ReviewScopeException, ...] = ()

    def __post_init__(self):
        if not isinstance(self.owner, str) or not OWNER.fullmatch(self.owner):
            raise AssemblyError("A contribution requires a lowercase owner name")
        for name, kind in (("entries", CatalogEntry), ("descriptors", OperationDescriptor),
                           ("review_exceptions", ReviewScopeException)):
            values = tuple(getattr(self, name))
            if any(type(value) is not kind for value in values):
                raise AssemblyError(f"Catalog {name} must be typed {kind.__name__} values")
            object.__setattr__(self, name, values)


@dataclass(frozen=True)
class CatalogComposition:
    registry: OperationRegistry
    review_exceptions: tuple[ReviewScopeException, ...]


def compose_catalog(contributions=(), *, include_batch: bool = True) -> CatalogComposition:
    """Build the frozen registry. Duplicate owners, duplicate metadata and duplicate installed operations fail."""
    contributions = tuple(contributions)
    registry, exceptions, owners, declared = OperationRegistry(), [], set(), set()
    for contribution in contributions:
        if type(contribution) is not CatalogContribution:
            raise AssemblyError("Only typed catalog contributions made by trusted assembly code can be registered")
        if contribution.owner in owners:
            raise AssemblyError(f"Catalog contribution already registered for owner {contribution.owner}")
        owners.add(contribution.owner)
        names = [entry.name for entry in contribution.entries]
        if len(set(names)) != len(names) or declared & set(names):
            raise AssemblyError("Operation metadata is declared by more than one contribution")
        declared.update(names)
        for entry in contribution.entries:
            registry.declare(entry)
        exceptions.extend(contribution.review_exceptions)
    for contribution in contributions:
        for descriptor in contribution.descriptors:
            registry.register(descriptor)
    if include_batch:
        registry.register(batches.descriptor())
    return CatalogComposition(registry.freeze(), tuple(exceptions))
