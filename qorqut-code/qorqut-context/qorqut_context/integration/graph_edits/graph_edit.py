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
  or unresolved original scope is protected global/unknown and supplies no project grant. Objects the plan creates belong to
  the admission namespace. Nothing reads the graph's current labels as authority.
* Authority is the retained one: the real resolver and ``Application.authorize`` for the ``context_edit`` descriptor, the
  role and Global restrictions of the context instruction surface, and for a human the actual installation owner. An agent
  needs a current allow in the admission namespace *and* in the namespace of every target and endpoint (a Global or
  unknown-owned object needs the Global namespace, which only the coordinator role holds). The owner keeps full authority.
* ``GraphEditAuthority.recheck`` is the typed port the driver calls before any fresh execution or explicit retry (and after a
  restart): it resolves the original caller's *current* registration, role and grants from the stored attribution, never the
  driver's system identity, and re-derives the frozen targets from the stored plan and the head snapshot. When other inputs
  published after the edit's base, the edit runs on the head only if nothing it depends on changed in between
  (``rebase_conflict``: its targets, the IDs it creates, its edges' endpoints, the incident edges of nodes it removes, the
  sources it cites or keeps as support, the configuration); otherwise it is refused as ``stale_base``. A ``personal_set``
  (told by the operation its attribution names) is the one exception: a change to the owner's record in between is merged
  field by field (``merged_put``), and only a change to the same field or support entry refuses it. The plan run on the head
  is ``plan_on(stored plan, attribution, head)``: the stored plan with ``expected_revision`` set to the head, and for a merged
  ``personal_set`` the merged record and support; IDs, explanations, citations and the rationale are never changed, and the
  stored binding, plan and digest are never rewritten.
* A delegated worker may queue an edit exactly as v1 let it run one. The attribution freezes the closed delegation of the
  authenticated caller (``null``, or exactly its binding ID, assignment ID and generation; never a credential or a decision);
  ``apply`` requires it to equal the invoking context's, and ``recheck`` rebuilds the parent's current context, attaches precisely
  that triplet and authorizes through the same ``Application.authorize`` path (live binding, lease, generation, registration,
  assignment operation and project scope) in the admission and every target namespace. It never looks up a newer binding,
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
from qorqut_context.git_store import CONFIG_PATH
from qorqut_context.schema import encode, record_path
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
    """The edit may not run now (a record it depends on changed since its base, revoked or insufficient authority, changed
    evidence). Never a Context failure."""

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

def scope_of(project_id: str | None) -> dict:
    return {"kind": "global"} if project_id is None else {"kind": "project", "project_id": project_id}


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
    """The recorded owner of an existing object, or an explicit unknown. Only a well-formed scope of a registered project or
    the explicit Global scope is recognised; anything else is unknown and protected."""
    if first is None or first["operation"] != "add":
        return {"state": "unknown", "event_id": None if first is None else first["event_id"], "scope": None}
    scope = first["scope"]
    if scope == {"kind": "global"}:
        return {"state": "recorded", "event_id": first["event_id"], "scope": scope}
    if (isinstance(scope, dict) and set(scope) == {"kind", "project_id"} and scope["kind"] == "project"
            and isinstance(scope["project_id"], str) and registered(scope["project_id"])):
        return {"state": "recorded", "event_id": first["event_id"], "scope": scope}
    return {"state": "unknown", "event_id": first["event_id"], "scope": None}


def owner_project(ownership: dict, admission: str | None) -> str | None:
    """The namespace whose authority an object's change needs: a new object's admission namespace, a recorded project, and
    Global for a recorded Global or an unknown owner (which supplies no project grant)."""
    if ownership["state"] == "new":
        return admission
    if ownership["state"] == "recorded" and ownership["scope"]["kind"] == "project":
        return ownership["scope"]["project_id"]
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


# -- applying a queued edit on a newer head --------------------------------------------------------------------------------

CONFLICTS = {
    "unpublished_base": "the edit's base is not in the published Context history",
    "configuration": "the Context configuration changed",
    "record_changed": "a node or edge this edit changes or removes was changed",
    "id_taken": "an ID this edit creates has been used",
    "endpoint_removed": "a node an edge of this edit points at was removed",
    "incident_edge": "an edge now points at a node this edit removes",
    "source_removed": "a source this edit cites or keeps as support was removed",
    "field_changed": "a field or the support of the owner's record that this edit changes was also changed",
}
PERSONAL_OPERATION = "personal_set"


class MergeConflict(Exception):
    """A personal_set whose change overlaps a change made to the owner's record after its base."""

    def __init__(self, code: str):
        super().__init__(CONFLICTS[code])
        self.code = code


def rebase_conflict(plan: dict, targets: list[dict], snapshot, *, merge: tuple | None = None) -> str | None:
    """Why ``plan`` (validated at its ``expected_revision`` with the frozen ``targets``) cannot be applied on the head
    ``snapshot``, as a key of ``CONFLICTS``, or ``None`` when nothing it depends on changed in between.

    What the plan depends on: every node and edge it puts or removes (its whole record and support), the absence of every
    ID it creates, the presence of every node its edges point at, the incident edges of every node it removes, the sources
    its citations and support name, and the configuration (which names the personal record). What changed is read from
    the events published after the base: each records every node, edge and note it changed, so a record with no change in
    them is exactly as it was at the base.

    ``merge`` is the key of a record whose change in between is not a conflict by itself: the owner's record of a
    ``personal_set``, which ``plan_on`` merges field by field instead."""
    base, head = plan["expected_revision"], snapshot.revision
    if base == head:
        return None
    git = snapshot.git
    try:
        git.run("merge-base", "--is-ancestor", base, head)
    except ContextError:
        return "unpublished_base"
    earlier = git.event_commits(base)
    later = [snapshot.events[event_id] for event_id in snapshot.event_commits if event_id not in earlier]
    if git.read(base, [CONFIG_PATH])[CONFIG_PATH] != snapshot.files.get(CONFIG_PATH):
        return "configuration"
    changed = {(change["kind"], change["id"]) for event in later for change in event["changes"]}
    created = {(item["kind"], item["id"]) for item in targets if item["change"] == "create"}
    operated = {(library.kind(operation["op"]), library.object_id(operation)) for operation in plan["operations"]}
    for key in sorted(operated):
        if key in created:
            if key[1] in snapshot.used_ids or key in changed:
                return "id_taken"
        elif key in changed and key != merge:
            return "record_changed"
    for item in targets:
        key = (item["kind"], item["id"])
        if "endpoint" in item["roles"] and key not in operated and item["id"] not in snapshot.nodes:
            return "endpoint_removed"
    removed = {operation["id"] for operation in plan["operations"] if operation["op"] == "remove_node"}
    edges = {object_id for kind, object_id in operated if kind == "edge"}
    if any(edge["id"] not in edges and {edge["from_id"], edge["to_id"]} & removed for edge in snapshot.edges.values()):
        return "incident_edge"
    withdrawn = {event["source"]["id"] for event in later if event["operation"] == "remove"}
    for operation in plan["operations"]:
        for ref in (*operation["citations"], *operation.get("support_after", ())):
            event = snapshot.by_commit.get(ref) if ref != "self" else None
            if event is not None and event["source"]["id"] in withdrawn:
                return "source_removed"
    return None


def mergeable(plan: dict, attribution: dict) -> dict | None:
    """The one ``put_node`` of a ``personal_set`` plan, which is merged field by field; ``None`` for any other edit.

    A ``personal_set`` is told from a ``context_edit`` by the operation its stored attribution names (``GraphEdits.prepare``
    freezes it with the plan); the plan of a ``personal_set`` is always one ``put_node`` of the owner's record."""
    operations = plan["operations"]
    if (isinstance(attribution, dict) and attribution.get("operation") == PERSONAL_OPERATION and len(operations) == 1
            and operations[0]["op"] == "put_node"):
        return operations[0]
    return None


def _between(snapshot, base: str, revision: str) -> list[tuple[dict, str]]:
    """The events (with their commits, in publication order) published after ``base`` up to and including ``revision``.
    ``snapshot`` is any published snapshot whose history contains ``revision``."""
    earlier = snapshot.git.event_commits(base)
    return [(snapshot.events[event_id], commit) for event_id, commit in snapshot.git.event_commits(revision).items()
            if event_id not in earlier]


def _state(snapshot, revision: str, key: tuple) -> tuple[dict | None, list]:
    """The record and resolved support of ``key`` as published at ``revision``, read from that revision's own files and
    the last event up to it that changed the record."""
    content = snapshot.git.read(revision, [record_path(*key)])[record_path(*key)]
    support = []
    for event_id, commit in snapshot.git.event_commits(revision).items():
        for change in snapshot.events[event_id]["changes"]:
            if (change["kind"], change["id"]) == key:
                support = snapshot.resolve_refs(change["support_after"], commit)
    return (None if content is None else json.loads(content)), support


def merged_put(operation: dict, base: tuple, head: tuple) -> dict:
    """``operation`` (a put planned on the ``base`` record and support) applied to the ``head`` record and support.

    Every attribute and the summary the operation changes relative to the base must be unchanged between base and head;
    the result is the head record with those changes. Support is merged the same way: the head's support, plus what the
    operation adds relative to the base and minus what it removes, provided the head did not add or remove those same
    entries. Anything else raises ``MergeConflict``."""
    (before, before_support), (after, after_support) = base, head
    if before is None or after is None:
        raise MergeConflict("record_changed")
    planned, same = operation["record"], lambda left, right: encode(left) == encode(right)
    record = {"id": after["id"], "summary": after["summary"], "attributes": dict(after["attributes"])}
    if not same(planned["summary"], before["summary"]):
        if not same(after["summary"], before["summary"]):
            raise MergeConflict("field_changed")
        record["summary"] = planned["summary"]
    for name in sorted(set(before["attributes"]) | set(planned["attributes"])):
        mine, theirs, was = (planned["attributes"].get(name), after["attributes"].get(name), before["attributes"].get(name))
        if same(mine, was):
            continue
        if not same(theirs, was):
            raise MergeConflict("field_changed")
        if mine is None:
            record["attributes"].pop(name, None)
        else:
            record["attributes"][name] = mine
    wanted = operation["support_after"]
    added = [ref for ref in wanted if ref not in before_support]
    dropped = [ref for ref in before_support if ref not in wanted]
    touched = set(after_support) ^ set(before_support)
    if touched & {*added, *dropped}:
        raise MergeConflict("field_changed")
    support = [ref for ref in after_support if ref not in dropped] + [ref for ref in added if ref not in after_support]
    return {**operation, "record": record, "support_after": support}


def plan_on(plan: dict, attribution: dict, revision: str, snapshot) -> dict:
    """The plan a queued edit runs on ``revision`` (its base or a later published revision): a deterministic function of
    the stored ``plan``, its base and ``revision``, so the driver's gate, a recovery and the check of a published event all
    build the same plan. ``snapshot`` is any published snapshot whose history contains ``revision``.

    For a ``context_edit``, and for a ``personal_set`` whose record no event in between changed, it is the stored plan with
    only ``expected_revision`` set to ``revision``. A ``personal_set`` whose record was changed in between has its one
    ``put_node`` merged onto the record at ``revision`` (``merged_put``); its explanation, citations and rationale are kept.
    Raises ``MergeConflict`` when that merge is not possible."""
    if revision == plan["expected_revision"]:
        return plan
    operations, operation = plan["operations"], mergeable(plan, attribution)
    if operation is not None:
        key = ("node", operation["record"]["id"])
        if any((change["kind"], change["id"]) == key for event, _ in _between(snapshot, plan["expected_revision"], revision)
               for change in event["changes"]):
            operations = [merged_put(operation, _state(snapshot, plan["expected_revision"], key), _state(snapshot, revision, key))]
    return library.normalize({**plan, "expected_revision": revision, "operations": operations})


def on_head(plan: dict, snapshot, attribution: dict | None = None) -> dict:
    """The plan a queued edit runs on the head ``snapshot`` (see ``plan_on``)."""
    return plan_on(plan, attribution or {}, snapshot.revision, snapshot)


def namespaces(targets: list[dict], admission: str | None) -> set:
    """Every project namespace (None is Global) whose authority the edit needs, the admission namespace included."""
    return {admission, *(owner_project(item["ownership"], admission) for item in targets)}


def namespace_name(project_id: str | None) -> str:
    return "global" if project_id is None else "project:" + project_id


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

    def caller(self, uow, attribution: dict, project_id: str | None):
        """The original caller's context in one namespace from its current registration (never the driver's identity), carrying
        the frozen delegation of the attribution when it was delegated."""
        ctx = self.application.policy.context_for(attribution["principal_id"], project_id=project_id, review_project_id=None, uow=uow)
        if ctx.principal_kind != attribution["principal_kind"]:
            raise DomainError("invalid_principal", "The principal's kind changed since the edit was queued")
        return self.with_delegation(ctx, attribution.get("delegation"))

    def _owner(self, ctx, uow) -> bool:
        owner = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
        return bool(owner and owner["owner_principal_id"] == ctx.principal_id)

    def _role(self, ctx, uow) -> None:
        """The preserved restrictions of the Context write surface: a supported global role (in a project or the Global
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
        self.application.authorize(ctx, self.descriptor, (), uow, namespace=namespace_name(ctx.project_id), arguments=arguments)

    def check(self, uow, ctx, needed: set, arguments=None) -> None:
        """``ctx`` is the caller in the admission namespace. It must hold the retained authority there, and an agent also in
        every other namespace in ``needed``; the installation owner keeps full authority. A delegated ``ctx`` is authorized,
        in every namespace, through the retained delegated checks with the same binding, assignment and generation."""
        self._namespace(ctx, uow, arguments)
        if ctx.principal_kind == "human":
            return
        delegation = bindings.delegation_of(ctx)
        for project_id in sorted(needed - {ctx.project_id}, key=lambda item: item or ""):
            other = self.application.policy.refresh_context(replace(ctx, project_id=project_id), uow)
            if other.project_id != project_id:
                # An agent re-selected to a project is not in the Global namespace; a registration that
                # resolves elsewhere has no authority in the namespace the object belongs to.
                raise DomainError("forbidden", "The agent's registration has no authority in the namespace of an object this edit changes")
            self._namespace(self.with_delegation(other, delegation), uow, arguments)

    def recheck(self, uow, binding: dict, snapshot) -> dict:
        """The driver's gate before any fresh execution or explicit retry. Raises ``GraphEditRefused`` and changes nothing.

        Returns the plan the edit runs on the head ``snapshot`` (``plan_on``). When the head has moved past the edit's base,
        the edit runs there only if nothing it depends on changed in between (``rebase_conflict``), or, for a
        ``personal_set`` whose only such change is to the owner's record, if its change merges onto that record field by
        field; every check below then runs against the head, exactly as for a plan prepared on it."""
        try:
            stored = library.normalize(binding["plan"])
        except ContextError as error:
            raise GraphEditRefused("edit_invalid", f"The stored edit is not a valid plan: {error}", "integrity") from None
        personal = mergeable(stored, binding["attribution"])
        merge = (("node", personal["record"]["id"]) if personal is not None
                 and personal["record"]["id"] == snapshot.personal_context_node else None)
        conflict = rebase_conflict(stored, binding["targets"], snapshot, merge=merge)
        if conflict is None:
            try:
                plan = plan_on(stored, binding["attribution"], snapshot.revision, snapshot)
            except MergeConflict as refused:
                conflict = refused.code
        if conflict is not None:
            raise GraphEditRefused("stale_base", f"The published Context moved from the edit's base {binding['expected_revision']} to "
                                   f"{snapshot.revision} and {CONFLICTS[conflict]} since. Read the Context again and submit a "
                                   "newly reviewed plan")
        try:
            fresh = analyze(plan, snapshot, binding["project_id"], lambda project: uow.repository("projects").get(project) is not None)
        except (ContextError, DomainError) as error:
            raise GraphEditRefused("edit_invalid", f"The edit no longer fits the published Context: {getattr(error, 'message', error)}",
                                   "integrity") from None
        if fresh != binding["targets"]:
            raise GraphEditRefused("targets_changed", "The edit's target ownership no longer matches the evidence frozen at "
                                   "preparation; discard it and submit a newly reviewed plan", "integrity")
        try:
            caller = self.caller(uow, binding["attribution"], binding["project_id"])
            self.check_personal(uow, caller, snapshot, plan)
            self.check(uow, caller, namespaces(fresh, binding["project_id"]), {"edit": binding["plan"]})
        except DomainError as error:
            raise GraphEditRefused("authority_revoked", "The original caller no longer holds the authority this edit needs: "
                                   f"{error.message}") from None
        return plan


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
        return lambda project: uow.repository("projects").get(project) is not None

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

    def prepare(self, ctx, arguments: dict, options, uow, clock, *, operation: str = "context_edit", edit: dict | None = None,
                title: str = "Context Graph Edit"):
        """``edit`` is the plan of an operation that derives it from its own arguments (``personal_set``) instead of taking it
        as ``arguments['edit']``. The prepared change then belongs to ``operation`` and carries that operation's arguments; the
        plan, its checks, the binding and the queue input are the same as for ``context_edit``."""
        self._actor(ctx)
        plan, content, digest = normalized(arguments["edit"] if edit is None else edit, self.ports.source_bytes)
        admission = ctx.project_id
        caller = self.application_context(ctx, uow)
        snapshot, targets = self._evidence(uow, plan, admission)
        self.authority.check_personal(uow, caller, snapshot, plan)
        self.authority.check(uow, caller, namespaces(targets, admission), arguments if edit is None else {"edit": plan})
        namespace = namespace_name(admission)
        builder = PlanBuilder(ctx, operation, arguments, title, clock=clock, namespace=namespace,
                              affected_scope=scope_of(admission))
        bindings.admit(builder, uow, plan=plan, content=content, plan_digest=digest, reserved_event_id=self._reserve(uow, snapshot, plan),
                       attribution=self.attribution(ctx, uow, admission, operation),
                       targets=targets, project_id=admission, max_content_bytes=self.ports.source_bytes)
        builder.effects.append("The published Context is not changed now. In queue order the driver rechecks your current authority "
                               "and that nothing this plan depends on changed since its base, then publishes exactly this plan as "
                               "one event on the published head, with no model.")
        return builder.build()

    @staticmethod
    def attribution(ctx, uow, admission, operation: str = "context_edit") -> dict:
        """The original caller, frozen with the edit: who, as what, in which scope and under which authenticated delegation (the
        registration and the closed binding/assignment/generation triplet, never the plan text, a credential or a decision)."""
        actor = uow.connection.execute("SELECT display_name FROM principals WHERE id=?", (ctx.principal_id,)).fetchone()
        return {"principal_id": ctx.principal_id, "principal_kind": ctx.principal_kind, "display_name": actor["display_name"] if actor else None,
                "agent_id": ctx.agent_id, "role_id": getattr(ctx.role_artifact_ref, "id", None), "project_id": admission,
                "operation": operation, "delegation": bindings.delegation_of(ctx)}

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
                or queued.resource.resource_id != identity or companion.resource.project_id != queued.resource.project_id
                or any(change.kind != "record" or change.action != "insert" or change.operation_name is not None
                       or change.resource.namespace is not None for change in changes)
                or targets != tuple(ExpectedTarget(change.resource, exists=False) for change in changes)):
            raise DomainError("invalid_plan", "A prepared graph edit is exactly its binding and its queue input, both new")
        values = tuple(plain(change.values) for change in changes)
        if (values[0].get("input_id") != identity or values[1].get("id") != identity
                or values[1].get("project_id") != queued.resource.project_id):
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
        admission = queued["project_id"]
        if admission != ctx.project_id:
            raise DomainError("forbidden", "A graph edit is queued in the caller's selected scope")
        snapshot, targets = self._evidence(uow, fresh, admission)
        if targets != companion["targets_json"]:
            raise DomainError("revision_conflict", "The objects this edit changes were re-owned or removed since preparation")
        caller = self.application_context(ctx, uow)
        self.authority.check_personal(uow, caller, snapshot, fresh)
        # An operation that derived its plan is checked against the plan it queued, as the driver's recheck does.
        self.authority.check(uow, caller, namespaces(targets, admission),
                             plan.arguments if plan.operation_name == self.authority.name else {"edit": fresh})
        event_id = companion["reserved_event_id"]
        if event_id in bindings.taken_event_ids(uow) or event_id in snapshot.used_ids or event_id in self._pending_ids():
            raise DomainError("conflict", "The reserved Context event ID is no longer available; prepare the edit again")
        apply_records(ctx, plan, uow)
        row = queue_module.lookup(uow, admission, queued["id"])
        facts = bindings.execution_facts(bindings.load(uow, row["id"]))
        return {"input": queue_module.input_view(row, execution=facts), "execution": facts}
