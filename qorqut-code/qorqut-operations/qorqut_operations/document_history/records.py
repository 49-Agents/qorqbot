"""History and activity for committed record changes (``apply_records`` and explicit bypass writers).

``RecordHistory`` is the one record-history callback of a composition: it owns the union of tracked tables and the order
in which one record change is considered. Documents, Tasks and Action logs are generic; SQL-authored documents and extra
append-only facts belong to the host that owns those tables and arrive as explicit arguments (``SqlDocuments`` descriptors
and a fact callback per table)."""

from __future__ import annotations

import json

from qorqut_operations.actions import one_line
from qorqut_operations.contracts import utc_now
from qorqut_operations.tasks import CLOSED_STATES
from .files import document_event, file_effects
from .text import SqlDocuments, append_event

__all__ = ["RecordHistory", "action_fact_event", "bounded", "task_event"]


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def bounded(refs):
    if not isinstance(refs, dict):
        return None
    return {key: str(value)[:200] for key, value in sorted(refs.items())[:20]}


def closed_states(connection, business_id):
    return set(CLOSED_STATES)


def task_event(connection, before, after, *, actor_id, data):
    """task.completed / task.reopened when a committed change crosses the business's configured closed states."""
    if not after.get("business_id"):
        return None
    closed = closed_states(connection, after["business_id"])
    was, now = before is not None and before["status"] in closed, after["status"] in closed
    if was == now:
        return None
    verb = "completed" if now else "reopened"
    return append_event(connection, after["business_id"], f"task:{after['id']}:r{after['revision']}:{verb}",
        f"task.{verb}", actor_id, f"Task {verb}", subject_type="task", subject_id=after["id"],
        occurred_at=after["updated_at"], data={**data, "task_id": after["id"], "status": after["status"],
        "previous_status": before["status"] if before else None, "revision": after["revision"]})


def action_fact_event(row, *, operation):
    """The summary event for one committed Action log; its ID is the source row's."""
    return {"business_id": row["business_id"], "event_id": f"action:{row['id']}", "kind": "action.recorded",
            "actor_id": row["recorded_by"], "summary": one_line(row["action_text"]), "subject_type": "action",
            "subject_id": row["id"], "occurred_at": row["occurred_at"], "data": {"log_id": row["id"], "action_key": row["action_key"],
            "refs": bounded(_json(row.get("refs_json"))), "operation": operation}}


class RecordHistory:
    """``sql_documents`` are the host's SQL document descriptors (a ``SqlDocuments``); ``facts`` maps each extra append-only
    fact table to the host callback ``fact_event(row, *, operation)`` that renders its event. Both are required arguments, so
    a host without either passes an empty value on purpose."""

    def __init__(self, *, sql_documents, facts):
        if not isinstance(sql_documents, SqlDocuments):
            raise TypeError("Record history needs the host's SqlDocuments descriptors")
        facts = dict(facts)
        if any(not callable(fact) for fact in facts.values()):
            raise TypeError("A fact table is registered with its event callback")
        if "action_logs" in facts:
            raise ValueError("Action log facts are generic and cannot be replaced")
        self.sql_documents = sql_documents
        self.facts = {"action_logs": action_fact_event, **facts}
        self.tracked_tables = frozenset({*sql_documents, *self.facts, "documents", "tasks"})

    def record_applied(self, uow, plan, table, before, after, *, actor_id=None):
        """History and activity for one committed record change, in its own transaction."""
        if table not in self.tracked_tables:
            return None
        connection = uow.connection
        actor = actor_id or plan.principal_id
        delegated = {"operation": plan.operation_name, "initiated_by_principal_id": plan.initiated_by_principal_id,
                     "proposer_agent_id": plan.proposer_agent_id}
        row = after or before
        if table in self.sql_documents:
            version = self.sql_documents.capture(connection, table, before, after, author=actor, operation=plan.operation_name)
            if after is None and row.get("business_id"):
                prefix, key, _ = self.sql_documents.descriptors[table]
                subject = prefix.replace("-", "_")
                append_event(connection, row["business_id"], f"{prefix}:{row[key]}:deleted", f"{subject}.deleted", actor,
                    subject.replace("_", " ").capitalize() + " deleted", subject_type=subject, subject_id=row[key],
                    occurred_at=utc_now(), data={**delegated, "history_document_id": self.sql_documents.document_id(table, row),
                                                 "revision": row.get("revision")})
            return version
        if table == "documents":
            event = document_event(before, after, actor_id=actor, operation=plan.operation_name, plan=plan)
            if event is None:
                return None
            writes, deleted = file_effects(plan)
            if row["id"] in writes and after is not None:
                uow.history_events[row["id"]] = event
            else:
                append_event(connection, **event)
            return None
        if not row.get("business_id") or after is None:
            return None
        if table == "tasks":
            return task_event(connection, before, after, actor_id=actor, data=delegated)
        if before is not None or table not in self.facts:
            return None
        return append_event(connection, **self.facts[table](after, operation=plan.operation_name))
