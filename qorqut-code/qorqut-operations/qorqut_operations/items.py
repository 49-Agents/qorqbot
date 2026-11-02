"""Neutral item identity: one resolver over host-contributed, typed lookups.

The deployed v1 code resolved shared items (people, topics, tasks, tables, materials, ...) through a class that was
constructed from the Story service and knew every domain. Here the engine keeps only the neutral contract. A trusted
host registers frozen ``ItemContribution`` values, each naming the item kinds a typed ``ItemLookup`` serves, plus one
``ItemCandidates`` enumeration port; ``ItemResolver`` freezes and validates them and dispatches by kind. Nothing is
registered from a request and there is no API to add a kind afterwards.

Registering a kind grants no access. The trusted lookup keeps the domain's private-source rules (owner-only journals,
excluded working memory, mailbox discussion access, active-member checks, file availability) and authorizes its
domain read through ``authorize_item_read``; enumeration does not grant read access either, because the relationship
service resolves every candidate before exposing any detail of it."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Protocol

from qorqut_operations.application.engine import Application
from qorqut_operations.application.plans import IDENTITY, schema
from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.assembly import AssemblyError, OWNER

KIND = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Lookup failures that mean "this item is not available to this caller right now", as in v1. Listing, graph and
# review reads skip such an endpoint; every other error is a real failure and propagates.
UNAVAILABLE = frozenset({"not_found", "forbidden", "invalid_assignee", "document_unavailable", "busy",
                         "operation_unavailable"})


def item_revision(row: Mapping[str, Any]):
    """The revision counter of a stored resource row, whichever column its table calls it."""
    return row.get("revision", row.get("status_revision", row.get("record_revision", 1)))


def authorize_item_read(application, ctx, operation: str, uow) -> None:
    """Refuse unless the caller may currently use the named *read* operation (v1 ``ContextItems.authorize``).

    Global/account callers are judged by the descriptor's account policy, business callers of a global-capable
    operation by its business policy; the resolved mode must be ``allow``. The domain's own read handler is still
    called by the lookup where v1 called it."""
    descriptor = application.registry.get(operation)
    if ctx.business_id is not None and descriptor.global_capability and "business" in descriptor.scope_kinds:
        descriptor = descriptor.business_policy
    elif ctx.business_id is None:
        descriptor = descriptor.account_policy
    if application.policy.resolve(ctx, descriptor, (), uow).mode != "allow":
        raise DomainError("forbidden", "The item's read operation is not permitted")


class ItemLookup(Protocol):
    """The domain half of item resolution, for the kinds of one ``ItemContribution``.

    ``resource(ctx, item, uow)`` returns the current ``(resource_table, row)`` under the same scoped lookup and
    privacy guards as v1. ``resolve(ctx, item, uow, builder=None, *, allow_busy=False)`` returns the authorized
    item DTO and, given a plan builder, adds the exact expected targets it read. ``relationship_item`` returns the
    canonical relationship endpoint of an item (a message resolves to its conversation). ``fingerprint(ctx, item,
    uow, builder=None)`` returns the content fingerprint and any file-content expected targets, after the same
    authorized read as ``resolve``."""

    def resource(self, ctx, item: Mapping[str, Any], uow) -> tuple[str, Mapping[str, Any]]: ...

    def resolve(self, ctx, item: Mapping[str, Any], uow, builder=None, *, allow_busy: bool = False) -> Mapping[str, Any]: ...

    def relationship_item(self, ctx, item: Mapping[str, Any], uow) -> Mapping[str, Any]: ...

    def fingerprint(self, ctx, item: Mapping[str, Any], uow, builder=None) -> str: ...


class ItemCandidates(Protocol):
    """Typed enumeration of candidate ``{"type", "id"}`` items in the caller's scope, in v1's order, optionally
    restricted to one kind. Enumeration is not authorization: callers resolve each candidate before using it."""

    def candidates(self, ctx, uow, kind: str | None = None) -> Iterable[Mapping[str, Any]]: ...


_LOOKUP_METHODS = ("resource", "resolve", "relationship_item", "fingerprint")


@dataclass(frozen=True)
class ItemContribution:
    """One owner's item kinds and the lookup that serves them. A lookup object may serve several kinds."""

    owner: str
    kinds: tuple[str, ...]
    lookup: ItemLookup

    def __post_init__(self):
        if not isinstance(self.owner, str) or not OWNER.fullmatch(self.owner):
            raise AssemblyError("An item contribution requires a lowercase owner name")
        if not isinstance(self.kinds, tuple) or not self.kinds:
            raise AssemblyError("An item contribution names a nonempty tuple of item kinds")
        if any(not isinstance(kind, str) or not KIND.fullmatch(kind) for kind in self.kinds):
            raise AssemblyError("Item kinds are lowercase snake_case names")
        if len(set(self.kinds)) != len(self.kinds):
            raise AssemblyError("An item contribution lists each kind once")
        missing = [name for name in _LOOKUP_METHODS if not callable(getattr(self.lookup, name, None))]
        if missing:
            raise AssemblyError(f"Item lookup for {self.owner} must provide {', '.join(missing)}")


class ItemResolver:
    """The frozen item registry of one Operations unit and its dispatching read methods.

    ``application`` is the one ``Application`` whose registry and policy authorize the item reads; it is only used
    when a method is called, so it may still be unpopulated while descriptors are being collected. The resolver is
    immutable after construction; an item kind that is not registered does not exist (``not_found``)."""

    __slots__ = ("_application", "_contributions", "_lookups", "_kinds", "_candidates", "_item_schema")

    def __init__(self, application, contributions: Sequence[ItemContribution], *, candidates: ItemCandidates):
        if not isinstance(application, Application) or application.policy is None:
            raise AssemblyError("The item resolver needs the Application, with its policy, that authorizes item reads")
        if isinstance(contributions, (str, bytes, Mapping)):
            raise AssemblyError("Item contributions are a sequence of ItemContribution values")
        contributions = tuple(contributions)
        if not contributions or any(type(value) is not ItemContribution for value in contributions):
            raise AssemblyError("Item contributions are typed values made by trusted assembly code")
        if not callable(getattr(candidates, "candidates", None)):
            raise AssemblyError("The item resolver needs an item candidates port providing candidates")
        owners = [contribution.owner for contribution in contributions]
        if len(set(owners)) != len(owners):
            raise AssemblyError("An item contribution is already registered for this owner")
        lookups: dict[str, ItemLookup] = {}
        for contribution in contributions:
            for kind in contribution.kinds:
                if kind in lookups:
                    raise AssemblyError(f"Item kind already registered: {kind}")
                lookups[kind] = contribution.lookup
        self._application = application
        self._contributions = contributions
        self._lookups = MappingProxyType(lookups)
        self._kinds = tuple(lookups)
        self._candidates = candidates
        self._item_schema = schema({"type": {"enum": list(self._kinds)}, "id": IDENTITY}, ("type", "id"))

    @property
    def application(self):
        return self._application

    @property
    def contributions(self) -> tuple[ItemContribution, ...]:
        return self._contributions

    @property
    def kinds(self) -> tuple[str, ...]:
        """The registered kinds in contribution order."""
        return self._kinds

    @property
    def item_schema(self) -> dict[str, Any]:
        """The JSON schema of an item reference, derived from the registered kinds (a fresh copy per call)."""
        return deepcopy(self._item_schema)

    def _lookup(self, item) -> ItemLookup:
        lookup = self._lookups.get(item["type"])
        if lookup is None:
            raise DomainError("not_found", "Context item does not exist in this project")
        return lookup

    def authorize(self, ctx, operation: str, uow) -> None:
        authorize_item_read(self._application, ctx, operation, uow)

    def resource(self, ctx, item, uow):
        return self._lookup(item).resource(ctx, item, uow)

    def resolve(self, ctx, item, uow, builder=None, *, allow_busy: bool = False):
        return self._lookup(item).resolve(ctx, item, uow, builder, allow_busy=allow_busy)

    def relationship_item(self, ctx, item, uow):
        return self._lookup(item).relationship_item(ctx, item, uow)

    def fingerprint(self, ctx, item, uow, builder=None):
        return self._lookup(item).fingerprint(ctx, item, uow, builder)

    def candidates(self, ctx, uow, kind: str | None = None):
        return self._candidates.candidates(ctx, uow, kind=kind)
