"""Host-owned references that follow a relocated Task, and the bindings that make a relocation impossible.

The Operations engine moves core Task storage (``qorqut_operations.tasks.relocation``). This adapter covers what only the
host knows about: the owner-Ordubashy private conversation and the Focus card of a Task, plus an inventory of every other
stored reference to a Task. A reference is either

* **followed** - rewritten to the destination identity in the same transaction (private conversations, Focus cards and their
  card bindings),
* **historical** - an immutable payload, a message reference, a delivery, a checkpoint finding or a terminal job that names the
  old scope; these stay as written and remain navigable through the audited relocation resolver, or
* **blocking** - an active binding this operation cannot carry without changing its meaning: an extension's own handoff rows,
  pending approvals, queued or running jobs and routines, active delegated assignments and Overseer development bindings.

The move fails as a whole when any blocking binding exists. A foreign key that points at ``tasks`` and is not classified
below with rows naming a moved Task is also a blocker, so a dependency added later can never be silently orphaned.

A blocking binding must name the selected Task itself, not merely a string equal to its id. ``task_references`` therefore
recognizes only typed references - ``{type: task, id}``, ``{resource_type: tasks, resource_id}``, ``{task_id}``,
``{item_type: task, item_id}`` and ``task_ids`` - and pairs each with its effective scope: the scope the reference names
itself (``business_id``, ``namespace`` or ``scope: global``) or inherits from the enclosing object that does, otherwise the
scope of the job, routine, approval or assignment that holds it. Delegated assignments also list bare Task ids
(``links.tasks``, ``scope.source_ids.tasks``, ``scope.record_ids.tasks``) in their own scope. Free text, and a reference that
names another scope, are not bindings. The scan of each table is complete or the request fails with ``result_too_large``; a move
is never granted from a partial scan.
"""

from __future__ import annotations

import json

from qorqut_operations.contracts import DomainError
from qorqut_operations.tasks.relocation import (FOREIGN_KEY_CHILDREN, AdapterReport, Relocation, blocker, digest,
                                                existing_tables, marks, scope_of)

# Core tables whose rows follow a Task and are carried by the Operations helper (its schema inventory test names them).
CORE_CHILDREN = FOREIGN_KEY_CHILDREN
# Every stored column that names a Task, by how a relocation treats it. ``followed`` columns are rewritten with the move,
# ``blocking`` ones make a move with live rows impossible, ``historical`` ones record what was true when written and stay as
# stored (their readers navigate through the audited location resolver or re-evaluate on the next check). A new column that
# names a Task must be added here; ``test_task_move_inventory`` fails until it is classified, so no dependency can be
# orphaned unnoticed.
TASK_COLUMNS = {
    "task_completions.task_id": "followed", "task_snooze_wakes.task_id": "followed",
    "crm_entries.record_task_id": "followed", "crm_activity_events.task_id": "followed",
    "overseer_development_tasks.task_id": "blocking", "overseer_development_tasks.task_ref_json": "blocking",
    "agent_checkpoint_findings.task_id": "historical", "system_incident_impacts.task_id": "historical",
    "task_moves.task_id": "audit", "task_moves.task_created_at": "audit",
    "attention_memberships.task_id": "followed", "attention_memberships.task_created_at": "followed",
    "attention_memberships.task_business_id": "followed",
    "agent_attention.work_task_json": "followed", "attention_workflows.work_task_json": "followed",
    "attention_membership_events.task_id": "historical", "attention_membership_events.task_created_at": "historical",
    "attention_membership_events.task_business_id": "historical",
}
# Typed item columns (``item_type``/``source_type`` plus an id): the stores that hold a Task through a type column.
TYPED_REFERENCES = {
    "signoff_source_links": "followed",
    "thread_messages": "followed", "thread_follows": "followed", "item_tags": "followed", "relationships": "followed",
    "project_item_links": "followed", "inbox_subscriptions": "followed", "owner_conversations": "followed",
    "focus_cards": "followed", "focus_notes": "historical", "inbox_events": "historical", "inbox_deliveries": "historical",
}
ACTIVE_JOB_STATES = ("queued", "running", "applying", "waiting_signoff", "cancel_requested")
ACTIVE_ASSIGNMENT_STATES = ("queued", "running", "waiting", "blocked", "paused")
ACTIVE_SIGNOFF_STATES = ("pending", "approved")
ACTIVE_ROUTINE_STATUSES = ("active", "paused")
MAXIMUM_ROWS = 5_000


def source_key(kind, business_id, identity) -> str:
    """The stable per-object key of a private conversation or Focus card (``private_conversations.source_key``)."""
    return "source:" + (business_id or "global") + ":" + kind + ":" + identity


def strings(value):
    """Every string in a JSON value, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def decoded(text):
    """A stored JSON value (text or bytes) decoded; anything that is not JSON is returned as it was stored."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    try:
        return json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return text


def explicit_scope(reference, default):
    """The scope a reference object names itself (``business_id``, ``namespace`` or ``scope: global``), else ``default``."""
    if "business_id" in reference:
        return reference["business_id"]
    if isinstance(reference.get("namespace"), str):
        valid, scope = scope_of(reference["namespace"])
        if valid:
            return scope
    return None if reference.get("scope") == "global" else default


def task_references(value, default):
    """``(task id, effective scope)`` of every typed Task reference in a decoded JSON value, at any depth.

    The effective scope of a dictionary is what it names itself (``explicit_scope``), else the one it inherits from the
    enclosing dictionary or, at the top, ``default``. Its typed ids are emitted in that scope and it is passed to every child,
    so ``{business_id: A, items: [{type: task, id}]}`` binds the Task in A, while a child that names another scope overrides
    the inherited one."""
    if isinstance(value, dict):
        scope = explicit_scope(value, default)
        named = [value[id_key] for key, kind, id_key in (("type", "task", "id"), ("resource_type", "tasks", "resource_id"),
                                                         ("item_type", "task", "item_id"))
                 if value.get(key) == kind and isinstance(value.get(id_key), str)]
        if isinstance(value.get("task_id"), str):
            named.append(value["task_id"])
        if isinstance(value.get("task_ids"), list):
            named.extend(item for item in value["task_ids"] if isinstance(item, str))
        for identity in named:
            yield identity, scope
        for item in value.values():
            yield from task_references(item, scope)
    elif isinstance(value, list):
        for item in value:
            yield from task_references(item, default)


def rewrite(value, source, destination, ids):
    """``value`` with every exact ``{type: task, id, business_id: source}`` reference moved to the destination; ``(value, n)``."""
    if isinstance(value, dict):
        if value.get("type") == "task" and value.get("id") in ids and value.get("business_id") == source:
            return {**value, "business_id": destination}, 1
        count, result = 0, {}
        for key, item in value.items():
            result[key], found = rewrite(item, source, destination, ids)
            count += found
        return result, count
    if isinstance(value, list):
        count, result = 0, []
        for item in value:
            changed, found = rewrite(item, source, destination, ids)
            result.append(changed)
            count += found
        return result, count
    return value, 0


class HostRelocation:
    """The one ``RelocationAdapter`` of this host."""

    name = "host"

    def __init__(self, application=None):
        self.application = application

    # -- inspection ------------------------------------------------------------------------------------------------

    def inspect(self, uow, relocation: Relocation) -> AdapterReport:
        connection, tables = uow.connection, existing_tables(uow)
        ids, blockers, counts, material = relocation.ids, [], {"private_messages": 0, "focus_cards": 0}, {}
        conversations, cards = self.conversations(connection, tables, relocation), self.cards(connection, tables, relocation)
        material["conversations"] = digest([[row["id"], row["revision"], row["source_key"], row["latest_sequence"]] for row in conversations])
        material["cards"] = digest([[row["id"], row["revision"], row["source_key"]] for row in cards])
        links = self.input_links(connection, tables, relocation)
        material['input_links'] = digest([list(row) for row in links])
        counts['input_links'] = len(links)
        for link in links:
            if connection.execute('SELECT 1 FROM signoff_source_links WHERE signoff_id=? AND source_type=\'task\' '
                    'AND source_id=? AND source_business_id IS ?',
                    (link['signoff_id'], link['source_id'], relocation.destination)).fetchone():
                blockers.append(blocker('input_link_exists', 'The destination already has this input dependency', link['source_id']))
        for row in conversations:
            counts["private_messages"] += connection.execute(
                "SELECT COUNT(*) FROM owner_conversation_messages WHERE conversation_id=?", (row["id"],)).fetchone()[0]
            key = source_key("task", relocation.destination, row["source_id"])
            if connection.execute("SELECT 1 FROM owner_conversations WHERE source_key=?", (key,)).fetchone():
                blockers.append(blocker("conversation_exists", "The destination project already has a private conversation for this Task",
                                        row["source_id"]))
        counts["focus_cards"] = len(cards)
        for row in cards:
            if row["rewritten_key"] and connection.execute("SELECT 1 FROM focus_cards WHERE source_key=?", (row["rewritten_key"],)).fetchone():
                blockers.append(blocker("focus_card_exists", "The destination project already has a Focus card for this Task",
                                        self.card_task(row, relocation)))
        blockers.extend(self.unclassified_children(connection, tables, relocation))
        blockers.extend(self.active_bindings(connection, tables, relocation, material))
        attention = self.attention_rows(connection, tables, relocation)
        if {"agent_attention", "attention_memberships", "attention_workflows"} & tables:
            material["attention"] = digest([[table, *list(row)] for table, row in attention])
            counts["attention_references"] = len(attention)
        for table, row in attention:
            if table == "attention_memberships" and connection.execute(
                "SELECT 1 FROM attention_memberships WHERE agent_id=? AND task_business_id IS ? AND task_id=?",
                (row["agent_id"], relocation.destination, row["task_id"])).fetchone():
                blockers.append(blocker("attention_membership_exists", "The destination already has this Attention membership", row["task_id"]))
        return AdapterReport(counts, blockers, material)

    @staticmethod
    def attention_rows(connection, tables, relocation):
        """Snapshot all mutable Attention references that follow these scoped Tasks."""
        result, ids = [], set(relocation.ids)
        fields = {"agent_attention": ("focus_json", "work_task_json"),
                  "attention_workflows": ("work_task_json", "related_refs_json")}
        if "attention_memberships" in tables:
            rows = connection.execute("SELECT * FROM attention_memberships WHERE task_business_id IS ? AND task_id IN "
                                      + marks(relocation.ids) + " ORDER BY id", (relocation.source, *relocation.ids)).fetchall()
            result.extend(("attention_memberships", row) for row in rows)
        for table, columns in fields.items():
            if table not in tables:
                continue
            for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                if any(row[field] and rewrite(decoded(row[field]), relocation.source, relocation.destination, ids)[1]
                       for field in columns):
                    result.append((table, row))
        if len(result) > MAXIMUM_ROWS:
            raise DomainError("result_too_large", "Too many Attention references to move safely")
        return result

    @staticmethod
    def conversations(connection, tables, relocation):
        if "owner_conversations" not in tables:
            return []
        ids = relocation.ids
        return connection.execute(
            "SELECT * FROM owner_conversations WHERE source_type='task' AND business_id IS ? AND source_id IN " + marks(ids)
            + " AND peer_agent_id IS NULL ORDER BY id", (relocation.source, *ids)).fetchall()

    @staticmethod
    def card_task(row, relocation):
        for field in ("source_json", "reference_json", "context_json"):
            reference = decoded(row[field]) if row[field] else None
            if isinstance(reference, dict) and reference.get("type") == "task" and reference.get("id") in relocation.ids:
                return reference["id"]
        return row["source_key"].rsplit(":", 1)[-1]

    def cards(self, connection, tables, relocation):
        """Focus cards bound to a moved Task through their source key or an exact task reference inside their stored JSON."""
        if "focus_cards" not in tables:
            return []
        ids, found = set(relocation.ids), []
        keys = {source_key("task", relocation.source, identity): identity for identity in relocation.ids}
        for row in connection.execute("SELECT * FROM focus_cards ORDER BY id"):
            row = dict(row)
            row["rewritten_key"] = None
            if row["source_key"] in keys:
                row["rewritten_key"] = source_key("task", relocation.destination, keys[row["source_key"]])
            touched = row["rewritten_key"] is not None
            for field in ("source_json", "reference_json", "context_json", "references_json"):
                if row[field]:
                    _, count = rewrite(decoded(row[field]), relocation.source, relocation.destination, ids)
                    touched = touched or bool(count)
            if touched:
                found.append(row)
        if len(found) > MAXIMUM_ROWS:
            raise_too_many()
        return found

    @staticmethod
    def unclassified_children(connection, tables, relocation):
        """Foreign keys to ``tasks`` that no adapter classifies: rows naming a moved Task make the move impossible."""
        found = []
        for table in sorted(tables - CORE_CHILDREN - {"attention_memberships"}):
            if table.startswith("sqlite_"):
                continue
            groups: dict[int, list] = {}
            for fk in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
                if fk["table"] == "tasks":
                    groups.setdefault(fk["id"], []).append((fk["from"], fk["to"]))
            for pairs in groups.values():
                id_column = next((source for source, target in pairs if target in (None, "id")), None)
                scope_column = next((source for source, target in pairs if target == "business_id"), None)
                if id_column is None:
                    continue
                clause = f'"{scope_column}" IS ? AND ' if scope_column else ""
                parameters = ([relocation.source] if scope_column else []) + list(relocation.ids)
                count = connection.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {clause}"{id_column}" IN ' + marks(relocation.ids),
                                           parameters).fetchone()[0]
                if count:
                    found.append(blocker("unsupported_binding",
                                         f"{count} active {table.replace('_', ' ')} record(s) are bound to these Tasks and cannot be moved "
                                         "with them; resolve or remove them first"))
        return found

    def active_bindings(self, connection, tables, relocation, material):
        """Active work that names a moved Task by a typed, scoped reference. Each table is read completely or the request fails."""
        ids, source, found = set(relocation.ids), relocation.source, []

        def mentions(parts, default):
            return any(identity in ids and scope == source for part in parts for identity, scope in task_references(part, default))

        def active(table, state_column, states, columns, label):
            if table not in tables:
                return []
            selected = ", ".join(f'"{column}"' for column in columns)
            rows = connection.execute(f'SELECT "id", {selected} FROM "{table}" WHERE "{state_column}" IN ' + marks(states)
                                      + " ORDER BY \"id\" LIMIT ?", (*states, MAXIMUM_ROWS + 1)).fetchall()
            if len(rows) > MAXIMUM_ROWS:
                raise DomainError("result_too_large", f"Too many active {label} to check them for these Tasks; finish or cancel some first")
            return rows

        def report(table, label, hits):
            material[table] = [len(hits), digest([[identity] for identity in hits])]
            if hits:
                found.append(blocker("unsupported_binding", f"{len(hits)} active {label} name these Tasks; finish or cancel them first"))

        label = "approval proposals"
        hits = []
        for row in active("signoffs", "state", ACTIVE_SIGNOFF_STATES, ("review_business_id", "change_set", "expected_targets_json"), label):
            plan = decoded(row["change_set"])
            plan = plan if isinstance(plan, dict) else {}
            valid, scope = scope_of(plan.get("target_namespace"))
            if mentions((plan.get("arguments"), plan.get("expected_targets"), plan.get("changes"), decoded(row["expected_targets_json"])),
                        scope if valid else row["review_business_id"]):
                hits.append(row["id"])
        report("signoffs", label, hits)
        for table, state_column, states, label in (("jobs", "state", ACTIVE_JOB_STATES, "background jobs"),
                                                    ("routines", "status", ACTIVE_ROUTINE_STATUSES, "routines")):
            rows = active(table, state_column, states, ("business_id", "arguments_json"), label)
            report(table, label, [row["id"] for row in rows if mentions((decoded(row["arguments_json"]),), row["business_id"])])
        label = "delegated assignments"
        hits = []
        for row in active("operational_assignments", "state", ACTIVE_ASSIGNMENT_STATES, ("business_id", "scope_json", "links_json", "plan_json"), label):
            scope, links = decoded(row["scope_json"]) or {}, decoded(row["links_json"]) or {}
            if mentions((scope, links, decoded(row["plan_json"])), row["business_id"]) or self.listed(row, scope, links, ids, source):
                hits.append(row["id"])
        report("operational_assignments", label, hits)
        if "overseer_development_tasks" in tables:
            rows = connection.execute("SELECT request_key, task_id, task_ref_json FROM overseer_development_tasks WHERE task_id IN "
                                      + marks(relocation.ids), relocation.ids).fetchall()
            count = sum(1 for row in rows if (decoded(row["task_ref_json"]) or {}).get("business_id") == source)
            material["overseer_development_tasks"] = [len(rows), digest([[row["request_key"], row["task_id"]] for row in rows])]
            if count:
                found.append(blocker("unsupported_binding", f"{count} Overseer development binding(s) point at these Tasks"))
        return found

    @staticmethod
    def listed(row, scope, links, ids, source):
        """Whether an assignment lists a moved Task among its bare Task ids. The ids belong to the assignment's own reach: its
        project, or for a Global assignment Global and the projects it may work in."""
        reach = row["business_id"] == source or (row["business_id"] is None and source in (scope.get("allowed_business_ids") or ()))
        if not reach:
            return False
        named = [*(links.get("tasks") or ()), *((scope.get("source_ids") or {}).get("tasks") or ()), *((scope.get("record_ids") or {}).get("tasks") or ())]
        return any(isinstance(identity, str) and identity in ids for identity in named)

    # -- application -----------------------------------------------------------------------------------------------

    @staticmethod
    def input_links(connection, tables, relocation):
        if 'signoff_source_links' not in tables:
            return []
        return connection.execute("SELECT * FROM signoff_source_links WHERE source_type='task' AND source_business_id IS ? AND source_id IN "
            + marks(relocation.ids) + ' ORDER BY id', (relocation.source, *relocation.ids)).fetchall()

    def apply(self, uow, relocation: Relocation, at: str):
        connection, tables = uow.connection, existing_tables(uow)
        ids, source, destination = set(relocation.ids), relocation.source, relocation.destination
        counts = {"private_messages": 0, "focus_cards": 0}
        links = self.input_links(connection, tables, relocation)
        for link in links:
            connection.execute('UPDATE signoff_source_links SET source_business_id=?,source_namespace=?,revision=revision+1 WHERE id=?',
                (destination, 'business:' + destination if destination else 'global', link['id']))
        counts['input_links'] = len(links)
        for row in self.conversations(connection, tables, relocation):
            updated = connection.execute(
                "UPDATE owner_conversations SET business_id=?, source_key=?, revision=revision+1, updated_at=? WHERE id=? AND revision=?",
                (destination, source_key("task", destination, row["source_id"]), at, row["id"], row["revision"])).rowcount
            if updated != 1:
                raise_changed()
            counts["private_messages"] += connection.execute(
                "SELECT COUNT(*) FROM owner_conversation_messages WHERE conversation_id=?", (row["id"],)).fetchone()[0]
        for row in self.cards(connection, tables, relocation):
            values = {}
            for field in ("source_json", "reference_json", "context_json", "references_json"):
                if row[field]:
                    changed, count = rewrite(decoded(row[field]), source, destination, ids)
                    if count:
                        values[field] = json.dumps(changed)
            if row["rewritten_key"]:
                values["source_key"] = row["rewritten_key"]
            assignments = ", ".join(f"{name}=?" for name in values)
            updated = connection.execute(
                f"UPDATE focus_cards SET {assignments}, revision=revision+1, updated_at=? WHERE id=? AND revision=?",
                (*values.values(), at, row["id"], row["revision"])).rowcount
            if updated != 1:
                raise_changed()
            uow.next_sequence("focus_board")
            counts["focus_cards"] += 1
        attention = self.attention_rows(connection, tables, relocation)
        if {"agent_attention", "attention_memberships", "attention_workflows"} & tables:
            counts["attention_references"] = len(attention)
        for table, row in attention:
            if table == "attention_memberships":
                updated = connection.execute("UPDATE attention_memberships SET task_business_id=?, revision=revision+1 WHERE id=? AND revision=?",
                                             (destination, row["id"], row["revision"])).rowcount
            else:
                fields = ("focus_json", "work_task_json") if table == "agent_attention" else ("work_task_json", "related_refs_json")
                values = {}
                for field in fields:
                    if row[field]:
                        value, found = rewrite(decoded(row[field]), source, destination, ids)
                        if found:
                            values[field] = json.dumps(value)
                assignments = ", ".join(f"{name}=?" for name in values)
                predicate = "agent_id=?" if table == "agent_attention" else "agent_id=? AND run_id=?"
                key = (row["agent_id"],) if table == "agent_attention" else (row["agent_id"], row["run_id"])
                updated = connection.execute(f"UPDATE {table} SET {assignments}, revision=revision+1, updated_at=? WHERE {predicate} AND revision=?",
                                             (*values.values(), at, *key, row["revision"])).rowcount
            if updated != 1:
                raise_changed()
        return counts


def raise_changed():
    raise DomainError("revision_conflict", "A private conversation or Focus card changed during the move; preview it again")


def raise_too_many():
    raise DomainError("result_too_large", "Too many Focus cards reference these Tasks to move them safely")


__all__ = ["ACTIVE_JOB_STATES", "HostRelocation", "rewrite", "source_key"]
