"""What the records say was done over a period: a read for the owner and the coordinator.

``work_review_get`` is the companion of ``owner_inputs_list``. That read lists what the owner asked; this one lists
what was recorded as done in the same period, so the two can be compared without relying on anyone's memory:

* ``tasks``: Tasks created, closed, reopened and handed to other Workers, from the Task rows and the system line every
  Task write leaves in its thread;
* ``actions``: Actions recorded;
* ``signoffs``: proposals put to the owner and proposals decided;
* ``owner_facing``: how many notes agents posted for the owner, replies in the private owner threads and completed
  chat replies, per agent;
* ``standing_now``: how much open work there is now and how much of it is unassigned, stale or waiting on the owner.
  The list itself is ``tasks_overview(view="holders")``; it is not repeated here.

Each section is read with the caller's grant for the equivalent read, and one that cannot be read is returned as
``{"unavailable": code}`` while the others still answer. Every list is capped and says how many it left out. Nothing
is written.
"""
from __future__ import annotations

import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import UUID, schema
from qorqut_operations.contracts import DomainError, InvocationOptions

from qorqut.domains.owner_inputs import MAX_HOURS, PAGE_LIMIT, TIME, cut, stamp
from qorqut.domains.records_health import CHECKS, PARKED_STALE, TOTALS, WORK, WORKING_STALE

DEFAULT_ITEMS = 30
TEXT = 200
DESCRIPTION = (
    "Owner and coordinator read. What the records say was done in a period, to set beside owner_inputs_list for the same "
    "period. Choose it with hours (default 24) or since/until (ISO-8601 with a UTC offset); agent_id keeps one agent's work. "
    "tasks: created, closed (with the Task's status line), reopened and workers_changed (delegations and hand-backs), each "
    "with count, by (who did how many) and the newest items, oldest first; more says how many older ones were left out. "
    "actions: Actions recorded. signoffs: proposed and decided. owner_facing: counts of notes posted for the owner, replies "
    "in private owner threads and completed chat replies per agent. standing_now: open work now, and how much of it has no "
    "Worker, is stale or waits on the owner; read tasks_overview(view=\"holders\") for the list. A section that cannot be "
    "read is {unavailable: code}. A record here shows that something was written, not that an ask was met: read the Task, "
    "Action or Signoff before you call it done. Changes nothing.")
SCHEMA = schema({
    "scope": {"const": "global"},
    "hours": {"type": "integer", "minimum": 1, "maximum": MAX_HOURS},
    "since": TIME, "until": TIME,
    "agent_id": UUID,
    "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_LIMIT},
})

# Work, not the Customer, Story or Email records that are stored as Tasks too. An archived Task still counts: closing
# and archiving it on the same day does not make the work disappear from that day.
PLAIN = "coalesce(json_extract(t.fields_json,'$.\"$task\".task_type'),'task')='task'"
TASK = "t.business_id AS business_id, t.id AS id, t.title AS title, t.status AS status, t.fields_json AS fields_json"
CHANGES = ("thread_messages m JOIN tasks t ON t.business_id IS m.business_id AND t.id=m.item_id "
           f"WHERE m.kind='system' AND m.item_type='task' AND m.created_at>=:since AND m.created_at<:until AND {PLAIN} "
           "AND (:actor IS NULL OR m.recorder_id=:actor)")
# What a Task write's system line says it changed (``tasks.events.describe``).
CHANGED = {
    "closed": "json_extract(m.change_json,'$.to') IN ('done','cancelled')",
    "reopened": ("json_extract(m.change_json,'$.from') IN ('done','cancelled') "
                 "AND json_extract(m.change_json,'$.to') NOT IN ('done','cancelled')"),
    "workers_changed": "EXISTS (SELECT 1 FROM json_each(m.change_json,'$.fields') f WHERE f.value='workers')",
}
CREATED = (f"tasks t WHERE t.created_at>=:since AND t.created_at<:until AND {PLAIN} "
           "AND (:actor IS NULL OR t.created_by=:actor)")
ACTIONS = ("action_logs a WHERE a.withdrawn=0 AND a.created_at>=:since AND a.created_at<:until "
           "AND (:actor IS NULL OR coalesce(a.performed_by,a.recorded_by)=:actor)")
NOTES = ("thread_messages m JOIN principals p ON p.id=m.author_id AND p.kind='agent' WHERE m.kind='comment' "
         "AND coalesce(json_extract(m.attribution_json,'$.audience'),'')='owner' AND m.created_at>=:since AND m.created_at<:until "
         "AND (:actor IS NULL OR m.author_id=:actor)")
THREAD_REPLIES = ("owner_conversation_messages m JOIN owner_conversations c ON c.id=m.conversation_id WHERE c.peer_agent_id IS NULL "
                  "AND m.author_role='coordinator' AND m.created_at>=:since AND m.created_at<:until")
CHAT_REPLIES = ("native_replies r WHERE r.completed_at>=:since AND r.completed_at<:until AND (r.origin='user' OR r.user_work=1) "
                "AND (:actor IS NULL OR r.agent_id=:actor)")
STANDING = ("open_without_worker", "working_untouched", "parked_untouched", "parked_without_wake_time")


class WorkReview:
    def __init__(self, runtime):
        self.runtime, self.app = runtime, runtime.application

    def register(self):
        self.runtime.registry.register(OperationDescriptor(
            "work_review_get", SCHEMA, description=DESCRIPTION, read=self.read, scope_kinds=frozenset({"global"}),
            global_capability="read_global_context", effect_kind="read", supports_signoff=False,
            exposure=frozenset({"mcp", "dashboard"}), resolve_namespace=lambda ctx, args: "global"))
        self.runtime.work_review = self
        return self

    def read(self, ctx, args, options, uow):
        inputs = self.runtime.owner_inputs
        # The same two readers and the same period as the owner's messages this is set beside.
        reading = inputs.caller(ctx, uow)
        window = inputs.window(args)
        review = _Review(self, reading.ctx, uow, window, args, inputs.clock())
        return {"since": window["since"], "until": window["until"],
                **{name: review.section(name) for name in ("tasks", "actions", "signoffs", "owner_facing", "standing_now")}}


class _Review:
    """One answer: the period, the caller and the names already looked up."""

    def __init__(self, service, ctx, uow, window, args, now):
        self.app, self.ctx, self.uow, self.connection, self.now = service.app, ctx, uow, uow.connection, now
        self.limit = args.get("limit", DEFAULT_ITEMS)
        self.period = {"since": window["since"], "until": window["until"], "actor": args.get("agent_id")}
        self.names = {}

    def section(self, name):
        try:
            return getattr(self, name)()
        except DomainError as error:
            return {"unavailable": error.code}

    def allowed(self, operation):
        """The caller's own grant for the read this section stands for; a section is never wider than that read."""
        self.app.authorize(self.ctx, self.app.registry.get(operation), (), self.uow, namespace="global")

    # ----- shared shapes ---------------------------------------------------------------------------------------------

    def who(self, identity):
        if identity not in self.names:
            row = self.connection.execute("SELECT display_name FROM principals WHERE id=?", (identity,)).fetchone()
            self.names[identity] = {"id": identity, "name": row[0] if row else None}
        return self.names[identity]

    def listed(self, body, actor, columns, order, build):
        """``{count, by, items, more}`` of one bounded query: every actor's count, and the newest items oldest first."""
        count = self.connection.execute(f"SELECT count(*) FROM {body}", self.period).fetchone()[0]
        by = [{**self.who(row[0]), "count": row[1]} for row in self.connection.execute(
            f"SELECT {actor} AS actor, count(*) AS n FROM {body} GROUP BY actor ORDER BY n DESC, actor", self.period)
              if row[0] is not None] if count else []
        rows = self.connection.execute(f"SELECT {columns} FROM {body} ORDER BY {order} DESC LIMIT :limit",
                                       {**self.period, "limit": self.limit}).fetchall() if count else []
        return {"count": count, "by": by, "items": [build(row) for row in reversed(rows)], "more": count - len(rows)}

    def counted(self, body, actor):
        rows = self.connection.execute(f"SELECT {actor} AS actor, count(*) AS n FROM {body} GROUP BY actor ORDER BY n DESC, actor",
                                       self.period).fetchall()
        return {"count": sum(row[1] for row in rows), "by": [{**self.who(row[0]), "count": row[1]} for row in rows]}

    @staticmethod
    def task(row):
        return {"id": row["id"], "business_id": row["business_id"], "title": cut(row["title"], 120), "status": row["status"]}

    # ----- sections --------------------------------------------------------------------------------------------------

    def tasks(self):
        self.allowed("tasks_overview")

        def change(row):
            return {"at": row["at"], "task": self.task(row), "by": self.who(row["actor"]), "change": cut(row["body"], TEXT)}

        def closed(row):
            line = (json.loads(row["fields_json"] or "{}").get("$task") or {}).get("status_line")
            return {**change(row), **({"status_line": cut(line, TEXT)} if line else {})}

        def created(row):
            workers = [self.who(identity)["name"] or identity for identity in json.loads(row["workers"] or "[]")]
            return {"at": row["at"], "task": self.task(row), "by": self.who(row["actor"]), "workers": workers}

        found = {"created": self.listed(CREATED, "t.created_by", f"t.created_at AS at, t.created_by AS actor, "
                                        f"t.worker_ids_json AS workers, {TASK}", "t.created_at DESC, t.id", created)}
        for name, condition in CHANGED.items():
            found[name] = self.listed(f"{CHANGES} AND {condition}", "m.recorder_id",
                                      f"m.created_at AS at, m.recorder_id AS actor, m.body AS body, {TASK}",
                                      "m.created_at DESC, m.id", closed if name == "closed" else change)
        return found

    def actions(self):
        self.allowed("actions_list")
        return self.listed(ACTIONS, "coalesce(a.performed_by,a.recorded_by)",
                           "a.occurred_at AS at, a.id AS id, a.business_id AS business_id, a.action_text AS text, "
                           "coalesce(a.performed_by,a.recorded_by) AS actor", "a.created_at DESC, a.id",
                           lambda row: {"at": row["at"], "id": row["id"], "business_id": row["business_id"],
                                        "text": cut(row["text"], TEXT), "by": self.who(row["actor"])})

    def signoffs(self):
        self.allowed("signoffs_list")
        service = self.app.signoffs
        if service is None:
            raise DomainError("operation_unavailable", "Signoffs are not installed")
        actor = self.period["actor"]

        def shown(found, moment, extra=()):
            items = [item for item in found["items"] if actor is None or item["proposer_principal_id"] == actor]
            return {"count": found["count"] if actor is None else len(items), "more": found["count"] - len(found["items"]),
                    "items": [{"at": item[moment], "id": item["id"], "business_id": item["review_business_id"],
                               "title": cut(item["title"], 120), "state": item["state"], "category": item["category"],
                               "by": item["proposer_display_name"] or None, **{name: item[name] for name in extra}}
                              for item in reversed(items)]}

        period = {"since": self.period["since"], "until": self.period["until"], "limit": self.limit}
        return {"proposed": shown(service.recent_proposals(self.ctx, self.uow, **period), "created_at"),
                "decided": shown(service.recent_outcomes(self.ctx, self.uow, side_chats=False, **period), "updated_at",
                                 ("decision_state",))}

    def owner_facing(self):
        replies = self.connection.execute(f"SELECT count(*) FROM {THREAD_REPLIES}", self.period).fetchone()[0]
        return {"notes_for_owner": self.counted(NOTES, "m.author_id"), "thread_replies": replies,
                "chat_replies": self.counted(CHAT_REPLIES, "r.agent_id")}

    def standing_now(self):
        self.allowed("tasks_overview")
        moment = {"working_before": stamp(self.now - WORKING_STALE), "parked_before": stamp(self.now - PARKED_STALE)}

        def count(body):
            return self.connection.execute(f"SELECT count(*) FROM {body}",
                                           {name: value for name, value in moment.items() if ":" + name in body}).fetchone()[0]

        # The same definitions records_health counts by, so the two reads never disagree about what stale means.
        found = {"as_of": stamp(self.now), "open_work_tasks": count(TOTALS["open_work_tasks"]),
                 **{name: count(CHECKS[name][1]) for name in STANDING},
                 "needs_owner_input": count(f"tasks t WHERE {WORK} AND t.status='needs_input'")}
        try:
            self.allowed("signoffs_list")
            listing = self.app.registry.get("signoffs_list")
            arguments = self.app.registry.bind_arguments("signoffs_list", {"scope": "global", "states": ["pending"], "limit": 1})
            found["pending_signoffs"] = listing.read(self.ctx, arguments, InvocationOptions(), self.uow)["counts"]["pending"]
        except DomainError:
            found["pending_signoffs"] = None
        return found


__all__ = ["WorkReview"]
