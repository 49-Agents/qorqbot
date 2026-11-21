"""Move Tables to another project: the read-only preview and the one atomic relocation of whole Tables.

``tables_move_to_project_preview`` reads exactly what a move would carry and what prevents it; ``tables_move_to_project``
applies it. The relocation itself is ``qorqut_operations.tables.relocation``; this module owns the host's authority gate and
wires the host adapter in. Destinations are the same as for Tasks (``tasks_move_targets``).

Authority is rechecked, never remembered, exactly as for a Task move: the installation owner or the configured active
Ordubashy, current reach of both projects, and an ``allow`` decision of ``tables_get`` and ``tables_update`` in the source and
``tables_create`` and ``tables_update`` in the destination. A Global table is never moved by this operation.
"""

from __future__ import annotations

from dataclasses import replace

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import UUID, PlanBuilder, ref, schema, timestamp, utc_clock
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.tables.relocation import COUNTS as CORE_COUNTS, MAXIMUM_TABLES, TableRelocation
from qorqut_operations.tasks.relocation import namespace

from qorqut.domains.projects import access

from .relocation import TableHostRelocation

PREVIEW = "tables_move_to_project_preview"
MOVE = "tables_move_to_project"
OPERATIONS = (PREVIEW, MOVE)
COUNTS = (*CORE_COUNTS, "private_messages", "focus_cards", "input_links")
VISIBILITY = ("Ordinary access to these tables follows the destination project's rules once they are moved. Links whose other "
              "end stays behind are kept in this project as history; moving grants no one membership and no new access.")

SELECTION = {"source_project_id": {**UUID, "description": instruction_text("moves-the-project-the-tables-are-in-now")},
             "destination_project_id": {**UUID, "description": instruction_text("moves-the-active-destination-project-never-inferred")},
             "table_ids": {"type": "array", "items": UUID, "minItems": 1, "maxItems": MAXIMUM_TABLES, "uniqueItems": True,
                           "description": instruction_text("moves-tables-to-move-with-their-rows-history")}}
REQUIRED = ("source_project_id", "destination_project_id", "table_ids")


def move_namespace(ctx, args):
    """The source's own namespace; a project route must agree with ``source_project_id``."""
    source = args.get("source_project_id")
    if ctx.project_id is not None and ctx.project_id != source:
        raise DomainError("validation_error", "source_project_id must match the selected project")
    return namespace(source)


COMMON = dict(supports_signoff=False, scope_kinds=frozenset({"project", "global"}), global_capability="read_global_context",
              resolve_namespace=move_namespace)


class TableMoves:
    """The table Move to project service of this host. ``documents`` is the host's ``DocumentRelocation``: with it, the file a
    table was imported from moves with the table."""

    PREVIEW, MOVE, KEY, NOUN, COUNTS, VISIBILITY = PREVIEW, MOVE, "table_ids", "tables", COUNTS, VISIBILITY
    SOURCE_OPERATIONS, DESTINATION_OPERATIONS = ("tables_get", "tables_update"), ("tables_create", "tables_update")
    SELECTION, DESCRIPTIONS = SELECTION, ("moves-owner-and-configured-ordubashy-read-exactly-what-moving-tables",
                                          "moves-owner-and-configured-ordubashy-move-the-previewed-tables")
    ATTRIBUTE = "table_moves"

    def __init__(self, application, *, clock=utc_clock, documents=None, relocation=None):
        self.application, self.clock, self.documents = application, clock, documents
        self.relocation = relocation or TableRelocation((TableHostRelocation(),), documents=documents)

    def register(self, registry=None):
        registry = registry or self.application.registry
        required = ("source_project_id", "destination_project_id", self.KEY)
        registry.register(OperationDescriptor(self.PREVIEW, schema(self.SELECTION, required), **COMMON, effect_kind="read",
            read=lambda ctx, args, options, uow: self.preview(ctx, args, uow), description=instruction_text(self.DESCRIPTIONS[0])))
        registry.register(OperationDescriptor(self.MOVE, schema({**self.SELECTION, "preview_token": {"type": "string", "minLength": 1,
                                                                                                     "maxLength": 200}},
                                                                (*required, "preview_token")),
            **COMMON, prepare=self.prepare, apply=self.apply, replay_validator=self.replay, description=instruction_text(self.DESCRIPTIONS[1])))
        setattr(self.application, self.ATTRIBUTE, self)
        return self

    # -- authority -------------------------------------------------------------------------------------------------

    def decide(self, ctx, uow, project_id, operations):
        scoped = replace(ctx, project_id=project_id, review_project_id=project_id)
        for name in operations:
            decision = self.application.authorize(scoped, self.application.registry.get(name), (), uow, namespace=namespace(project_id))
            if decision.mode != "allow":
                raise DomainError("policy_denied", f"This move needs a {self.NOUN} permission that is not granted without approval")
        return scoped

    def source(self, ctx, args, uow):
        current = self.application.current_context(ctx, uow)
        access.role(current, uow)
        source = args["source_project_id"]
        if not access.project_participant(current, source, uow):
            raise DomainError("not_found", "Source project does not exist or is outside your current access")
        if uow.repository("projects").get(source)["archived_at"] is not None:
            raise DomainError("project_archived", "The source project is archived")
        return self.decide(current, uow, source, self.SOURCE_OPERATIONS), current

    def destination(self, current, destination, uow):
        row = access.project_row(current, destination, uow)
        self.decide(current, uow, destination, self.DESTINATION_OPERATIONS)
        return row

    @staticmethod
    def project(row):
        return {"id": row["id"], "name": row["name"], "slug": row["slug"]}

    # -- reads -----------------------------------------------------------------------------------------------------

    def inspect(self, ctx, args, uow):
        selected, current = self.source(ctx, args, uow)
        target = self.destination(current, args["destination_project_id"], uow)
        inspection = self.relocation.inspect(selected, uow, args["source_project_id"], args["destination_project_id"], args[self.KEY])
        return selected, target, inspection

    def preview(self, ctx, args, uow):
        if self.documents is not None:
            self.documents.sweep(uow)  # retire the source files of committed moves
        selected, target, inspection = self.inspect(ctx, args, uow)
        source = args["source_project_id"]
        counts = {name: inspection.counts.get(name, 0) for name in self.COUNTS}
        return {"preview": {
            "token": inspection.fingerprint(selected.principal_id),
            "source": {"project_id": source, "name": uow.repository("projects").get(source)["name"]},
            "destination": self.project(target), self.NOUN: self.listed(inspection), "counts": counts,
            "visibility": {"summary": self.VISIBILITY}, "blockers": inspection.blockers, "can_move": inspection.can_move}}

    @staticmethod
    def listed(inspection):
        return inspection.tables

    # -- the move --------------------------------------------------------------------------------------------------

    def prepare(self, ctx, args, options, uow):
        selected, target, inspection = self.inspect(ctx, args, uow)
        if inspection.fingerprint(selected.principal_id) != args["preview_token"]:
            raise DomainError("revision_conflict", "The selection changed after the preview; preview it again")
        if not inspection.can_move:
            raise DomainError("move_blocked", "The move is blocked", details={"blockers": inspection.blockers})
        source = args["source_project_id"]
        builder = PlanBuilder(selected, self.MOVE, args, f"Move {self.NOUN} to project", namespace=namespace(source), clock=self.clock)
        builder.expect(ref("projects", target["id"], None), target)
        builder.expect(ref("projects", source, None), uow.repository("projects").get(source))
        self.expect(builder, inspection, source, uow)
        return builder.build()

    @staticmethod
    def expect(builder, inspection, source, uow):
        for identity in inspection.move.ids:
            builder.expect(ref("data_tables", identity, source), uow.repository("data_tables", project_id=source).get(identity))

    def apply(self, ctx, plan, uow):
        args = plan.arguments
        selected, current = self.source(ctx, args, uow)
        target = self.destination(current, args["destination_project_id"], uow)
        move_id = new_id()
        result = self.relocate(selected, uow, args, move_id, timestamp(self.clock))
        counts = {name: result["counts"].get(name, 0) for name in self.COUNTS}
        return {"move_id": move_id, "destination": self.project(target), "moved": result["moved"], "counts": counts}

    def relocate(self, selected, uow, args, move_id, at):
        return self.relocation.apply(selected, uow, args["source_project_id"], args["destination_project_id"], args[self.KEY],
                                     args["preview_token"], move_id=move_id, at=at)

    def replay(self, ctx, args, saved, uow):
        """A saved receipt never outlives the identity or destination authority that produced it."""
        current = self.application.current_context(ctx, uow)
        access.role(current, uow)
        self.destination(current, args["destination_project_id"], uow)


__all__ = ["COUNTS", "MOVE", "OPERATIONS", "PREVIEW", "TableMoves", "VISIBILITY"]
