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

Reading is an ordinary Task read: the operation is authorized in the requested business-or-Global namespace, and every
returned record is resolved again through the authorized ``ItemResolver``, so an unauthorized or unavailable record is
never exposed. Nothing is written and no data is backfilled."""

from __future__ import annotations

from collections.abc import Mapping
import json

from qorqut_operations.actions.scope import is_owner
from qorqut_operations.application.plans import IDENTITY, PAGE, page, schema
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.items import UNAVAILABLE
from qorqut_operations.storage.repositories import decode_row
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
            description="Read the Actions or Signoffs associated with a Task and, by default, with every Task below it in the same "
                        "owning scope. Explicit ancestry, concerns relationships, an Action's task evidence, a Signoff's "
                        "expected Task target and typed references on a Story Task's own discussion are the only associations; "
                        "each record is read with its own permission, returned once by canonical identity and paged with a bound "
                        "cursor. Withdrawn Actions and decided Signoffs remain part of the record's history. For the "
                        "installation owner, a business-operational Action stored in Global is also associated when it is "
                        "explicitly shared with the requested business and its bare task evidence names exactly one Task among "
                        "Global and the Action's shared businesses, in that business; that inferred path is owner-only.",
            # The Task domain's own Global guard is applied by ``TaskOperations.read``, not merely by the generic policy.
            read=lambda ctx, args, options, uow: self.tasks.read(OPERATION, ctx, args, uow))

    # -- the read --------------------------------------------------------------------------------------------------

    def read(self, ctx, arguments, uow):
        record_type = arguments["record_type"]
        if record_type not in self.items.kinds:
            # An empty list would claim there is no history; the record domain itself is absent from this assembly.
            raise DomainError("operation_unavailable", f"The {record_type} record domain is not installed in this host")
        root = uow.repository("tasks", business_id=ctx.business_id).get(arguments["task_id"], required=True)
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
        """``(raw record ID, source Task ID)`` pairs; a record may be found by several routes and several Tasks."""
        found = self.concerns(uow, business_id, task_ids, record_type)
        if record_type == "action":
            found |= self.action_evidence(uow, business_id, task_ids)
            found |= self.shared_action_evidence(ctx, uow, business_id, task_ids)
        else:
            found |= self.signoff_targets(uow, business_id, task_ids)
        found |= self.thread_references(uow, business_id, task_ids, record_type)
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
        """Typed Action/Signoff references stored on the Story Tasks' own discussion messages."""
        found = set()
        for chunk in _chunks(task_ids):
            rows = uow.connection.execute(
                f"SELECT item_id, refs_json FROM thread_messages WHERE business_id IS ? AND item_type='task' "
                f"AND item_id IN ({_marks(chunk)}) AND refs_json NOT IN ('[]', 'null')", [business_id, *chunk])
            for row in rows:
                for reference in json.loads(row["refs_json"]):
                    if (isinstance(reference, Mapping) and reference.get("type") == record_type
                            and isinstance(reference.get("id"), str) and reference["id"]):
                        found.add((reference["id"], row["item_id"]))
        return found

    # -- canonical identity, authorization and paging ---------------------------------------------------------------

    def records(self, ctx, uow, sources, record_type):
        """Resolve every distinct record with the authorized item resolver and merge its matching Task IDs."""
        grouped = {}
        for identity, task_id in sorted(sources):
            record = self.record(uow, ctx.business_id, record_type, identity)
            if record is None:
                continue
            canonical, business_id = record
            grouped.setdefault(canonical, (business_id, set()))[1].add(task_id)
        items = []
        for canonical, (business_id, task_ids) in sorted(grouped.items()):
            view = self.view(ctx, uow, record_type, canonical)
            if view is None:
                continue
            items.append({"type": record_type, "id": canonical, "business_id": business_id,
                          "label": view.get("label", ""), "state": view.get("state"), "revision": view.get("revision"),
                          "task_ids": sorted(task_ids)})
        return items

    @staticmethod
    def record(uow, business_id, record_type, identity):
        """``(canonical scoped ID, owning business ID)`` of a stored record, before any authorization."""
        if record_type == "action":
            # Actions are named by their canonical action_id (with its global/ or business/ prefix) or, in one
            # business, by their bare ID inside that business's own storage.
            if identity.startswith(("global/", "business/")):
                row = decode_row(uow.connection.execute(
                    "SELECT action_key, business_id FROM action_logs WHERE action_key=?", (identity,)).fetchone())
            else:
                row = uow.repository("action_logs", business_id=business_id).get(identity)
            return (row["action_key"], row["business_id"]) if row else None
        row = uow.connection.execute("SELECT id, review_business_id FROM signoffs WHERE id=?", (identity,)).fetchone()
        # A Signoff belongs to one review scope; a record of another scope is not part of this namespace's read.
        if row is None or row["review_business_id"] != business_id:
            return None
        return row["id"], row["review_business_id"]

    def view(self, ctx, uow, record_type, identity):
        try:
            return self.items.resolve(ctx, {"type": record_type, "id": identity}, uow)
        except DomainError as error:
            if error.code not in UNAVAILABLE:
                raise
            return None


__all__ = ["OPERATION", "RECORD_TYPES", "TaskAssociatedRecords"]