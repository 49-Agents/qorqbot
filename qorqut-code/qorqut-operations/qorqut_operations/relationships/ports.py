"""Typed host ports for the extracted relationship and review operations.

The generic relationship flow (the read, list, graph and CRUD operations, workflow-role refusal, revision and cursor
rules, the explicit review decisions) is the engine's. Exactly the host knowledge v1 kept beside it comes in through
``RelationshipPorts``: the role vocabulary, the Material classification and archive adapters over the host's domains,
the navigation counts and the operations a review may compose. Every port is
required: there are no permissive production defaults and nothing is discovered by attribute or module.

A port supplies vocabulary or a domain adapter; it cannot replace application policy. Descriptor lookup, argument
binding, nested preparation and nested target validation of a composed edit all go through the one application
registry, and the composed changes keep their own operation attribution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Protocol

from qorqut_operations.contracts import OPERATION_NAME, ChangeSet, InvocationContext
from qorqut_operations.items import KIND
from qorqut_operations.relationships.edges import ROLE
from qorqut_operations.storage.assembly import AssemblyError, TABLE_NAME

ROLE_NAME = re.compile(ROLE["pattern"])


class MaterialClassification(Protocol):
    """The existing Material classification mutation (v1 ``documents.materials`` and ``prepare_material``).

    ``filters`` is the property schema of the Material listing filters, ``material_types`` the Material type enum
    (in order) and ``tags`` the tag-list schema. ``matches(value, args)`` filters a resolved item. ``prepare`` and
    ``apply`` are the ``materials_update`` handlers, with v1's arguments and results."""

    filters: Mapping[str, Any]
    material_types: Sequence[str]
    tags: Mapping[str, Any]

    def matches(self, value: Mapping[str, Any], args: Mapping[str, Any]) -> bool: ...

    def prepare(self, ctx: InvocationContext, args, options, uow) -> ChangeSet: ...

    def apply(self, ctx: InvocationContext, plan: ChangeSet, uow) -> Mapping[str, Any]: ...


class ItemArchive(Protocol):
    """The composed archive and restore of an item (v1 ``prepare_archive`` / ``apply_archive``): the composed domain
    archive operations, document availability and protected-kind rules and the table-specific application."""

    def prepare(self, ctx: InvocationContext, args, options, uow) -> ChangeSet: ...

    def apply(self, ctx: InvocationContext, plan: ChangeSet, uow) -> Mapping[str, Any]: ...


class ContextNavigation(Protocol):
    """The navigation-count read (v1 ``context_navigation_get``), including any authorized old Context read until
    the host replaces those callers. ``read(ctx, args, uow)`` returns the result data."""

    def read(self, ctx: InvocationContext, args: Mapping[str, Any], uow) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ReviewEdit:
    """One operation a review decision may compose atomically with itself (v1 ``EDITS``).

    ``argument`` is the nested operation's argument that names the edited resource and must equal the reviewed item's
    id. ``item_kind`` restricts the reviewed item's kind; a generic edit sets ``item_kind=None`` and the
    ``resource_type`` of the stored resource it may change instead. ``apply_through_domain`` makes the engine apply
    the composed plan through the nested operation's own apply handler (v1: table updates and row writes) rather
    than as plain prepared records."""

    item_kind: str | None
    argument: str
    resource_type: str | None = None
    apply_through_domain: bool = False

    def __post_init__(self):
        if self.item_kind is not None and (not isinstance(self.item_kind, str) or not KIND.fullmatch(self.item_kind)):
            raise AssemblyError("A review edit's item kind is a lowercase snake_case name")
        if self.resource_type is not None and (not isinstance(self.resource_type, str)
                                               or not TABLE_NAME.fullmatch(self.resource_type)):
            raise AssemblyError("A review edit's resource type is a table name")
        if self.item_kind is None and self.resource_type is None:
            raise AssemblyError("A review edit names the item kind or the resource type it may change")
        if not isinstance(self.argument, str) or not self.argument:
            raise AssemblyError("A review edit names the argument that identifies the edited resource")
        if type(self.apply_through_domain) is not bool:
            raise AssemblyError("apply_through_domain is a boolean")


def _need(value, label: str, methods: Sequence[str] = (), attributes: Sequence[str] = ()) -> None:
    if value is None:
        raise AssemblyError(f"Required relationship port missing: {label}")
    missing = [name for name in methods if not callable(getattr(value, name, None))]
    missing += [name for name in attributes if getattr(value, name, None) is None]
    if missing:
        raise AssemblyError(f"Relationship port {label} must provide {', '.join(missing)}")


@dataclass(frozen=True)
class RelationshipPorts:
    """All host knowledge of one ``RelationshipOperations``, built once by trusted assembly code.

    ``roles`` is the ordered ``role -> (label, inverse_label)`` mapping and ``workflow_roles`` the roles that only
    their dedicated tools may change (``role -> tool text``); both are copied into immutable mappings. Everything else
    is a port object (see the protocols above) and ``review_edits`` is the ``operation name -> ReviewEdit`` mapping."""

    roles: Mapping[str, tuple[str, str]]
    workflow_roles: Mapping[str, str]
    materials: MaterialClassification
    archive: ItemArchive
    navigation: ContextNavigation
    review_edits: Mapping[str, ReviewEdit]

    def __post_init__(self):
        if not isinstance(self.roles, Mapping) or not self.roles:
            raise AssemblyError("Required relationship port missing: roles")
        roles = {}
        for name, labels in self.roles.items():
            if (not isinstance(name, str) or not ROLE_NAME.fullmatch(name)
                    or not isinstance(labels, (tuple, list)) or len(labels) != 2
                    or any(not isinstance(label, str) or not label for label in labels)):
                raise AssemblyError("Roles map a snake_case role to its label and inverse label")
            roles[name] = (labels[0], labels[1])
        if not isinstance(self.workflow_roles, Mapping):
            raise AssemblyError("Required relationship port missing: workflow_roles")
        workflow = dict(self.workflow_roles)
        if any(not isinstance(tool, str) or not tool for tool in workflow.values()) or set(workflow) - set(roles):
            raise AssemblyError("Workflow roles name a known role and the dedicated tool text that changes it")
        _need(self.materials, "materials", ("matches", "prepare", "apply"), ("filters", "material_types", "tags"))
        if (not isinstance(self.materials.filters, Mapping) or not isinstance(self.materials.tags, Mapping)
                or isinstance(self.materials.material_types, (str, bytes)) or not self.materials.material_types
                or any(not isinstance(value, str) for value in self.materials.material_types)):
            raise AssemblyError("Relationship port materials must provide filter and tag schemas and the type enum")
        _need(self.archive, "archive", ("prepare", "apply"))
        _need(self.navigation, "navigation", ("read",))
        if not isinstance(self.review_edits, Mapping) or not self.review_edits:
            raise AssemblyError("Required relationship port missing: review_edits")
        edits = dict(self.review_edits)
        if any(not isinstance(name, str) or not OPERATION_NAME.fullmatch(name) or type(edit) is not ReviewEdit
               for name, edit in edits.items()):
            raise AssemblyError("Review edits map operation names to typed ReviewEdit values")
        object.__setattr__(self, "roles", MappingProxyType(roles))
        object.__setattr__(self, "workflow_roles", MappingProxyType(workflow))
        object.__setattr__(self, "review_edits", MappingProxyType(edits))

