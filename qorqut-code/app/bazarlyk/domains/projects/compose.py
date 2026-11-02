"""The ``project_*`` operations: one existing Project's record, inventory, item reads and explicit links.

These are the only new Project operations. They extend no Task, relationship, permission or conversation engine:
they read through the existing canonical readers and the frozen neutral item resolver, and they write only the one
Operations-owned ``project_item_links`` association store. No Project creation, rename, archive or lifecycle
operation is introduced, and no UI is implemented here.

Every call lands on the same three gates in the handler: the owner/configured-Ordubashy identity, the Project's
current reach, and the ordinary read or mutation policy of this operation in exactly that Project's scope. A
business route must agree with ``project_id``; from the root route ``scope: "global"`` names the Project and the
engine switches to that Project's own scope for this invocation only. Linking never writes into the target's scope
and never grants access. An inaccessible target is refused or skipped, never disclosed.
"""

from __future__ import annotations

from dataclasses import replace

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import UUID, PlanBuilder, page, ref, schema, utc_clock
from qorqut_operations.contracts import DomainError
from qorqut_operations.projects.links import ProjectItemLinks, ProjectTarget, view as link_view

from . import access

READ = "project_record_get"
LIST = "project_items_list"
CANDIDATES = "project_items_candidates"
ITEM = "project_item_get"
LINK = "project_items_link"
UNLINK = "project_items_unlink"
OPERATIONS = (READ, LIST, CANDIDATES, ITEM, LINK, UNLINK)

SCOPE_FIELDS = {
    "project_id": {**UUID, "description": "The actual Project UUID; on a business route it must be the selected project."},
    "scope": {"enum": ["business", "global"],
              "description": "business on the project route; global from the root route names the project."}}
ITEM_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["type", "id", "business_id"],
               "properties": {"type": {"type": "string", "minLength": 1, "maxLength": 64},
                              "id": {"type": "string", "minLength": 1, "maxLength": 300},
                              "business_id": {"anyOf": [UUID, {"type": "null"}]}},
               "description": "One existing readable item, keeping its own scope: an Action keeps its canonical action_id."}
PAGE_FIELDS = {"cursor": {"type": "string", "maxLength": 2000},
               "limit": {"type": "integer", "minimum": 1, "maximum": 100},
               "include_closed": {"type": "boolean"}, "include_archived": {"type": "boolean"},
               "root_tasks_only": {"type": "boolean"}, "section": {"enum": ["all", "work", "signoffs", "actions",
                                                                         "documents", "tables", "automation", "other"]}}
COMMON = dict(supports_signoff=False, scope_kinds=frozenset({"business", "global"}),
              global_capability="read_global_context",
              resolve_namespace=lambda ctx, args: "business:" + args["project_id"])


class ProjectRecords:
    """The one Project record service of this host: the inventory, the six operations and their link store."""

    def __init__(self, application, items, *, clock=utc_clock):
        from .inventory import Inventory
        self.application, self.items, self.clock = application, items, clock
        self.links = ProjectItemLinks(application, clock=clock)
        self.inventory = Inventory(application, items)
        # The one explicit association store, shared with later readers of this application.
        application.project_links = self.links

    # -- registration ----------------------------------------------------------------------------------------------

    def register(self, registry=None):
        registry = registry or self.application.registry
        registry.register(OperationDescriptor(READ, schema(SCOPE_FIELDS, ("project_id",)), **COMMON, effect_kind="read",
            read=lambda ctx, args, options, uow: self.record_get(ctx, args, uow),
            description="Owner and configured Ordubashy: one existing Project's name, slug, actual summary, revision and "
                        "archive state. Project cards take the real name; a custom Focus display name never applies."))
        registry.register(OperationDescriptor(LIST, schema({**SCOPE_FIELDS, **PAGE_FIELDS}, ("project_id",)),
            **COMMON, effect_kind="read", read=lambda ctx, args, options, uow: self.items_list(ctx, args, uow),
            description="Owner and configured Ordubashy: one Project's accessible records - its own work, signoffs, actions, "
                        "documents, tables, automation and other records plus explicitly linked items in any scope. "
                        "Owned and linked yield one row with both flags; denied candidates are omitted."))
        registry.register(OperationDescriptor(CANDIDATES, schema({**SCOPE_FIELDS,
            "source_business_id": {"anyOf": [UUID, {"type": "null"}],
                                   "description": "The chosen source scope: a project UUID, or null for Global."},
            "query": {"type": "string", "maxLength": 200}, "type": {"type": "string", "minLength": 1, "maxLength": 64},
            "cursor": {"type": "string", "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "include_archived": {"type": "boolean"}}, ("project_id",)),
            **COMMON, effect_kind="read", read=lambda ctx, args, options, uow: self.candidates(ctx, args, uow),
            description="Owner and configured Ordubashy: actual readable records of the chosen source scope, for choosing an "
                        "explicit link target. Candidates are authorized one by one; a link grants no access."))
        registry.register(OperationDescriptor(ITEM, schema({**SCOPE_FIELDS, "item": ITEM_SCHEMA}, ("project_id", "item")),
            **COMMON, effect_kind="read", read=lambda ctx, args, options, uow: self.item_get(ctx, args, uow),
            description="Owner and configured Ordubashy: one authorized Project item plus the appropriate existing domain read "
                        "of that record for in-pane presentation. This is not arbitrary table access."))
        registry.register(OperationDescriptor(LINK, schema({**SCOPE_FIELDS, "item": ITEM_SCHEMA}, ("project_id", "item")),
            **COMMON, prepare=self.prepare_link, apply=self.apply_link,
            description="Owner and configured Ordubashy: explicitly associate one currently readable item with this Project, "
                        "without changing its owning scope or granting access. Linking an already owned item is a no-op."))
        registry.register(OperationDescriptor(UNLINK, schema({**SCOPE_FIELDS, "link_id": UUID,
            "expected_revision": {"type": "integer", "minimum": 1}},
            ("project_id", "link_id", "expected_revision")), **COMMON, prepare=self.prepare_unlink, apply=self.apply_unlink,
            description="Owner and configured Ordubashy: remove exactly this explicit association with its current revision. "
                        "An owned item, a Project, its records and its conversations are never changed."))
        self.application.projects = self
        return self

    # -- reads -----------------------------------------------------------------------------------------------------

    def record_get(self, ctx, args, uow):
        project_id = access.project_id_from(args)
        _, row = access.allow(ctx, args, self.application, uow, project_id=project_id, operation=READ, include_archived=True)
        return {"project": access.project_view(row)}

    def items_list(self, ctx, args, uow):
        project_id = access.project_id_from(args)
        selected, _ = access.allow(ctx, args, self.application, uow, project_id=project_id, operation=LIST)
        links = self.links.index(selected, project_id, uow)
        entries = [self.inventory.association_of(uow, item, project_id, links)
                   for item in self.inventory.inventory(selected, project_id, args, uow, links=links)]
        from .inventory import page_items
        selected, cursor = page_items(entries, args, project_id)
        return {"items": selected, "next_cursor": cursor}

    def item_get(self, ctx, args, uow):
        project_id = access.project_id_from(args)
        selected, _ = access.allow(ctx, args, self.application, uow, project_id=project_id, operation=ITEM)
        spec = args["item"]
        item = self.inventory.target(selected, project_id, spec["type"], spec["id"], spec["business_id"], uow,
                                     allow_self=True)
        links = self.links.index(selected, project_id, uow)
        self.inventory.association_of(uow, item, project_id, links)
        return {"item": item, "record": self.inventory.record(selected, item, uow)}

    def candidates(self, ctx, args, uow):
        """Link candidates of one chosen scope: actual readable records only, never a guessed or synthetic item."""
        project_id = access.project_id_from(args)
        selected, _ = access.allow(ctx, args, self.application, uow, project_id=project_id, operation=CANDIDATES)
        source = args.get("source_business_id")
        scan = self.scan_context(selected, project_id, source, uow)
        entries = self.inventory.inventory(scan, source, {**args, "section": "all"}, uow, links={}, projects=True)
        links = self.links.index(selected, project_id, uow)
        found = []
        for item in entries:
            if item["type"] == access.PROJECT_TYPE and item["id"] == project_id:
                continue
            if args.get("type") and item["type"] != args["type"]:
                continue
            if args.get("query") and args["query"].casefold() not in (item["label"] + " " + item["id"]).casefold():
                continue
            found.append(self.inventory.association_of(uow, item, project_id, links))
        items, cursor = page(found, args, source or "global", CANDIDATES,
                             key=lambda item: ((item.get("updated_at") or item.get("created_at") or ""),
                                               item["type"], item["id"]))
        return {"items": items, "next_cursor": cursor}

    def related(self, ctx, project_id, args, uow):
        """A Project's related records for the private thread pane: the shared inventory and its own paging envelope."""
        ctx = self.application.current_context(ctx, uow)
        access.role(ctx, uow)
        access.business_row(ctx, project_id, uow, include_archived=True)
        links = self.links.index(ctx, project_id, uow)
        wanted = {"include_closed": args.get("include_closed", True), "include_archived": args.get("include_archived", False)}
        entries = self.inventory.inventory(ctx, project_id, wanted, uow, links=links)
        offset, limit = args.get("offset", 0), args.get("limit", 50)
        selected = entries[offset:offset + limit]
        return {"items": [{**item, "created_at": item.get("created_at")} for item in selected],
                "next_offset": offset + limit if len(entries) > offset + limit else None}

    # -- writes ----------------------------------------------------------------------------------------------------

    def prepare_link(self, ctx, args, options, uow):
        project_id = access.project_id_from(args)
        selected, row = access.allow(ctx, args, self.application, uow, project_id=project_id, operation=LINK)
        self.resolve_link_target(selected, project_id, args["item"], uow)
        builder = PlanBuilder(ctx, LINK, args, "Link project record", namespace="business:" + project_id, clock=self.clock)
        builder.expect(ref("businesses", project_id, None), row)
        return builder.build()

    def apply_link(self, ctx, plan, uow):
        args = plan.arguments
        project_id = access.project_id_from(args)
        selected, _ = access.allow(ctx, args, self.application, uow, project_id=project_id, operation=LINK)
        target, item, owned = self.resolve_link_target(selected, project_id, args["item"], uow)
        already, stored = owned, self.links.find(ctx, project_id, target, uow)
        if not owned:
            stored, already = self.links.link(ctx, project_id, target, uow)
        self.inventory.association_of(uow, item, project_id, self.links.index(ctx, project_id, uow))
        return {"item": item, "link": link_view(stored) if stored else None,
                "already_present": bool(already or owned)}

    def prepare_unlink(self, ctx, args, options, uow):
        project_id = access.project_id_from(args)
        _, row = access.allow(ctx, args, self.application, uow, project_id=project_id, operation=UNLINK)
        stored = self.require_link(ctx, project_id, args, uow)
        builder = PlanBuilder(ctx, UNLINK, args, "Unlink project record", namespace="business:" + project_id, clock=self.clock)
        builder.expect(ref("businesses", project_id, None), row)
        builder.expect(ref("project_item_links", stored["id"], project_id), stored)
        return builder.build()

    def apply_unlink(self, ctx, plan, uow):
        args = plan.arguments
        project_id = access.project_id_from(args)
        access.allow(ctx, args, self.application, uow, project_id=project_id, operation=UNLINK)
        self.links.unlink(ctx, project_id, args["link_id"], args["expected_revision"], uow)
        return {"removed": True}

    # -- link helpers ----------------------------------------------------------------------------------------------

    def require_link(self, ctx, project_id, args, uow):
        stored = self.links.get(ctx, project_id, args["link_id"], uow)
        if stored is None:
            raise DomainError("not_found", "No such association in this project")
        if stored["revision"] != args["expected_revision"]:
            raise DomainError("revision_conflict", "The association changed; read the project again",
                              current_revision=stored["revision"])
        return stored

    def resolve_link_target(self, ctx, project_id, spec, uow):
        """The canonical target, the label we may disclose, and whether the Project already owns it."""
        item = self.inventory.target(ctx, project_id, spec["type"], spec["id"], spec["business_id"], uow)
        target = ProjectTarget(item["type"], item["id"], item.get("business_id"))
        from .inventory import owned
        return target, item, owned(uow, item, project_id)

    def scan_context(self, ctx, project_id, source, uow):
        """The chosen link source scope, authorized as a real read for this caller."""
        if source is not None and not access.business_participant(ctx, source, uow):
            raise DomainError("not_found", "That project is outside your current access")
        selected = replace(ctx, business_id=source, review_business_id=source)
        self.application.authorize(selected, self.application.registry.get(CANDIDATES), (), uow,
                                   namespace="business:" + source if source else "global")
        return selected

__all__ = ["CANDIDATES", "ITEM", "LINK", "LIST", "OPERATIONS", "ProjectRecords", "READ", "UNLINK"]