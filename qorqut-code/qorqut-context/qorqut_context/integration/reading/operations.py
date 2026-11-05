"""The shared Context read operations as one typed catalog contribution.

The authority metadata is the retained v1 one for the existing names: ``context_home``, ``context_object``,
``context_expand`` and ``context_find`` are agent-facing reads of every registered agent (the host policy resolver's
core default, unchanged), and ``context_changes`` stays an owner view. Scope resolution is also v1's: a caller reads
under its selected business, or under Global with the ``read_global_context`` capability. Because the one published
graph is shared, the scope only selects *who may ask*; the content returned never depends on it. Handlers use the
reader alone: they take nothing from the caller's arguments to decide identity or scope, and they never reach
operational records."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.registry import CatalogEntry
from qorqut_operations.contracts import DomainError

from . import schemas
from .reader import SharedContextReader

OWNER = "shared-context"
AGENT_READS = ("context_home", "context_object", "context_expand", "context_find")

INPUTS = {
    "context_home": (schemas.HOME_INPUT, ()),
    "context_object": (schemas.OBJECT_INPUT, ("ref",)),
    "context_find": (schemas.FIND_INPUT, ()),
    "context_expand": (schemas.EXPAND_INPUT, ("ref",)),
    "context_changes": (schemas.CHANGES_INPUT, ()),
}
OUTPUTS = {
    "context_home": schemas.HOME_OUTPUT,
    "context_object": schemas.OBJECT_OUTPUT,
    "context_find": schemas.FIND_OUTPUT,
    "context_expand": schemas.EXPAND_OUTPUT,
    "context_changes": schemas.CHANGES_OUTPUT,
}
DESCRIPTIONS = {
    "context_home": instruction_text('operations-the-shared-published-context-graph-a-bounded-page'),
    "context_object": instruction_text('operations-one-published-node-relationship-edge-note-or-source'),
    "context_find": instruction_text('operations-literal-word-and-prefix-search-over-published-node'),
    "context_expand": instruction_text('operations-the-relationships-around-a-published-node-out-to'),
    "context_changes": instruction_text('operations-owner-view-published-change-events-newest-first-with'),
}
ENTRIES = (
    *(CatalogEntry(name, False, "read") for name in AGENT_READS),
    CatalogEntry("context_changes", False, "read", frozenset({"dashboard", "worker"}), True),
)


def _namespace(context, _arguments):
    return "business:" + context.business_id if context.business_id else "global"


class SharedContextOperations:
    """Binds one ``SharedContextReader`` to the five read descriptors."""

    def __init__(self, reader: SharedContextReader):
        if not isinstance(reader, SharedContextReader):
            raise TypeError("SharedContextOperations requires a SharedContextReader")
        self.reader = reader

    def contribution(self) -> CatalogContribution:
        return CatalogContribution(owner=OWNER, entries=ENTRIES, descriptors=self.descriptors())

    def descriptors(self) -> tuple[OperationDescriptor, ...]:
        handlers = {"context_home": self._home, "context_object": self._object, "context_find": self._find,
                    "context_expand": self._expand, "context_changes": self._changes}
        descriptors = []
        for entry in ENTRIES:
            properties, required = INPUTS[entry.name]
            handler = handlers[entry.name]
            descriptors.append(OperationDescriptor(
                entry.name,
                {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False},
                OUTPUTS[entry.name], effect_kind="read", supports_signoff=False, exposure=entry.exposure,
                owner_only=entry.owner_only, description=DESCRIPTIONS[entry.name], scope_kinds=frozenset({"business", "global"}),
                global_capability="read_global_context", resolve_namespace=_namespace,
                read=lambda ctx, args, options, uow, handler=handler: handler(ctx, args)))
        return tuple(descriptors)

    # The caller's identity is whatever the Application authenticated into ``ctx``; no argument names a principal,
    # agent, business or revision-independent scope.

    def _home(self, ctx, args):
        return self.reader.home(limit=args.get("limit"), cursor=args.get("cursor"))

    def _object(self, ctx, args):
        return self.reader.object(args["ref"], history_limit=args.get("history_limit"),
                                  history_cursor=args.get("history_cursor"), text_limit=args.get("text_limit"),
                                  text_cursor=args.get("text_cursor"))

    def _find(self, ctx, args):
        return self.reader.find(args.get("query", ""), kind=args.get("kind"), attributes=args.get("attributes"),
                                limit=args.get("limit"), cursor=args.get("cursor"))

    def _expand(self, ctx, args):
        return self.reader.expand(args["ref"], depth=args.get("depth"), direction=args.get("direction"),
                                  limit=args.get("limit"), cursor=args.get("cursor"))

    def _changes(self, ctx, args):
        if ctx.principal_kind != "human":
            raise DomainError("forbidden", "Change attribution is an owner view")
        return self.reader.changes(ref=args.get("ref"), limit=args.get("limit"), cursor=args.get("cursor"))


def shared_context_contribution(reader: SharedContextReader) -> CatalogContribution:
    """The contribution trusted assembly installs for ``reader`` in place of the retired per-business Context reads."""
    return SharedContextOperations(reader).contribution()
