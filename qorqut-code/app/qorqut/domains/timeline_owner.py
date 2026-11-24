"""The owner's view of timelines: one item read for the dashboard.

``timeline_view`` is ``timeline_get`` for the owner's dashboard (state, or the history with the quoted words and links). It is
read-only and owner-only. An open question for the owner is a question Signoff: the view lists the pending ones asked about the
item (its Task or Project source) beside any question an older filing left open on the timeline itself. The owner answers them
in the sign-off queue. ``timeline_file`` is never offered here. Global has no timeline, so the view refuses it as it refuses an
item that does not exist."""
from __future__ import annotations

import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.timeline import state as timeline_state

from qorqut.domains.timeline import GET_SCHEMA, LINKS_LIMIT

VIEW_DESCRIPTION = (
    "Owner dashboard read of the timeline of a Task or a Project: item names it as in timeline_get. Returns the title, "
    "the state (goal, decisions, facts, progress, open questions: the pending question Signoffs asked about the item) and, with "
    "history true, every entry oldest first with removed ones marked, the owner's quoted words, and the item's subtasks, "
    "approvals and reports as links. Changes nothing.")
VIEW_SCHEMA = GET_SCHEMA
# A pending question Signoff: an input request that asks or requests something, not a draft to review or a report to read.
ASKS = ("s.category='input_request' AND s.state='pending' AND s.superseded_by_id IS NULL "
        "AND json_extract(s.preview_json,'$.input.kind') IN ('question','request')")
QUESTION_ROW = ("SELECT s.id, json_extract(s.preview_json,'$.title') AS text, s.created_at, COALESCE(a.blocking,0) AS blocking "
                "FROM signoffs s LEFT JOIN signoff_attention a ON a.signoff_id=s.id")


class TimelineOwner:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application

    def register(self):
        self.runtime.registry.register(OperationDescriptor(
            "timeline_view", VIEW_SCHEMA, description=VIEW_DESCRIPTION, read=lambda c, a, o, u: self.view(c, a["item"], u, a),
            supports_signoff=False, effect_kind="read", owner_only=True, exposure=frozenset({"dashboard"}),
            scope_kinds=frozenset({"global"}), global_capability="read_global_context", resolve_namespace=lambda ctx, args: "global"))
        self.app.timeline_owner = self
        return self

    @staticmethod
    def asks(item, uow):
        """The pending question Signoffs asked about ``item``, blocking ones first, in the shape of a timeline question."""
        rows = uow.connection.execute(
            f"{QUESTION_ROW} WHERE {ASKS} AND EXISTS (SELECT 1 FROM signoff_source_links l WHERE l.signoff_id=s.id "
            "AND l.source_type=? AND l.source_id=? AND l.source_project_id IS ?) ORDER BY blocking DESC, s.created_at, s.id LIMIT ?",
            (item["type"], item["id"], item["project_id"], LINKS_LIMIT)).fetchall()
        return [{"id": row["id"], "signoff_id": row["id"], "text": row["text"] or "", "blocking": bool(row["blocking"]),
                 "raised_by": "coordinator", "created_at": row["created_at"]} for row in rows]

    def view(self, ctx, item, uow, args=None):
        """The item's title, state and, with ``history``, its entries and links. ``not_found`` when it is not readable."""
        args = args or {}
        timeline = self.runtime.timeline
        title = timeline.readable(ctx, item, uow)
        held = timeline_state.state(uow.connection, item)
        held = {**held, "questions": [*self.asks(item, uow), *held["questions"]]}
        result = {"item": held["item"], "title": title, "revision": held["revision"], "state": held,
                  "further": timeline.further_reading(item, uow)}
        if args.get("history"):
            cursor = int(args["cursor"]) if args.get("cursor") else 0
            entries, after = timeline_state.history(uow.connection, item, args.get("limit", 50), cursor)
            result.update(entries=entries, next_cursor=str(after) if after else None,
                          **({"closed_questions": timeline_state.closed_questions(uow.connection, item, LINKS_LIMIT),
                              "links": timeline.links(item, uow, timeline.trusted(ctx, uow))} if not cursor else {}))
        return result
