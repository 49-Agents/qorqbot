"""Move to project: the three operations that relocate Tasks, with their descendants, into another active Project.

``tasks_move_targets`` lists the destinations, ``tasks_move_to_project_preview`` reads exactly what a move would carry and
what prevents it, and ``tasks_move_to_project`` applies it. The relocation itself (guarded identity-preserving updates,
dependency classes, blockers, the append-only audit) is ``TaskOperations.relocation`` in Operations; this module owns the
host's authority gate and wires the host adapter in.

Authority is rechecked, never remembered. Every call, a preview, a mutation and a replayed receipt alike, requires the
installation owner or the exact configured active Ordubashy, current reach of the Projects it names, and the ordinary policy
decision (tool overrides included) of ``tasks_get`` and ``tasks_update`` in the source and ``tasks_create`` and
``tasks_update`` in the destination. Only an ``allow`` decision moves anything: a Signoff-gated or denied Task tool is never
bypassed by this operation. A replay re-checks the destination and the identity gate, and never executes the move again.
"""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from dataclasses import replace

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import IDENTITY, UUID, PlanBuilder, ref, schema, timestamp, utc_clock
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.tasks.relocation import namespace
from qorqut_operations.tasks.scope import task_context

from bazarlyk.domains.projects import access

from .relocation import HostRelocation

TARGETS = "tasks_move_targets"
PREVIEW = "tasks_move_to_project_preview"
MOVE = "tasks_move_to_project"
OPERATIONS = (TARGETS, PREVIEW, MOVE)
MAXIMUM_TARGETS = 1000
COUNTS = ("tasks", "comments", "private_messages", "project_links", "focus_cards", "crm_entries")
VISIBILITY = ("Ordinary access to these Tasks follows the destination project's rules once they are moved. The private "
              "owner-Ordubashy conversations of the moved Tasks stay private and keep their messages; moving grants no one "
              "membership and no new access.")

SOURCE = {"source_business_id": {"anyOf": [UUID, {"type": "null"}],
                                 "description": instruction_text('moves-the-project-the-tasks-are-in-now-its')}}
SELECTION = {**SOURCE, "destination_project_id": {**UUID, "description": instruction_text('moves-the-active-destination-project-never-inferred')},
             "task_ids": {"type": "array", "items": IDENTITY, "minItems": 1, "maxItems": 100, "uniqueItems": True,
                          "description": instruction_text('moves-root-tasks-to-move-with-all-of-their')}}


def move_namespace(ctx, args):
    """The source's own namespace; a business route must agree with ``source_business_id``."""
    source = args.get("source_business_id")
    if ctx.business_id is not None and ctx.business_id != source:
        raise DomainError("validation_error", "source_business_id must match the selected project")
    return namespace(source)


COMMON = dict(supports_signoff=False, scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
              resolve_namespace=move_namespace)


class TaskMoves:
    """The Move to project service of this host."""

    def __init__(self, application, tasks, *, clock=utc_clock):
        self.application, self.tasks, self.clock = application, tasks, clock
        self.adapter = HostRelocation(application)
        tasks.relocation.adapters = (*tasks.relocation.adapters, self.adapter)

    # -- registration ----------------------------------------------------------------------------------------------

    def register(self, registry=None):
        registry = registry or self.application.registry
        registry.register(OperationDescriptor(TARGETS, schema(SOURCE, ("source_business_id",)), **COMMON, effect_kind="read",
            read=lambda ctx, args, options, uow: self.targets(ctx, args, uow),
            description=instruction_text('moves-owner-and-configured-ordubashy-the-active-projects-tasks')))
        registry.register(OperationDescriptor(PREVIEW, schema(SELECTION, ("source_business_id", "destination_project_id", "task_ids")),
            **COMMON, effect_kind="read", read=lambda ctx, args, options, uow: self.preview(ctx, args, uow),
            description=instruction_text('moves-owner-and-configured-ordubashy-read-exactly-what-moving')))
        registry.register(OperationDescriptor(MOVE, schema({**SELECTION, "preview_token": {"type": "string", "minLength": 1,
                                                                                            "maxLength": 200}},
                                                           ("source_business_id", "destination_project_id", "task_ids", "preview_token")),
            **COMMON, prepare=self.prepare, apply=self.apply, replay_validator=self.replay,
            description=instruction_text('moves-owner-and-configured-ordubashy-move-the-previewed-tasks')))
        self.application.task_moves = self
        return self

    # -- authority -------------------------------------------------------------------------------------------------

    def scoped(self, ctx, business_id):
        return replace(ctx, business_id=business_id, review_business_id=business_id)

    def decide(self, ctx, uow, business_id, operations):
        """Every named Task tool must currently be allowed in ``business_id``: policy, overrides and membership included."""
        scoped = self.scoped(ctx, business_id)
        for name in operations:
            decision = self.application.authorize(scoped, self.application.registry.get(name), (), uow, namespace=namespace(business_id))
            if decision.mode != "allow":
                raise DomainError("policy_denied", "This move needs a Task permission that is not granted without approval")
        return scoped

    def source(self, ctx, args, uow):
        """The caller refreshed, the identity gate passed and the source's read and update authority proven."""
        current = self.application.current_context(ctx, uow)
        access.role(current, uow)
        source = args["source_business_id"]
        if source is None:
            current = task_context(current, {"scope": "global"}, uow)
        elif not access.business_participant(current, source, uow):
            raise DomainError("not_found", "Source project does not exist or is outside your current access")
        elif uow.repository("businesses").get(source)["archived_at"] is not None:
            raise DomainError("business_archived", "The source project is archived")
        return self.decide(current, uow, source, ("tasks_get", "tasks_update")), current

    def destination(self, current, destination, uow, *, archived=False):
        """The destination Project row, once its reach, state and create and update authority are proven."""
        row = access.business_row(current, destination, uow, include_archived=archived)
        self.decide(current, uow, destination, ("tasks_create", "tasks_update"))
        return row

    @staticmethod
    def project(row):
        return {"id": row["id"], "name": row["name"], "slug": row["slug"]}

    # -- reads -----------------------------------------------------------------------------------------------------

    def targets(self, ctx, args, uow):
        selected, current = self.source(ctx, args, uow)
        source = args["source_business_id"]
        rows = uow.connection.execute("SELECT id, name, slug FROM businesses WHERE archived_at IS NULL ORDER BY name, id").fetchall()
        found = []
        for row in rows:
            if row["id"] == source or not access.business_participant(current, row["id"], uow):
                continue
            try:
                self.decide(current, uow, row["id"], ("tasks_create", "tasks_update"))
            except DomainError:
                continue
            found.append(self.project(row))
            if len(found) > MAXIMUM_TARGETS:
                raise DomainError("result_too_large", "Too many destination projects to list; none were returned")
        return {"projects": found, "next_cursor": None}

    def inspect(self, ctx, args, uow):
        selected, current = self.source(ctx, args, uow)
        source, destination = args["source_business_id"], args["destination_project_id"]
        target = self.destination(current, destination, uow)
        inspection = self.tasks.relocation.inspect(selected, uow, source, destination, args["task_ids"])
        return selected, target, inspection

    def preview(self, ctx, args, uow):
        selected, target, inspection = self.inspect(ctx, args, uow)
        source = args["source_business_id"]
        origin = uow.repository("businesses").get(source)["name"] if source else "Global"
        counts = {name: inspection.counts.get(name, 0) for name in COUNTS}
        return {"preview": {
            "token": inspection.fingerprint(selected.principal_id),
            "source": {"business_id": source, "name": origin}, "destination": self.project(target),
            "roots": inspection.roots, "tasks": inspection.tasks, "counts": counts,
            "visibility": {"summary": VISIBILITY}, "blockers": inspection.blockers, "can_move": inspection.can_move}}

    # -- the move --------------------------------------------------------------------------------------------------

    def prepare(self, ctx, args, options, uow):
        selected, target, inspection = self.inspect(ctx, args, uow)
        if inspection.fingerprint(selected.principal_id) != args["preview_token"]:
            raise DomainError("revision_conflict", "The selection changed after the preview; preview it again")
        if not inspection.can_move:
            raise DomainError("move_blocked", "The move is blocked", details={"blockers": inspection.blockers})
        source = args["source_business_id"]
        builder = PlanBuilder(selected, MOVE, args, "Move to project", namespace=namespace(source), clock=self.clock)
        builder.expect(ref("businesses", target["id"], None), target)
        if source is not None:
            builder.expect(ref("businesses", source, None), uow.repository("businesses").get(source))
        for root in inspection.relocation.roots:
            row = uow.repository("tasks", business_id=source).get(root)
            builder.expect(ref("tasks", root, source, "global" if source is None else None), row)
        return builder.build()

    def apply(self, ctx, plan, uow):
        args = plan.arguments
        selected, current = self.source(ctx, args, uow)
        target = self.destination(current, args["destination_project_id"], uow)
        move_id = new_id()
        result = self.tasks.relocation.apply(selected, uow, args["source_business_id"], args["destination_project_id"], args["task_ids"],
                                             args["preview_token"], move_id=move_id, at=timestamp(self.clock))
        counts = {name: result["counts"].get(name, 0) for name in COUNTS}
        return {"move_id": move_id, "destination": self.project(target), "moved": result["moved"], "counts": counts}

    def replay(self, ctx, args, saved, uow):
        """A saved receipt never outlives the identity or destination authority that produced it."""
        current = self.application.current_context(ctx, uow)
        access.role(current, uow)
        self.destination(current, args["destination_project_id"], uow)


__all__ = ["COUNTS", "MOVE", "OPERATIONS", "PREVIEW", "TARGETS", "TaskMoves", "VISIBILITY", "move_namespace"]
