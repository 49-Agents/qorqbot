"""Directional, many-to-many relationships between typed items: the generic operations of v1.

Everything here is the deployed v1 flow with its operation ids, authority metadata, revisions, cursor binding, errors
and result shapes unchanged. The only things it does not know are the host's: the role vocabulary, the Material and
archive adapters, the navigation counts, the business-setup edge guard and the reviewable edit operations arrive
through ``RelationshipPorts``, and items are resolved through the frozen ``ItemResolver``.

Construction is inert. The descriptors go into a frozen catalog through ``contribution()`` and the application calls
their handlers; relationship rows are written only by the prepared plans of the three ``relationships_*`` writes
(and by the domains that build their own plans with ``create_edge``)."""

from __future__ import annotations

from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.plans import (PAGE, UUID, PlanBuilder, apply_records, page, require_revision, schema,
                                                 utc_clock)
from qorqut_operations.application.registry import CatalogEntry, OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.items import UNAVAILABLE, ItemResolver
from qorqut_operations.relationships.edges import (CONTEXT, ROLE, edge_values, edge_view, endpoints, related_rows)
from qorqut_operations.relationships.ports import RelationshipPorts
from qorqut_operations.relationships.reviews import ContextReviews
from qorqut_operations.storage.assembly import AssemblyError

RELATIONSHIP_READS = ("relationship_roles_list", "relationships_list", "relationships_get", "context_items_list",
                      "context_items_get", "context_navigation_get", "context_graph_get")
RELATIONSHIP_MUTATIONS = ("relationships_create", "relationships_update", "relationships_remove",
                          "context_items_archive", "materials_update")
# The deployed v1 catalog lines of the operations installed here, review operations included (unchanged).
RELATIONSHIP_ENTRIES = (
    *(CatalogEntry(name, False, "read") for name in (*RELATIONSHIP_READS, "context_reviews_list", "context_reviews_history")),
    *(CatalogEntry(name, True, "records") for name in (*RELATIONSHIP_MUTATIONS, "context_review", "context_review_reopen")),
)


class RelationshipOperations:
    def __init__(self, items: ItemResolver, ports: RelationshipPorts, *, clock=utc_clock):
        if not isinstance(items, ItemResolver):
            raise AssemblyError("Relationships need the frozen ItemResolver built by trusted assembly code")
        if not isinstance(ports, RelationshipPorts):
            raise AssemblyError("Relationships need the RelationshipPorts built by trusted assembly code")
        if not callable(clock):
            raise AssemblyError("The relationship clock is a callable returning an aware timestamp")
        unknown = sorted({edit.item_kind for edit in ports.review_edits.values() if edit.item_kind} - set(items.kinds))
        if unknown:
            raise AssemblyError("Review edits name unregistered item kinds: " + ", ".join(unknown))
        self.items, self.ports, self.clock = items, ports, clock
        self.reviews = ContextReviews(self)

    # -- registration ----------------------------------------------------------------------------------------------

    def contribution(self) -> CatalogContribution:
        return CatalogContribution("relationships", RELATIONSHIP_ENTRIES, self.descriptors())

    def descriptors(self):
        item = self.items.item_schema
        materials = self.ports.materials
        fields = {"source": item, "target": item, "role": ROLE, "context": CONTEXT}
        filters = {"source": item, "target": item, "source_type": item["properties"]["type"],
                   "target_type": item["properties"]["type"], "item": item,
                   "direction": {"enum": ["incoming", "outgoing", "both"]}, "role": ROLE,
                   "query": {"type": "string", "maxLength": 500}, "include_archived": {"type": "boolean"}}
        reads = {
            "relationship_roles_list": ({}, (), "List preferred relationship roles. Custom snake_case names are allowed when no predefined role fits. Workflow roles use their dedicated tools."),
            "relationships_list": ({**PAGE, **filters}, (), "Find directional relationships by source, target, role or item. Returns authorized endpoint labels and explanations. No cross-business results."),
            "relationships_get": ({"relationship_id": UUID}, ("relationship_id",), "Read one relationship, including direction, role, explanation and creation/update timestamps."),
            "context_items_list": ({**PAGE, **plain(materials.filters), "type": item["properties"]["type"], "query": filters["query"], "include_archived": filters["include_archived"], "include_sources": {"type": "boolean"}}, (), "Find existing context items before linking them. Filter Materials by material_type, origin or tag. Raw uploads and archived items are excluded by default."),
            "context_items_get": ({"item": item}, ("item",), "Resolve a context item to its current title, subtype, archive state and domain resource identity."),
            "context_navigation_get": ({}, (), "Read authorized story, table, product and people counts without loading the graph."),
            "context_graph_get": ({"item": item, "include_archived": filters["include_archived"]}, (), "Read the business context map, or one item's immediate neighborhood. Original uploads and generated projections are never map nodes. Reading does not acknowledge context."),
        }
        descriptors = []
        for name, (properties, required, description) in reads.items():
            descriptors.append(OperationDescriptor(name, schema(properties, required), effect_kind="read", supports_signoff=False,
                description=description, read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow)))
        writes = {
            "relationships_create": (fields, ("source", "target", "role"), "Create a directional relationship. Prefer predefined roles; custom roles and duplicates are allowed. This never grants access or wakes an agent. Assign tasks using tasks_assign."),
            "relationships_update": ({"relationship_id": UUID, "changes": {**schema(fields), "minProperties": 1}}, ("relationship_id", "changes"), "Edit a relationship's endpoints, role or explanation using its expected_revision. Workflow edges must use their domain tools."),
            "relationships_remove": ({"relationship_id": UUID}, ("relationship_id",), "Remove only this relationship using its expected_revision. Items and their contents are preserved. Workflow edges must use their domain tools."),
        }
        for name, (properties, required, description) in writes.items():
            descriptors.append(OperationDescriptor(name, schema(properties, required), description=description,
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow),
                apply=self.apply, validate_targets=self.validate_prepared))
        descriptors.append(OperationDescriptor("context_items_archive", schema({"item": item, "archived": {"type": "boolean"}}, ("item", "archived")),
            description="Archive or restore an item without deleting its content or relationships. Does not cascade. Requires the item's expected_revision. Agent retirement and business membership use their dedicated administration tools.",
            prepare=self.ports.archive.prepare, apply=self.ports.archive.apply))
        descriptors.append(OperationDescriptor("materials_update", schema({"material_id": UUID,
            "changes": {**schema({"material_type": {"enum": list(materials.material_types)}, "tags": plain(materials.tags)}), "minProperties": 1}}, ("material_id", "changes")),
            description="Classify a business Material or replace its descriptive tags, using expected_revision. Never edits its body, origin, relationships or permissions. Email stays conversation; raw uploads stay mixed. Personal/agent notes and plans use their own tools.",
            prepare=materials.prepare, apply=materials.apply))
        descriptors.extend(self.reviews.descriptors())
        return tuple(descriptors)

    # -- helpers ---------------------------------------------------------------------------------------------------

    def edge_view(self, edge):
        return edge_view(edge, self.ports.roles)

    def generic_role(self, role):
        if role in self.ports.workflow_roles:
            raise DomainError("workflow_required", f"Use {self.ports.workflow_roles[role]} to change {role} relationships")

    def visible_edge(self, ctx, edge, uow):
        source, target = endpoints(edge)
        return {**self.edge_view(edge), "source_item": self.items.resolve(ctx, source, uow), "target_item": self.items.resolve(ctx, target, uow)}

    # -- reads -----------------------------------------------------------------------------------------------------

    def read(self, operation, ctx, args, uow):
        if operation == "context_navigation_get":
            return self.ports.navigation.read(ctx, args, uow)
        if operation == "context_graph_get":
            return self.graph(ctx, args, uow)
        if operation == "relationship_roles_list":
            return {"roles": [{"role": name, "label": labels[0], "inverse_label": labels[1],
                               "workflow_tool": self.ports.workflow_roles.get(name)} for name, labels in self.ports.roles.items()]}
        if operation == "context_items_get":
            return {"item": self.items.resolve(ctx, args["item"], uow)}
        if operation == "relationships_get":
            edge = uow.repository("relationships", business_id=ctx.business_id).get(args["relationship_id"], required=True)
            return {"relationship": self.visible_edge(ctx, edge, uow)}
        found = []
        if operation == "context_items_list":
            for item in self.items.candidates(ctx, uow, args.get("type")):
                try:
                    value = self.items.resolve(ctx, item, uow)
                except DomainError as exc:
                    if exc.code not in UNAVAILABLE:
                        raise
                    continue
                if value["archived"] and not args.get("include_archived"):
                    continue
                if value["raw_source"] and not args.get("include_sources"):
                    continue
                if not self.ports.materials.matches(value, args):
                    continue
                if args.get("query") and args["query"].casefold() not in (value["label"] + " " + value["summary"]).casefold():
                    continue
                found.append(value)
        else:
            for item in (args.get("source"), args.get("target"), args.get("item")):
                if item:
                    self.items.resolve(ctx, item, uow)
            for edge in related_rows(uow, ctx.business_id, source=args.get("source"), target=args.get("target"), role=args.get("role")):
                source, target = endpoints(edge)
                if args.get("source_type") and source["type"] != args["source_type"]:
                    continue
                if args.get("target_type") and target["type"] != args["target_type"]:
                    continue
                if args.get("item"):
                    direction = args.get("direction", "both")
                    if not ((direction != "incoming" and source == args["item"]) or (direction != "outgoing" and target == args["item"])):
                        continue
                try:
                    value = self.visible_edge(ctx, edge, uow)
                except DomainError as exc:
                    if exc.code not in UNAVAILABLE:
                        raise
                    continue
                if not args.get("include_archived") and (value["source_item"]["archived"] or value["target_item"]["archived"]):
                    continue
                if args.get("query") and args["query"].casefold() not in (edge["context"] + " " + edge["role"] + " " + value["source_item"]["label"] + " " + value["target_item"]["label"]).casefold():
                    continue
                found.append(value)
        selected, cursor = page(found, args, ctx.business_id, operation)
        return {"items": selected, "next_cursor": cursor}

    def graph(self, ctx, args, uow):
        key = lambda item: (item["type"], item["id"])
        nodes = {}
        if args.get("item"):
            self.items.resolve(ctx, args["item"], uow)
        for item in self.items.candidates(ctx, uow):
            try:
                value = self.items.resolve(ctx, item, uow)
            except DomainError as error:
                if error.code not in UNAVAILABLE:
                    raise
                continue
            if value["raw_source"] or (value["archived"] and not args.get("include_archived")):
                continue
            nodes[key(item)] = value
        edges = [self.edge_view(edge) for edge in related_rows(uow, ctx.business_id)
                 if all(key(endpoint) in nodes for endpoint in endpoints(edge))]
        if args.get("item"):
            edges = [edge for edge in edges if args["item"] in (edge["source"], edge["target"])]
            chosen = {key(args["item"]), *[key(endpoint) for edge in edges for endpoint in (edge["source"], edge["target"])]}
            nodes = {identity: value for identity, value in nodes.items() if identity in chosen}
        return {"nodes": list(nodes.values()), "edges": edges}

    # -- writes ----------------------------------------------------------------------------------------------------

    def prepare(self, operation, ctx, args, options, uow):
        builder = PlanBuilder(ctx, operation, args, "Update context relationship", clock=self.clock)
        creating = operation == "relationships_create"
        identity = new_id() if creating else args["relationship_id"]
        before = None if creating else uow.repository("relationships", business_id=ctx.business_id).get(identity, required=True)
        if before:
            require_revision(before, options.expected_revision)
            self.generic_role(before["role"])
            self.ports.protected_edges.check(ctx, uow, before)
            for item in endpoints(before):
                self.items.resolve(ctx, item, uow, builder)
        if operation == "relationships_remove":
            builder.record("relationships", identity, before, {}, action="delete")
        else:
            proposed = {**(self.edge_view(before) if before else {}), **plain(args if creating else args["changes"])}
            self.generic_role(proposed["role"])
            for key in ("source", "target"):
                self.items.resolve(ctx, proposed[key], uow, builder)
                proposed[key] = self.items.relationship_item(ctx, proposed[key], uow)
                self.items.resolve(ctx, proposed[key], uow, builder)
            values = edge_values(proposed["source"], proposed["target"], proposed["role"], proposed.get("context", ""))
            values["updated_at"] = builder.at
            if creating:
                values.update(id=identity, created_at=builder.at)
            builder.record("relationships", identity, before, values)
        return builder.build()

    def validate_prepared(self, ctx, plan, uow):
        for change in plan.changes:
            if change.resource.resource_type != "relationships":
                continue
            before = uow.repository("relationships", business_id=ctx.business_id).get(change.resource.resource_id)
            if before:
                self.ports.protected_edges.check(ctx, uow, before)
            for edge in (before, {**(before or {}), **plain(change.values)}):
                if not edge:
                    continue
                self.generic_role(edge["role"])
                for item in endpoints(edge):
                    self.items.resolve(ctx, item, uow)

    def apply(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        identity = next(change.resource.resource_id for change in plan.changes if change.resource.resource_type == "relationships")
        row = uow.repository("relationships", business_id=ctx.business_id).get(identity)
        return {"relationship": self.visible_edge(ctx, row, uow)} if row else {"removed_id": identity}
