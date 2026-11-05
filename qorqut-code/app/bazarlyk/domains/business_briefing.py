"""One bounded business briefing assembled from the caller's own authorized reads."""

from qorqut_agentic.behavior.tool_help import text as instruction_text
from collections import Counter
from dataclasses import replace
from datetime import timedelta
import json

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, InvocationOptions
from qorqut_operations.application.plans import schema, timestamp, utc_clock
from qorqut_operations.tasks import TaskOperations

WINDOW_DAYS = 14
STUCK_AFTER = timedelta(hours=1)
# Hard bound on the compact JSON answer; per-section caps keep it well below.
MAX_CHARACTERS = 10000
CAPS = {"context": 6, "blocked": 5, "active": 6, "paused": 3, "groups": 5, "routines": 5, "jobs": 3,
        "signoffs": 5, "owner_tasks": 4, "done": 4, "agents": 6, "work": 3, "notes": 4}
DESCRIPTION = (instruction_text('business-briefing-one-bounded-snapshot-of-a-project-the'))


def clip(value, limit, *, lines=False):
    text = str(value or "")
    text = ("\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip()) if lines
            else " ".join(text.split()))
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def when(value):
    return str(value)[:10] if value else None


def compact(value):
    return {key: item for key, item in value.items() if item not in (None, [], {}, "")}


class BusinessBriefing:
    def __init__(self, application, *, clock=utc_clock):
        self.app, self.clock = application, clock

    def register(self, registry):
        registry.register(OperationDescriptor("business_briefing", schema({}), effect_kind="read",
            supports_signoff=False, exposure=frozenset({"mcp", "dashboard"}), description=DESCRIPTION, read=self.read))
        return self

    def read(self, ctx, args, options, uow):
        if ctx.delegation_assignment_id is not None:
            raise DomainError("forbidden", "Delegated workers read explicit records, not project briefings")
        return _Briefing(self.app, ctx, uow, self.clock()).build()


class _Briefing:
    def __init__(self, app, ctx, uow, now):
        self.app, self.ctx, self.uow, self.now = app, ctx, uow, now
        self.since = (now - timedelta(days=WINDOW_DAYS)).isoformat().replace("+00:00", "Z")
        self.names, self.owners, self.unavailable = {}, set(), {}
        self.tasks = None

    def call(self, name, args=None):
        """Run one domain read with the caller's own authorization."""
        descriptor = self.app.registry.get(name)
        arguments = self.app.registry.bind_arguments(name, args or {})
        namespace = self.app.namespace(self.ctx, descriptor, arguments)
        ctx = replace(self.ctx, business_id=None) if namespace == "global" else self.ctx
        self.app.authorize(ctx, descriptor, (), self.uow, namespace=namespace)
        return descriptor.read(ctx, arguments, InvocationOptions(), self.uow)

    def gated(self, name, build):
        """A bounded query gated by the caller's grant for the equivalent read; None when denied."""
        try:
            self.app.authorize(self.ctx, self.app.registry.get(name), (), self.uow,
                               namespace="business:" + self.ctx.business_id)
            return build()
        except DomainError:
            return None

    def section(self, key, build):
        try:
            return build()
        except DomainError as error:
            self.unavailable[key] = error.code
            return {"unavailable": error.code}

    def optional(self, build, default=None):
        try:
            return build()
        except DomainError:
            return default

    def name(self, identity):
        return self.names.get(identity, identity)

    def learn_names(self, identities):
        """Display names for assignees outside assignees_list, such as Global agents."""
        missing = sorted({identity for identity in identities if identity not in self.names})
        if missing:
            marks = ",".join("?" for _ in missing)
            self.names.update({row["id"]: row["display_name"] for row in self.uow.connection.execute(
                f"SELECT id, display_name FROM principals WHERE id IN ({marks})", missing)})

    def build(self):
        business = self.uow.repository("businesses").get(self.ctx.business_id, required=True)
        people = self.optional(lambda: self.call("assignees_list")["assignees"], [])
        self.names = {row["id"]: row["display_name"] for row in people}
        self.owners = {row["id"] for row in people if row["kind"] == "human" and row.get("is_owner")}
        config = business.get("config_json") or {}
        config = json.loads(config) if isinstance(config, str) else config
        result = {"business": compact({"id": business["id"], "slug": business["slug"], "name": business["name"],
                                       "stage": business.get("stage"), "one_liner": clip(config.get("one_liner"), 200)}),
                  "as_of": timestamp(lambda: self.now), "window_days": WINDOW_DAYS}
        for key, build in (("context", self.context), ("open_work", self.open_work), ("work_groups", self.work_groups),
                           ("processes", self.processes), ("waiting_on_owner", self.waiting_on_owner),
                           ("done_recently", self.done_recently), ("agents", self.agents), ("notes", self.notes)):
            result[key] = self.section(key, build)
        if self.unavailable:
            result["unavailable"] = self.unavailable
        result["note"] = (self.app.policy.agent_resolver.require_agentic_view().runtime_instructions().render('project-briefing-evidence'))
        return bound(result)

    def context(self):
        """The one shared published Context graph, by the same bounded ``context_home`` page every agent reads."""
        home = self.call("context_home", {"limit": CAPS["context"]})
        more = "context_home (follow page.next_cursor); context_find; context_object(ref)"
        if home["status"] != "ready":
            return compact({"status": home["status"], "reason": (home.get("reason") or {}).get("code"), "more": more})
        return compact({"status": "ready", "revision": home["revision"], "empty": home["empty"], "totals": home["totals"],
            "nodes": [{"ref": node["ref"], "summary": clip(node["summary"], 120)} for node in home["nodes"]],
            "more": more})

    def task(self, row):
        return compact({"id": row["id"], "title": clip(row["title"], 90), "status": row["status"], "due": row.get("due"),
                        "assignees": [self.name(identity) for identity in row.get("assignee_ids", [])]})

    def open_work(self):
        listing = self.call("tasks_list", {"include_closed": False, "snooze_filter": "all", "limit": 200})
        self.tasks = listing["tasks"]
        self.learn_names(identity for row in self.tasks for identity in row.get("assignee_ids", []))
        awake = [row for row in self.tasks if not row.get("snoozed")]
        today = timestamp(lambda: self.now)[:10]
        titles = {row["id"]: clip(row["title"], 60) for row in self.tasks}
        blocked = [row for row in awake if row.get("unresolved_blockers") or row["status"] == "blocked"]
        blocked_ids = {row["id"] for row in blocked}
        recent = sorted(awake, key=lambda row: row.get("updated_at") or "", reverse=True)
        active = sorted((row for row in recent if not row.get("paused") and row["id"] not in blocked_ids),
                        key=lambda row: (row.get("due") is None, row.get("due") or ""))
        paused = [row for row in recent if row.get("paused") and row["id"] not in blocked_ids]
        return compact({"total": len(self.tasks), "complete": not listing.get("next_cursor"),
            "by_status": dict(sorted(Counter(row["status"] for row in awake).items())),
            "snoozed": len(self.tasks) - len(awake),
            "overdue": sum(1 for row in awake if row.get("due") and row["due"] < today),
            "blocked": [{**self.task(row), **compact({"blocked_by": [{"id": identity, "title": titles.get(identity)}
                        for identity in row.get("unresolved_blockers", [])]})} for row in blocked[:CAPS["blocked"]]],
            "active": [self.task(row) for row in active[:CAPS["active"]]],
            "paused": [self.task(row) for row in paused[:CAPS["paused"]]],
            "more": 'tasks_list(include_closed=false, snooze_filter="all"); tasks_get(task_id)'})

    def work_groups(self):
        """Parent Tasks of the open work, with how many open child Tasks each groups (read from ``open_work``'s listing)."""
        by_id = {row["id"]: row for row in self.tasks}
        groups = {}
        for row in self.tasks:
            parent = row.get("parent_id")
            if parent:
                group = groups.setdefault(parent, {"id": parent, "open_children": 0})
                group["open_children"] += 1
                group["title"] = clip((by_id.get(parent) or row.get("parent") or {}).get("title"), 80)
        rows = sorted(groups.values(), key=lambda group: (-group["open_children"], group["id"]))
        return compact({"total": len(rows),
            "items": [compact(group) for group in rows[:CAPS["groups"]]],
            "more": "tasks_list(parent_id=...); tasks_get(task_id) lists a Task's children"})

    def processes(self):
        result, found = {}, False
        routines = self.optional(lambda: self.call("routines_list", {"limit": 100})["items"])
        if routines is not None:
            found = True
            result["routines"] = [compact({"id": row["id"], "name": clip(row["name"], 60), "status": row["status"],
                "schedule": row.get("schedule"), "program": row.get("program") or row.get("job_kind")})
                for row in sorted(routines, key=lambda row: row["status"] != "active")[:CAPS["routines"]]]
        runs = self.gated("routines_runs", self.run_summary)
        if runs is not None:
            found = True
            own = {row["id"] for row in routines or []}
            for item in result.get("routines", []):
                item.update({key: value for key, value in runs.get(item["id"], {}).items()
                             if key not in {"name", "program", "shared"}})
            shared = [value for key, value in runs.items() if key not in own and value.get("shared")]
            result["shared_routines"] = [{key: value for key, value in item.items() if key != "shared"}
                                         for item in shared[:CAPS["routines"]]]
        jobs = self.gated("jobs_get", self.jobs)
        if jobs is not None:
            found = True
            result.update(jobs)
        if not found:
            raise DomainError("policy_denied", "Routine and job reads are not available")
        result["more"] = "routines_list; routines_runs(routine_id); jobs_get(job_id)"
        return result

    def run_summary(self):
        # routine_runs_business_started bounds this to one business's recent window.
        rows = self.uow.connection.execute(
            "SELECT routine_id, namespace, MAX(started_at) AS last_at, state, COUNT(*) AS runs, SUM(state='failed') AS failed, "
            "json_extract(result_json,'$.definition.name') AS name, json_extract(result_json,'$.definition.program') AS program "
            "FROM routine_runs WHERE business_id=? AND started_at>=? GROUP BY routine_id ORDER BY last_at DESC",
            (self.ctx.business_id, self.since)).fetchall()
        return {row["routine_id"]: compact({"name": clip(row["name"], 60), "program": row["program"],
                "last_run": when(row["last_at"]), "last_state": row["state"], "runs_14d": row["runs"],
                "failed_14d": row["failed"] or None, "shared": row["namespace"] == "global" or None}) for row in rows}

    def jobs(self):
        connection, business = self.uow.connection, self.ctx.business_id
        failed = connection.execute("SELECT id, kind, attempts, error_json, updated_at FROM jobs WHERE state='failed' "
            "AND business_id=? AND updated_at>=? ORDER BY updated_at DESC, id DESC", (business, self.since)).fetchall()
        stale = (self.now - STUCK_AFTER).isoformat().replace("+00:00", "Z")
        stuck = connection.execute("SELECT id, kind, state, updated_at FROM jobs WHERE state IN "
            "('queued','running','applying','cancel_requested') AND business_id=? AND updated_at<? "
            "AND (next_retry_at IS NULL OR next_retry_at<?) ORDER BY updated_at LIMIT ?",
            (business, stale, timestamp(lambda: self.now), CAPS["jobs"] + 1)).fetchall()
        running = connection.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('running','applying') AND business_id=?",
                                     (business,)).fetchone()[0]

        def code(value):
            try:
                return (json.loads(value) or {}).get("code") if value else None
            except (TypeError, ValueError, AttributeError):
                return None
        return compact({"running_jobs": running, "failed_jobs_14d": len(failed),
            "failed_jobs": [compact({"id": row["id"], "kind": row["kind"], "error": code(row["error_json"]),
                "at": when(row["updated_at"])}) for row in failed[:CAPS["jobs"]]],
            "stuck_jobs": [{"id": row["id"], "kind": row["kind"], "state": row["state"], "since": row["updated_at"]}
                           for row in stuck[:CAPS["jobs"]]]})

    def waiting_on_owner(self):
        result, found = {}, False
        listing = self.optional(lambda: self.call("signoffs_list", {"states": ["pending"], "limit": 50}))
        if listing is not None:
            found = True
            rows = [row for row in listing["items"] if row.get("actionable")]

            def kind(row):
                if row["category"] == "input_request":
                    return "question"
                return "approval" if row["category"] == "platform_action" else "change"
            attention = lambda row: (not row["attention"].get("blocking"), not row["attention"].get("urgent"))
            result["pending_signoffs"] = listing["counts"].get("pending", len(rows))
            result["by_kind"] = dict(sorted(Counter(kind(row) for row in rows).items()))
            result["signoffs"] = [compact({"id": row["id"], "kind": kind(row), "title": clip(row.get("title"), 90),
                "by": row.get("proposer_display_name"), "at": when(row.get("created_at")),
                "urgent": row["attention"].get("urgent") or None, "blocking": row["attention"].get("blocking") or None})
                for row in sorted(rows, key=attention)[:CAPS["signoffs"]]]
        if self.tasks is not None and self.owners:
            found = True
            owned = [row for row in self.tasks if not row.get("snoozed") and self.owners & set(row.get("assignee_ids", []))]
            result["owner_tasks"] = len(owned)
            result["owner_task_items"] = [self.task(row) for row in owned[:CAPS["owner_tasks"]]]
        if not found:
            raise DomainError("policy_denied", "Signoff and task reads are not available")
        result["more"] = 'signoffs_list(states=["pending"]); signoffs_get(signoff_id)'
        return compact(result)

    def done_recently(self):
        result, found = {}, False
        if self.tasks is not None:
            found = True
            config, _ = TaskOperations.configuration(self.ctx, self.uow)
            closed = [state["name"] for state in config["states"] if state.get("kind") == "closed"]
            marks = ",".join("?" for _ in closed) or "NULL"
            rows = self.uow.connection.execute("SELECT t.id, t.title, c.completed_at FROM task_completions c "
                "JOIN tasks t ON t.business_id=c.business_id AND t.id=c.task_id WHERE c.business_id=? AND c.completed_at>=? "
                f"AND t.archived=0 AND t.status IN ({marks}) ORDER BY c.completed_at DESC, t.id",
                (self.ctx.business_id, self.since, *closed)).fetchall()
            result["tasks_completed"] = len(rows)
            result["tasks"] = [{"id": row["id"], "title": clip(row["title"], 90), "at": when(row["completed_at"])}
                               for row in rows[:CAPS["done"]]]
        logs = self.optional(lambda: self.call("actions_list", {"since": self.since[:10], "limit": CAPS["done"]}))
        if logs is not None:
            found = True
            result["log"] = [compact({"at": when(row["occurred"].get("instant") or row["occurred"].get("date")),
                                      "tag": ", ".join(row.get("tags", [])) or None,
                                      "summary": clip(row.get("summary"), 110)}) for row in logs["actions"]]
            result["log_more"] = bool(logs.get("next_cursor")) or None
        outcomes = self.gated("signoffs_list", lambda: self.app.signoffs.recent_outcomes(
            self.ctx, self.uow, since=self.since, limit=CAPS["done"]) if self.app.signoffs else None)
        if outcomes is not None:
            found = True
            result["signoff_outcomes_14d"] = outcomes["count"]
            result["signoff_outcomes"] = [compact({"id": row["id"], "title": clip(row.get("title"), 80),
                "state": row["state"], "execution": row.get("execution_state"), "at": when(row.get("updated_at"))})
                for row in outcomes["items"]]
        if not found:
            raise DomainError("policy_denied", "Task, log and Signoff reads are not available")
        result["more"] = f'actions_list(since="{self.since[:10]}"); tasks_list(statuses=["done"]); signoffs_get(signoff_id)'
        return compact(result)

    def agents(self):
        # The business view lists the active globals with only this business's visible work; the brief keeps the ones
        # with current work here, never an idle global merely because it can reach the business.
        listing = self.call("agents_activity_list", {"limit": 50})

        def work_of(row):
            current = row.get("current_work")
            if current is not None:  # A worker's one shared assignment; its old session list is never a second one.
                # Its source is redacted to no business when this reader cannot read it, so only work visibly belonging to
                # this business is briefed (the global team view keeps the public text).
                item = current.get("item")
                if not item or item.get("business_id") != self.ctx.business_id:
                    return []
                return [clip(item.get("summary") or item.get("title") or item.get("blocked_reason") or "Task in progress", 100)
                        + (" (blocked)" if item.get("blocked") else "")]
            return [clip(item.get("summary"), 100) + (" (blocked)" if item.get("blocked") else "")
                    for session in row.get("sessions", []) for item in session.get("items", [])]
        rows = [row for row in listing["agents"] if work_of(row)]

        def view(row):
            return compact({"agent_id": row["agent_id"], "name": row.get("display_name") or row.get("name"),
                "role": row.get("role_id"), "state": row.get("state"), "last_seen": row.get("last_seen_at"),
                "work": work_of(row)[:CAPS["work"]]})
        return {"items": [view(row) for row in rows[:CAPS["agents"]]], "more": "agents_activity_list"}

    def notes(self):
        rows = self.call("notes_list", {"target_business_id": self.ctx.business_id, "limit": 50})["items"]
        rows = sorted(rows, key=lambda row: row.get("updated_at") or "", reverse=True)
        return {"items": [{"id": row["id"], "title": clip((row.get("metadata") or {}).get("title"), 80),
                           "updated": when(row.get("updated_at"))} for row in rows[:CAPS["notes"]]],
                "more": "notes_list(target_business_id=..., tag=...); notes_get(document_id)"}


def bound(result):
    """Drop trailing list items from the largest lists until the answer fits."""
    def size():
        return len(json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str))
    trimmed = False
    while size() > MAX_CHARACTERS:
        lists = []

        def walk(value):
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                if len(value) > 1:
                    lists.append(value)
                for item in value:
                    walk(item)
        walk(result)
        if not lists:
            break
        largest = max(lists, key=lambda value: len(json.dumps(value, ensure_ascii=False, default=str)))
        largest.pop()
        trimmed = True
    if trimmed:
        result["trimmed"] = True
    return result
