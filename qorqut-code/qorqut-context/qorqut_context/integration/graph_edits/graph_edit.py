"""Host side of the explicit Context graph edit (FEATURE_COMPLETION D2): schema, target ownership, authority, admission.

``context_edit`` queues one ``qorqut-graph-edit-v1`` plan through the ordinary shared Context queue; the Context library
executes it later, deterministically and with no model (``driver.py`` dispatches it). This module is everything the host
decides before that: what the edit touches, whose it is, whether the original caller may change it, and the durable
admission of the Operations ``context_graph_edits`` binding with its queue input.

* Preparation is read-only with respect to Context. It reads the published snapshot, requires the plan's
  ``expected_revision`` to be the current head, normalizes the plan with the library (closed D1 schema, full records,
  caller-supplied IDs, citations, support), enforces the configured source-byte bound, validates the typed objects, the
  target and endpoint existence against the fixed base and the plan's own new objects, and freezes the exact target
  evidence. It never calls ``begin_edit``/``submit_edit``, stages a draft, runs a model or publishes.
* Ownership of an existing node or edge is the scope in its first creation event's controlled ``record_context.scope`` in
  the published history at the fixed base. Later support, attributes, links or tags never transfer it. A missing, malformed
  or unresolved original scope is protected global/unknown and supplies no business grant. Objects the plan creates belong to
  the admission namespace. Nothing reads the graph's current labels as authority.
* Authority is the retained one: the real resolver and ``Application.authorize`` for the ``context_edit`` descriptor, the
  role and Global restrictions of the context instruction surface, and for a human the actual installation owner. An agent
  needs a current allow in the admission namespace *and* in the namespace of every target and endpoint (a Global or
  unknown-owned object needs the Global namespace, which only the coordinator role holds). The owner keeps full authority.
* ``GraphEditAuthority.recheck`` is the typed port the driver calls before any fresh execution or explicit retry (and after a
  restart): it resolves the original caller's *current* registration, role and grants from the stored attribution, never the
  driver's system identity, and re-derives the frozen targets from the stored plan and the fixed snapshot. It does not change
  the plan, base or IDs, and never rebases a stale edit.
* A delegated worker may queue an edit exactly as v1 let it run one. The attribution freezes the closed delegation of the
  authenticated caller (``null``, or exactly its binding ID, assignment ID and generation; never a credential or a decision);
  ``apply`` requires it to equal the invoking context's, and ``recheck`` rebuilds the parent's current context, attaches precisely
  that triplet and authorizes through the same ``Application.authorize`` path (live binding, lease, generation, registration,
  assignment operation and business scope) in the admission and every target namespace. It never looks up a newer binding,
  clears the delegation or authorizes as the unrestricted parent: an expired, released, completed or revoked assignment blocks an
  unpublished edit and its retry, while an exact already-published edit still reconciles first.

The supervised variant of ``context_edit`` (a ``qorqut-supervised-edit-v1`` plan with its Markdown document, notes included) is
not queued and has no binding: ``supervised_edit.py`` publishes it directly through ``GraphEditPorts.supervised_pipeline`` with
this module's ownership rules and ``GraphEditAuthority``, which this module leaves unchanged.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import string
from dataclasses import dataclass, replace
from typing import Any, Callable, Protocol

from qorqut_context import Conflict, ContextError, graph_edits as library
from qorqut_context.integration import graph_edits as bindings
from qorqut_operations.application.plans import PlanBuilder, apply_records, schema
from qorqut_context.integration.queue import queue as queue_module
from qorqut_context.integration.queue.operations import EXECUTION_FACTS, INPUT_OUTPUT
from qorqut_operations.contracts import DomainError, ExpectedTarget, plain

EXECUTION_KIND = library.EXECUTION
_ALPHABET = string.ascii_lowercase + string.digits

# -- the closed D1 plan schema -------------------------------------------------------------------------------------------

_ID = {"type": "string", "pattern": "^[a-z0-9]{5}$"}
_COMMIT = {"type": "string", "pattern": "^([0-9a-f]{40}|[0-9a-f]{64})$"}
_REFERENCE = {"type": "string", "pattern": "^(self|[0-9a-f]{40}|[0-9a-f]{64})$"}
_TEXT = {"type": "string", "minLength": 1}
_ATTRIBUTES = {"type": "object", "additionalProperties": {"type": "array", "items": {"type": ["string", "number", "boolean"]}}}
_NODE = schema({"id": _ID, "summary": {"type": "string"}, "attributes": _ATTRIBUTES}, ("id", "summary", "attributes"))
_EDGE = schema({"id": _ID, "summary": {"type": "string"}, "attributes": _ATTRIBUTES, "from_id": _ID, "to_id": _ID},
               ("id", "summary", "attributes", "from_id", "to_id"))
_CITATIONS = {"type": "array", "items": _REFERENCE, "minItems": 1, "maxItems": 15}
_SUPPORT = {"type": "array", "items": _REFERENCE}


def _put(verb, record):
    return schema({"op": {"const": verb}, "record": record, "explanation": _TEXT, "citations": _CITATIONS, "support_after": _SUPPORT},
                  ("op", "record", "explanation", "citations", "support_after"))


def _remove(verb):
    return schema({"op": {"const": verb}, "id": _ID, "explanation": _TEXT, "citations": _CITATIONS},
                  ("op", "id", "explanation", "citations"))


PLAN_SCHEMA = schema({
    "format": {"const": library.FORMAT}, "expected_revision": _COMMIT, "rationale": _TEXT,
    "operations": {"type": "array", "minItems": 1,
                   "items": {"oneOf": [_put("put_node", _NODE), _put("put_edge", _EDGE), _remove("remove_node"), _remove("remove_edge")]}}},
    ("format", "expected_revision", "rationale", "operations"))
INPUT_SCHEMA = schema({"edit": PLAN_SCHEMA}, ("edit",))
OUTPUT_SCHEMA = schema({"input": INPUT_OUTPUT, "execution": EXECUTION_FACTS}, ("input", "execution"))


class GraphEditRefused(Exception):
    """The edit may not run now (stale base, revoked or insufficient authority, changed evidence). Never a Context failure."""

    def __init__(self, code: str, message: str, phase: str = "authority"):
        super().__init__(message)
        self.code, self.phase = code, phase


class SnapshotSource(Protocol):
    """Read access to the published Context: the head snapshot and the pending job (if a draft is in flight)."""

    def snapshot(self): ...

    def pending(self): ...


@dataclass(frozen=True)
class GraphEditPorts:
    """What the host supplies for graph edits: the published Context and the configured source-byte bound.

    ``supervised_pipeline`` is the Context ``Pipeline`` the supervised variant of ``context_edit`` publishes through; its
    ``preview_supervised`` and ``submit_supervised`` are the only entries used (``supervised_edit.py``). Without it the queued
    graph edit is unchanged and the supervised variant is unavailable."""

    context: SnapshotSource
    source_bytes: int = 256 * 1024
    supervised_pipeline: Any = None

    def __post_init__(self):
        if type(self.source_bytes) is not int or self.source_bytes < 1:
            raise ValueError("The graph-edit source bound is a positive number of bytes")
        if self.supervised_pipeline is not None and not all(
                callable(getattr(self.supervised_pipeline, name, None)) for name in ("preview_supervised", "submit_supervised")):
            raise ValueError("The supervised pipeline must provide preview_supervised and submit_supervised")


# -- normalization -------------------------------------------------------------------------------------------------------

def _wrap(error: Exception) -> DomainError:
    if isinstance(error, Conflict):
        return DomainError("conflict", str(error))
    return DomainError("validation_error", str(error))


def normalized(plan: Any, source_bytes: int) -> tuple[dict, str, str]:
    """The library-normalized plan, its canonical source text and the SHA-256 digest of that text's UTF-8 bytes (which is the
    library's own plan digest). Bounded by the configured source bytes."""
    try:
        plan = library.normalize(plan)
        content = library.canonical(plan)
    except ContextError as error:
        raise _wrap(error) from None
    if len(content) > source_bytes:
        raise DomainError("validation_error", f"The graph edit exceeds the configured source limit of {source_bytes} bytes")
    return plan, content.decode("utf-8"), hashlib.sha256(content).hexdigest()


# -- targets and ownership -----------------------------------------------------------------------------------------------

def scope_of(business_id: str | None) -> dict:
    return {"kind": "global"} if business_id is None else {"kind": "business", "business_id": business_id}


def _first_creations(snapshot, wanted: set) -> dict:
    """The first published event that changed each wanted ``(kind, id)``, with its change operation and controlled scope."""
    found = {}
    for event_id, event in snapshot.events.items():
        for change in event["changes"]:
            key = (change["kind"], change["id"])
            if key in wanted and key not in found:
                context = event.get("record_context")
                found[key] = {"event_id": event_id, "operation": change["operation"],
                              "scope": context.get("scope") if isinstance(context, dict) else None}
        if len(found) == len(wanted):
            break
    return found


def _ownership(first: dict | None, registered: Callable[[str], bool]) -> dict:
    """The recorded owner of an existing object, or an explicit unknown. Only a well-formed scope of a registered business or
    the explicit Global scope is recognised; anything else is unknown and protected."""
    if first is None or first["operation"] != "add":
        return {"state": "unknown", "event_id": None if first is None else first["event_id"], "scope": None}
    scope = first["scope"]
    if scope == {"kind": "global"}:
        return {"state": "recorded", "event_id": first["event_id"], "scope": scope}
    if (isinstance(scope, dict) and set(scope) == {"kind", "business_id"} and scope["kind"] == "business"
            and isinstance(scope["business_id"], str) and registered(scope["business_id"])):
        return {"state": "recorded", "event_id": first["event_id"], "scope": scope}
    return {"state": "unknown", "event_id": first["event_id"], "scope": None}


def owner_business(ownership: dict, admission: str | None) -> str | None:
    """The namespace whose authority an object's change needs: a new object's admission namespace, a recorded business, and
    Global for a recorded Global or an unknown owner (which supplies no business grant)."""
    if ownership["state"] == "new":
        return admission
    if ownership["state"] == "recorded" and ownership["scope"]["kind"] == "business":
        return ownership["scope"]["business_id"]
    return None


def analyze(plan: dict, snapshot, admission: str | None, registered: Callable[[str], bool]) -> list[dict]:
    """Validate a normalized plan against its fixed base and return its frozen target evidence.

    Checks existence (a removal needs a current object, a new ID is unused), that every edge of the resulting graph has both
    endpoints (no hidden cascade or merge: a node removal must account for its incident edges in the plan), and freezes every
    updated, removed and created object plus every old and proposed endpoint of an affected edge with its ownership."""
    subjects: dict[tuple, dict] = {}
    created: set = set()
    final = {"node": dict(snapshot.nodes), "edge": dict(snapshot.edges)}

    def note(kind, target, change, role):
        entry = subjects.setdefault((kind, target), {"kind": kind, "id": target, "change": "reference", "roles": set()})
        entry["roles"].add(role)
        if change:
            entry["change"] = change

    for operation in plan["operations"]:
        verb, kind, target = operation["op"], library.kind(operation["op"]), library.object_id(operation)
        current = snapshot.objects(kind).get(target)
        if verb in library.REMOVES:
            if current is None:
                raise DomainError("validation_error", f"{verb} requires a current {kind}: {target}")
            final[kind].pop(target)
            note(kind, target, "remove", "target")
        else:
            if current is None:
                if target in snapshot.used_ids:
                    raise DomainError("conflict", f"Object ID {target} is already reserved or has been published before")
                created.add((kind, target))
            final[kind][target] = operation["record"]
            note(kind, target, "create" if current is None else "update", "target")
        if kind == "edge":
            for source in ((current,) if current is not None else ()) + ((operation["record"],) if verb in library.PUTS else ()):
                for endpoint in (source["from_id"], source["to_id"]):
                    note("node", endpoint, None, "endpoint")
    for edge in final["edge"].values():
        for endpoint in (edge["from_id"], edge["to_id"]):
            if endpoint not in final["node"]:
                raise DomainError("validation_error", f"Edge {edge['id']} would point at node {endpoint}, which is not in the "
                                  "resulting graph; account for every incident edge explicitly")
    first = _first_creations(snapshot, {key for key in subjects if key not in created})
    result = []
    for key in sorted(subjects):
        entry = subjects[key]
        if key in created:
            ownership = {"state": "new", "event_id": None, "scope": scope_of(admission)}
        else:
            ownership = _ownership(first.get(key), registered)
        result.append({"kind": entry["kind"], "id": entry["id"], "change": entry["change"], "roles": sorted(entry["roles"]),
                       "ownership": ownership})
    return result


def namespaces(targets: list[dict], admission: str | None) -> set:
    """Every business namespace (None is Global) whose authority the edit needs, the admission namespace included."""
    return {admission, *(owner_business(item["ownership"], admission) for item in targets)}


def namespace_name(business_id: str | None) -> str:
    return "global" if business_id is None else "business:" + business_id


ID_SCHEMA, TEXT_SCHEMA, COMMIT_SCHEMA, NODE_SCHEMA, EDGE_SCHEMA = _ID, _TEXT, _COMMIT, _NODE, _EDGE
put_operation, remove_operation = _put, _remove
first_creations, ownership_of = _first_creations, _ownership


# -- authority -----------------------------------------------------------------------------------------------------------

class GraphEditAuthority:
    """The retained authority for ``context_edit``: nothing here grants anything the resolver does not.

    ``roles`` is the set of roles that may write the Context through the focused instruction surface. A caller's context is
    always built from its *current* registration (``refresh_context`` for an invocation, ``context_for`` for a stored
    attribution), so a revoked grant, an inactive agent or an owner change is seen the next time anything is checked."""

    def __init__(self, application, *, roles, name: str = "context_edit", coordinator=None):
        self.application, self.roles, self.name = application, frozenset(roles), name
        self.coordinator = coordinator

    def check_personal(self, uow, ctx, snapshot, plan):
        """Profile maintenance belongs to the configured coordinator, not every agent with a Context role."""
        from qorqut_context.personal import edits_profile
        try:
            affected = edits_profile(snapshot, plan)
        except ContextError as error:
            raise DomainError("validation_error", str(error)) from error
        if affected and (ctx.principal_kind != "agent" or self.coordinator is None
                         or ctx.principal_id != self.coordinator(uow)):
            raise DomainError("forbidden", "Only the configured Ordubashy may maintain personal context")

    @property
    def descriptor(self):
        return self.application.registry.get(self.name)

    @staticmethod
    def with_delegation(ctx, delegation: dict | None):
        """``ctx`` carrying exactly the frozen delegation: a direct caller's context carries none, and a delegated one carries the
        original triplet. A refreshed context can never replace the binding, assignment or generation with another."""
        refreshed = (ctx.delegation_binding_id, ctx.delegation_assignment_id, ctx.delegation_generation)
        if delegation is None:
            if any(value is not None for value in refreshed):
                raise DomainError("invalid_principal", "A direct caller's context cannot carry a delegation")
            return ctx
        frozen = (delegation["binding_id"], delegation["assignment_id"], delegation["generation"])
        if any(value is not None for value in refreshed) and refreshed != frozen:
            raise DomainError("invalid_principal", "Context refresh cannot replace a delegated worker binding")
        return replace(ctx, delegation_binding_id=frozen[0], delegation_assignment_id=frozen[1], delegation_generation=frozen[2])

    def caller(self, uow, attribution: dict, business_id: str | None):
        """The original caller's context in one namespace from its current registration (never the driver's identity), carrying
        the frozen delegation of the attribution when it was delegated."""
        ctx = self.application.policy.context_for(attribution["principal_id"], business_id=business_id, review_business_id=None, uow=uow)
        if ctx.principal_kind != attribution["principal_kind"]:
            raise DomainError("invalid_principal", "The principal's kind changed since the edit was queued")
        return self.with_delegation(ctx, attribution.get("delegation"))

    def _owner(self, ctx, uow) -> bool:
        owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
        return bool(owner and owner["owner_principal_id"] == ctx.principal_id)

    def _role(self, ctx, uow) -> None:
        """The preserved restrictions of the Context write surface: a supported global role (in a business or the Global
        namespace), and for a human the actual installation owner."""
        if ctx.principal_kind == "agent":
            role_id = getattr(ctx.role_artifact_ref, "id", None)
            if role_id not in self.roles:
                raise DomainError("forbidden", "This role cannot edit the context base")
        elif ctx.principal_kind != "human":
            raise DomainError("forbidden", "Only an owner or an authorized context role may edit the context base")
        elif not self._owner(ctx, uow):
            raise DomainError("forbidden", "Only the installation owner may edit the context base")

    def _namespace(self, ctx, uow, arguments=None) -> None:
        self._role(ctx, uow)
        self.application.authorize(ctx, self.descriptor, (), uow, namespace=namespace_name(ctx.business_id), arguments=arguments)

    def check(self, uow, ctx, needed: set, arguments=None) -> None:
        """``ctx`` is the caller in the admission namespace. It must hold the retained authority there, and an agent also in
        every other namespace in ``needed``; the installation owner keeps full authority. A delegated ``ctx`` is authorized,
        in every namespace, through the retained delegated checks with the same binding, assignment and generation."""
        self._namespace(ctx, uow, arguments)
        if ctx.principal_kind == "human":
            return
        delegation = bindings.delegation_of(ctx)
        for business_id in sorted(needed - {ctx.business_id}, key=lambda item: item or ""):
            other = self.application.policy.refresh_context(replace(ctx, business_id=business_id), uow)
            if other.business_id != business_id:
                # A business agent re-selected to its home business is not in the Global namespace; a registration that
                # resolves elsewhere has no authority in the namespace the object belongs to.
                raise DomainError("forbidden", "The agent's registration has no authority in the namespace of an object this edit changes")
            self._namespace(self.with_delegation(other, delegation), uow, arguments)

    def recheck(self, uow, binding: dict, snapshot) -> None:
        """The driver's gate before any fresh execution or explicit retry. Raises ``GraphEditRefused`` and changes nothing."""
        if snapshot.revision != binding["expected_revision"]:
            raise GraphEditRefused("stale_base", f"The published Context moved from the edit's base {binding['expected_revision']} to "
                                   f"{snapshot.revision}; an edit is never rebased. Discard it and submit a newly reviewed plan")
        try:
            plan = library.normalize(binding["plan"])
            fresh = analyze(plan, snapshot, binding["business_id"], lambda business: uow.repository("businesses").get(business) is not None)
        except (ContextError, DomainError) as error:
            raise GraphEditRefused("edit_invalid", f"The edit no longer matches its fixed base: {getattr(error, 'message', error)}",
                                   "integrity") from None
        if fresh != binding["targets"]:
            raise GraphEditRefused("targets_changed", "The edit's target ownership no longer matches the evidence frozen at "
                                   "preparation; discard it and submit a newly reviewed plan", "integrity")
        try:
            caller = self.caller(uow, binding["attribution"], binding["business_id"])
            self.check_personal(uow, caller, snapshot, plan)
            self.check(uow, caller, namespaces(fresh, binding["business_id"]), {"edit": binding["plan"]})
        except DomainError as error:
            raise GraphEditRefused("authority_revoked", "The original caller no longer holds the authority this edit needs: "
                                   f"{error.message}") from None


# -- admission -----------------------------------------------------------------------------------------------------------

class GraphEdits:
    """Prepare and apply ``context_edit``: the admission of one binding and its queue input."""

    def __init__(self, application, ports: GraphEditPorts, *, roles, id_source: Callable[[], str] | None = None,
                 coordinator=None):
        if not isinstance(ports, GraphEditPorts):
            raise TypeError("Graph edits need the typed GraphEditPorts")
        self.ports, self.authority = ports, GraphEditAuthority(application, roles=roles, coordinator=coordinator)
        self._new_id = id_source or (lambda: "".join(secrets.choice(_ALPHABET) for _ in range(5)))

    # -- reads of the published Context -------------------------------------------------------------------------------

    def _head(self):
        try:
            return self.ports.context.snapshot()
        except (ContextError, OSError, KeyError, ValueError) as error:
            raise DomainError("operation_unavailable", "The published Context cannot be read now", retryable=True) from error

    def _pending_ids(self) -> set:
        try:
            job = self.ports.context.pending()
        except Conflict as error:
            raise DomainError("busy", "The Context writer is busy; retry the edit", retryable=True) from error
        except (ContextError, OSError) as error:
            raise DomainError("operation_unavailable", "The pending Context state cannot be read now", retryable=True) from error
        return set() if job is None else {job["id"], *job.get("reserved_ids", ())}

    @staticmethod
    def _registered(uow) -> Callable[[str], bool]:
        return lambda business: uow.repository("businesses").get(business) is not None

    def _evidence(self, uow, plan: dict, admission: str | None) -> tuple[Any, list[dict]]:
        snapshot = self._head()
        if snapshot.revision != plan["expected_revision"]:
            raise DomainError("conflict", "The published Context is no longer at this edit's expected_revision; read it again and "
                              "submit a newly reviewed plan", details={"expected_revision": plan["expected_revision"],
                                                                       "published_revision": snapshot.revision})
        return snapshot, analyze(plan, snapshot, admission, self._registered(uow))

    def _reserve(self, uow, snapshot, plan: dict) -> str:
        taken = bindings.taken_event_ids(uow) | set(snapshot.used_ids) | self._pending_ids() | library.object_ids(plan)
        for _ in range(1024):
            candidate = self._new_id()
            if isinstance(candidate, str) and len(candidate) == 5 and not set(candidate) - set(_ALPHABET) and candidate not in taken:
                return candidate
        raise DomainError("conflict", "Could not reserve an unused Context event ID; try again", retryable=True)

    # -- the prepared operation ---------------------------------------------------------------------------------------

    @staticmethod
    def _actor(ctx) -> None:
        """Only a registered agent (direct or delegated) or the owner may queue an edit."""
        if ctx.principal_kind not in ("agent", "human"):
            raise DomainError("forbidden", "Only an owner or an authorized context role may edit the context base")

    def prepare(self, ctx, arguments: dict, options, uow, clock):
        self._actor(ctx)
        plan, content, digest = normalized(arguments["edit"], self.ports.source_bytes)
        admission = ctx.business_id
        caller = self.application_context(ctx, uow)
        snapshot, targets = self._evidence(uow, plan, admission)
        self.authority.check_personal(uow, caller, snapshot, plan)
        self.authority.check(uow, caller, namespaces(targets, admission), arguments)
        namespace = namespace_name(admission)
        builder = PlanBuilder(ctx, "context_edit", arguments, "Context Graph Edit", clock=clock, namespace=namespace,
                              affected_scope=scope_of(admission))
        bindings.admit(builder, uow, plan=plan, content=content, plan_digest=digest, reserved_event_id=self._reserve(uow, snapshot, plan),
                       attribution=self.attribution(ctx, uow, admission),
                       targets=targets, business_id=admission, max_content_bytes=self.ports.source_bytes)
        builder.effects.append("The published Context is not changed now. The queue driver rechecks your current authority and the "
                               "published base, then publishes exactly this plan as one event, with no model, in queue order.")
        return builder.build()

    @staticmethod
    def attribution(ctx, uow, admission) -> dict:
        """The original caller, frozen with the edit: who, as what, in which scope and under which authenticated delegation (the
        registration and the closed binding/assignment/generation triplet, never the plan text, a credential or a decision)."""
        actor = uow.connection.execute("SELECT display_name FROM principals WHERE id=?", (ctx.principal_id,)).fetchone()
        return {"principal_id": ctx.principal_id, "principal_kind": ctx.principal_kind, "display_name": actor["display_name"] if actor else None,
                "agent_id": ctx.agent_id, "role_id": getattr(ctx.role_artifact_ref, "id", None), "business_id": admission,
                "operation": "context_edit", "delegation": bindings.delegation_of(ctx)}

    def application_context(self, ctx, uow):
        """The invoking caller's context refreshed from its current registration inside this transaction, still carrying the
        delegation it was authenticated with."""
        return self.authority.with_delegation(self.authority.application.policy.refresh_context(ctx, uow), bindings.delegation_of(ctx))

    @staticmethod
    def _pair(plan) -> tuple[dict, dict]:
        """The binding values and the queue-input values of a prepared edit, whose effect set must be exactly those two new record
        inserts in that order under one shared ID, each expected absent: nothing may ride along on the creation group."""
        changes, targets = tuple(plan.changes), tuple(plan.expected_targets)
        if len(changes) != 2:
            raise DomainError("invalid_plan", "A prepared graph edit is exactly its binding and its queue input")
        companion, queued = changes
        identity = companion.resource.resource_id
        if (companion.resource.resource_type != "context_graph_edits" or queued.resource.resource_type != "context_inputs"
                or queued.resource.resource_id != identity or companion.resource.business_id != queued.resource.business_id
                or any(change.kind != "record" or change.action != "insert" or change.operation_name is not None
                       or change.resource.namespace is not None for change in changes)
                or targets != tuple(ExpectedTarget(change.resource, exists=False) for change in changes)):
            raise DomainError("invalid_plan", "A prepared graph edit is exactly its binding and its queue input, both new")
        values = tuple(plain(change.values) for change in changes)
        if (values[0].get("input_id") != identity or values[1].get("id") != identity
                or values[1].get("business_id") != queued.resource.business_id):
            raise DomainError("invalid_plan", "A prepared graph edit's binding and queue input must share one new identity and scope")
        return values

    def apply(self, ctx, plan, uow) -> dict:
        self._actor(ctx)
        companion, queued = self._pair(plan)
        stored, attribution = companion["plan_json"], companion["attribution_json"]
        if (attribution["principal_id"] != ctx.principal_id or attribution["principal_kind"] != ctx.principal_kind
                or attribution.get("delegation") != bindings.delegation_of(ctx)):
            raise DomainError("forbidden", "A prepared graph edit belongs to the caller, and the delegation, that prepared it")
        fresh, content, digest = normalized(stored, self.ports.source_bytes)
        if fresh != stored or digest != companion["plan_digest"] or queued["payload_json"]["content"] != content:
            raise DomainError("revision_conflict", "The prepared graph edit no longer matches its plan")
        bindings.check_relation(stored, content, digest, companion["reserved_event_id"], companion["expected_revision"], attribution,
                                queued["submitted_by"], companion["targets_json"])
        admission = queued["business_id"]
        if admission != ctx.business_id:
            raise DomainError("forbidden", "A graph edit is queued in the caller's selected scope")
        snapshot, targets = self._evidence(uow, fresh, admission)
        if targets != companion["targets_json"]:
            raise DomainError("revision_conflict", "The objects this edit changes were re-owned or removed since preparation")
        caller = self.application_context(ctx, uow)
        self.authority.check_personal(uow, caller, snapshot, fresh)
        self.authority.check(uow, caller, namespaces(targets, admission), plan.arguments)
        event_id = companion["reserved_event_id"]
        if event_id in bindings.taken_event_ids(uow) or event_id in snapshot.used_ids or event_id in self._pending_ids():
            raise DomainError("conflict", "The reserved Context event ID is no longer available; prepare the edit again")
        apply_records(ctx, plan, uow)
        row = queue_module.lookup(uow, admission, queued["id"])
        facts = bindings.execution_facts(bindings.load(uow, row["id"]))
        return {"input": queue_module.input_view(row, execution=facts), "execution": facts}
