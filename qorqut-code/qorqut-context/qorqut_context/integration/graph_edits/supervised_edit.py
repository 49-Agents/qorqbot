"""Host side of the supervised Context edit: the second, direct variant of ``context_edit``.

``context_edit`` accepts either a ``qorqut-graph-edit-v1`` plan, which ``graph_edit.py`` queues unchanged, or a
``qorqut-supervised-edit-v1`` plan (node, edge and note operations) with its exact Markdown ``document`` and a caller-chosen
``event_id``. The supervised variant is not queued and has no Operations binding: it is a directly published external effect of
the Context library, run through the descriptor's existing prepare / external / apply lifecycle, as calendar operations are.

* ``prepare`` is read-only. It normalizes the plan and document within the configured source bytes, asks the Context for its
  pure ``preview_supervised`` (a stale base, a busy writer, a foreign pending job or a reused event ID is refused here), derives
  the frozen target evidence (``analyze``) and requires the caller's retained authority in the admission namespace and in the
  namespace of every target. The bound request (event ID, plan and document digests, source bytes, original attribution and the
  frozen targets) is the prepared plan's single ``context_supervised_edit`` preview resource and its metadata; no record effect
  is prepared.
* ``external`` runs before the short apply transaction and outside any SQLite write lock. It validates the complete prepared
  shape against the invocation's own arguments, resolves the original caller's *current* registration, role, grants and
  delegation (never the driver's identity), re-derives the targets and requires them equal to the frozen evidence, and only then
  publishes through ``Pipeline.submit_supervised`` (or returns the pure preview when ``preview_only`` is set). That pipeline
  entry is the only publication path; it makes no model call, and the host never builds a filer of its own.
* ``apply`` verifies the same bound request and returns exactly the outcome ``external`` produced for this prepared plan.
* A published event with the same ``event_id`` and an identical submission is reconciled, never repeated: it is reported
  without requiring the target authority that ran the first time, and only an exact already-done effect is reported this way.
  A stale ``expected_revision`` is never rebased.

Ownership of an existing node, edge or note is the scope in its first creation event's ``record_context.scope`` (the shared rules
of ``graph_edit.py``; a record with no or malformed scope is unknown and protected). A note also needs the authority of every
subject it links, before or after the edit, and a note with no linked subject needs the Global namespace.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any, Callable

from qorqut_context import Conflict, ContextError, PropagationError, supervised_edits as library
from qorqut_context.integration import graph_edits as bindings
from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.contracts import DomainError, PreviewResource, canonical_hash, plain

from qorqut_context.integration.driver import writer_busy
from .graph_edit import (COMMIT_SCHEMA, EDGE_SCHEMA, ID_SCHEMA, NODE_SCHEMA, OUTPUT_SCHEMA as GRAPH_OUTPUT, PLAN_SCHEMA as GRAPH_PLAN,
                         TEXT_SCHEMA, GraphEdits, first_creations, namespace_name, namespaces, ownership_of, put_operation,
                         remove_operation, scope_of)

FORMAT = library.FORMAT
RESOURCE = "context_supervised_edit"
PREPARED = frozenset({"kind", "event_id", "preview_only", "expected_revision", "plan_digest", "document_digest", "document_bytes",
                      "source_bytes", "business_id", "attribution", "targets"})
RECEIPT_FIELDS = ("status", "event_id", "source_id", "base_revision", "published_commit", "input_mode", "handoff_disposition",
                  "execution", "result_counts", "model", "recovered")
SUPERVISED_ONLY = ("document", "event_id", "preview_only")
STASHED_OUTCOMES = 64
IN_FLIGHT = "Complete, retry, or discard the pending event"
STALE = "Stale expected revision"

LINK_SCHEMA = schema({"kind": {"enum": ["node", "edge"]}, "id": ID_SCHEMA}, ("kind", "id"))
NOTE_SCHEMA = schema({"id": ID_SCHEMA, "text": TEXT_SCHEMA, "objects": {"type": "array", "items": LINK_SCHEMA}}, ("id", "text", "objects"))
PLAN_SCHEMA = schema({
    "format": {"const": FORMAT}, "expected_revision": COMMIT_SCHEMA, "rationale": TEXT_SCHEMA,
    "operations": {"type": "array", "minItems": 1, "items": {"oneOf": [
        put_operation("put_node", NODE_SCHEMA), put_operation("put_edge", EDGE_SCHEMA), put_operation("put_note", NOTE_SCHEMA),
        remove_operation("remove_node"), remove_operation("remove_edge"), remove_operation("remove_note")]}}},
    ("format", "expected_revision", "rationale", "operations"))
DISCRIMINATED_PLAN = {"type": "object", "if": {"properties": {"format": {"const": FORMAT}}, "required": ["format"]},
                      "then": PLAN_SCHEMA, "else": GRAPH_PLAN}
EDIT_INPUT_SCHEMA = schema({
    "edit": DISCRIMINATED_PLAN,
    "document": {"type": "string", "minLength": 1},
    "event_id": ID_SCHEMA,
    "preview_only": {"type": "boolean"}}, ("edit",))
PREVIEW_SCHEMA = {"type": "object", "required": ["status", "event_id", "plan_digest", "document_digest", "submission_digest"],
                  "properties": {"status": {"enum": ["ready", "pending", "published"]}, "event_id": ID_SCHEMA}}
RECEIPT_SCHEMA = schema({
    "status": {"const": "published"}, "event_id": ID_SCHEMA, "source_id": ID_SCHEMA, "base_revision": COMMIT_SCHEMA,
    "published_commit": COMMIT_SCHEMA, "input_mode": {"type": "string"}, "handoff_disposition": {"type": "string"},
    "execution": {"type": "object"}, "result_counts": {"type": "object"}, "model": {"type": "null"}, "recovered": {"type": "boolean"}},
    ("status", "event_id", "source_id", "base_revision", "published_commit", "input_mode", "execution", "result_counts", "model",
     "recovered"))
EDIT_OUTPUT_SCHEMA = {"type": "object", "anyOf": [GRAPH_OUTPUT, PREVIEW_SCHEMA, RECEIPT_SCHEMA]}


def is_supervised(arguments: Any) -> bool:
    """Whether validated ``context_edit`` arguments carry a supervised plan."""
    edit = arguments.get("edit") if hasattr(arguments, "get") else None
    return hasattr(edit, "get") and edit.get("format") == FORMAT


def require_variant(arguments: Any) -> None:
    """The supervised-only fields belong to the supervised variant, which in turn needs its document and event ID."""
    if is_supervised(arguments):
        missing = [name for name in ("document", "event_id") if name not in arguments]
        if missing:
            raise DomainError("validation_error", "A supervised edit requires its exact document and a five-character event_id",
                              field_errors={name: "required for a supervised edit" for name in missing})
        return
    stray = [name for name in SUPERVISED_ONLY if name in arguments]
    if stray:
        raise DomainError("validation_error", "document, event_id and preview_only belong to a qorqut-supervised-edit-v1 plan",
                          field_errors={name: "not accepted with a qorqut-graph-edit-v1 plan" for name in stray})


@dataclass(frozen=True)
class Request:
    """The caller's exact supervised proposal: the normalized plan, the document bytes and the engine's execution marker."""

    plan: dict
    document: bytes
    event_id: str
    preview_only: bool
    marker: dict


def build_request(arguments: Any, source_bytes: int) -> Request:
    """The validated request of ``context_edit`` arguments, bounded by the configured source bytes."""
    arguments = plain(arguments)
    try:
        plan = library.normalize(arguments["edit"], source_bytes)
        document = arguments["document"].encode("utf-8")
        library.document(document, source_bytes)
        if len(library.canonical(plan)) > source_bytes:
            raise ContextError(f"The supervised edit plan exceeds the configured source limit of {source_bytes} bytes")
        marker = library.marker(plan, document)
    except KeyError:
        raise DomainError("validation_error", "A supervised edit requires its exact document and a five-character event_id") from None
    except (ContextError, UnicodeEncodeError) as error:
        raise DomainError("validation_error", str(error)) from None
    return Request(plan, document, arguments["event_id"], bool(arguments.get("preview_only", False)), marker)


def record_context_of(attribution: dict, business_id: str | None) -> dict:
    """The controlled evidence kept with the published event: the exact original scope and attribution of the caller."""
    return {"scope": scope_of(business_id), "provenance": {"submitted_by": attribution["principal_id"], "attribution": attribution,
                                                           "operation": "context_edit", "variant": "supervised"}}


def refused(error: Exception) -> DomainError:
    """The caller-facing refusal of a Context failure: transient states are retryable, a changed base or a reused ID is not."""
    message = str(error)
    if isinstance(error, Conflict):
        if writer_busy(error):
            return DomainError("busy", "The Context writer is busy; retry the same supervised edit shortly", retryable=True)
        if IN_FLIGHT in message:
            return DomainError("busy", "Another Context job is pending; retry the same supervised edit after it has completed",
                               retryable=True)
        if message.startswith(STALE):
            return DomainError("conflict", f"{message}. Read the Context again and submit a newly reviewed plan; an edit is never rebased")
        return DomainError("conflict", message)
    if isinstance(error, PropagationError):
        return DomainError("operation_unavailable", "The supervised edit could not be completed; retry the same edit", retryable=True)
    if isinstance(error, ContextError):
        return DomainError("validation_error", message)
    return DomainError("operation_unavailable", "The Context cannot be reached now; retry the same supervised edit", retryable=True)


def analyze(plan: dict, snapshot, admission: str | None, registered: Callable[[str], bool]) -> list[dict]:
    """The frozen target evidence of a normalized supervised plan against its fixed base.

    Every created, updated and removed node, edge and note is a target; the old and proposed endpoints of an edge and the old and
    proposed subjects of a note are references. Existing records are owned by their first creation event's controlled scope,
    records the plan creates by the admission namespace, and everything else is unknown. A note with no subject, before or after
    the edit, is marked ``unattached``."""
    subjects: dict[tuple, dict] = {}
    created: set = set()
    unattached: set = set()

    def touch(kind, target, change, role):
        entry = subjects.setdefault((kind, target), {"kind": kind, "id": target, "change": "reference", "roles": set()})
        entry["roles"].add(role)
        if change:
            entry["change"] = change

    for operation in plan["operations"]:
        verb, kind, target = operation["op"], library.kind(operation["op"]), library.object_id(operation)
        current = snapshot.records(kind).get(target)
        proposed = operation["record"] if verb in library.PUTS else None
        if verb in library.REMOVES:
            if current is None:
                raise DomainError("validation_error", f"{verb} requires a current {kind}: {target}")
            touch(kind, target, "remove", "target")
        else:
            if current is None:
                if target in snapshot.used_ids:
                    raise DomainError("conflict", f"Object ID {target} is already reserved or has been published before")
                created.add((kind, target))
            touch(kind, target, "create" if current is None else "update", "target")
        versions = [record for record in (current, proposed) if record is not None]
        if kind == "edge":
            for record in versions:
                for endpoint in (record["from_id"], record["to_id"]):
                    touch("node", endpoint, None, "endpoint")
        if kind == "note":
            if any(not record["objects"] for record in versions):
                unattached.add(target)
            for record in versions:
                for link in record["objects"]:
                    touch(link["kind"], link["id"], None, "subject")
    first = first_creations(snapshot, {key for key in subjects if key not in created})
    result = []
    for key in sorted(subjects):
        entry = subjects[key]
        ownership = ({"state": "new", "event_id": None, "scope": scope_of(admission)} if key in created
                     else ownership_of(first.get(key), registered))
        item = {"kind": entry["kind"], "id": entry["id"], "change": entry["change"], "roles": sorted(entry["roles"]),
                "ownership": ownership}
        if entry["kind"] == "note":
            item["unattached"] = entry["id"] in unattached
        result.append(item)
    return result


def needed_namespaces(targets: list[dict], admission: str | None) -> set:
    """Every business namespace (``None`` is Global) whose authority the edit needs: the shared ownership rules, plus Global for a
    note with no linked subject."""
    needed = namespaces(targets, admission)
    if any(item.get("unattached") for item in targets):
        needed.add(None)
    return needed


class SupervisedEdits:
    """Prepare, publish and apply the supervised variant of ``context_edit`` for one ``GraphEdits`` composition."""

    def __init__(self, edits: GraphEdits):
        if not isinstance(edits, GraphEdits):
            raise TypeError("Supervised edits need the composed GraphEdits")
        self.edits = edits
        self.outcomes: dict[str, dict] = {}
        self.guard = threading.Lock()

    @property
    def pipeline(self):
        pipeline = self.edits.ports.supervised_pipeline
        if pipeline is None:
            raise DomainError("operation_unavailable", "Supervised Context edits are not composed on this installation")
        return pipeline

    def head(self):
        try:
            return self.edits.ports.context.snapshot()
        except (ContextError, OSError, KeyError, ValueError) as error:
            raise DomainError("operation_unavailable", "The published Context cannot be read now", retryable=True) from error

    @staticmethod
    def actor(ctx) -> None:
        if ctx.principal_kind not in ("agent", "human"):
            raise DomainError("forbidden", "Only an owner or an authorized context role may edit the context base")

    def preview(self, request: Request, attribution: dict, admission: str | None) -> dict:
        """The Context's pure preview of this exact proposal: it stages, locks and writes nothing."""
        try:
            return self.pipeline.preview_supervised(request.plan, content=request.document, submitted_by=attribution["principal_id"],
                                                    event_id=request.event_id, record_context=record_context_of(attribution, admission))
        except (ContextError, OSError) as error:
            raise refused(error) from error

    def evaluate(self, uow, caller, request: Request, status: str, admission: str | None, arguments) -> list[dict] | None:
        """Require the retained authority for the proposal as it stands now; the frozen targets, or ``None`` for an event that
        is already published and is only being reconciled."""
        authority = self.edits.authority
        if status == "published":
            authority.check(uow, caller, {admission}, arguments)
            return None
        snapshot = self.head()
        if snapshot.revision != request.plan["expected_revision"]:
            raise DomainError("conflict", "The published Context is no longer at this edit's expected_revision; read it again and "
                              "submit a newly reviewed plan", details={"expected_revision": request.plan["expected_revision"],
                                                                       "published_revision": snapshot.revision})
        targets = analyze(request.plan, snapshot, admission, lambda business: uow.repository("businesses").get(business) is not None)
        authority.check(uow, caller, needed_namespaces(targets, admission), arguments)
        return targets

    def prepare(self, ctx, arguments, options, uow, clock):
        self.actor(ctx)
        require_variant(arguments)
        edits = self.edits
        request = build_request(arguments, edits.ports.source_bytes)
        admission = ctx.business_id
        caller = edits.application_context(ctx, uow)
        attribution = edits.attribution(ctx, uow, admission)
        preview = self.preview(request, attribution, admission)
        targets = self.evaluate(uow, caller, request, preview["status"], admission, arguments)
        if targets is not None and request.event_id in self.reserved(uow):
            raise DomainError("conflict", "This event_id is reserved by a queued Context edit; choose another five-character ID")
        value = plain({"kind": RESOURCE, "event_id": request.event_id, "preview_only": request.preview_only,
                       "expected_revision": request.plan["expected_revision"], "plan_digest": request.marker["plan_digest"],
                       "document_digest": request.marker["document_digest"], "document_bytes": len(request.document),
                       "source_bytes": edits.ports.source_bytes, "business_id": admission, "attribution": attribution,
                       "targets": targets})
        builder = PlanBuilder(ctx, "context_edit", arguments, "Supervised Context Edit", clock=clock, namespace=namespace_name(admission),
                              affected_scope=scope_of(admission))
        builder.resources.append(PreviewResource(RESOURCE, canonical_hash(value), f"Supervised Context edit {request.event_id}"))
        builder.effects.append(
            "Previews this exact supervised edit; no Context file, event or question is changed." if request.preview_only else
            "Publishes exactly this reviewed plan and document directly, with no model, as one new immutable Context event if your "
            "current authority and the published base still hold. An identical already published event is only reported.")
        plan = builder.build()
        return replace(plan, preview={**plain(plan.preview), "context_supervised": value})

    @staticmethod
    def reserved(uow) -> set:
        return bindings.taken_event_ids(uow)

    def prepared(self, plan) -> tuple[Request, dict]:
        """The request bound into a prepared plan, accepted only when it is complete and equal to what the plan's own validated
        arguments derive. Nothing in the metadata is ever used as a plan."""
        preview = plain(plan.preview)
        value = preview.get("context_supervised")
        digests = [item["resource_id"] for item in preview.get("resources", ()) if item.get("resource_type") == RESOURCE]
        if not isinstance(value, dict) or set(value) != PREPARED or len(digests) != 1 or canonical_hash(value) != digests[0]:
            raise DomainError("invalid_plan", "A prepared supervised edit must carry its complete bound request")
        source_bytes = self.edits.ports.source_bytes
        request = build_request(plan.arguments, source_bytes)
        attribution = value["attribution"]
        if (value["kind"] != RESOURCE or value["event_id"] != request.event_id or value["preview_only"] != request.preview_only
                or value["expected_revision"] != request.plan["expected_revision"] or value["plan_digest"] != request.marker["plan_digest"]
                or value["document_digest"] != request.marker["document_digest"] or value["document_bytes"] != len(request.document)
                or value["source_bytes"] != source_bytes or not isinstance(attribution, dict)
                or attribution.get("principal_id") != plan.principal_id or attribution.get("operation") != "context_edit"
                or attribution.get("business_id") != value["business_id"] or plan.target_namespace != namespace_name(value["business_id"])
                or (value["targets"] is not None and not isinstance(value["targets"], list))):
            raise DomainError("invalid_plan", "The prepared supervised edit no longer matches its arguments")
        return request, value

    def external(self, ctx, plan) -> None:
        """Re-verify, then preview or publish; the outcome is kept for ``apply`` and nothing here holds a SQLite write lock."""
        self.actor(ctx)
        request, value = self.prepared(plan)
        attribution, admission = value["attribution"], value["business_id"]
        arguments = plain(plan.arguments)
        edits = self.edits
        with edits.authority.application.database.read() as uow:
            if edits.attribution(ctx, uow, admission) != attribution:
                raise DomainError("revision_conflict", "The caller's registration changed since this edit was prepared; prepare it again")
            caller = edits.authority.caller(uow, attribution, admission)
            preview = self.preview(request, attribution, admission)
            targets = self.evaluate(uow, caller, request, preview["status"], admission, arguments)
            if targets is not None and (targets != value["targets"] or request.event_id in self.reserved(uow)):
                raise DomainError("revision_conflict", "The records this edit changes were re-owned, re-linked or removed since "
                                  "preparation; prepare it again")
        outcome = preview if request.preview_only else self.publish(request, attribution, admission)
        with self.guard:
            while len(self.outcomes) >= STASHED_OUTCOMES:
                self.outcomes.pop(next(iter(self.outcomes)))
            self.outcomes[plan.payload_hash] = outcome

    def publish(self, request: Request, attribution: dict, admission: str | None) -> dict:
        """The one publication entry: ``Pipeline.submit_supervised``. Its receipt must be this exact deterministic edit."""
        try:
            receipt = self.pipeline.submit_supervised(request.plan, content=request.document, submitted_by=attribution["principal_id"],
                                                      event_id=request.event_id, record_context=record_context_of(attribution, admission))
        except (ContextError, OSError) as error:
            raise refused(error) from error
        return self.checked_receipt(request, {name: receipt[name] for name in RECEIPT_FIELDS if name in receipt})

    @staticmethod
    def checked_receipt(request: Request, receipt: dict) -> dict:
        if (receipt.get("status") != "published" or receipt.get("event_id") != request.event_id
                or receipt.get("execution") != request.marker or receipt.get("model") is not None):
            raise DomainError("invalid_result", "The Context receipt is not this exact deterministic supervised edit")
        return receipt

    def apply(self, ctx, plan, uow) -> dict:
        self.actor(ctx)
        request, value = self.prepared(plan)
        attribution = value["attribution"]
        if (attribution["principal_id"] != ctx.principal_id or attribution["principal_kind"] != ctx.principal_kind
                or attribution.get("delegation") != bindings.delegation_of(ctx) or ctx.business_id != value["business_id"]):
            raise DomainError("forbidden", "A prepared supervised edit belongs to the caller, scope and delegation that prepared it")
        with self.guard:
            outcome = self.outcomes.pop(plan.payload_hash, None)
        if outcome is None:
            raise DomainError("operation_unavailable", "The supervised edit was not executed; retry the same edit", retryable=True)
        if request.preview_only:
            if (outcome.get("event_id") != request.event_id or outcome.get("plan_digest") != request.marker["plan_digest"]
                    or outcome.get("document_digest") != request.marker["document_digest"]):
                raise DomainError("invalid_result", "The Context preview is not this exact supervised edit")
            return outcome
        return self.checked_receipt(request, outcome)
