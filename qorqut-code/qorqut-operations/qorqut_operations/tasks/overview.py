"""Small cross-scope owner/self index of Tasks, without hydrating discussions or custom fields."""

from __future__ import annotations

from dataclasses import replace

from qorqut_operations.application.plans import PAGE, page, schema, timestamp
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError
from qorqut_operations.global_agents import effective_business_ids, is_global_agent
from qorqut_operations.roles import ORDUBASHY_ROLE
from qorqut_operations.tags.catalog import labels_by_item
from qorqut_operations.tasks.models import snooze_view
from qorqut_operations.tasks.scope import task_context

MAX_MATCHES = 20_000


class TaskOverview:
    def __init__(self, tasks, application):
        self.tasks, self.app = tasks, application

    def descriptor(self):
        return OperationDescriptor(
            "tasks_overview", schema({
                **PAGE, "limit": {**PAGE["limit"], "default": 100},
                "assignee": {"enum": ["both", "owner", "self"], "default": "both"},
                "snooze_filter": {"enum": ["all", "active", "snoozed"], "default": "all"},
                "include_closed": {"type": "boolean", "default": False}}),
            effect_kind="read", supports_signoff=False, scope_kinds=frozenset({"global"}), global_capability="read_global_context",
            description="Ordubashy only: brief owner/self task overview across Global and all currently authorized projects in one call, "
                        "regardless of selected project. Defaults to both assignees, unfinished non-archived tasks, active and snoozed "
                        "separated. Entries include revision and every tag label (or []); titles capped at 180 characters; no notes or "
                        "custom fields. Counts cover all matches; follow next_cursor if present. Use the returned revision for optimistic "
                        "edits to explicit overview fields such as due and tags. Read tasks_get first when editing fields or evidence not "
                        "shown here.", read=self.read)

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
        for task in selected:
            business_id = task["business_id"]
            group = groups.setdefault(business_id, {"business_id": business_id, "name": scopes[business_id], "active": [], "snoozed": []})
            brief = {key: task[key] for key in ("id", "revision", "title", "status", "due", "task_type")}
            if len(brief["title"]) > 180:
                brief.update(title=brief["title"][:179] + "…", title_truncated=True)
            brief["assignees"] = [name for name in ("owner", "self") if task[name + "_assigned"]]
            brief["tags"] = task["tags"]
            if task["snoozed"]:
                brief["snoozed_until"] = task["snoozed_until"]
            group["snoozed" if task["snoozed"] else "active"].append(brief)
        return {"assignees": {"owner": owner["id"], "self": ctx.principal_id}, "counts": counts, "scopes": list(groups.values()),
                "next_cursor": cursor}
