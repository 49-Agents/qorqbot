"""The neutral knowledge behind the ``RelationshipPorts``: role vocabulary, Material classification, composed archive and
the reviewable edit operations. The navigation counts and the workflow-role guidance stay with the host.

Each adapter is the deployed v1 code of the generic relationship module that was domain-specific, with its dependencies
stated (the item resolver, the document service, the application registry). None of them can replace application policy:
composed archive and review edits are prepared through the one registry and judged by their own operation's policy,
including any Signoff floor. The host's own records enter through two required hooks (``MaterialHost``) and one required
set of table names (``direct_record_tables``); nothing is discovered by attribute or module."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, Protocol

from qorqut_operations.application.plans import PlanBuilder, apply_records, require_revision, utc_clock
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.documents.materials import FILTERS as MATERIAL_FILTERS, MATERIAL_TYPES, RAW_KINDS, TAGS, classification, matches
from qorqut_operations.relationships import ReviewEdit

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
# The operations a review may compose with its decision, keyed by operation: the edited item must be the reviewed one.
REVIEW_EDITS = {
    "tasks_update": ReviewEdit("task", "task_id"),
    "tables_update": ReviewEdit("table", "table_id", apply_through_domain=True),
    "tables_rows_write": ReviewEdit("table", "table_id", apply_through_domain=True),
    "context_document_update": ReviewEdit(None, "document_id", resource_type="documents"),
    "context_people_update": ReviewEdit("person", "document_id"),
    "notes_update": ReviewEdit("material", "document_id"),
}
# The refusal of a resource that is not a classifiable Material (the host's refusals of its own records use the same words).
NOT_A_MATERIAL = "Use the item's dedicated tools to classify it"


class MaterialHost(Protocol):
    """The host's rules for its own records in the Material classification.

    ``validate_material(ctx, table, row, material, uow)`` refuses (``DomainError``) a classifiable resource the host
    keeps out of ``materials_update``; it runs after the generic not-a-material refusal and before the revision check.
    ``fixed_material(table, row)`` says whether the host fixes this source's Material type, beside the raw and content
    document kinds."""

    def validate_material(self, ctx, table: str, row: Mapping[str, Any], material: Mapping[str, Any], uow) -> None: ...

    def fixed_material(self, table: str, row: Mapping[str, Any]) -> bool: ...


class RecordMaterials:
    """The Material classification mutation (``materials_update``) and its listing vocabulary."""

    filters = MATERIAL_FILTERS
    material_types = MATERIAL_TYPES
    tags = TAGS

    def __init__(self, items, *, host, clock=utc_clock):
        if host is None:
            raise ValueError("Material classification needs the material host")
        self.items, self.host, self.clock = items, host, clock

    @staticmethod
    def matches(value, args):
        return matches(value, args)

    def prepare(self, ctx, args, options, uow):
        item = {"type": "material", "id": args["material_id"]}
        self.items.resolve(ctx, item, uow)
        table, row = self.items.resource(ctx, item, uow)
        material = classification(row, table)
        if not material:
            raise DomainError("workflow_required", NOT_A_MATERIAL)
        self.host.validate_material(ctx, table, row, material, uow)
        require_revision(row, options.expected_revision)
        changes = plain(args["changes"])
        fixed = self.host.fixed_material(table, row) or row.get("kind") in RAW_KINDS | {"content"}
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


class RecordArchive:
    """Archive and restore, composed over the owning domain's own operation (and policy) where it has one.
    ``direct_record_tables`` are the host's record tables archived by a plain ``archived`` flag on the row."""

    def __init__(self, items, documents, *, direct_record_tables, clock=utc_clock):
        if direct_record_tables is None:
            raise ValueError("Composed archive needs the direct record tables")
        self.items, self.documents, self.clock = items, documents, clock
        self.direct_record_tables = frozenset(direct_record_tables)

    def prepare(self, ctx, args, options, uow):
        self.items.resolve(ctx, args["item"], uow)
        table, row = self.items.resource(ctx, args["item"], uow)
        require_revision(row, options.expected_revision)
        adapters = {"tasks": ("tasks_update", "task_id"), "data_tables": ("tables_update", "table_id")}
        if table in adapters:
            operation, key = adapters[table]
            # Composed changes keep the domain operation's own policy, including Signoff floors.
            plan = self.items.application.registry.get(operation).prepare(ctx, {key: args["item"]["id"], "changes": {"archived": args["archived"]}}, options, uow)
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
        elif table in self.direct_record_tables:
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
