"""Move whole Task trees between owning scopes without changing any Task's identity.

A Task is owned by exactly one scope (a project, or Global) and its id is only unique inside that scope. Moving a Task
means changing the one ``business_id`` of the Task row and of the rows that belong to it, in one transaction, while its id,
parents inside the moved tree, discussion, follows, completion, snooze history, tags, assignments and CRM memberships keep
their identity and content. Nothing is copied, recreated or merged.

``TaskRelocation.inspect`` reads (never writes) and returns an ``Inspection``: the expanded tree in ancestry order, what
travels with it, the concrete reasons the move cannot be done (``blockers``) and a fingerprint of that exact state.
``TaskRelocation.apply`` recomputes the inspection inside the applying write transaction, refuses a changed state, and only
then arms the unit's relocation guard (``UnitOfWork.relocating``) for exactly these ids and this source/destination pair to
update the rows in ancestry order. Anything unexpected raises and the whole unit rolls back.

The host supplies ``RelocationAdapter`` objects for the references only it owns (private conversations, Focus, ...). They
see the same scope and ids, report their own counts and blockers, and are applied inside the same guard and transaction.

``task_moves`` is the append-only audit of every relocation and the only input of ``moved_location``, the bounded resolver
that lets a stale scoped reference find the Task's current scope. It is a location aid, not a second Task store."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import Any, Protocol

from qorqut_operations.contracts import DomainError, canonical_bytes
from qorqut_operations.storage.relocation import MAXIMUM_TASKS
from qorqut_operations.tasks.links import KEY as LINKS
from qorqut_operations.tasks.models import DEFAULT_CONFIG, stored_fields, validate_task
from qorqut_operations.tasks.types import attributes

MAXIMUM_ROOTS = 100
MAXIMUM_BLOCKERS = 100
MAXIMUM_MESSAGES = 100_000
MAXIMUM_RELATED = 20_000
MAXIMUM_STEPS = 1_000
FORMAT = 1
# Core tables whose rows follow a moved Task. A host table that references a Task is classified by its own adapter; the
# schema inventory test requires every foreign key to ``tasks`` to be named by one of the two.
FOREIGN_KEY_CHILDREN = frozenset({"task_snooze_wakes", "crm_entries", "crm_activity_events"})


def namespace(business_id) -> str:
    return "global" if business_id is None else "business:" + business_id


def scope_of(text):
    """The inverse of ``namespace``: ``(True, business_id)`` for ``global`` or ``business:<id>``, ``(False, None)`` otherwise."""
    if text == "global":
        return True, None
    if isinstance(text, str) and text.startswith("business:") and len(text) > len("business:"):
        return True, text[len("business:"):]
    return False, None


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def digest(rows) -> str:
    """SHA-256 of the full content of ``rows`` in the order given."""
    value = hashlib.sha256()
    for row in rows:
        value.update(canonical(list(row)).encode())
        value.update(b"\n")
    return value.hexdigest()


def existing_tables(uow) -> set:
    """The ordinary tables this store really has: optional domains (CRM, links, subscriptions) may be absent."""
    return {row[0] for row in uow.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def marks(values) -> str:
    return "(" + ",".join("?" * len(values)) + ")"


@dataclass(frozen=True)
class AdapterReport:
    """What a host adapter found for the exact tasks: counts by name, blockers and the content its fingerprint covers."""

    counts: Mapping[str, int] = field(default_factory=dict)
    blockers: Sequence[Mapping[str, Any]] = ()
    material: Any = None


@dataclass(frozen=True)
class Relocation:
    """The exact move an adapter or the guard works for: scopes and the ids, roots first (ancestry order)."""

    source: str | None
    destination: str
    ids: tuple[str, ...]
    roots: tuple[str, ...]


class RelocationAdapter(Protocol):
    """References outside core Task storage that follow a Task. ``inspect`` reads; ``apply`` runs inside the armed guard."""

    name: str

    def inspect(self, uow, relocation: Relocation) -> AdapterReport: ...

    def apply(self, uow, relocation: Relocation, at: str) -> Mapping[str, int]: ...


def blocker(code, message, task_id=None) -> dict:
    return {"code": code, "message": message, **({"task_id": task_id} if task_id else {})}


@dataclass(frozen=True)
class Inspection:
    relocation: Relocation
    roots: list
    tasks: list
    counts: dict
    blockers: list
    material: dict
    rows: dict = field(repr=False, default_factory=dict)

    @property
    def can_move(self) -> bool:
        return not self.blockers

    def fingerprint(self, principal_id) -> str:
        """Deterministic fingerprint of this exact state and of the caller it was authorized for."""
        return hashlib.sha256(canonical({"format": FORMAT, "principal": principal_id, "material": self.material}).encode()).hexdigest()


class TaskRelocation:
    """The Task relocation helper of one ``TaskOperations`` (its configuration and assignee rules decide validity)."""

    def __init__(self, operations, adapters: Sequence[RelocationAdapter] = ()):
        self.operations, self.adapters = operations, tuple(adapters)

    # -- selection -------------------------------------------------------------------------------------------------

    @staticmethod
    def selection(task_ids) -> tuple[str, ...]:
        if (not isinstance(task_ids, (list, tuple)) or not 1 <= len(task_ids) <= MAXIMUM_ROOTS
                or any(not isinstance(item, str) or not item for item in task_ids) or len(set(task_ids)) != len(task_ids)):
            raise DomainError("validation_error", f"Name 1 to {MAXIMUM_ROOTS} unique Task ids")
        return tuple(task_ids)

    @staticmethod
    def tree(uow, source, given):
        """``(rows by id, ordered ids, root ids)`` of the given Tasks and all their descendants in the source scope."""
        found = uow.connection.execute(
            "WITH RECURSIVE tree(id) AS (SELECT id FROM tasks WHERE business_id IS ? AND id IN " + marks(given)
            + " UNION SELECT t.id FROM tasks t JOIN tree ON t.parent_id=tree.id WHERE t.business_id IS ?) "
            "SELECT * FROM tasks WHERE business_id IS ? AND id IN (SELECT id FROM tree) ORDER BY id LIMIT ?",
            (source, *given, source, source, MAXIMUM_TASKS + 1)).fetchall()
        if len(found) > MAXIMUM_TASKS:
            raise DomainError("validation_error", f"The selection expands to more than {MAXIMUM_TASKS} Tasks")
        rows = {row["id"]: dict(row) for row in found}
        if set(given) - set(rows):
            raise DomainError("not_found", "Task does not exist in this scope")
        # Overlapping selections move once: a given Task whose parent is inside the selection is not a root.
        roots = tuple(item for item in given if rows[item]["parent_id"] not in rows)
        depth = {}

        def level(identity):
            chain = []
            while identity not in depth and rows[identity]["parent_id"] in rows and identity not in chain:
                chain.append(identity)
                identity = rows[identity]["parent_id"]
            base = depth.get(identity, 0)
            for step, item in enumerate(reversed(chain), 1):
                depth[item] = base + step
            depth.setdefault(identity, base)

        for identity in rows:
            level(identity)
        return rows, tuple(sorted(rows, key=lambda item: (depth[item], item))), roots

    # -- inspection ------------------------------------------------------------------------------------------------

    def inspect(self, ctx, uow, source, destination, task_ids) -> Inspection:
        """Everything the move would touch and everything that prevents it, read-only. Errors are never blockers:
        a missing Task, a missing or archived destination and an oversized selection fail the request itself."""
        given = self.selection(task_ids)
        if not isinstance(destination, str) or not destination or destination == source:
            raise DomainError("validation_error", "Choose a project other than the Task's current scope")
        target = uow.connection.execute("SELECT id, slug, name, archived_at FROM businesses WHERE id=?", (destination,)).fetchone()
        if target is None or target["archived_at"] is not None:
            raise DomainError("not_found", "Destination project is unavailable")
        if source is not None and uow.connection.execute("SELECT 1 FROM businesses WHERE id=?", (source,)).fetchone() is None:
            raise DomainError("not_found", "Source project is unavailable")
        rows, ordered, roots = self.tree(uow, source, given)
        relocation = Relocation(source, destination, ordered, roots)
        ids, tables, blockers = list(ordered), existing_tables(uow), []
        connection, material, counts = uow.connection, {}, {}
        material["source"], material["destination"] = source, destination
        material["destination_project"] = [target["id"], target["slug"], target["name"], target["archived_at"]]
        material["tasks"] = [[row["id"], row["revision"], row["parent_id"], row["archived"], row["updated_at"]]
                             for row in (rows[item] for item in ordered)]
        material["roots"] = list(roots)

        # Destination identity: a Task id is unique only inside its scope, so another record there with the same id blocks.
        for row in connection.execute("SELECT id FROM tasks WHERE business_id IS ? AND id IN " + marks(ids) + " ORDER BY id", (destination, *ids)):
            blockers.append(blocker("task_id_exists", "The destination project already has a Task with this id", row["id"]))

        # Typed fields must validate under the destination's own configuration; links among the moved Tasks follow them.
        config = connection.execute("SELECT config_json, revision FROM business_configs WHERE business_id IS ? AND domain='tasks'",
                                    (destination,)).fetchone()
        fields = stored_fields(json.loads(config["config_json"]) if config else DEFAULT_CONFIG)
        material["destination_config"] = [config["revision"], digest([[config["config_json"]]])] if config else None
        rewritten = {}
        for identity in ordered:
            row = dict(rows[identity])
            row["fields_json"] = json.loads(row["fields_json"]) if isinstance(row["fields_json"], str) else row["fields_json"]
            links = row["fields_json"].get(LINKS)
            if links:
                row["fields_json"][LINKS] = [{**item, "business_id": destination} if item.get("business_id") == source
                                             and item.get("task_id") in rows else item for item in links]
            rewritten[identity] = row
            shown = attributes(row)
            if shown["task_type"] == "email" and shown["email_conversation_id"] is not None:
                blockers.append(blocker("email_binding", "A Task bound to an email conversation cannot move; "
                                        "this operation does not move mailbox ownership", identity))
            try:
                validate_task(row, fields)
            except DomainError as error:
                if error.code != "validation_error":
                    raise
                blockers.append(blocker("fields_invalid", "The destination project does not accept this Task as it is: "
                                        + error.message, identity))
        counts["tasks"] = len(ordered)

        # Discussion: immutable messages keep id, content, authors and event association; only their scope changes.
        count = connection.execute("SELECT COUNT(*) FROM thread_messages WHERE business_id IS ? AND item_type='task' AND item_id IN "
                                   + marks(ids), (source, *ids)).fetchone()[0]
        if count > MAXIMUM_MESSAGES:
            blockers.append(blocker("history_too_large", f"The selection has more than {MAXIMUM_MESSAGES} discussion entries"))
        else:
            messages = connection.execute("SELECT item_id, id, event_id, created_at, reference_namespace FROM thread_messages WHERE business_id IS ? AND "
                                          "item_type='task' AND item_id IN " + marks(ids) + " ORDER BY item_id, id", (source, *ids)).fetchall()
            material["messages"] = [len(messages), digest(messages)]
            clash = connection.execute("SELECT id FROM thread_messages WHERE business_id IS ? AND id IN (SELECT id FROM thread_messages "
                                       "WHERE business_id IS ? AND item_type='task' AND item_id IN " + marks(ids) + ") LIMIT 1",
                                       (destination, source, *ids)).fetchone()
            if clash:
                blockers.append(blocker("message_id_exists", "The destination project already has a discussion entry with the same id"))
        counts["comments"] = count

        follows = connection.execute("SELECT * FROM thread_follows WHERE business_id IS ? AND item_type='task' AND item_id IN " + marks(ids)
                                     + " ORDER BY item_id, principal_id", (source, *ids)).fetchall()
        material["follows"] = [len(follows), digest(follows)]
        if connection.execute("SELECT 1 FROM thread_follows WHERE business_id IS ? AND item_type='task' AND item_id IN " + marks(ids) + " LIMIT 1",
                              (destination, *ids)).fetchone():
            blockers.append(blocker("follow_exists", "The destination project already has follows for the same Task ids"))

        completions = connection.execute("SELECT * FROM task_completions WHERE business_id IS ? AND task_id IN " + marks(ids) + " ORDER BY task_id",
                                         (source, *ids)).fetchall() if source is not None else []
        material["completions"] = [len(completions), digest(completions)]
        if connection.execute("SELECT 1 FROM task_completions WHERE business_id=? AND task_id IN " + marks(ids) + " LIMIT 1",
                              (destination, *ids)).fetchone():
            blockers.append(blocker("completion_exists", "The destination project already has completion history for the same Task ids"))
        wakes = connection.execute("SELECT * FROM task_snooze_wakes WHERE business_id IS ? AND task_id IN " + marks(ids)
                                   + " ORDER BY task_id, snooze_generation", (source, *ids)).fetchall()
        material["wakes"] = [len(wakes), digest(wakes)]
        tags = connection.execute("SELECT * FROM item_tags WHERE business_id IS ? AND item_type='task' AND item_id IN " + marks(ids)
                                  + " ORDER BY item_id, tag_id", (source, *ids)).fetchall()
        material["tags"] = [len(tags), digest(tags)]
        if connection.execute("SELECT 1 FROM item_tags WHERE business_id IS ? AND item_type='task' AND item_id IN " + marks(ids) + " LIMIT 1",
                              (destination, *ids)).fetchone():
            blockers.append(blocker("tag_exists", "The destination project already has tags for the same Task ids"))

        # Relationships: edges inside the move and assignments travel; any other edge would change meaning, so it blocks.
        edges = connection.execute("SELECT * FROM relationships WHERE business_id IS ? AND ((from_type='task' AND from_id IN " + marks(ids)
                                   + ") OR (to_type='task' AND to_id IN " + marks(ids) + ")) ORDER BY id LIMIT ?",
                                   (source, *ids, *ids, MAXIMUM_RELATED + 1)).fetchall()
        carried_edges, assignees = [], set()
        if len(edges) > MAXIMUM_RELATED:
            blockers.append(blocker("relationships_too_large", "The selection has too many relationships to move safely"))
            edges = []
        for edge in edges:
            inside = lambda kind, item: kind == "task" and item in rows  # noqa: E731
            moving_from, moving_to = inside(edge["from_type"], edge["from_id"]), inside(edge["to_type"], edge["to_id"])
            if moving_from and moving_to:
                carried_edges.append(edge)
            elif moving_from and edge["role"] == "assigned_to":
                carried_edges.append(edge)
                assignees.add((edge["from_id"], edge["to_id"]))
            else:
                owner = edge["from_id"] if moving_from else edge["to_id"]
                other = edge["to_type"] + " " + edge["to_id"] if moving_from else edge["from_type"] + " " + edge["from_id"]
                blockers.append(blocker("crossing_relationship", f"The '{edge['role']}' relationship with {other} would cross the "
                                        "move; remove it or include both ends", owner))
        material["relationships"] = [[edge["id"], edge["revision"], edge["role"], edge["from_type"], edge["from_id"], edge["to_type"],
                                      edge["to_id"], edge["context"]] for edge in edges]
        if carried_edges:
            keys = [edge["id"] for edge in carried_edges]
            if connection.execute("SELECT 1 FROM relationships WHERE business_id IS ? AND id IN " + marks(keys) + " LIMIT 1",
                                  (destination, *keys)).fetchone():
                blockers.append(blocker("relationship_id_exists", "The destination project already has a relationship with the same id"))
            reviewed = connection.execute("SELECT relationship_id FROM context_reviews WHERE business_id IS ? AND relationship_id IN "
                                          + marks(keys) + " LIMIT 1", (source, *keys)).fetchone() if "context_reviews" in tables else None
            if reviewed:
                blockers.append(blocker("reviewed_relationship", "A moved relationship has Context review decisions that cannot be rewritten"))
        for task_id, identity in sorted(assignees):
            try:
                self.operations.assignee(replace(ctx, business_id=destination, review_business_id=destination), identity, uow)
            except DomainError:
                blockers.append(blocker("assignee_unavailable", "An assignee cannot work in the destination project", task_id))

        # Links stored on a Task outside the move that name a moved Task would silently go stale: they block.
        for row in connection.execute(
                "SELECT DISTINCT t.business_id AS scope, t.id AS identity, json_extract(edge.value,'$.task_id') AS target "
                "FROM tasks t, json_each(t.fields_json, '$.\"$task_links\"') edge WHERE json_extract(edge.value,'$.business_id') IS ? "
                "AND json_extract(edge.value,'$.task_id') IN " + marks(ids) + " AND NOT (t.business_id IS ? AND t.id IN " + marks(ids)
                + ") ORDER BY 1, 2, 3 LIMIT 100", (source, *ids, source, *ids)):
            blockers.append(blocker("external_task_link", "A Task outside this move links to this Task; remove that link first", row["target"]))

        # Memberships and observations of CRM Tables: table ownership stays where it is; only the record scope changes.
        entries = events = []
        if "crm_entries" in tables:
            entries = connection.execute("SELECT id, business_id, table_id, revision, removed FROM crm_entries WHERE record_business_id IS ? "
                                         "AND record_task_id IN " + marks(ids) + " ORDER BY business_id, id", (source, *ids)).fetchall()
            events = connection.execute("SELECT id, content_hash FROM crm_activity_events WHERE business_id IS ? AND task_id IN " + marks(ids)
                                        + " ORDER BY id", (source, *ids)).fetchall()
            if connection.execute("SELECT 1 FROM crm_entries WHERE record_business_id IS ? AND record_task_id IN " + marks(ids) + " LIMIT 1",
                                  (destination, *ids)).fetchone() or connection.execute(
                    "SELECT 1 FROM crm_activity_events WHERE business_id IS ? AND (task_id IN " + marks(ids) + " OR id IN (SELECT id FROM "
                    "crm_activity_events WHERE business_id IS ? AND task_id IN " + marks(ids) + ")) LIMIT 1", (destination, *ids, source, *ids)).fetchone():
                blockers.append(blocker("crm_record_exists", "The destination project already has CRM records for the same Task ids"))
        material["crm"] = [[tuple(row) for row in entries], len(events), digest(events)]
        counts["crm_entries"] = len(entries)

        # Subscriptions and project links are Operations tables that name a Task by exact scope.
        subscriptions = connection.execute("SELECT id, agent_id, source_type, source_id, include_related FROM inbox_subscriptions WHERE "
                                           "business_id IS ? AND source_type='tasks' AND source_id IN " + marks(ids) + " ORDER BY id",
                                           (source, *ids)).fetchall() if "inbox_subscriptions" in tables else []
        material["subscriptions"] = [len(subscriptions), digest(subscriptions)]
        if subscriptions and connection.execute("SELECT 1 FROM inbox_subscriptions WHERE business_id IS ? AND source_type='tasks' AND "
                                                "source_id IN " + marks(ids) + " LIMIT 1", (destination, *ids)).fetchone():
            blockers.append(blocker("subscription_exists", "The destination project already has subscriptions for the same Task ids"))
        links = connection.execute("SELECT * FROM project_item_links WHERE target_type='task' AND target_namespace=? AND target_id IN "
                                   + marks(ids) + " ORDER BY id", (namespace(source), *ids)).fetchall() if "project_item_links" in tables else []
        material["project_links"] = [len(links), digest(links)]
        counts["project_links"] = len(links)

        for adapter in self.adapters:
            report = adapter.inspect(uow, relocation)
            material["adapter:" + adapter.name] = report.material
            for name, value in report.counts.items():
                counts[name] = counts.get(name, 0) + value
            blockers.extend(dict(item) for item in report.blockers)

        blockers.sort(key=lambda item: (item["code"], item.get("task_id") or "", item["message"]))
        if len(blockers) > MAXIMUM_BLOCKERS:
            more = len(blockers) - MAXIMUM_BLOCKERS
            blockers = [*blockers[:MAXIMUM_BLOCKERS], blocker("more_blockers", f"{more} more reasons are not listed")]
        material["blockers"] = blockers
        for name in ("comments", "crm_entries", "project_links", "tasks"):
            counts.setdefault(name, 0)
        shown = [{"id": identity, "title": rows[identity]["title"], "parent_id": rows[identity]["parent_id"],
                  "task_type": attributes(rewritten[identity])["task_type"], "archived": bool(rows[identity]["archived"])} for identity in ordered]
        shown_roots = [{"id": identity, "title": rows[identity]["title"], "previous_parent_id": rows[identity]["parent_id"]} for identity in roots]
        return Inspection(relocation, shown_roots, shown, counts, blockers, material,
                          {"original": rows, "tasks": rewritten, "edges": carried_edges, "project": dict(target), "links": links,
                           "tables": tables})

    # -- application -----------------------------------------------------------------------------------------------

    def apply(self, ctx, uow, source, destination, task_ids, token, *, move_id, at):
        """Recompute inside the write transaction, refuse any difference from the preview, then move everything atomically.

        ``token`` is the preview fingerprint. A different state is ``revision_conflict`` (a new preview is required) and a
        state that now has blockers is ``move_blocked``; neither has written anything."""
        uow.require_write()
        inspection = self.inspect(ctx, uow, source, destination, task_ids)
        if inspection.fingerprint(ctx.principal_id) != token:
            raise DomainError("revision_conflict", "The selection changed after the preview; preview it again")
        if not inspection.can_move:
            raise DomainError("move_blocked", "The move is blocked", details={"blockers": inspection.blockers})
        relocation, rows = inspection.relocation, inspection.rows
        ids = relocation.ids
        connection = uow.connection
        tables = rows["tables"]
        watermark = connection.execute("SELECT COALESCE(MAX(id),0) FROM inbox_changes").fetchone()[0] if "inbox_changes" in tables else None
        counts = {"tasks": len(ids), "comments": 0, "crm_entries": 0, "project_links": 0}
        with uow.relocating(source, destination, ids):
            for identity in ids:
                before, after = rows["original"][identity], rows["tasks"][identity]
                changed = after["fields_json"] != json.loads(before["fields_json"])
                updated = connection.execute(
                    "UPDATE tasks SET business_id=?, parent_id=" + ("NULL" if identity in relocation.roots else "parent_id")
                    + (", fields_json=?" if changed else "") + ", revision=revision+1, updated_at=? WHERE business_id IS ? AND id=? AND revision=?",
                    (destination, *((canonical_bytes(after["fields_json"]).decode(),) if changed else ()), at, source, identity,
                     before["revision"])).rowcount
                if updated != 1:
                    raise DomainError("revision_conflict", "A Task changed during the move; preview it again")
            params = (destination, source)
            for identity in ids:
                # The references a message holds name Tasks of the scope it was written in. The first relocation pins that
                # scope (a later one keeps it) so the references keep their meaning; the payload itself never changes.
                counts["comments"] += connection.execute(
                    "UPDATE thread_messages SET business_id=?, reference_namespace=COALESCE(reference_namespace, ?) WHERE business_id IS ? "
                    "AND item_type='task' AND item_id=?", (destination, namespace(source), source, identity)).rowcount
                connection.execute("UPDATE thread_follows SET business_id=? WHERE business_id IS ? AND item_type='task' AND item_id=?", (*params, identity))
                connection.execute("UPDATE item_tags SET business_id=? WHERE business_id IS ? AND item_type='task' AND item_id=?", (*params, identity))
                connection.execute("UPDATE task_snooze_wakes SET business_id=? WHERE business_id IS ? AND task_id=?", (*params, identity))
                if source is not None:
                    connection.execute("UPDATE task_completions SET business_id=? WHERE business_id=? AND task_id=?", (*params, identity))
                if "inbox_subscriptions" in tables:
                    connection.execute("UPDATE inbox_subscriptions SET business_id=? WHERE business_id IS ? AND source_type='tasks' AND source_id=?",
                                       (*params, identity))
                if "crm_entries" in tables:
                    counts["crm_entries"] += connection.execute(
                        "UPDATE crm_entries SET record_business_id=?, record_namespace=?, revision=revision+1, updated_at=? "
                        "WHERE record_business_id IS ? AND record_task_id=?", (destination, namespace(destination), at, source, identity)).rowcount
                    connection.execute("UPDATE crm_activity_events SET business_id=? WHERE business_id IS ? AND task_id=?", (*params, identity))
            for edge in inspection.rows["edges"]:
                connection.execute("UPDATE relationships SET business_id=? WHERE business_id IS ? AND id=?", (*params, edge["id"]))
            for link in inspection.rows["links"]:
                if link["business_id"] == destination:
                    connection.execute("DELETE FROM project_item_links WHERE id=?", (link["id"],))
                else:
                    connection.execute("UPDATE project_item_links SET target_business_id=?, target_namespace=?, revision=revision+1, "
                                       "updated_at=? WHERE id=?", (destination, namespace(destination), at, link["id"]))
                counts["project_links"] += 1
            for adapter in self.adapters:
                for name, value in adapter.apply(uow, relocation, at).items():
                    counts[name] = counts.get(name, 0) + value
            for identity in ids:
                before = rows["original"][identity]
                connection.execute(
                    "INSERT INTO task_moves(move_id,task_id,source_business_id,destination_business_id,task_created_at,previous_parent_id,"
                    "revision_before,revision_after,actor_id,moved_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (move_id, identity, source, destination, before["created_at"], before["parent_id"], before["revision"],
                     before["revision"] + 1, ctx.principal_id, at))
            if watermark is not None:
                # A relocation is not new activity: the capture triggers' rows would otherwise announce every moved record.
                connection.execute("DELETE FROM inbox_changes WHERE id>?", (watermark,))
            self.verify(uow, relocation, inspection)
        moved = [dict(row) for row in connection.execute(
            "SELECT id, business_id, parent_id, revision FROM tasks WHERE business_id=? AND id IN " + marks(ids) + " ORDER BY id", (destination, *ids))]
        order = {identity: position for position, identity in enumerate(ids)}
        moved.sort(key=lambda row: order[row["id"]])
        return {"moved": moved, "counts": counts}

    @staticmethod
    def verify(uow, relocation, inspection):
        """Nothing of the move may remain in the source and no reference may be left dangling; raises (rolling back)."""
        connection, ids, source = uow.connection, relocation.ids, relocation.source
        left = connection.execute("SELECT COUNT(*) FROM tasks WHERE business_id IS ? AND id IN " + marks(ids), (source, *ids)).fetchone()[0]
        left += connection.execute("SELECT COUNT(*) FROM thread_messages WHERE business_id IS ? AND item_type='task' AND item_id IN "
                                   + marks(ids), (source, *ids)).fetchone()[0]
        left += connection.execute("SELECT COUNT(*) FROM thread_messages WHERE business_id=? AND item_type='task' AND item_id IN "
                                   + marks(ids) + " AND reference_namespace IS NULL", (relocation.destination, *ids)).fetchone()[0]
        if left:
            raise DomainError("relocation_failed", "The move left records behind; nothing was changed")
        present = connection.execute("SELECT COUNT(*) FROM tasks WHERE business_id=? AND id IN " + marks(ids), (relocation.destination, *ids)).fetchone()[0]
        if present != len(ids):
            raise DomainError("relocation_failed", "The move did not reach every Task; nothing was changed")
        for table in sorted(FOREIGN_KEY_CHILDREN & inspection.rows["tables"]):
            if connection.execute(f'PRAGMA foreign_key_check("{table}")').fetchone() is not None:
                raise DomainError("relocation_failed", "The move would leave a dangling reference; nothing was changed")


# -- stale references -----------------------------------------------------------------------------------------------


_UNSET = object()


def journeys(uow, task_id):
    """Every recorded Task journey of this id, keyed by the Task's immutable creation time: ``{created_at: [move, ...]}`` in
    order, or ``None`` when the bounded history overflows (a Task is identified across moves by its id and creation time)."""
    history = uow.connection.execute("SELECT source_business_id, destination_business_id, task_created_at, moved_at FROM task_moves WHERE task_id=? "
                                     "ORDER BY seq LIMIT ?", (task_id, MAXIMUM_STEPS + 1)).fetchall()
    if len(history) > MAXIMUM_STEPS:
        return None
    found = {}
    for move in history:
        found.setdefault(move["task_created_at"], []).append(move)
    return found


def moved_location(uow, business_id, task_id):
    """The project that holds the Task a stale ``(business_id, task_id)`` reference once named, or ``None``.

    ``business_id`` is ``None`` for the Global scope, which is a real place a Task can be moved from: the walk below keeps
    "not started yet" apart from "at Global" so a Global -> Project move resolves like any other.

    A current Task with that exact scoped identity always wins and is never redirected. Otherwise the recorded moves of that
    task id are replayed in order as one Task's journey: it must be unambiguous - one Task whose journey includes the scope -
    and its last location must still hold that very Task. Any doubt, a bounded history overflow or a missing current record
    returns ``None``. A found location is always a project (a destination is never Global)."""
    if uow.connection.execute("SELECT 1 FROM tasks WHERE business_id IS ? AND id=?", (business_id, task_id)).fetchone():
        return None
    found = journeys(uow, task_id)
    if found is None:
        return None
    candidates = [created for created, moves in found.items() if any(move["source_business_id"] == business_id for move in moves)]
    if len(candidates) != 1:
        return None
    created, location = candidates[0], _UNSET
    for move in found[created]:
        if location is _UNSET and move["source_business_id"] == business_id:
            location = move["source_business_id"]
        if location is not _UNSET and move["source_business_id"] == location:
            location = move["destination_business_id"]
    if location is _UNSET or location == business_id:
        return None
    current = uow.connection.execute("SELECT created_at FROM tasks WHERE business_id IS ? AND id=?", (location, task_id)).fetchone()
    return location if current is not None and current["created_at"] == created else None


def proven_journey(uow, business_id, task_id):
    """Where the Task now at ``(business_id, task_id)`` has been, as ``journey_spans`` lists it, or ``None`` when the audit
    cannot prove it.

    A Task whose bare id no recorded move of its own names has a proven, current-only stay. Anything the bounded audit cannot
    settle - a history that overflows, moves that are not one continuous journey ending here, or a Task that is not here -
    proves nothing: ``None`` is "unknown", never a stay. Readers that infer an association from a stay must fail closed on it;
    ``journey_spans`` keeps its whole-stay fallback for everyone else."""
    row = uow.connection.execute("SELECT created_at FROM tasks WHERE business_id IS ? AND id=?", (business_id, task_id)).fetchone()
    history = journeys(uow, task_id) if row is not None else None
    if history is None:
        return None
    moves = history.get(row["created_at"], [])
    if not moves:
        return [(business_id, row["created_at"], None)]
    stays, scope, since = [], moves[0]["source_business_id"], row["created_at"]
    for move in moves:
        if move["source_business_id"] != scope:
            return None
        stays.append((scope, since, move["moved_at"]))
        scope, since = move["destination_business_id"], move["moved_at"]
    return None if scope != business_id else [(scope, since, None), *stays]


def journey_spans(uow, business_id, task_id) -> list:
    """Where the Task now at ``(business_id, task_id)`` has been: ``[(scope, since, until), ...]``, the current scope first
    (``until=None``) and then every earlier stay in order, each with the time it began and the time it was left (Global is
    ``None``; a Task's first stay begins when it was created).

    Only moves of this very Task count. An unrelated Task that reuses the bare id in a scope this one left or entered has
    another creation time, and what it wrote before this Task arrived or after it left lies outside the stay. A Task that
    never moved, whose history overflows or whose recorded moves are not one continuous journey has a single stay in its
    current scope (``proven_journey`` tells those apart)."""
    row = uow.connection.execute("SELECT created_at FROM tasks WHERE business_id IS ? AND id=?", (business_id, task_id)).fetchone()
    if row is None:
        return [(business_id, "", None)]
    return proven_journey(uow, business_id, task_id) or [(business_id, row["created_at"], None)]


__all__ = ["AdapterReport", "FOREIGN_KEY_CHILDREN", "Inspection", "Relocation", "RelocationAdapter", "TaskRelocation", "blocker",
           "digest", "journey_spans", "moved_location", "namespace", "proven_journey", "scope_of"]
