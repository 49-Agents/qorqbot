"""Small cross-scope index of Tasks, without hydrating discussions or custom fields.

Two views of the same authorized scopes: ``assigned`` (the owner's and the coordinator's own assignments) and ``holders``
(all open work grouped by who must act next, so the coordinator can answer for work other agents hold)."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

import json
from dataclasses import replace
from datetime import datetime

from qorqut_operations.application.plans import PAGE, page, schema, timestamp
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.global_agents import effective_business_ids, is_global_agent
from qorqut_operations.roles import ORDUBASHY_ROLE
from qorqut_operations.tags.catalog import labels_by_item
from qorqut_operations.tasks.models import snooze_view
from qorqut_operations.tasks.scope import task_context
# The holders view marks a Task stale by the same rule the backend pushes with. The mark is derived on read from
# ``updated_at``; nothing is stored and no state changes.
from qorqut_operations.tasks.stale import STALE_AFTER, limits as stale_limits, stamp

MAX_MATCHES = 20_000
HOLDER_STATES = ("open", "working", "blocked", "waiting")


def moment(value):
    """A caller's ISO-8601 instant in the stored UTC form, so it compares with ``updated_at`` as text."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("validation_error", "updated_since must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise DomainError("validation_error", "updated_since must carry a UTC offset")
    return stamp(parsed)


class TaskOverview:
    def __init__(self, tasks, application):
        self.tasks, self.app = tasks, application

    def descriptor(self):
        return OperationDescriptor(
            "tasks_overview", schema({
                **PAGE, "limit": {**PAGE["limit"], "default": 100},
                "assignee": {"enum": ["both", "owner", "self"], "default": "both"},
                "snooze_filter": {"enum": ["all", "active", "snoozed"], "default": "all"},
                "include_closed": {"type": "boolean", "default": False},
                # Both are explained in the operation's one description; the help catalog keeps a fixed key set.
                "view": {"enum": ["assigned", "holders"], "default": "assigned"},
                "updated_since": {"type": "string", "minLength": 1, "maxLength": 40}}),
            effect_kind="read", supports_signoff=False, scope_kinds=frozenset({"global"}), global_capability="read_global_context",
            description=instruction_text('overview-ordubashy-only-brief-owner-self-task-overview-across'), read=self.read)

    def read(self, ctx, args, options, uow):
        ctx = self.app.current_context(replace(ctx, business_id=None, review_business_id=None), uow)
        profile = uow.repository("agent_profiles", business_id=None).get(ctx.principal_id)
        if (ctx.principal_kind != "agent" or not profile or not is_global_agent(uow.connection, ctx.principal_id)
                or profile["profile_json"]["role"]["id"] != ORDUBASHY_ROLE):
            raise DomainError("forbidden", "Task overview requires an active ecosystem Ordubashy")
        ctx = task_context(ctx, {"scope": "global"}, uow)
        installation = uow.repository("installation").get("service")
        owner = uow.repository("principals").get(installation["owner_principal_id"]) if installation else None
        if not owner or owner["kind"] != "human" or not owner["active"]:
            raise DomainError("unavailable", "An active installation owner is required")
        # The aggregate must not become an alternate route around task read denials.
        descriptor = self.app.registry.get("tasks_list")
        if args.get("view", "assigned") == "holders":
            return self.holders(ctx, args, uow, descriptor, owner)
        if "updated_since" in args:
            raise DomainError("validation_error", "updated_since applies to view=holders")
        now = timestamp(self.tasks.clock)
        filters = {"assignee": args.get("assignee", "both"), "snooze_filter": args.get("snooze_filter", "all"),
                   "include_closed": args.get("include_closed", False)}
        matches, scopes = [], {}
        # The caller is an active global agent: its businesses are the active directory, not membership rows.
        for business_id in [None, *sorted(ctx.allowed_business_ids & effective_business_ids(uow.connection, ctx.principal_id))]:
            selected = replace(ctx, business_id=business_id, review_business_id=business_id)
            self.app.authorize(selected, descriptor, (), uow, namespace="business:" + business_id if business_id else "global")
            business = uow.repository("businesses").get(business_id) if business_id else None
            if business_id and not business:
                continue
            scopes[business_id] = business["name"] if business else "Global"
            clauses, parameters = ["t.business_id IS ?", "t.archived=0"], [business_id]
            if not filters["include_closed"]:
                clauses.append("t.status NOT IN ('done','cancelled')")
            if filters["snooze_filter"] != "all":
                active = "(t.snoozed_until IS NULL OR t.snoozed_until<=?)"
                clauses.append("NOT " + active if filters["snooze_filter"] == "snoozed" else active)
                parameters.append(now)
            owner_membership = uow.repository("business_memberships", business_id=business_id).get(owner["id"]) if business_id else None
            owner_present = business_id is None or bool(owner_membership and owner_membership["active"])
            # IS (not =) keeps NULL/Global joins scoped, including duplicate task IDs.
            assigned = ("EXISTS (SELECT 1 FROM relationships r WHERE r.business_id IS t.business_id AND r.from_type='task' AND r.from_id=t.id "
                        "AND r.role='assigned_to' AND r.to_type=? AND r.to_id=?)")
            owner_sql = assigned if owner_present else "0"
            assignment_parameters = (["person", owner["id"]] if owner_present else []) + ["agent", ctx.principal_id]
            predicate = {"both": "(owner_assigned OR self_assigned)", "owner": "owner_assigned", "self": "self_assigned"}[filters["assignee"]]
            found = uow.connection.execute(
                "SELECT t.id,t.revision,t.title,t.status,t.due,t.snoozed_until,t.snooze_generation,"
                "COALESCE(json_extract(t.fields_json,'$.\"$task\".task_type'),'task') AS task_type," + owner_sql + " AS owner_assigned," + assigned
                + " AS self_assigned FROM tasks t WHERE " + " AND ".join(clauses) + " AND " + predicate + " LIMIT ?",
                [*assignment_parameters, *parameters, MAX_MATCHES + 1 - len(matches)]).fetchall()
            labels = labels_by_item(uow, business_id, "task")
            for row in found:
                task = dict(row)
                task["tags"] = labels.get(task["id"], [])
                matches.append({**task, "business_id": business_id, **snooze_view(task, now)})
            if len(matches) > MAX_MATCHES:
                raise DomainError("result_too_large", f"Task overview exceeds {MAX_MATCHES:,} matches; narrow assignee or snooze_filter")
        counts = {"active": sum(not task["snoozed"] for task in matches), "snoozed": sum(task["snoozed"] for task in matches)}
        # Bind continuation to identity and current readable scopes as well as filters.
        selected, cursor = page(matches, {**filters, "principal_id": ctx.principal_id, "owner_id": owner["id"],
                                          "scopes": sorted(business or "" for business in scopes), "limit": args.get("limit", 100),
                                          **({"cursor": args["cursor"]} if "cursor" in args else {})},
                                None, "tasks_overview", reverse=False,
                                key=lambda task: (int(task["snoozed"]), task["due"] or "9999-12-31", task["business_id"] or "", task["id"]))
        groups = {}
        for task in self.tasks.project_input(ctx, selected, uow):
            business_id = task["business_id"]
            group = groups.setdefault(business_id, {"business_id": business_id, "name": scopes[business_id], "active": [], "snoozed": []})
            brief = {key: task[key] for key in ("id", "revision", "title", "status", "due", "task_type")}
            brief.update({key: task[key] for key in ('work_status', 'input_needed', 'input_requests', 'input_version', 'input_request_missing') if key in task})
            if len(brief["title"]) > 180:
                brief.update(title=brief["title"][:179] + "…", title_truncated=True)
            brief["assignees"] = [name for name in ("owner", "self") if task[name + "_assigned"]]
            brief["tags"] = task["tags"]
            if task["snoozed"]:
                brief["snoozed_until"] = task["snoozed_until"]
            group["snoozed" if task["snoozed"] else "active"].append(brief)
        return {"assignees": {"owner": owner["id"], "self": ctx.principal_id}, "counts": counts, "scopes": list(groups.values()),
                "next_cursor": cursor}

    def holders(self, ctx, args, uow, descriptor, owner):
        """All open work across the readable scopes, grouped by who must act next (the Task's Workers).

        Every holder is listed with its counts over all matches; the Tasks themselves are one page, oldest change
        first, so the work most likely to have been dropped leads. A Task with several Workers appears under each,
        and a Task that names nobody under ``unassigned``. Customer records are not work items and are left out. An
        archived project is not current work and is skipped."""
        for name in ("assignee", "snooze_filter", "include_closed"):
            if name in args:
                raise DomainError("validation_error", f"{name} applies to view=assigned")
        since = moment(args["updated_since"]) if "updated_since" in args else None
        current = self.tasks.clock()
        limits = stale_limits(current)
        pairs, people, tasks_seen = [], {}, 0

        def holder(identity):
            if identity not in people:
                principal = uow.repository("principals").get(identity) if identity else None
                if identity is None:
                    people[identity] = {"id": None, "name": "Unassigned", "kind": "unassigned", "rank": 4}
                elif not principal:
                    people[identity] = {"id": identity, "name": identity, "kind": "person", "rank": 3}
                else:
                    kind, rank = (("self", 0) if identity == ctx.principal_id else ("agent", 1) if principal["kind"] == "agent"
                                  else ("owner", 2) if identity == owner["id"] else ("person", 3))
                    people[identity] = {"id": identity, "name": principal["display_name"], "kind": kind, "rank": rank}
            return people[identity]

        for business_id in [None, *sorted(ctx.allowed_business_ids & effective_business_ids(uow.connection, ctx.principal_id))]:
            selected = replace(ctx, business_id=business_id, review_business_id=business_id)
            try:
                self.app.authorize(selected, descriptor, (), uow, namespace="business:" + business_id if business_id else "global")
            except DomainError as error:
                if error.code != "business_archived":
                    raise
                continue
            if business_id and not uow.repository("businesses").get(business_id):
                continue
            clauses = ["t.business_id IS ?", "t.archived=0", "t.status NOT IN ('done','cancelled')",
                       "COALESCE(json_extract(t.fields_json,'$.\"$task\".task_type'),'task')!='customer'"]
            parameters = [business_id]
            if since:
                clauses.append("t.updated_at>=?")
                parameters.append(since)
            found = uow.connection.execute(
                "SELECT t.id,t.revision,t.title,t.status,t.due,t.updated_at,t.worker_ids_json FROM tasks t WHERE "
                + " AND ".join(clauses) + " LIMIT ?", [*parameters, MAX_MATCHES + 1 - tasks_seen]).fetchall()
            tasks_seen += len(found)
            if tasks_seen > MAX_MATCHES:
                raise DomainError("result_too_large", f"Task overview exceeds {MAX_MATCHES:,} matches; narrow updated_since")
            for row in found:
                task = {key: row[key] for key in ("id", "revision", "title", "status", "due", "updated_at")}
                task.update(business_id=business_id,
                            stale=task["status"] in limits and (task["updated_at"] or "") < limits[task["status"]])
                for identity in json.loads(row["worker_ids_json"] or "[]") or [None]:
                    pairs.append({**task, "holder": holder(identity)})
        groups = {}
        for pair in pairs:
            entry = pair["holder"]
            group = groups.setdefault(entry["id"], {"holder": {key: entry[key] for key in ("id", "name", "kind")},
                                                    "counts": {**{status: 0 for status in HOLDER_STATES}, "stale": 0, "total": 0},
                                                    "tasks": [], "rank": entry["rank"]})
            if pair["status"] in group["counts"]:
                group["counts"][pair["status"]] += 1
            group["counts"]["stale"] += int(pair["stale"])
            group["counts"]["total"] += 1
        selected, cursor = page(pairs, {"view": "holders", "updated_since": since, "principal_id": ctx.principal_id,
                                        "limit": args.get("limit", 100), **({"cursor": args["cursor"]} if "cursor" in args else {})},
                                None, "tasks_overview", reverse=False,
                                key=lambda pair: (pair["holder"]["rank"], pair["holder"]["id"] or "", pair["updated_at"] or "",
                                                  pair["business_id"] or "", pair["id"]))
        for pair, task in zip(selected, self.tasks.project_input(ctx, selected, uow)):
            brief = {key: task[key] for key in ("id", "business_id", "revision", "title", "status", "due", "updated_at")}
            brief.update({key: task[key] for key in ("work_status", "input_needed") if key in task})
            if len(brief["title"]) > 180:
                brief.update(title=brief["title"][:179] + "…", title_truncated=True)
            if pair["stale"]:
                brief["stale"] = True
            groups[pair["holder"]["id"]]["tasks"].append(brief)
        ordered = sorted(groups.values(), key=lambda group: (group.pop("rank"), group["holder"]["id"] or ""))
        return {"view": "holders", "holders": ordered,
                "counts": {"tasks": tasks_seen, "stale": sum(group["counts"]["stale"] for group in ordered),
                           "unassigned": groups[None]["counts"]["total"] if None in groups else 0},
                "stale_after_days": {status: age.days for status, age in STALE_AFTER.items()}, "next_cursor": cursor}
