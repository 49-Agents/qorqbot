"""``context_queue_*``: ordinary registered operations over the one durable Context input queue.

Source admission, inspection, retry and discard use the v1 prepare/apply authority like any other operation: the
caller's own namespace (a selected business, otherwise Global), the normal policy decision for that operation (which may
be allow, signoff or deny), idempotency and Signoff-bound plans. Nothing here reads or controls another namespace's
inputs; the single cross-namespace fact a reader sees is ``queue_blocked``.

Attempts, publication and the blocked/discard outcomes are not operations: only the backend system principal advances
them, through ``transitions``. Accepted Action revisions enter through ``queue.enqueue_action``, never through these."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.plans import PAGE, UUID, PlanBuilder, apply_records, ref, schema, utc_clock
from qorqut_operations.application.registry import CatalogEntry, OperationDescriptor
from qorqut_context.integration.queue.outcomes import TerminalOutcomes, require_outcomes
from qorqut_context.integration.queue.queue import (OPERATIONS, STATES, detail, general_arguments, general_values, input_view,
                                                   list_inputs, lookup, no_executions, public_payload)
from qorqut_operations.contracts import DomainError, new_id, plain

QUEUE_ENTRIES = (CatalogEntry("context_queue_list", False, "read"), CatalogEntry("context_queue_get", False, "read"),
                 CatalogEntry("context_queue_submit", True, "records"), CatalogEntry("context_queue_retry", True, "records"),
                 CatalogEntry("context_queue_discard", True, "records"))
SCOPE_KINDS = frozenset({"business", "global"})
NULLABLE_TEXT = {"type": ["string", "null"]}
NULLABLE_OBJECT = {"type": ["object", "null"]}
# A controlled input's execution facts (the graph-edit binding's), or null for an ordinary input.
EXECUTION_FACTS = schema({"kind": {"const": "deterministic_graph_edit"}, "plan_digest": {"type": "string"},
                          "expected_revision": {"type": "string"}, "reserved_event_id": {"type": "string"}},
                         ("kind", "plan_digest", "expected_revision", "reserved_event_id"))
EXECUTION = {"oneOf": [{"type": "null"}, EXECUTION_FACTS]}
SCOPE = {"oneOf": [schema({"kind": {"const": "global"}}, ("kind",)),
                   schema({"kind": {"const": "business"}, "business_id": {"type": "string"}}, ("kind", "business_id"))]}
INPUT_PROPERTIES = {
    "id": {"type": "string"}, "sequence": {"type": "integer"}, "input_kind": {"enum": ["general", "action"]},
    "operation": {"enum": list(OPERATIONS)}, "item_id": NULLABLE_TEXT, "revision": {"type": ["integer", "null"]},
    "submitted_by": {"type": "string"}, "created_at": {"type": "string"}, "state": {"enum": list(STATES)},
    "context_event_id": NULLABLE_TEXT, "attempt": {"type": "integer"}, "scope": SCOPE, "result": NULLABLE_OBJECT,
    "error": NULLABLE_OBJECT, "summary": {"type": "object"}, "execution": EXECUTION}
INPUT_OUTPUT = schema(INPUT_PROPERTIES, tuple(INPUT_PROPERTIES))
DETAIL_PROPERTIES = {**INPUT_PROPERTIES, "payload": {"type": "object"},
                     "attempts": {"type": "array", "items": schema({
                         "attempt": {"type": "integer"}, "context_event_id": NULLABLE_TEXT, "cause": {"enum": ["queue", "retry"]},
                         "control_id": NULLABLE_TEXT, "started_at": {"type": "string"}, "outcome": {"enum": ["published", "blocked", None]},
                         "finished_at": NULLABLE_TEXT, "result": NULLABLE_OBJECT, "error": NULLABLE_OBJECT},
                         ("attempt", "context_event_id", "cause", "control_id", "started_at", "outcome", "finished_at", "result", "error"))},
                     "controls": {"type": "array", "items": schema({
                         "id": {"type": "string"}, "intent": {"enum": ["retry", "discard"]}, "expected_state": {"enum": list(STATES)},
                         "requested_by": {"type": "string"}, "requested_at": {"type": "string"},
                         "status": {"enum": ["pending", "applied", "rejected"]}, "resolved_at": NULLABLE_TEXT, "result": NULLABLE_OBJECT},
                         ("id", "intent", "expected_state", "requested_by", "requested_at", "status", "resolved_at", "result"))}}
DETAIL_OUTPUT = schema(DETAIL_PROPERTIES, tuple(DETAIL_PROPERTIES))
SOURCE_METADATA = schema({"kind": {"type": "string", "minLength": 1}, "title": {"type": "string", "minLength": 1},
                          "origin": {"type": "string", "minLength": 1}, "origin_ref": {"type": "string", "minLength": 1},
                          "created_at": {"type": "string", "minLength": 1}})
CONTROL_RESULT = schema({"input_id": {"type": "string"}, "control_id": {"type": "string"}, "intent": {"enum": ["retry", "discard"]},
                         "status": {"enum": ["pending", "applied"]}, "state": {"enum": list(STATES)}},
                        ("input_id", "control_id", "intent", "status", "state"))
CONTROL_INPUT = schema({"input_id": UUID, "expected_state": {"enum": list(STATES)},
                        "rationale": {"type": "string", "minLength": 1, "maxLength": 2000}}, ("input_id", "expected_state"))


def queue_namespace(ctx, arguments):
    """The caller's own namespace: its selected business, otherwise Global (which needs a global capability)."""
    return "business:" + ctx.business_id if ctx.business_id else "global"


def decide(uow, row, intent: str, expected_state: str) -> tuple[str, str, dict | None]:
    """The control's immediate status, the input's next state and its resolution, from the input's current state."""
    if row["state"] != expected_state:
        raise DomainError("state_conflict", "The input is not in the expected state", details={"current_state": row["state"]})
    if uow.connection.execute("SELECT 1 FROM context_input_controls WHERE input_id=? AND status='pending'", (row["id"],)).fetchone():
        raise DomainError("control_pending", "A control request for this input is already pending")
    if intent == "retry":
        if row["state"] != "blocked":
            raise DomainError("invalid_transition", "Only the blocked head can be retried")
        return "pending", "blocked", None
    if row["state"] == "blocked":
        return "pending", "discard_requested", None
    if row["state"] == "queued" and row["attempt"] == 0 and row["context_event_id"] is None:
        # Definitely unstarted: no Context work exists, so no mechanical discard is needed.
        return "applied", "discarded", {"outcome": "discarded", "reason": "never_started", "mechanical": False}
    raise DomainError("invalid_transition", "Only the blocked head or an unstarted queued input can be discarded")


class ContextQueueOperations:
    def __init__(self, *, outcomes: TerminalOutcomes, max_content_bytes: int | None = None, clock=utc_clock, executions=None):
        if max_content_bytes is not None and (type(max_content_bytes) is not int or max_content_bytes < 1):
            raise ValueError("The Context input limit is a positive number of bytes")
        # A control can finish an input without the driver (a discard before any Context work), so it carries the same
        # required terminal-outcome contribution as the driver's transitions.
        self.outcomes, self.max_content_bytes, self.clock = require_outcomes(outcomes), max_content_bytes, clock
        # The execution facts of a controlled input (``context_graph_edits.ExecutionFacts``). Without the contribution an
        # ordinary input reads ``execution: null`` and a stored graph-edit input is refused rather than shown as ordinary.
        if executions is not None and not callable(executions):
            raise TypeError("The queue's execution contribution is a callable over a transaction and a stored input")
        self.executions = executions or no_executions

    def contribution(self) -> CatalogContribution:
        return CatalogContribution("context-queue", QUEUE_ENTRIES, self.descriptors())

    def descriptors(self):
        common = dict(scope_kinds=SCOPE_KINDS, global_capability="read_global_context", resolve_namespace=queue_namespace)
        page_output = schema({"items": {"type": "array", "items": INPUT_OUTPUT}, "next_cursor": NULLABLE_TEXT,
                              "queue_blocked": {"type": "boolean"}, "blocked_input_id": NULLABLE_TEXT},
                             ("items", "next_cursor", "queue_blocked", "blocked_input_id"))
        control = dict(output_schema=schema({"result": CONTROL_RESULT}, ("result",)), validate_targets=self.validate_control, **common)
        return (
            OperationDescriptor("context_queue_list", schema({**PAGE, "state": {"enum": list(STATES)}}), page_output,
                effect_kind="read", supports_signoff=False, read=self.list_inputs, **common,
                description=instruction_text('operations-list-this-namespace-s-context-inputs-in-queue')),
            OperationDescriptor("context_queue_get", schema({"input_id": UUID}, ("input_id",)), schema({"input": DETAIL_OUTPUT}, ("input",)),
                effect_kind="read", supports_signoff=False, read=self.get_input, **common,
                description=instruction_text('operations-read-one-of-this-namespace-s-context-inputs')),
            OperationDescriptor("context_queue_submit", schema({
                "operation": {"enum": list(OPERATIONS)}, "path": {"type": "string", "minLength": 1, "maxLength": 500},
                "content": {"type": "string", "minLength": 1}, "metadata": SOURCE_METADATA,
                "rationale": {"type": "string", "minLength": 1, "maxLength": 2000}}, ("operation", "path")),
                schema({"input": INPUT_OUTPUT}, ("input",)), prepare=self.prepare_submit, apply=self.apply_submit, **common,
                description=instruction_text('operations-queue-one-context-source-change-add-edit-or')),
            OperationDescriptor("context_queue_retry", CONTROL_INPUT, prepare=lambda *args: self.prepare_control("retry", *args),
                apply=self.apply_control, description=instruction_text('operations-ask-for-another-attempt-at-the-blocked-head'), **control),
            OperationDescriptor("context_queue_discard", CONTROL_INPUT, prepare=lambda *args: self.prepare_control("discard", *args),
                apply=self.apply_control, description=instruction_text('operations-discard-this-namespace-s-blocked-head-or-an'), **control))

    # -- reads -----------------------------------------------------------------------------------------------------

    def list_inputs(self, ctx, arguments, options, uow):
        return list_inputs(uow, ctx.business_id, arguments, "context_queue_list", ctx.principal_id, self.executions)

    def get_input(self, ctx, arguments, options, uow):
        view = detail(uow, lookup(uow, ctx.business_id, arguments["input_id"]), self.executions)
        # The driver works from the complete payload; a reader of the queue never sees an Action's model-only metadata.
        return {"input": {**view, "payload": public_payload(view["payload"])}}

    # -- source admission ------------------------------------------------------------------------------------------

    def prepare_submit(self, ctx, arguments, options, uow):
        change = general_arguments(arguments["operation"], arguments["path"], arguments.get("content"), arguments.get("metadata"),
                                   self.max_content_bytes)
        builder = PlanBuilder(ctx, "context_queue_submit", arguments, f"Queue Context source {change['operation']} {change['path']}",
                              clock=self.clock)
        input_id = new_id()
        values = {**general_values(input_id, change, ctx.principal_id, builder.at), "business_id": ctx.business_id}
        builder.record("context_inputs", input_id, None, values, business_id=ctx.business_id, label=change["path"])
        builder.effects.append("Queues this source change in the shared Context order. Nothing is published until the "
                               "backend driver processes it; it cannot be edited afterwards.")
        return builder.build()

    def apply_submit(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        change = next(change for change in plan.changes if change.resource.resource_type == "context_inputs")
        row = lookup(uow, change.resource.business_id, plain(change.values)["id"])
        return {"input": input_view(row, execution=self.executions(uow, row))}

    # -- controls --------------------------------------------------------------------------------------------------

    def prepare_control(self, intent, ctx, arguments, options, uow):
        row = lookup(uow, ctx.business_id, arguments["input_id"])
        status, state, resolution = decide(uow, row, intent, arguments["expected_state"])
        builder = PlanBuilder(ctx, "context_queue_" + intent, arguments, f"Request {intent} of Context input {row['id']}", clock=self.clock)
        control_id = new_id()
        builder.record("context_input_controls", control_id, None, {
            "id": control_id, "business_id": row["business_id"], "input_id": row["id"], "intent": intent,
            "expected_state": row["state"], "requested_by": ctx.principal_id, "requested_at": builder.at, "status": status,
            "resolved_at": builder.at if status == "applied" else None, "result_json": resolution},
            business_id=row["business_id"], label=intent)
        if state != row["state"]:
            builder.record("context_inputs", row["id"], dict(row), {"state": state, **({"result_json": resolution} if state == "discarded" else {})},
                           business_id=row["business_id"], label=row["id"])
        else:
            builder.expect(ref("context_inputs", row["id"], row["business_id"]), dict(row))
        if intent == "retry":
            builder.effects.append("Records the request; the backend driver starts the next attempt.")
        elif state == "discarded":
            builder.effects.append("Discards the input before any Context work exists: it stays in the history and is never published.")
        else:
            builder.effects.append("Records the request; the input is discarded once Context confirms the mechanical discard.")
        return builder.build()

    def validate_control(self, ctx, plan, uow):
        """Approval and apply re-derive the decision from the input's current state."""
        control = next(change for change in plan.changes if change.resource.resource_type == "context_input_controls")
        values = plain(control.values)
        row = lookup(uow, control.resource.business_id, values["input_id"])
        status, state, _ = decide(uow, row, values["intent"], values["expected_state"])
        moves = [change for change in plan.changes if change.resource.resource_type == "context_inputs" and change.action == "update"]
        if status != values["status"] or (state != row["state"]) != bool(moves) or (moves and plain(moves[0].values)["state"] != state):
            raise DomainError("revision_conflict", "The input changed since this request was prepared")

    def apply_control(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        control = next(change for change in plan.changes if change.resource.resource_type == "context_input_controls")
        values = plain(control.values)
        row = lookup(uow, control.resource.business_id, values["input_id"])
        if row["state"] == "discarded":
            self.outcomes.record(uow, row)
        return {"result": {"input_id": row["id"], "control_id": values["id"], "intent": values["intent"], "status": values["status"],
                           "state": row["state"]}}
