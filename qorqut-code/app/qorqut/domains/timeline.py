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

import json
from dataclasses import replace

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, ref, schema, timestamp
from qorqut_operations.contracts import DomainError, FieldChange, PreviewResource, StructuredPreview, plain
from qorqut_operations.tasks.scope import task_namespace
from qorqut_operations.timeline import filing, state as timeline_state
from qorqut_operations.timeline.schema import ENTRIES, ITEM_TYPES, KINDS, QUESTIONS

from qorqut.domains.projects.access import business_participant
from qorqut.management.identity import configured_ordubashy, installation_owner

GET_DESCRIPTION = (
    "Reads the timeline of a Task, a Project or Global: what holds now, or its whole history. item names it: type (task, project "
    "or global), id (the Task id, the Project id, or \"global\") and business_id (the Project a Task is in, the Project's own id "
    "for a project, null for Global or a Global Task). By default it returns the state with the ids of its entries and questions "
    "(the ids timeline_file removes, answers and drops by): the latest goal, every decision and fact added and not removed, the "
    "latest progress note and the open questions for the owner, plus the same as text and the item's revision. With history true it "
    "pages every entry oldest first (limit, cursor), removed ones marked, with the owner's quoted words and their links, and the "
    "first page lists the questions already answered or dropped and the item's subtasks, approvals and reports as one-line links "
    "(approval and report titles are shown to the coordinator and the owner only). You may read a Task's timeline when you may "
    "read the Task, a Project's when you take part in the Project, and Global's always. Changes nothing.")
FILE_DESCRIPTION = (
    "Coordinator write. Changes the timeline of a Task, a Project or Global, which every agent that opens the item is then told. "
    "item names it as in timeline_get. changes is 1 to 10 of: {action: add, kind: goal|decision|fact|progress, text, refs?} "
    "(text 1 to 400 characters, one or two sentences; a goal and a progress note replace the previous one, a decision and a fact "
    "hold until removed; an add that repeats what already stands is skipped and listed under filed.skipped); {action: remove, "
    "entry_id, reason?} (takes out an entry that no longer holds, and removing the current goal or progress note leaves none; read the ids with "
    "timeline_get). An open question filed earlier is closed with {action: answer, question_id, kind: decision|fact, text} (files "
    "the answer) or {action: drop, question_id} (no answer needed). Ask the owner a new question with a Signoff "
    "(signoffs_input_create, its source the item), never on the timeline, and file the owner's answer with timeline_file_answer. "
    "sources is up to ten {quote, ref}: the owner's own words behind the change (at most 1000 "
    "characters each) and a link to the message. The entries are never edited or deleted, so file at the most general level that "
    "fits and keep what you file true. The result is the item's new state; when it passes about 3,000 characters it asks you to "
    "remove what no longer holds. During the trial every filing is first held for the owner's review: the call returns "
    "signoff_required, which means the filing was submitted for the owner's review and is filed by itself when the owner "
    "approves it, with no further call from you. Do not file it again; timeline_get lists the filings still waiting.")
ANSWER_DESCRIPTION = (
    "Coordinator write. Files the owner's answer to one of your question Signoffs on a timeline, at once and without review, "
    "since the words are the owner's: signoff_id is the answered question (signoffs_input_create, now answered), kind is "
    "decision or fact, text is the answer as it should hold (1 to 400 characters, one or two sentences). item names where it "
    "holds as in timeline_get; without it, the Task or Project the question was asked about, else Global or the Signoff's "
    "Project. The owner's own answer is quoted on the entry and linked to the Signoff. Each answer is filed once. File only "
    "an answer that settles a decision or fact; when the owner says the question is not needed or was settled already, file "
    "nothing. If unclear, please ask the user.")
REVIEW_NOTICE = ("Submitted for the owner's review. It is filed by itself when the owner approves it; do not file it again. "
                 "timeline_get lists the filings still waiting.")

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
ANSWER_SCHEMA = schema({"scope": {"const": "global"}, "signoff_id": {"type": "string", "format": "uuid"},
                       "kind": {"enum": ["decision", "fact"]}, "text": TEXT, "item": ITEM}, ("signoff_id", "kind", "text"))
ANSWER_REF = "signoff:"
LINKS_LIMIT = 50
NOT_READABLE = "This item does not exist or you may not read it"
CLOSED = {"forbidden", "policy_denied", "not_found", "invalid_principal", "record_moved"}


class Timeline:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application

    def register(self):
        common = dict(global_capability="read_global_context", exposure=frozenset({"mcp"}))
        self.runtime.registry.register(OperationDescriptor(
            "timeline_get", GET_SCHEMA, description=GET_DESCRIPTION, effect_kind="read", read=self.get, supports_signoff=False,
            scope_kinds=frozenset({"business", "global"}), resolve_namespace=task_namespace, **common))
        # During the trial a filing is held for the owner's approval and applied by the service as its proposer; an agent override
        # (``permissions_set``) for the coordinator replaces this standing mode, which is how the trial ends.
        self.runtime.registry.register(OperationDescriptor(
            "timeline_file", FILE_SCHEMA, description=FILE_DESCRIPTION, effect_kind="records", prepare=self.prepare,
            apply=self.apply, scope_kinds=frozenset({"global"}), resolve_namespace=lambda ctx, args: "global",
            supports_signoff=True, default_mode="signoff", project_result=self.project, **common))
        # The owner's answer to a question Signoff is the owner's own words, so filing it needs no second review.
        self.runtime.registry.register(OperationDescriptor(
            "timeline_file_answer", ANSWER_SCHEMA, description=ANSWER_DESCRIPTION, effect_kind="records", prepare=self.prepare_answer,
            apply=self.apply_answer, scope_kinds=frozenset({"global"}), resolve_namespace=lambda ctx, args: "global",
            supports_signoff=False, default_mode="allow", **common))
        # A removal pins its entry, which has no revision: it is gone for a filing once another filing has removed it.
        self.app.register_target_resolver(ENTRIES, self.entry_snapshot)
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
        # The item may sit outside the call's own scope, which is all the engine checked: the caller must reach the Task's
        # Project, or hold the Global read for a Global Task.
        if not (business in current.allowed_business_ids or business == current.business_id if business
                else descriptor.global_capability_selected(current.global_capabilities) is not None):
            raise DomainError("not_found", NOT_READABLE)
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

    def trusted(self, ctx, uow):
        """Whether the caller is the installation owner or the configured coordinator in person: the two who may read every
        approval, and so the only ones shown approval and report titles or the filings waiting for review."""
        current = self.app.current_context(replace(ctx, business_id=None, review_business_id=None), uow)
        if current.principal_kind == "human":
            return current.principal_id == installation_owner(uow)
        return current.delegation_assignment_id is None and current.principal_id == configured_ordubashy(uow)

    @staticmethod
    def links(item, uow, titles=True):
        """The subtasks, approvals and reports of a Task as one-line links read from their own records. An approval's title
        can carry what its own read would hide (an email subject, a Global proposal), so ``titles`` false leaves the titles
        of approvals and reports out; a subtask's title is what the Task read already lists."""
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
                **{name: [{"type": "signoff", "id": row["id"], **({"title": row["title"]} if titles else {}), "state": row["state"]}
                          for row in rows] for name, rows in (("approvals", approvals), ("reports", reports))}}

    # -- reading ---------------------------------------------------------------------------------------------------

    def get(self, ctx, args, options, uow):
        item = args["item"]
        title = self.readable(ctx, item, uow)
        held = timeline_state.state(uow.connection, item)
        result = {"item": held["item"], "revision": held["revision"]}
        cursor = int(args["cursor"]) if args.get("cursor") else 0
        # The coordinator and the owner also see what already waits for the owner's review, so nothing is filed twice.
        waiting = self.waiting(ctx, item, uow) if not cursor else []
        if waiting:
            result["waiting_for_review"] = waiting
        if not args.get("history"):
            text = timeline_state.render(held, title, self.further_reading(item, uow))
            if waiting:
                text += "\nFilings waiting for the owner's review (already submitted; do not file them again):\n" + "\n".join(
                    f"- [{row['id'][:8]}] " + "; ".join(self.sentence(line) for line in row["changes"]) for row in waiting)
            return {**result, "state": held, "text": text}
        entries, after = timeline_state.history(uow.connection, item, args.get("limit", 50), cursor)
        return {**result, "entries": entries, "next_cursor": str(after) if after else None,
                **({"closed_questions": timeline_state.closed_questions(uow.connection, item, LINKS_LIMIT),
                    "links": self.links(item, uow, self.trusted(ctx, uow))} if not cursor else {})}

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

    @staticmethod
    def current(item, uow):
        """Refuse an item in an archived Project: nothing new is filed on work that is no longer current."""
        business = item["business_id"]
        if business and uow.connection.execute("SELECT 1 FROM businesses WHERE id=? AND archived_at IS NOT NULL", (business,)).fetchone():
            raise DomainError("business_archived", "This project is archived; nothing is filed on its timelines")

    def check(self, ctx, args, uow):
        """The gate of a filing and the rows its changes depend on: ``(title, pins)`` where ``pins`` lists, as
        ``{'entries': [...], 'questions': [...]}``, the stored rows a removal, answer or drop closes."""
        self.filer(ctx, uow)
        title = self.readable(ctx, args["item"], uow)
        self.current(args["item"], uow)
        return title, filing.check(uow.connection, args["item"], args["changes"])

    @staticmethod
    def pin(builder, item, pins):
        """Make a prepared filing depend on the rows its removals, answers and drops name, so one made stale by another filing
        ends as a conflict instead of applying. An add pins nothing."""
        for table, rows in ((ENTRIES, pins["entries"]), (QUESTIONS, pins["questions"])):
            for row in rows:
                builder.expect(ref(table, row["id"], item["business_id"], tables=builder.tables), row)

    @staticmethod
    def entry_snapshot(ctx, resource, uow):
        """The entry a prepared removal named while it still stands; ``None`` once removed (or moved to another item)."""
        row = uow.connection.execute(
            f"SELECT * FROM {ENTRIES} WHERE id=? AND business_id IS ? AND action='add' AND NOT EXISTS "
            f"(SELECT 1 FROM {ENTRIES} r WHERE r.removes_id={ENTRIES}.id)", (resource.resource_id, resource.business_id)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def lines(changes, pins):
        """The changes of a filing as the owner reads them; a removal, answer or drop carries the text it names."""
        entries = {row["id"]: row for row in pins["entries"]}
        questions = {row["id"]: row for row in pins["questions"]}
        lines = []
        for change in changes:
            action = change["action"]
            if action == "add":
                lines.append({"action": "add", "kind": change["kind"], "text": change["text"]})
            elif action == "remove":
                row = entries[change["entry_id"]]
                lines.append({"action": "remove", "kind": row["kind"], "text": row["text"], "reason": change.get("reason", "")})
            elif action == "ask":
                lines.append({"action": "ask", "text": change["text"], "blocking": change["blocking"]})
            elif action == "answer":
                lines.append({"action": "answer", "kind": change["kind"], "text": change["text"], "question": questions[change["question_id"]]["text"]})
            else:
                lines.append({"action": "drop", "question": questions[change["question_id"]]["text"]})
        return lines

    def prepare(self, ctx, args, options, uow):
        title, pins = self.check(ctx, args, uow)
        item = args["item"]
        builder = PlanBuilder(ctx, "timeline_file", args, f"File on the timeline of {item['type']} \"{title}\"" if title else "File on the Global timeline")
        self.pin(builder, item, pins)
        lines = plain(self.lines(args["changes"], pins))
        # The same changes already waiting on this item would only make the owner decide them twice.
        if any(row["changes"] == lines for row in self.waiting(ctx, item, uow)):
            raise DomainError("validation_error", "These changes already wait for the owner's review; do not file them again")
        # What the owner reads: the item, one line per change and the quoted words, ahead of the generic field list.
        fields = {"item_type": item["type"], "item_id": item["id"], "item_business_id": item["business_id"], "item_title": title or "",
                  "changes": lines, "sources": args.get("sources", [])}
        resource = PreviewResource("timeline_filing", "filing", "Timeline filing")
        builder.previews.append(StructuredPreview(resource, tuple(FieldChange(path, None, value) for path, value in fields.items())))
        builder.resources.append(resource)
        return builder.build()

    @staticmethod
    def project(result):
        """What the coordinator is told when the filing was held for review."""
        if result.status != "signoff_required":
            return result
        return replace(result, warnings=(*result.warnings, REVIEW_NOTICE))

    # -- filings still waiting for review --------------------------------------------------------------------------

    def waiting(self, ctx, item, uow):
        """The ``timeline_file`` approvals on ``item`` that are not on the timeline yet, as ``{id, state, changes}``: the ones
        the owner has still to decide (``pending``) and the ones approved and about to be applied (``approved``), so a filing is
        not sent twice in the moment between the two. For the coordinator in person and the installation owner, never for
        another agent or another person. Read from the approvals themselves."""
        if not self.trusted(ctx, uow):
            return []
        found = []
        for row in uow.connection.execute(
                "SELECT id, state, preview_json, created_at FROM signoffs WHERE operation_name='timeline_file' "
                "AND state IN ('pending','approved') AND superseded_by_id IS NULL ORDER BY created_at, id").fetchall():
            fields = {}
            for change in json.loads(row["preview_json"]).get("changes", []):
                fields.update({field["path"]: field["after"] for field in change.get("fields", [])})
            if (fields.get("item_type"), fields.get("item_id"), fields.get("item_business_id")) == (item["type"], item["id"], item["business_id"]):
                found.append({"id": row["id"], "state": row["state"], "filed_at": row["created_at"], "changes": fields.get("changes", [])})
        return found

    @staticmethod
    def sentence(line):
        action, text = line["action"], line.get("text", "")
        if action == "add":
            return f"Add {line['kind']}: {text}"
        if action == "remove":
            return f"Remove {line['kind']}: {text}"
        if action == "ask":
            return f"Ask ({'blocking' if line['blocking'] else 'not blocking'}): {text}"
        if action == "answer":
            return f"Answer \"{line['question']}\" with a {line['kind']}: {text}"
        return f"Drop question: {line['question']}"

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
        self.current(item, uow)
        made = filing.write(uow.connection, item, args["changes"], args.get("sources", []), created_by=principal_id, at=timestamp())
        held = timeline_state.state(uow.connection, item)
        result = {"item": held["item"], "revision": held["revision"], "filed": made, "state": held,
                  "text": timeline_state.render(held, title or None, self.further_reading(item, uow))}
        size = timeline_state.standing_size(held, title or None)
        if size > timeline_state.WARNING_CHARACTERS:
            result["warning"] = (f"This item's standing state is now {size} characters; about {timeline_state.WARNING_CHARACTERS} "
                                 "is the most worth carrying. Remove the entries that no longer hold.")
        return result

    # -- the owner's answer to a question Signoff --------------------------------------------------------------------

    @staticmethod
    def owner_words(row, uow):
        """The owner's submitted answer as one quote: each question's chosen labels and written words. A single question keeps
        its written words in the Signoff note its decision names."""
        form, decision = json.loads(row["preview_json"]).get("input") or {}, json.loads(row["decision_json"] or "{}")
        def written(answer):
            if "notes" in answer:
                return answer.get("notes") or ""
            note = uow.connection.execute("SELECT body FROM signoff_notes WHERE id=? AND signoff_id=?",
                                          (answer.get("message_id"), row["id"])).fetchone() if answer.get("message_id") else None
            return note["body"] if note else ""
        def said(question, answer):
            labels = {choice["id"]: choice["label"] for choice in question.get("choices", [])}
            chosen = ", ".join(labels.get(choice, choice) for choice in answer.get("selected_choices") or [])
            return "; ".join(part for part in (chosen, written(answer).strip()) if part)
        if isinstance(decision.get("answers"), list):
            questions = {question["id"]: question for question in form.get("questions", [])}
            parts = [f"{questions.get(answer.get('question_id'), {}).get('prompt', '')}: {said(questions.get(answer.get('question_id'), {}), answer)}"
                     for answer in decision["answers"] if said(questions.get(answer.get("question_id"), {}), answer)]
        else:
            parts = [said(form, decision)]
        words = "\n".join(part for part in parts if part.strip(": ").strip())
        if not words:
            raise DomainError("validation_error", "This answer has no words or choices to file")
        return words[:filing.MAX_QUOTE]

    @staticmethod
    def asked_about(row, uow):
        """The item a question Signoff was asked about: its Task or Project source, else its Project, else Global."""
        for link in uow.connection.execute(
                "SELECT source_type, source_id, source_business_id FROM signoff_source_links WHERE signoff_id=? "
                "AND source_type IN ('task','project') ORDER BY source_type DESC, created_at, id", (row["id"],)).fetchall():
            return {"type": link["source_type"], "id": link["source_id"], "business_id": link["source_business_id"]}
        business = row["review_business_id"]
        return {"type": "project", "id": business, "business_id": business} if business else {"type": "global", "id": "global", "business_id": None}

    def answered(self, ctx, args, uow):
        """``(row, item, title, quote)`` for a filing of the owner's answer: the caller is the coordinator in person, the Signoff
        is the coordinator's own question, answered by the owner and not filed yet, and the item is readable and current."""
        current = self.filer(ctx, uow)
        row = uow.connection.execute("SELECT * FROM signoffs WHERE id=?", (args["signoff_id"],)).fetchone()
        if row is None or row["proposer_principal_id"] != current.principal_id or row["category"] != "input_request":
            raise DomainError("not_found", "No question of yours has this id")
        if row["state"] != "answered":
            raise DomainError("validation_error", "The owner has not answered this question")
        # Only the owner's own answer skips review; an answer given on the owner's behalf is filed with timeline_file.
        if json.loads(row["decision_json"] or "{}").get("by") != installation_owner(uow):
            raise DomainError("validation_error", "This answer was not given by the owner; file it with timeline_file")
        reference = ANSWER_REF + row["id"]
        if uow.connection.execute(f"SELECT 1 FROM {ENTRIES} WHERE sources_json LIKE ? LIMIT 1", (f'%"{reference}"%',)).fetchone():
            raise DomainError("validation_error", "This answer is already on a timeline")
        item = args.get("item") or self.asked_about(row, uow)
        title = self.readable(ctx, item, uow)
        self.current(item, uow)
        return row, item, title, self.owner_words(row, uow)

    def prepare_answer(self, ctx, args, options, uow):
        row, item, title, _ = self.answered(ctx, args, uow)
        builder = PlanBuilder(ctx, "timeline_file_answer", args,
                              f"File the owner's answer on the timeline of {item['type']} \"{title}\"" if title else "File the owner's answer on the Global timeline")
        return builder.build()

    def apply_answer(self, ctx, plan, uow):
        args = plain(plan.arguments)
        row, item, title, quote = self.answered(ctx, args, uow)
        return self.file(plan.principal_id, {"item": item, "changes": [{"action": "add", "kind": args["kind"], "text": args["text"]}],
                                             "sources": [{"quote": quote, "ref": ANSWER_REF + row["id"]}]}, uow)
