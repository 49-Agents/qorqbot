"""The Actions and Signoffs associated with one Task Story: the root and its recursive Task descendants.

A Task's Story is the root plus every Task below it through ``parent_id`` in the same owning namespace, including closed
and archived descendants; a Task merely linked to the root is not a descendant. Traversal is breadth-first over the
stored parent links with a visited set, so a corrupt cycle terminates and no arbitrary depth or page cap applies.

An Action or a Signoff belongs to the Story through one of the canonical associations only: an explicit ``concerns``
relationship naming a Story Task in either direction, an Action's ``refs_json.task`` evidence in the Action's own
owning scope, a Signoff's typed Task expected target in the matching scope, or a typed Action/Signoff reference stored
on one of the Story Tasks' own discussion threads. Titles, prose, Context pointers, ``related_objects`` and any other
JSON are never parsed into links. A record found by several routes is returned once, by canonical scoped identity, with
all matching source Task IDs combined.

One further association is inferred, and only for the installation owner reading a business Story: a
business-operational Action stored in Global belongs to the Story when it is explicitly shared through
``action_businesses`` with the requested business and its bare ``refs_json.task`` names exactly one Task among Global
and the Action's shared businesses, that one being in the requested business. The Action keeps its canonical
``global/...`` identity and Global storage. Membership alone is never a link, and an ID that is ambiguous in those
namespaces links nothing; a business the Action is not shared with is no namespace for it at all. The path is
owner-only because the Action domain withholds a Global Action's evidence from a business-scoped non-owner reader, so
no other reader gains this inferred association.

A Story that has been moved between owning scopes keeps what it had. Each Story Task whose ID a relocation recorded (the
audited ``task_moves`` identity: its id and immutable creation time, read through ``journey_spans``) is also associated
through the scopes it stayed in before, but only with what was established during that very stay: an Action whose current task
evidence was established while the Task was there (the earliest of the unbroken run of immutable revisions that all carry
that evidence, so a text-only correction detaches nothing and a later re-pointing starts a new run), or a Signoff whose
frozen expected target named the Task when it was proposed. Another Task that merely reuses the bare ID in a scope, before
this one arrived or after it left, is a different lifetime and links nothing; the user-editable ``occurred_at`` is never
identity evidence. The same stay rule bounds the destination's own evidence for a moved Task, and the owner-only shared
Global inference holds across a move only when the evidence, the explicit sharing and the unique bare ID are all proven at
the time the evidence was established. A typed reference on a Story Task's own discussion is read in the scope the message
was written in: its pinned ``reference_namespace`` (first stored by a relocation) or else the message's own scope, so a
bare Action ID never names a same-ID record of the destination. A Task whose ID was never relocated keeps the
current scope's behavior exactly; one whose recorded history is not one continuous bounded journey has only its current
stay, from its creation, and no historical stay is invented for it.

Reading is an ordinary Task read: the operation is authorized in the requested business-or-Global namespace, and every
returned record is resolved again through the authorized ``ItemResolver``, so an unauthorized or unavailable record is
never exposed. A record associated in another scope is resolved through the caller's current authority in that scope (the
``HistoricalReferences.context`` seam), never in the record's owning scope merely to make it readable: a revoked source
membership or a denied read leaves it out without disclosing its ID, label, state or location. Nothing is written and no
data is backfilled."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from collections.abc import Mapping
import json

from qorqut_operations.actions.scope import is_owner
from qorqut_operations.application.plans import IDENTITY, PAGE, page, schema
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.items import UNAVAILABLE
from qorqut_operations.storage.repositories import decode_row
from qorqut_operations.tasks.relocation import proven_journey, scope_of
from qorqut_operations.tasks.scope import task_namespace

SCOPE = {"scope": {"enum": ["business", "global"]}}
RECORD_TYPES = ("action", "signoff")
# The one generic role that states a record concerns a Task (see records/associations.py); a role is never a grant.
RELATION_ROLE = "concerns"
TASK_RESOURCE = "tasks"
# SQLite parameter chunks: one chunk may appear on both sides of the relationship predicate.
CHUNK = 400
OPERATION = "tasks_associated_records"


def _chunks(values):
    ordered = sorted(values)
    for start in range(0, len(ordered), CHUNK):
        yield ordered[start:start + CHUNK]


def _marks(values):
    return ",".join("?" for _ in values)


class TaskAssociatedRecords:
    """The ``tasks_associated_records`` read, owned by ``TaskOperations`` and reading through its item resolver.

    The registered callback enters ``TaskOperations.read`` so the explicit Task Global guard (owner or authorized global
    agent, with ``read_global_context``) applies exactly as it does to ``tasks_get`` before this helper reads anything."""

    def __init__(self, tasks):
        self.tasks = tasks

    @property
    def items(self):
        return self.tasks.items

    def descriptor(self):
        return OperationDescriptor(
            OPERATION, schema({**SCOPE, **PAGE, "task_id": IDENTITY, "record_type": {"enum": list(RECORD_TYPES)},
                               "include_descendants": {"type": "boolean", "default": True}}, ("task_id", "record_type")),
            scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
            resolve_namespace=task_namespace, effect_kind="read", supports_signoff=False,
            description=instruction_text('associated-read-the-actions-or-signoffs-associated-with-a'),
            # The Task domain's own Global guard is applied by ``TaskOperations.read``, not merely by the generic policy.
            read=lambda ctx, args, options, uow: self.tasks.read(OPERATION, ctx, args, uow))

    # -- the read --------------------------------------------------------------------------------------------------

    def read(self, ctx, arguments, uow):
        record_type = arguments["record_type"]
        if record_type not in self.items.kinds:
            # An empty list would claim there is no history; the record domain itself is absent from this assembly.
            raise DomainError("operation_unavailable", f"The {record_type} record domain is not installed in this host")
        # A Story that moved answers like every other Task read: ``record_moved`` for a caller who may read it where it is now.
        root = self.tasks.require(ctx, uow, arguments["task_id"])
        story = self.story_tasks(uow, ctx.business_id, root["id"], arguments.get("include_descendants", True))
        sources = self.sources(ctx, uow, ctx.business_id, story, record_type)
        items = self.records(ctx, uow, sources, record_type)
        selected, cursor = page(items, arguments, ctx.business_id, OPERATION,
                                key=lambda item: (item["type"], item["id"]), reverse=False)
        return {"items": selected, "next_cursor": cursor}

    # -- explicit ancestry ------------------------------------------------------------------------------------------

    @staticmethod
    def story_tasks(uow, business_id, root_id, include_descendants):
        """The root and every Task below it through ``parent_id``; a corrupt cycle cannot loop forever."""
        found = {root_id}
        if not include_descendants:
            return found
        frontier = [root_id]
        while frontier:
            discovered = []
            for chunk in _chunks(frontier):
                for row in uow.connection.execute(
                        f"SELECT id FROM tasks WHERE business_id IS ? AND parent_id IN ({_marks(chunk)})", [business_id, *chunk]):
                    if row["id"] not in found:
                        found.add(row["id"])
                        discovered.append(row["id"])
            frontier = discovered
        return found

    # -- the canonical association sources -------------------------------------------------------------------------

    def sources(self, ctx, uow, business_id, task_ids, record_type):
        """``(raw record ID, source Task ID, origin scope)`` triples; a record may be found by several routes, Tasks and origins.

        The origin is the scope the association was established in, where the record's ID is resolved and where the
        caller's authority is checked. It is the current scope for everything a Task that never moved has, and for a
        relationship, which a relocation never lets cross a scope."""
        stays = self.stays(uow, business_id, task_ids)
        plain = set(task_ids) - stays.keys()
        found = self.concerns(uow, business_id, task_ids, record_type)
        if record_type == "action":
            found |= self.action_evidence(uow, business_id, plain)
            found |= self.shared_action_evidence(ctx, uow, business_id, plain)
        else:
            found |= self.signoff_targets(uow, business_id, plain)
        found = {(identity, task_id, business_id) for identity, task_id in found}
        found |= self.thread_references(uow, business_id, task_ids, record_type)
        found |= self.stay_sources(ctx, uow, record_type, stays)
        return found

    @staticmethod
    def concerns(uow, business_id, task_ids, record_type):
        """``concerns`` relationships whose Task endpoint is in the Story, in either direction."""
        found = set()
        for chunk in _chunks(task_ids):
            rows = uow.connection.execute(
                "SELECT from_type, from_id, to_type, to_id FROM relationships WHERE business_id IS ? AND role=? AND "
                f"((from_type='task' AND from_id IN ({_marks(chunk)})) "
                f"OR (to_type='task' AND to_id IN ({_marks(chunk)})))",
                [business_id, RELATION_ROLE, *chunk, *chunk])
            for row in rows:
                if row["from_type"] == "task":
                    task_id, kind, identity = row["from_id"], row["to_type"], row["to_id"]
                else:
                    task_id, kind, identity = row["to_id"], row["from_type"], row["from_id"]
                if kind == record_type:
                    found.add((identity, task_id))
        return found

    def action_evidence(self, uow, business_id, task_ids):
        """Actions whose ``refs_json.task`` names a Story Task in the Action's own owning scope.

        A business-stored Action names a Task in that business; a Global-stored Action names a Global Task. The stored
        Task ID is never borrowed across scopes: the Action's storage scope is the Task's scope.
        """
        found = set()
        for chunk in _chunks(task_ids):
            for row in uow.connection.execute(
                    f"SELECT action_key, refs_json FROM action_logs WHERE business_id IS ? "
                    f"AND json_extract(refs_json, '$.task') IN ({_marks(chunk)})", [business_id, *chunk]):
                found.add((row["action_key"], json.loads(row["refs_json"])["task"]))
        return found

    @staticmethod
    def shared_action_evidence(ctx, uow, business_id, task_ids):
        """Global shared Actions whose bare ``refs_json.task`` uniquely names a Story Task in this business.

        Only the installation owner reading a business Story reaches this inferred path: the Action domain withholds a
        Global Action's evidence from a business-scoped non-owner reader, so no other reader gains the association. The
        bare evidence ID is namespaced, so it links only when exactly one Task carries it among Global and every
        business explicitly linked to the Action, and that Task is in the requested business. A business the Action is
        not linked to is no namespace for it at all, so an unrelated coincidence there never blocks the link. Every
        lookup is bounded by the Story's Task IDs and the candidate Actions' links. Nothing is stored: the Action keeps
        its canonical ``global/...`` identity and Global storage, and the ordinary item resolver still authorizes it.
        """
        if business_id is None or not is_owner(ctx, uow):
            return set()
        candidates = {}
        for chunk in _chunks(task_ids):
            for row in uow.connection.execute(
                    "SELECT a.action_key AS action_key, json_extract(a.refs_json, '$.task') AS task_id FROM action_logs a "
                    "JOIN action_businesses m ON m.action_key = a.action_key "
                    "WHERE a.business_id IS NULL AND a.category = 'business-operational' AND m.business_id = ? "
                    f"AND json_extract(a.refs_json, '$.task') IN ({_marks(chunk)})", [business_id, *chunk]):
                candidates[row["action_key"]] = row["task_id"]
        if not candidates:
            return set()
        linked = {}
        for chunk in _chunks(candidates):
            for row in uow.connection.execute(
                    f"SELECT action_key, business_id FROM action_businesses WHERE action_key IN ({_marks(chunk)})", chunk):
                linked.setdefault(row["action_key"], set()).add(row["business_id"])
        namespaces = {None}
        for action_links in linked.values():
            namespaces |= action_links
        matches = {}
        for namespace in sorted(namespaces, key=lambda business: (business is not None, business or "")):
            for chunk in _chunks(candidates.values()):
                for row in uow.connection.execute(
                        f"SELECT id FROM tasks WHERE business_id IS ? AND id IN ({_marks(chunk)})", [namespace, *chunk]):
                    matches.setdefault(row["id"], set()).add(namespace)
        found = set()
        for action_key, task_id in candidates.items():
            matching = [namespace for namespace in matches.get(task_id, ())
                        if namespace is None or namespace in linked.get(action_key, ())]
            if matching == [business_id]:
                found.add((action_key, task_id))
        return found

    @staticmethod
    def signoff_targets(uow, business_id, task_ids):
        """Signoffs whose typed expected targets name a Story Task in the matching scope (NULL is Global)."""
        found = set()
        for chunk in _chunks(task_ids):
            rows = uow.connection.execute(
                "SELECT s.id AS signoff_id, json_extract(t.value, '$.resource.resource_id') AS task_id "
                "FROM signoffs s, json_each(s.expected_targets_json) t "
                "WHERE s.review_business_id IS ? AND json_extract(t.value, '$.resource.resource_type') = ? "
                "AND json_extract(t.value, '$.resource.business_id') IS ? "
                f"AND json_extract(t.value, '$.resource.resource_id') IN ({_marks(chunk)})",
                [business_id, TASK_RESOURCE, business_id, *chunk])
            for row in rows:
                found.add((row["signoff_id"], row["task_id"]))
        return found

    @staticmethod
    def thread_references(uow, business_id, task_ids, record_type):
        """``(reference ID, Task ID, origin)`` for the typed Action/Signoff references on the Story Tasks' own messages.

        A reference is relative to the scope its message was written in: the ``reference_namespace`` a relocation pinned
        (and no later one changes) or, for a message that never needed one, the scope it is stored in. A namespace that
        names no scope reads nothing, and a bare ID is never looked up anywhere else."""
        found = set()
        for chunk in _chunks(task_ids):
            rows = uow.connection.execute(
                f"SELECT item_id, refs_json, reference_namespace FROM thread_messages WHERE business_id IS ? AND item_type='task' "
                f"AND item_id IN ({_marks(chunk)}) AND refs_json NOT IN ('[]', 'null')", [business_id, *chunk])
            for row in rows:
                valid, origin = (True, business_id) if row["reference_namespace"] is None else scope_of(row["reference_namespace"])
                if not valid:
                    continue
                for reference in json.loads(row["refs_json"]):
                    if (isinstance(reference, Mapping) and reference.get("type") == record_type
                            and isinstance(reference.get("id"), str) and reference["id"]):
                        found.add((reference["id"], row["item_id"], origin))
        return found

    # -- scopes a moved Story Task has been in -----------------------------------------------------------------------

    @staticmethod
    def stays(uow, business_id, task_ids):
        """``{Task ID: [(scope, since, until), ...] or None}`` for each Story Task whose bare ID a relocation ever recorded,
        newest stay first.

        Only the audited journey of that very Task counts (``proven_journey``). A relocation can free an ID in a scope for
        another Task to take, so a Task whose ID was ever moved - itself, or an earlier lifetime of the same ID - is bound
        to its own stays, and the evidence of another lifetime never names it. A Task whose recorded history overflows or is
        not one continuous journey has no provable stay: it maps to ``None`` and no Action or Signoff is inferred for it from
        any scope (a current-scope stay from its creation would be a guess, and could attach another lifetime's records).
        Its explicit relationships and typed discussion references are unaffected. A Task whose ID no relocation ever
        recorded is absent and keeps the unbounded current-scope behavior."""
        moved = set()
        for chunk in _chunks(task_ids):
            moved.update(row["task_id"] for row in uow.connection.execute(
                f"SELECT DISTINCT task_id FROM task_moves WHERE task_id IN ({_marks(chunk)})", chunk))
        return {task_id: proven_journey(uow, business_id, task_id) for task_id in sorted(moved)}

    @staticmethod
    def within(when, since, until):
        """Whether an instant falls in the half-open stay ``[since, until)``; an open stay has no end."""
        return since <= when and (until is None or when < until)

    @staticmethod
    def evidence(uow, action_key, revision, holds):
        """``(created_at, accepted state)`` of the revision that began the Action's present ``holds``, else ``None``.

        An Action row is a projection; its immutable revisions say when its current evidence was established. The run
        starts at the earliest revision such that every revision up to the current one satisfies ``holds`` on its exact
        accepted state, so a correction of the text alone does not move it and a correction of the evidence starts a new run.
        The Action's user-editable ``occurred_at`` is never consulted. A current state that does not hold, or a history with
        a missing revision, proves nothing and yields ``None``."""
        expected, began = revision, None
        for row in uow.connection.execute(
                "SELECT revision, created_at, snapshot_json FROM action_revisions WHERE action_key=? ORDER BY revision DESC",
                (action_key,)):
            if row["revision"] != expected:
                return None
            state = json.loads(row["snapshot_json"])
            if not holds(state):
                return began
            began, expected = (row["created_at"], state), expected - 1
        return began if expected == 0 else None

    def stay_sources(self, ctx, uow, record_type, stays):
        """``(raw record ID, Story Task ID, origin)`` triples established during a stay of each moved Story Task."""
        stays = {task_id: spans for task_id, spans in stays.items() if spans}
        if not stays:
            return set()
        by_scope = {}
        for task_id, spans in stays.items():
            for scope, since, until in spans:
                by_scope.setdefault(scope, {}).setdefault(task_id, []).append((since, until))
        if record_type == "signoff":
            return self.stay_signoff_targets(uow, by_scope)
        return self.stay_action_evidence(uow, by_scope) | self.stay_shared_evidence(ctx, uow, stays, by_scope)

    def stay_action_evidence(self, uow, by_scope):
        """Actions whose present ``refs_json.task`` was established, in the Action's own scope, during a stay in that scope."""
        found = set()
        for scope, tasks in by_scope.items():
            for chunk in _chunks(tasks):
                for row in uow.connection.execute(
                        "SELECT action_key, revision, json_extract(refs_json, '$.task') AS task_id FROM action_logs "
                        f"WHERE business_id IS ? AND json_extract(refs_json, '$.task') IN ({_marks(chunk)})", [scope, *chunk]):
                    task_id = row["task_id"]
                    began = self.evidence(uow, row["action_key"], row["revision"],
                                          lambda state: state.get("refs", {}).get("task") == task_id)
                    if began is not None and any(self.within(began[0], *span) for span in tasks[task_id]):
                        found.add((row["action_key"], task_id, scope))
        return found

    def stay_signoff_targets(self, uow, by_scope):
        """Signoffs whose frozen expected Task target named the Task, in the review scope, when it was proposed in a stay.

        The proposal and its targets are immutable; the later decision time is not when the target was established."""
        found = set()
        for scope, tasks in by_scope.items():
            for chunk in _chunks(tasks):
                rows = uow.connection.execute(
                    "SELECT s.id AS signoff_id, s.created_at AS proposed_at, json_extract(t.value, '$.resource.resource_id') AS task_id "
                    "FROM signoffs s, json_each(s.expected_targets_json) t "
                    "WHERE s.review_business_id IS ? AND json_extract(t.value, '$.resource.resource_type') = ? "
                    "AND json_extract(t.value, '$.resource.business_id') IS ? "
                    f"AND json_extract(t.value, '$.resource.resource_id') IN ({_marks(chunk)})", [scope, TASK_RESOURCE, scope, *chunk])
                for row in rows:
                    if any(self.within(row["proposed_at"], *span) for span in tasks[row["task_id"]]):
                        found.add((row["signoff_id"], row["task_id"], scope))
        return found

    def stay_shared_evidence(self, ctx, uow, stays, by_scope):
        """The owner-only shared Global Action inference, proven across a move.

        It holds for a stay in a business only when the Action's explicit sharing with that business and its bare task
        evidence were both established during the stay, and when, at that instant, exactly one Task lifetime carried the
        bare ID among Global and every business the Action is (or was then) shared with - the Task itself, in that
        business. Anything else, including a lifetime of another Task with the same ID, links nothing."""
        if not is_owner(ctx, uow):
            return set()
        found = set()
        for scope, tasks in by_scope.items():
            if scope is None:
                continue
            for chunk in _chunks(tasks):
                for row in uow.connection.execute(
                        "SELECT a.action_key AS action_key, a.revision AS revision, json_extract(a.refs_json, '$.task') AS task_id "
                        "FROM action_logs a JOIN action_businesses m ON m.action_key = a.action_key "
                        "WHERE a.business_id IS NULL AND a.category = 'business-operational' AND m.business_id = ? "
                        f"AND json_extract(a.refs_json, '$.task') IN ({_marks(chunk)})", [scope, *chunk]):
                    task_id = row["task_id"]
                    began = self.evidence(
                        uow, row["action_key"], row["revision"],
                        lambda state: (state.get("category") == "business-operational" and state.get("refs", {}).get("task") == task_id
                                       and scope in state.get("business_ids", ())))
                    if began is None or not any(self.within(began[0], *span) for span in tasks[task_id]):
                        continue
                    namespaces = {None, *began[1].get("business_ids", ()), *(
                        link["business_id"] for link in uow.connection.execute(
                            "SELECT business_id FROM action_businesses WHERE action_key=?", (row["action_key"],)))}
                    # The Task's own lifetime is identified by its immutable creation time, read from the Task itself: after
                    # several moves no stay in the journey starts at it except the first, and that one is not last.
                    created = uow.connection.execute("SELECT created_at FROM tasks WHERE business_id IS ? AND id=?",
                                                     (stays[task_id][0][0], task_id)).fetchone()
                    if created is not None and self.lifetimes_at(uow, task_id, namespaces, began[0]) == {(scope, created["created_at"])}:
                        found.add((row["action_key"], task_id, scope))
        return found

    @staticmethod
    def lifetimes_at(uow, task_id, namespaces, when):
        """``{(scope, creation time)}`` of every Task lifetime that carried ``task_id`` in one of ``namespaces`` at ``when``,
        or ``None`` when some lifetime of the id has a journey the audit cannot prove.

        Uniqueness is a proof that no other lifetime carried the ID; a lifetime whose whereabouts are unknown might have, so
        it proves nothing and the caller must not link."""
        found = set()
        for row in uow.connection.execute("SELECT business_id, created_at FROM tasks WHERE id=?", (task_id,)).fetchall():
            spans = proven_journey(uow, row["business_id"], task_id)
            if spans is None:
                return None
            for scope, since, until in spans:
                if scope in namespaces and since <= when and (until is None or when < until):
                    found.add((scope, row["created_at"]))
        return found

    # -- canonical identity, authorization and paging ---------------------------------------------------------------

    def records(self, ctx, uow, sources, record_type):
        """Resolve every distinct record with the authorized item resolver and merge its matching Task IDs.

        A record is named once by its canonical scoped ID. It is read in each scope its association was established in,
        the current scope first, with the caller's authority there; only the origins that may read it contribute their
        Story Task IDs, and a record no origin may read is left out without a trace."""
        grouped = {}
        for identity, task_id, origin in sorted(sources, key=lambda source: (source[0], source[1], source[2] is not None, source[2] or "")):
            record = self.record(uow, origin, record_type, identity)
            if record is None:
                continue
            canonical, business_id = record
            grouped.setdefault(canonical, (business_id, {}))[1].setdefault(origin, set()).add(task_id)
        items = []
        for canonical, (business_id, origins) in sorted(grouped.items()):
            shown, task_ids = None, set()
            for origin in sorted(origins, key=lambda scope: (scope != ctx.business_id, scope is not None, scope or "")):
                view = self.view(ctx, uow, origin, record_type, canonical)
                if view is not None:
                    shown = shown or view
                    task_ids |= origins[origin]
            if shown is None:
                continue
            items.append({"type": record_type, "id": canonical, "business_id": business_id,
                          "label": shown.get("label", ""), "state": shown.get("state"), "revision": shown.get("revision"),
                          "task_ids": sorted(task_ids)})
        return items

    @staticmethod
    def record(uow, origin, record_type, identity):
        """``(canonical scoped ID, owning business ID)`` of a stored record named in ``origin``, before any authorization."""
        if record_type == "action":
            # Actions are named by their canonical action_id (with its global/ or business/ prefix) or, in one
            # business, by their bare ID inside that business's own storage.
            if identity.startswith(("global/", "business/")):
                row = decode_row(uow.connection.execute(
                    "SELECT action_key, business_id FROM action_logs WHERE action_key=?", (identity,)).fetchone())
            else:
                row = uow.repository("action_logs", business_id=origin).get(identity)
            return (row["action_key"], row["business_id"]) if row else None
        row = uow.connection.execute("SELECT id, review_business_id FROM signoffs WHERE id=?", (identity,)).fetchone()
        # A Signoff belongs to one review scope; a record of another scope is not part of this origin's read.
        if row is None or row["review_business_id"] != origin:
            return None
        return row["id"], row["review_business_id"]

    def view(self, ctx, uow, origin, record_type, identity):
        """The record as the caller may read it in ``origin``, or ``None`` when that scope or record is unavailable to them."""
        try:
            scoped = self.tasks.history.context(ctx, origin, uow)
            return self.items.resolve(scoped, {"type": record_type, "id": identity}, uow)
        except DomainError as error:
            if error.code not in UNAVAILABLE:
                raise
            return None


__all__ = ["OPERATION", "RECORD_TYPES", "TaskAssociatedRecords"]