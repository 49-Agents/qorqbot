"""The timelines of Tasks, Projects and Global as agent tools.

``timeline_get`` reads what holds now on one item (or its whole history); ``timeline_file`` is how the configured coordinator
changes it. An item is ``{type, id, business_id}`` as ``thread_pins_set`` names a record: a Task (``task``, its id, its Project
or null), a Project (``project``, its id as both id and business_id) or Global (``global``, id ``global``, business_id null).

Who may read: a Task's timeline follows the ordinary ``tasks_get`` read of that Task; a Project's needs the caller to be a
participant of that Project; Global's is open to every agent. Who may file: the configured coordinator in person, never a
delegated identity, never the dashboard.

Filing is split so that a later review step can sit between the two halves: ``check`` is the caller's gate, the item's
existence and ``filing.check`` of the changes (it is what ``prepare`` runs and what lists the rows a removal or answer depends
on, to be pinned with ``pin``); ``file`` appends the changes. ``apply`` calls ``file`` with the plan's own arguments and proposer.
"""
from __future__ import annotations

from dataclasses import replace

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, ref, schema, timestamp
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.tasks.scope import task_namespace
from qorqut_operations.timeline import filing, state as timeline_state
from qorqut_operations.timeline.schema import ENTRIES, ITEM_TYPES, KINDS, QUESTIONS

from qorqut.domains.projects.access import business_participant
from qorqut.management.identity import configured_ordubashy

GET_DESCRIPTION = (
    "Reads the timeline of a Task, a Project or Global: what holds now, or its whole history. item names it: type (task, project "
    "or global), id (the Task id, the Project id, or \"global\") and business_id (the Project a Task is in, the Project's own id "
    "for a project, null for Global or a Global Task). By default it returns the state with the ids of its entries and questions "
    "(the ids timeline_file removes, answers and drops by): the latest goal, every decision and fact added and not removed, the "
    "latest progress note and the open questions for the owner, plus the same as text and the item's revision. With history true it "
    "pages every entry oldest first (limit, cursor), removed ones marked, with the owner's quoted words and their links, and the "
    "first page lists the item's subtasks, approvals and reports as one-line links. You may read a Task's timeline when you may "
    "read the Task, a Project's when you take part in the Project, and Global's always. Changes nothing.")
FILE_DESCRIPTION = (
    "Coordinator write. Changes the timeline of a Task, a Project or Global, which every agent that opens the item is then told. "
    "item names it as in timeline_get. changes is 1 to 10 of: {action: add, kind: goal|decision|fact|progress, text, refs?} "
    "(text 1 to 400 characters, one or two sentences; a goal and a progress note replace the previous one, a decision and a fact "
    "hold until removed); {action: remove, entry_id, reason?} (takes out an entry that no longer holds; read the ids with "
    "timeline_get); {action: ask, text, blocking, raised_by?} (an open question for the owner: a point the owner has yet to "
    "decide; blocking says whether work waits on it; raised_by owner or coordinator, default coordinator); {action: answer, "
    "question_id, kind: decision|fact, text} (files the answer and closes the question); {action: drop, question_id} (closes a "
    "question nobody needs answered). sources is up to ten {quote, ref}: the owner's own words behind the change (at most 1000 "
    "characters each) and a link to the message. The entries are never edited or deleted, so file at the most general level that "
    "fits and keep what you file true. The result is the item's new state; when it passes about 3,000 characters it asks you to "
    "remove what no longer holds.")

ITEM = {"type": "object", "additionalProperties": False, "required": ["type", "id", "business_id"], "properties": {
    "type": {"enum": list(ITEM_TYPES)},
    "id": {"type": "string", "minLength": 1, "maxLength": 300},
    "business_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 200}}}
TEXT = {"type": "string", "minLength": 1, "maxLength": filing.MAX_TEXT}
IDENTITY = {"type": "string", "minLength": 1, "maxLength": 100}
REFS = {"type": "array", "maxItems": 10, "items": schema({
    "type": {"type": "string", "pattern": "^[a-z][a-z_]{0,39}$"}, "id": {"type": "string", "minLength": 1, "maxLength": 300},
    "business_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 200}}, ("type", "id"))}
CHANGE = {"oneOf": [
    schema({"action": {"const": "add"}, "kind": {"enum": list(KINDS)}, "text": TEXT, "refs": REFS}, ("action", "kind", "text")),
    schema({"action": {"const": "remove"}, "entry_id": IDENTITY, "reason": {"type": "string", "maxLength": filing.MAX_TEXT}},
           ("action", "entry_id")),
    schema({"action": {"const": "ask"}, "text": TEXT, "blocking": {"type": "boolean"},
            "raised_by": {"enum": ["owner", "coordinator"]}}, ("action", "text", "blocking")),
    schema({"action": {"const": "answer"}, "question_id": IDENTITY, "kind": {"enum": ["decision", "fact"]}, "text": TEXT},
           ("action", "question_id", "kind", "text")),
    schema({"action": {"const": "drop"}, "question_id": IDENTITY}, ("action", "question_id"))]}
SOURCES = {"type": "array", "maxItems": filing.MAX_SOURCES, "items": schema({
    "quote": {"type": "string", "minLength": 1, "maxLength": filing.MAX_QUOTE},
    "ref": {"type": "string", "minLength": 1, "maxLength": 500}}, ("quote", "ref"))}
SCOPE = {"scope": {"enum": ["business", "global"]}}
GET_SCHEMA = schema({**SCOPE, "item": ITEM, "history": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                     "cursor": {"type": "string", "pattern": "^[0-9]{1,18}$"}}, ("item",))
FILE_SCHEMA = schema({"scope": {"const": "global"}, "item": ITEM,
                      "changes": {"type": "array", "minItems": 1, "maxItems": filing.MAX_CHANGES, "items": CHANGE},
                      "sources": SOURCES, "rationale": {"type": "string", "maxLength": 2000}}, ("item", "changes"))
LINKS_LIMIT = 50
NOT_READABLE = "This item does not exist or you may not read it"
CLOSED = {"forbidden", "policy_denied", "not_found", "invalid_principal", "record_moved"}


class Timeline:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application

    def register(self):
        common = dict(global_capability="read_global_context", supports_signoff=False, exposure=frozenset({"mcp"}))
        self.runtime.registry.register(OperationDescriptor(
            "timeline_get", GET_SCHEMA, description=GET_DESCRIPTION, effect_kind="read", read=self.get,
            scope_kinds=frozenset({"business", "global"}), resolve_namespace=task_namespace, **common))
        self.runtime.registry.register(OperationDescriptor(
            "timeline_file", FILE_SCHEMA, description=FILE_DESCRIPTION, effect_kind="records", prepare=self.prepare,
            apply=self.apply, scope_kinds=frozenset({"global"}), resolve_namespace=lambda ctx, args: "global", **common))
        self.runtime.timeline = self
        return self

    # -- the item and who may read it ------------------------------------------------------------------------------

    @staticmethod
    def exists(item, uow):
        """The item's title when it exists, without any caller's authority (``''`` for Global, which has none); else None."""
        kind, identity, business = item["type"], item["id"], item["business_id"]
        if kind == "global":
            return "" if identity == "global" and business is None else None
        if kind == "project":
            row = uow.connection.execute("SELECT name FROM businesses WHERE id=?", (identity,)).fetchone() if business == identity else None
            return row["name"] if row else None
        row = uow.connection.execute("SELECT title FROM tasks WHERE business_id IS ? AND id=?", (business, identity)).fetchone()
        return row["title"] if row else None

    def readable(self, ctx, item, uow):
        """The item's title (None for Global) once the caller's own read of it succeeds; ``not_found`` otherwise."""
        current = self.app.current_context(ctx, uow)
        title = self.exists(item, uow)
        if title is None:
            raise DomainError("not_found", NOT_READABLE)
        kind, business = item["type"], item["business_id"]
        if kind == "global":
            return None
        if kind == "project":
            if not business_participant(current, business, uow):
                raise DomainError("not_found", NOT_READABLE)
            return title
        # The ordinary authorized Task read, as a report's Task is checked: whoever may not read the Task has no timeline for it.
        scoped = replace(current, business_id=business, review_business_id=business)
        descriptor = self.app.registry.get("tasks_get")
        try:
            self.app.authorize(scoped, descriptor, (), uow, namespace="business:" + business if business else "global")
            task = descriptor.read(scoped, {"task_id": item["id"], "scope": "business" if business else "global", "notes_limit": 0},
                                   None, uow)["task"]
        except DomainError as error:
            if error.code in CLOSED:
                raise DomainError("not_found", NOT_READABLE) from None
            raise
        return task["title"]

    @staticmethod
    def further_reading(item, uow):
        """``{subtasks, approvals, reports}`` counts for a Task; empty for a Project or Global."""
        if item["type"] != "task":
            return {}
        connection, business, identity = uow.connection, item["business_id"], item["id"]
        count = lambda sql: connection.execute(sql, (business, identity)).fetchone()[0]
        return {"subtasks": count("SELECT COUNT(*) FROM tasks WHERE business_id IS ? AND parent_id=?"),
                "approvals": count("SELECT COUNT(*) FROM signoff_source_links WHERE source_business_id IS ? AND source_type='task' AND source_id=?"),
                "reports": count("SELECT COUNT(*) FROM signoff_report_tasks WHERE task_business_id IS ? AND task_id=?")}

    @staticmethod
    def links(item, uow):
        """The subtasks, approvals and reports of a Task as one-line links read from their own records."""
        if item["type"] != "task":
            return {}
        connection, business, identity = uow.connection, item["business_id"], item["id"]
        title = "COALESCE(s.display_title, json_extract(s.preview_json,'$.title'))"
        approvals = connection.execute(
            f"SELECT s.id, {title} AS title, s.state FROM signoff_source_links l JOIN signoffs s ON s.id=l.signoff_id "
            "WHERE l.source_business_id IS ? AND l.source_type='task' AND l.source_id=? ORDER BY s.created_at LIMIT ?",
            (business, identity, LINKS_LIMIT)).fetchall()
        reports = connection.execute(
            f"SELECT s.id, {title} AS title, s.state FROM signoff_report_tasks r JOIN signoffs s ON s.id=r.signoff_id "
            "WHERE r.task_business_id IS ? AND r.task_id=? ORDER BY s.created_at LIMIT ?", (business, identity, LINKS_LIMIT)).fetchall()
        subtasks = connection.execute(
            "SELECT id, title, status FROM tasks WHERE business_id IS ? AND parent_id=? ORDER BY created_at, id LIMIT ?",
            (business, identity, LINKS_LIMIT)).fetchall()
        return {"subtasks": [{"type": "task", "id": row["id"], "business_id": business, "title": row["title"], "status": row["status"]}
                             for row in subtasks],
                "approvals": [{"type": "signoff", "id": row["id"], "title": row["title"], "state": row["state"]} for row in approvals],
                "reports": [{"type": "signoff", "id": row["id"], "title": row["title"], "state": row["state"]} for row in reports]}

    # -- reading ---------------------------------------------------------------------------------------------------

    def get(self, ctx, args, options, uow):
        item = args["item"]
        title = self.readable(ctx, item, uow)
        held = timeline_state.state(uow.connection, item)
        result = {"item": held["item"], "revision": held["revision"]}
        if not args.get("history"):
            return {**result, "state": held, "text": timeline_state.render(held, title, self.further_reading(item, uow))}
        cursor = int(args["cursor"]) if args.get("cursor") else 0
        entries, after = timeline_state.history(uow.connection, item, args.get("limit", 50), cursor)
        return {**result, "entries": entries, "next_cursor": str(after) if after else None,
                **({"links": self.links(item, uow)} if not cursor else {})}

    # -- filing ----------------------------------------------------------------------------------------------------

    @staticmethod
    def permit(principal_id, uow):
        """Only the configured coordinator files; ``principal_id`` is the filer a plan was prepared for."""
        coordinator = configured_ordubashy(uow)
        if not coordinator or principal_id != coordinator:
            raise DomainError("forbidden", "Only the configured coordinator files on a timeline")

    def filer(self, ctx, uow):
        """The coordinator in person: never a delegated identity, never another agent or the owner's dashboard."""
        current = self.app.current_context(replace(ctx, business_id=None, review_business_id=None), uow)
        if current.delegation_assignment_id is not None or current.principal_kind != "agent":
            raise DomainError("forbidden", "Only the configured coordinator in person files on a timeline")
        self.permit(current.principal_id, uow)
        return current

    def check(self, ctx, args, uow):
        """The gate of a filing and the rows its changes depend on: ``(title, pins)`` where ``pins`` lists, as
        ``{'entries': [...], 'questions': [...]}``, the stored rows a removal, answer or drop closes."""
        self.filer(ctx, uow)
        title = self.readable(ctx, args["item"], uow)
        return title, filing.check(uow.connection, args["item"], args["changes"])

    @staticmethod
    def pin(builder, item, pins):
        """Make a prepared filing depend on the rows its removals, answers and drops name, so one made stale by another filing
        ends as a conflict instead of applying. An add pins nothing."""
        for table, rows in ((ENTRIES, pins["entries"]), (QUESTIONS, pins["questions"])):
            for row in rows:
                builder.expect(ref(table, row["id"], item["business_id"], tables=builder.tables), row)

    def prepare(self, ctx, args, options, uow):
        self.check(ctx, args, uow)
        item = args["item"]
        return PlanBuilder(ctx, "timeline_file", args, f"File on the timeline of {item['type']} {item['id']}").build()

    def apply(self, ctx, plan, uow):
        return self.file(plan.principal_id, plan.arguments, uow)

    def file(self, principal_id, args, uow):
        """Append a filing's changes as ``principal_id`` and return the item's new state. The item must still exist, and every
        removal, answer and drop must still find its target standing."""
        self.permit(principal_id, uow)
        args = plain(args)
        item = args["item"]
        title = self.exists(item, uow)
        if title is None:
            raise DomainError("not_found", "The item no longer exists")
        made = filing.write(uow.connection, item, args["changes"], args.get("sources", []), created_by=principal_id, at=timestamp())
        held = timeline_state.state(uow.connection, item)
        result = {"item": held["item"], "revision": held["revision"], "filed": made, "state": held,
                  "text": timeline_state.render(held, title or None, self.further_reading(item, uow))}
        size = timeline_state.standing_size(held, title or None)
        if size > timeline_state.WARNING_CHARACTERS:
            result["warning"] = (f"This item's standing state is now {size} characters; about {timeline_state.WARNING_CHARACTERS} "
                                 "is the most worth carrying. Remove the entries that no longer hold.")
        return result
