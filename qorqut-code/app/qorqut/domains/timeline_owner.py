"""The owner's view of timelines: one item read for the dashboard, and the list of every open question.

``timeline_view`` is ``timeline_get`` for the owner's dashboard (state, or the history with the quoted words and links);
``timeline_questions_list`` lists the open questions of Global, every Project and every Task the owner can read, blocking ones
first. Both are read-only and owner-only. ``timeline_question_answer`` lets the owner answer an open question in their own
words: the answer is filed at once as a decision or fact on the question's item and closes the question, with no review (the
words are the owner's), and the coordinator gets a required inbox update. ``timeline_file`` is never offered here."""
from __future__ import annotations

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, ref, schema, timestamp
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.discussion.events import emit_event
from qorqut_operations.timeline import filing, state as timeline_state
from qorqut_operations.timeline.schema import QUESTIONS

from qorqut.domains.timeline import GET_SCHEMA, IDENTITY, LINKS_LIMIT, TEXT
from qorqut.management.identity import configured_ordubashy

VIEW_DESCRIPTION = (
    "Owner dashboard read of the timeline of a Task, a Project or Global: item names it as in timeline_get. Returns the title, "
    "the state (goal, decisions, facts, progress, open questions) and, with history true, every entry oldest first with removed "
    "ones marked, the owner's quoted words, and the item's subtasks, approvals and reports as links. Changes nothing.")
QUESTIONS_DESCRIPTION = (
    "Owner dashboard read of every open question across Global, the Projects and the Tasks the owner can read, blocking ones "
    "first, each with the item it belongs to. Changes nothing.")
VIEW_SCHEMA = GET_SCHEMA
QUESTIONS_SCHEMA = schema({"scope": {"const": "global"}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}})
ANSWER_DESCRIPTION = (
    "Owner dashboard write: answer one open question in the owner's own words. The answer is filed at once as a decision (the "
    "default) or a fact on the question's item, with the owner's words as its source, and closes the question; it is not held "
    "for review. The coordinator gets a required inbox update.")
ANSWER_SCHEMA = schema({"scope": {"const": "global"}, "question_id": IDENTITY, "text": TEXT,
                        "kind": {"enum": ["decision", "fact"]}}, ("question_id", "text"))
ANSWER_SOURCE = "owner:open-questions"
EVENT_SOURCE = "timeline_questions"
LIMIT = 200


class TimelineOwner:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application

    def register(self):
        common = dict(supports_signoff=False, effect_kind="read", owner_only=True, exposure=frozenset({"dashboard"}),
                      scope_kinds=frozenset({"global"}), global_capability="read_global_context",
                      resolve_namespace=lambda ctx, args: "global")
        self.runtime.registry.register(OperationDescriptor(
            "timeline_view", VIEW_SCHEMA, description=VIEW_DESCRIPTION, read=lambda c, a, o, u: self.view(c, a["item"], u, a), **common))
        self.runtime.registry.register(OperationDescriptor(
            "timeline_questions_list", QUESTIONS_SCHEMA, description=QUESTIONS_DESCRIPTION, read=self.questions, **common))
        self.runtime.registry.register(OperationDescriptor(
            "timeline_question_answer", ANSWER_SCHEMA, description=ANSWER_DESCRIPTION, prepare=self.prepare_answer,
            apply=self.apply_answer, **{**common, "effect_kind": "records"}))
        self.app.timeline_owner = self
        return self

    def view(self, ctx, item, uow, args=None):
        """The item's title, state and, with ``history``, its entries and links. ``not_found`` when it is not readable."""
        args = args or {}
        timeline = self.runtime.timeline
        title = timeline.readable(ctx, item, uow)
        held = timeline_state.state(uow.connection, item)
        result = {"item": held["item"], "title": title, "revision": held["revision"], "state": held,
                  "further": timeline.further_reading(item, uow)}
        if args.get("history"):
            cursor = int(args["cursor"]) if args.get("cursor") else 0
            entries, after = timeline_state.history(uow.connection, item, args.get("limit", 50), cursor)
            result.update(entries=entries, next_cursor=str(after) if after else None,
                          **({"closed_questions": timeline_state.closed_questions(uow.connection, item, LINKS_LIMIT),
                              "links": timeline.links(item, uow, timeline.trusted(ctx, uow))} if not cursor else {}))
        return result

    def questions(self, ctx, args, options, uow):
        rows = uow.connection.execute(
            f"SELECT * FROM {QUESTIONS} WHERE state='open' ORDER BY blocking DESC, sequence, id").fetchall()
        limit, items, truncated = args.get("limit", LIMIT), [], False
        for row in rows:
            item = {"type": row["item_type"], "id": row["item_id"], "business_id": row["business_id"]}
            try:
                title = self.runtime.timeline.readable(ctx, item, uow)
            except DomainError as error:
                if error.code == "not_found":
                    continue
                raise
            if len(items) >= limit:
                # A further readable question exists: only matching rows count, never the ones filtered out.
                truncated = True
                break
            project = (uow.connection.execute("SELECT name FROM businesses WHERE id=?", (row["business_id"],)).fetchone()
                       if row["business_id"] else None)
            items.append({"id": row["id"], "text": row["text"], "blocking": bool(row["blocking"]), "raised_by": row["raised_by"],
                          "created_at": row["created_at"], "source": item, "title": title or "Global",
                          "business_name": project["name"] if project else "Global"})
        return {"items": items, "truncated": truncated}

    # -- the owner answers an open question ---------------------------------------------------------------------------

    def open_question(self, ctx, question_id, uow):
        """The open question and its item, readable by the owner on an item that is still current."""
        row = uow.connection.execute(f"SELECT * FROM {QUESTIONS} WHERE id=?", (question_id,)).fetchone()
        if row is None:
            raise DomainError("not_found", "This question does not exist")
        if row["state"] != "open":
            raise DomainError("validation_error", "This question is already closed")
        item = {"type": row["item_type"], "id": row["item_id"], "business_id": row["business_id"]}
        title = self.runtime.timeline.readable(ctx, item, uow)
        self.runtime.timeline.current(item, uow)
        return dict(row), item, title

    def prepare_answer(self, ctx, args, options, uow):
        row, item, title = self.open_question(ctx, args["question_id"], uow)
        builder = PlanBuilder(ctx, "timeline_question_answer", args, f"Answer the open question on {title or 'Global'}")
        # A question closed meanwhile (by the coordinator or another answer) makes this answer a conflict, never a second answer.
        builder.expect(ref(QUESTIONS, row["id"], item["business_id"], tables=builder.tables), row)
        return builder.build()

    def apply_answer(self, ctx, plan, uow):
        args = plain(plan.arguments)
        row, item, title = self.open_question(ctx, args["question_id"], uow)
        change = {"action": "answer", "question_id": row["id"], "kind": args.get("kind", "decision"), "text": args["text"]}
        made = filing.write(uow.connection, item, [change], [{"quote": args["text"], "ref": ANSWER_SOURCE}],
                            created_by=plan.principal_id, at=timestamp())
        coordinator = configured_ordubashy(uow)
        if coordinator:
            emit_event(uow, business_id=item["business_id"], source_type=EVENT_SOURCE, source_id=row["id"],
                       source_revision=row["revision"] + 1, actor_id=plan.principal_id, event_type="answered",
                       summary=f"The owner answered the open question on {title or 'Global'}: \"{row['text']}\" Answer "
                               f"({change['kind']}): {args['text']}", required=[coordinator])
        held = timeline_state.state(uow.connection, item)
        return {"item": held["item"], "question_id": row["id"], "entry_id": made["entries"][0] if made["entries"] else None,
                "revision": held["revision"]}
