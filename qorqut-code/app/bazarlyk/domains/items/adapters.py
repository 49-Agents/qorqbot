"""The host knowledge behind the Operations ``RelationshipPorts``: role vocabulary, Material classification, composed
archive, navigation counts, the business-setup protected edge and the reviewable edit operations.

Each adapter is the deployed v1 code of the generic relationship module that was domain-specific, moved here with its
dependencies stated (the item resolver, the document service, the application registry, business setup). None of them
can replace application policy: composed archive and review edits are prepared through the one registry and judged by
their own operation's policy, including any Signoff floor."""

from dataclasses import replace

from qorqut_operations.application.plans import PlanBuilder, apply_records, require_revision
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.items import UNAVAILABLE
from qorqut_operations.relationships import RelationshipPorts, ReviewEdit
from qorqut_operations.application.plans import utc_clock

from ..documents.materials import FILTERS as MATERIAL_FILTERS, MATERIAL_TYPES, RAW_KINDS, TAGS, classification, matches

ROLES = {
    "manages": ("Manages", "Managed by"),
    "participates_in": ("Participates in", "Participants"),
    "assigned_to": ("Assigned to", "Assigned work"),
    "executes": ("Executes", "Executed by"),
    "part_of": ("Part of", "Includes"),
    "continues": ("Continues", "Continued by"),
    "blocked_by": ("Blocked by", "Blocks"),
    "concerns": ("Concerns", "Relevant material"),
    "mentions": ("Mentions", "Mentioned in"),
    "extracted_from": ("Extracted from", "Source for"),
    "related_to": ("Related to", "Related from"),
}
ROLES["campaign_responsible"] = ("Campaign responsible agent", "Responsible for campaign")
ROLES["campaign_content_of"] = ("Campaign output of", "Campaign output")
WORKFLOW_ROLES = {"assigned_to": "tasks_assign or email assignment tools", "blocked_by": "tasks_update",
    "campaign_responsible": "stories_campaign_responsible_set", "campaign_content_of": "campaign_attribution_set"}
# The operations a review may compose with its decision, keyed by operation: the edited item must be the reviewed one.
REVIEW_EDITS = {
    "tasks_update": ReviewEdit("task", "task_id"),
    "stories_update": ReviewEdit("story", "story_id"),
    "tables_update": ReviewEdit("table", "table_id", apply_through_domain=True),
    "tables_rows_write": ReviewEdit("table", "table_id", apply_through_domain=True),
    "context_document_update": ReviewEdit(None, "document_id", resource_type="documents"),
    "context_people_update": ReviewEdit("person", "document_id"),
    "notes_update": ReviewEdit("material", "document_id"),
    "products_update": ReviewEdit("product", "product_id"),
}


class HostMaterials:
    """The Material classification mutation (``materials_update``) and its listing vocabulary."""

    filters = MATERIAL_FILTERS
    material_types = MATERIAL_TYPES
    tags = TAGS

    def __init__(self, items, *, clock=utc_clock):
        self.items, self.clock = items, clock

    @staticmethod
    def matches(value, args):
        return matches(value, args)

    def prepare(self, ctx, args, options, uow):
        item = {"type": "material", "id": args["material_id"]}
        self.items.resolve(ctx, item, uow)
        table, row = self.items.resource(ctx, item, uow)
        material = classification(row, table)
        if not material or row.get("kind") == "email_message":
            raise DomainError("workflow_required", "Use the item's dedicated tools; classify email conversations rather than individual messages")
        require_revision(row, options.expected_revision)
        changes = plain(args["changes"])
        fixed = table == "email_conversations" or row.get("kind") in RAW_KINDS | {"content"}
        if fixed and changes.get("material_type", material["material_type"]) != material["material_type"]:
            raise DomainError("validation_error", "This source has a fixed material type")
        builder = PlanBuilder(ctx, "materials_update", args, "Update Material classification", clock=self.clock)
        values = {"updated_at": builder.at}
        if table == "documents":
            values["metadata_json"] = {**row["metadata_json"], **material, **changes}
        elif "tags" in changes:
            values["tags_json"] = changes["tags"]
        builder.record(table, row["id"], row, values)
        return builder.build()

    def apply(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        return {"item": self.items.resolve(ctx, {"type": "material", "id": plan.arguments["material_id"]}, uow)}


class HostArchive:
    """Archive and restore, composed over the owning domain's own operation (and policy) where it has one."""

    def __init__(self, items, documents, *, clock=utc_clock):
        self.items, self.documents, self.clock = items, documents, clock

    def prepare(self, ctx, args, options, uow):
        self.items.resolve(ctx, args["item"], uow)
        table, row = self.items.resource(ctx, args["item"], uow)
        require_revision(row, options.expected_revision)
        adapters = {"tasks": ("tasks_update", "task_id"), "stories": ("stories_update", "story_id"), "data_tables": ("tables_update", "table_id"),
                    "products": ("products_archive", "product_id")}
        if table in adapters:
            operation, key = adapters[table]
            # Composed changes keep the domain operation's own policy, including Signoff floors.
            changes = {"archived": args["archived"]} if table == "products" else {"changes": {"archived": args["archived"]}}
            plan = self.items.application.registry.get(operation).prepare(ctx, {key: args["item"]["id"], **changes}, options, uow)
            return replace(plan, operation_name="context_items_archive", arguments=args,
                changes=tuple(replace(change, operation_name=change.operation_name or operation) for change in plan.changes))
        builder = PlanBuilder(ctx, "context_items_archive", args, "Archive context item" if args["archived"] else "Restore context item", clock=self.clock)
        if table == "documents":
            store = self.documents.store
            row = store.get(ctx, args["item"]["id"], uow, write=True)
            if row["kind"] in {"plan", "shared_instruction", "shared_instructions", "context_index"}:
                raise DomainError("workflow_required", "Manage instructions and plans with their dedicated tools")
            operation = "notes_update" if row["kind"] in {"agent_note", "business_note", "journal", "business_owned_note"} else "context_document_update"
            if row["kind"] == "asset": operation = "assets_update"
            store.archive(builder, uow, row, expected_revision=options.expected_revision, archived=args["archived"], operation_name=operation)
        elif table in {"email_conversations", "context_entities"}:
            builder.record(table, args["item"]["id"], row, {"archived": args["archived"], "updated_at": builder.at})
        else:
            raise DomainError("workflow_required", "Use the item's dedicated lifecycle tool")
        return builder.build()

    def apply(self, ctx, plan, uow):
        if plan.arguments["item"]["type"] == "table":
            self.items.application.registry.get("tables_update").apply(ctx, plan, uow)
        else:
            apply_records(ctx, plan, uow)
        return {"item": self.items.resolve(ctx, plain(plan.arguments["item"]), uow)}


class HostNavigation:
    """``context_navigation_get``: authorized story, table, product and people counts, including the authorized old
    Context-base people count until the new Context replaces those callers."""

    def __init__(self, items, documents):
        self.items, self.documents = items, documents

    def read(self, ctx, args, uow):
        counts = {}
        for kind in ('story', 'table', 'product', 'person'):
            count = 0
            for item in self.items.candidates(ctx, uow, kind=kind):
                try: value = self.items.resolve(ctx, item, uow)
                except DomainError as error:
                    if error.code not in UNAVAILABLE: raise
                    continue
                if not value['raw_source'] and not value['archived']: count += 1
            if kind == "person":
                base = getattr(self.documents, "context_base", None)
                if base is not None and base.service and base.service.enabled:
                    self.items.authorize(ctx, "context_find", uow)
                    from ..contextbase.service import _package
                    with base.service.open() as store:
                        allowed = _package().business_refs(store, ctx.business_id)
                        count = sum(o["kind"] == "person" and (allowed is None or o["ref"] in allowed)
                                    for o in store.list_objects(include_relationships=False))
            counts[kind] = count
        return {'navigation_only': True, 'counts': counts}


class HostProtectedEdges:
    """Business setup's membership protection of a stored edge (``BusinessSetupOperations.edge_guard``)."""

    def __init__(self, business_setup):
        self.business_setup = business_setup

    def check(self, ctx, uow, edge):
        self.business_setup.edge_guard(ctx, uow, edge)


def relationship_ports(items, *, documents, business_setup, clock=utc_clock) -> RelationshipPorts:
    """The one ``RelationshipPorts`` of this host, over the already built item resolver."""
    return RelationshipPorts(roles=ROLES, workflow_roles=WORKFLOW_ROLES, materials=HostMaterials(items, clock=clock),
                             archive=HostArchive(items, documents, clock=clock), navigation=HostNavigation(items, documents),
                             protected_edges=HostProtectedEdges(business_setup), review_edits=REVIEW_EDITS)
