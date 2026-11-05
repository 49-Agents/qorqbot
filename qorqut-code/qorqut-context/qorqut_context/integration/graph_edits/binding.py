"""Admission and reads of the graph-edit binding.

A trusted adapter (the ``context_edit`` operation, ``qorqut_context.integration.filing.source_operations``) calls ``admit`` from ``prepare``. Nothing is written
here: the companion binding and the GENERAL queue input are two prepared records of the adapter's plan, applied in that
order in one transaction. The engine imports no Context library and does not interpret the edit: the host supplies the
validated primitive plan, its canonical source text and digest, the reserved event ID, the original caller's attribution
(which includes the closed delegation triplet of a delegated caller, or ``null``) and the frozen target evidence. What this module enforces is the queue and binding invariant: the payload is exactly the
plan text at the reserved path with the fixed metadata and the original submitter, the digest is the SHA-256 of those
bytes, the base is the plan's own ``expected_revision``, and the reserved event ID is one nothing used.

``load`` is the one read. It never infers a binding from a path, JSON shape or metadata: a stored input is bound only by
its companion row, and a reserved path with no (or a mismatched) companion is an integrity failure, not an ordinary input.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from uuid import UUID

from qorqut_context.integration.graph_edits.schema import EXECUTION, METADATA, PREFIX
from qorqut_context.integration.queue.queue import general_values, text
from qorqut_operations.contracts import DomainError, new_id, plain

_EVENT_ID = re.compile(r"[a-z0-9]{5}")
_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ATTRIBUTION = ("principal_id", "principal_kind")
_DELEGATION = {"binding_id", "assignment_id", "generation"}
INTEGRITY = "graph_edit_integrity"
UNBOUND = "graph_edit_unbound"
BINDING_ERRORS = (INTEGRITY, UNBOUND)
COMPOSITION = "composition_error"


@dataclass(frozen=True)
class Admission:
    input_id: str
    reserved_event_id: str
    plan_digest: str
    expected_revision: str


def source_path(event_id: str) -> str:
    return PREFIX + event_id + ".txt"


def is_reserved(payload: dict) -> bool:
    """Whether a stored GENERAL payload names the reserved graph-edit path. A pure shape test, never authority."""
    path = payload.get("path") if isinstance(payload, dict) else None
    return isinstance(path, str) and path.startswith(PREFIX)


def _select(uow, sql: str, parameters=()):
    """The binding table is a required part of the composed schema: a database without it is a composition error, never an
    ordinary input and never an empty set of reservations. Nothing falls back and nothing is probed."""
    try:
        return uow.connection.execute(sql, parameters)
    except sqlite3.OperationalError as problem:
        if "no such table: context_graph_edits" in str(problem):
            raise DomainError(COMPOSITION, "The context_graph_edits storage contribution is required by the Context queue and is not "
                              "composed into this database") from None
        raise


# -- the original caller's delegation -------------------------------------------------------------------------------------

def _canonical_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def delegation_of(ctx) -> dict | None:
    """The delegation of an authenticated invocation context: ``None`` for a direct caller, otherwise the binding, assignment and
    generation it was authenticated with (a context cannot carry a partial triplet)."""
    parts = (ctx.delegation_binding_id, ctx.delegation_assignment_id, ctx.delegation_generation)
    if all(part is None for part in parts):
        return None
    return dict(zip(("binding_id", "assignment_id", "generation"), parts))


def check_delegation(attribution: dict, code: str = "validation_error") -> None:
    """The attribution's closed ``delegation`` value: present, and ``None`` for a direct caller or exactly the delegated agent's
    binding ID, assignment ID (canonical UUIDs) and positive integer generation. No credential, lease or decision is part of it."""
    if "delegation" not in attribution:
        _refuse("The graph edit's attribution must say whether the caller was delegated (null for a direct caller)", code)
    value = attribution["delegation"]
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != _DELEGATION or attribution.get("principal_kind") != "agent"
            or not _canonical_uuid(value["binding_id"]) or not _canonical_uuid(value["assignment_id"])
            or type(value["generation"]) is not int or value["generation"] < 1):
        _refuse("A delegated graph edit records exactly its binding, assignment and positive generation on an agent principal", code)


# -- event IDs ----------------------------------------------------------------------------------------------------------

def reserved_event_ids(uow) -> set[str]:
    """Every Context event ID some graph edit has reserved, whether or not that edit started."""
    return {row[0] for row in _select(uow, "SELECT reserved_event_id FROM context_graph_edits")}


def taken_event_ids(uow) -> set[str]:
    """Event IDs the queue or an earlier reservation already holds: a new reservation must be none of them."""
    return (reserved_event_ids(uow)
            | {row[0] for row in uow.connection.execute("SELECT context_event_id FROM context_inputs WHERE context_event_id IS NOT NULL")}
            | {row[0] for row in uow.connection.execute(
                "SELECT context_event_id FROM context_input_attempts WHERE context_event_id IS NOT NULL")})


# -- admission ----------------------------------------------------------------------------------------------------------

def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _refuse(message: str, code: str = "validation_error"):
    raise DomainError(code, message)


def check_relation(plan, content, digest, event_id, revision, attribution, submitted_by, targets, *, code: str = "validation_error") -> None:
    """The invariants between one binding's parts, shared by admission (a refusal) and reads (an integrity failure)."""
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        _refuse("The reserved Context event ID is five lowercase letters or digits", code)
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or not isinstance(revision, str) or not _COMMIT.fullmatch(revision):
        _refuse("A graph edit needs its plan digest and a full published base commit", code)
    if not isinstance(plan, dict) or plan.get("expected_revision") != revision:
        _refuse("The graph-edit plan must be for its recorded base", code)
    if not isinstance(content, str):
        _refuse("The graph-edit source text is missing", code)
    try:
        parsed = json.loads(content)
    except ValueError:
        _refuse("The graph-edit source text is not a plan", code)
    if parsed != plan or _sha256(content) != digest:
        _refuse("The graph-edit source text is not exactly its plan and digest", code)
    if (not isinstance(attribution, dict) or any(not isinstance(attribution.get(name), str) or not attribution[name]
                                                 for name in _ATTRIBUTION) or attribution["principal_id"] != submitted_by):
        _refuse("The graph edit's attribution must name its submitter", code)
    check_delegation(attribution, code)
    if not isinstance(targets, list) or any(not isinstance(item, dict) or not isinstance(item.get("kind"), str)
                                           or not isinstance(item.get("id"), str) for item in targets):
        _refuse("The graph edit's target evidence is a list of kind/ID subjects", code)


def admit(builder, uow, *, plan: dict, content: str, plan_digest: str, reserved_event_id: str, attribution: dict,
          targets: list, business_id: str | None, max_content_bytes: int | None = None) -> Admission:
    """Prepare the two records of one graph edit (its binding, then its queue input) and return their identities.

    ``plan`` is the host's normalized primitive plan (a JSON object that carries its own ``expected_revision``), ``content``
    its canonical source text and ``plan_digest`` the SHA-256 of that text's UTF-8 bytes. ``attribution`` is the original
    caller (``principal_id`` must be the builder's caller, ``principal_kind`` the caller's kind, ``delegation`` the caller's own
    authenticated delegation triplet or ``None``) and ``targets`` the frozen target evidence. ``business_id`` is the selected owning namespace and must be the caller's selected scope."""
    ctx = builder.ctx
    if business_id != ctx.business_id:
        _refuse("A graph edit is queued in the caller's selected scope", "forbidden")
    text(content, "content")
    if max_content_bytes is not None and len(content.encode("utf-8")) > max_content_bytes:
        _refuse(f"content exceeds the configured limit of {max_content_bytes} bytes")
    plan, attribution, targets = plain(plan), plain(attribution), plain(targets)
    check_relation(plan, content, plan_digest, reserved_event_id, plan.get("expected_revision") if isinstance(plan, dict) else None,
                   attribution, ctx.principal_id, targets)
    if attribution["principal_kind"] != ctx.principal.kind:
        _refuse("The graph edit's attribution must name its submitter")
    if attribution["delegation"] != delegation_of(ctx):
        _refuse("The graph edit's attribution must carry the delegation the caller was authenticated with", "forbidden")
    if reserved_event_id in taken_event_ids(uow):
        raise DomainError("conflict", "The reserved Context event ID is already used; prepare the edit again",
                          details={"reserved_event_id": reserved_event_id})
    input_id = new_id()
    arguments = {"operation": "add", "path": source_path(reserved_event_id), "content": content, "metadata": dict(METADATA)}
    builder.record("context_graph_edits", input_id, None, {
        "input_id": input_id, "reserved_event_id": reserved_event_id, "plan_digest": plan_digest,
        "expected_revision": plan["expected_revision"], "plan_json": plan, "attribution_json": attribution, "targets_json": targets},
        business_id=business_id, label=f"Graph edit {reserved_event_id}")
    builder.record("context_inputs", input_id, None, {**general_values(input_id, arguments, ctx.principal_id, builder.at),
                   "business_id": business_id}, business_id=business_id, label=arguments["path"])
    builder.effects.append(f"Queues one explicit graph edit (Context event {reserved_event_id}) in the shared Context order. "
                           "Nothing is published until the backend driver rechecks the caller's current authority and the "
                           "published base and runs exactly this plan with no model.")
    return Admission(input_id, reserved_event_id, plan_digest, plan["expected_revision"])


# -- reads --------------------------------------------------------------------------------------------------------------

def execution_facts(binding: dict) -> dict:
    return {"kind": EXECUTION, "plan_digest": binding["plan_digest"], "expected_revision": binding["expected_revision"],
            "reserved_event_id": binding["reserved_event_id"]}


def load(uow, input_id: str) -> dict | None:
    """The verified binding of a stored queue input, or ``None`` for an ordinary input.

    A binding exists only as a companion row. An input on the reserved path with no companion, a companion on any other
    kind of input, and every mismatch between the companion, the payload bytes, the submitter and the base are
    integrity failures: nothing is dispatched or shown as an ordinary input."""
    row = uow.connection.execute("SELECT * FROM context_inputs WHERE id=?", (input_id,)).fetchone()
    if row is None:
        raise DomainError("not_found", "Context input does not exist")
    stored = _select(uow, "SELECT * FROM context_graph_edits WHERE input_id=?", (input_id,)).fetchone()
    if row["input_kind"] != "general":
        if stored is not None:
            raise DomainError(INTEGRITY, "A graph-edit binding belongs to a GENERAL input")
        return None
    payload = json.loads(row["payload_json"])
    if stored is None:
        if is_reserved(payload):
            raise DomainError(UNBOUND, "This input uses the reserved graph-edit path but has no graph-edit binding")
        return None
    plan, attribution, targets = (json.loads(stored[name]) for name in ("plan_json", "attribution_json", "targets_json"))
    event_id = stored["reserved_event_id"]
    shape = {"operation": "add", "path": source_path(event_id), "metadata": METADATA, "submitted_by": row["submitted_by"]}
    if (row["operation"] != "add" or set(payload) != {*shape, "content"} or any(payload.get(name) != value for name, value in shape.items())):
        raise DomainError(INTEGRITY, "The input no longer matches its graph-edit binding")
    try:
        check_relation(plan, payload["content"], stored["plan_digest"], event_id, stored["expected_revision"], attribution,
                       row["submitted_by"], targets, code=INTEGRITY)
    except DomainError as problem:
        raise DomainError(INTEGRITY, problem.message) from None
    return {"input_id": input_id, "business_id": row["business_id"], "reserved_event_id": event_id,
            "plan_digest": stored["plan_digest"], "expected_revision": stored["expected_revision"], "plan": plan,
            "attribution": attribution, "targets": targets, "content": payload["content"], "submitted_by": row["submitted_by"]}


class ExecutionFacts:
    """The queue's ``executions`` contribution: the execution facts a graph-edit input exposes on every read, or ``None``."""

    def __call__(self, uow, row) -> dict | None:
        binding = load(uow, row["id"])
        return None if binding is None else execution_facts(binding)
