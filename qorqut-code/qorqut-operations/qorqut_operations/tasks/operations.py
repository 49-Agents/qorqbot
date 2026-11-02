"""Task records, configuration, discussion, assignment and follows: the v1 operations on the new Task model.

Every operation keeps its v1 name, namespace rule (one owning business, or Global for the owner and granted global
agents), prepare/apply authority, revision checks and idempotency. What changed is the model underneath: five fixed
states, one shared ``snoozed_until``, a parent Task, labels from the shared catalog, and a single discussion writer
(``thread_messages``) for comments, summaries, typed references and the system entries that describe a change. Events
are written by the operation that changed the Task, in its own transaction; see ``events``.

Host-specific behaviour (who may be assigned, Focus presentation, the incident link, host note
references, owner-work routing) arrives through ``TaskPorts``."""

from __future__ import annotations

from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.plans import (DATE, IDENTITY, PAGE, TEXT, UUID, PlanBuilder, apply_records, page, ref,
                                                 require_revision, rows, schema, timestamp, utc_clock)
from qorqut_operations.application.registry import CatalogEntry, OperationDescriptor
from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.conversations import SUMMARY, participants
from qorqut_operations.discussion.follows import ThreadItem, auto_follow, can_follow, follow_state
from qorqut_operations.discussion.threads import BODY, RELAY, DiscussionReferences, message_count, plan_comment, read_messages
from qorqut_operations.global_agents import global_agent_ids, is_global_agent
from qorqut_operations.items import ItemResolver
from qorqut_operations.relationships import create_edge, related_rows
from qorqut_operations.storage.assembly import AssemblyError
from qorqut_operations.tags.catalog import LABELS_SCHEMA, canonical_labels, item_labels, labels_by_item, matches, plan_item_tags
from qorqut_operations.tasks import events, links
from qorqut_operations.tasks.models import (DEFAULT_CONFIG, DEFAULT_STATE, full_config, normalize_snooze, snooze_view, stored_fields,
                                            validate_config, validate_dependencies, validate_parent, validate_task)
from qorqut_operations.tasks.overview import TaskOverview
from qorqut_operations.tasks.ports import TaskPorts
from qorqut_operations.tasks.scope import task_context, task_namespace
from qorqut_operations.tasks.schema import CLOSED_STATES, KINDS, PAUSED_STATES, STATES
from qorqut_operations.tasks.snooze import reconcile_snoozes
from qorqut_operations.tasks.types import TYPES, apply_attributes, attributes, custom_fields
from qorqut_operations.team_channel import reject_owner_team_write

TASK_READS = ("tasks_list", "tasks_get", "tasks_thread_get", "tasks_notes_list", "tasks_config_get", "assignees_list", "tasks_overview")
TASK_MUTATIONS = ("tasks_create", "tasks_update", "tasks_assign", "tasks_comment", "tasks_complete", "tasks_config_update")
TASK_FOLLOW = "tasks_follow"
TASK_ENTRIES = (
    *(CatalogEntry(name, False, "read") for name in TASK_READS),
    *(CatalogEntry(name, True, "records") for name in TASK_MUTATIONS),
    CatalogEntry(TASK_FOLLOW, False, "records"),
)

ASSIGNEES = {"type": "array", "items": UUID, "maxItems": 100}
SCOPE = {"scope": {"enum": ["business", "global"]}}
TASK_FIELDS = {
    "task_type": {"enum": list(TYPES), "description": "task for an actionable step; story for an ongoing relationship, opportunity or work group; email for one whole email conversation."},
    "overview": {"type": "string", "maxLength": 20000, "description": "Maintained current summary and next steps; history stays in the Task discussion and related items."},
    "email_conversation_id": {"anyOf": [IDENTITY, {"type": "null"}], "description": "Required for email type: an existing readable email conversation in the same owning scope. Clear when changing to another type."},
    "kind": {"enum": list(KINDS)}, "title": {"type": "string", "minLength": 1, "maxLength": 1000},
    "status": {"enum": list(STATES)}, "assignee_ids": ASSIGNEES, "archived": {"type": "boolean"}, "tags": LABELS_SCHEMA,
    "due": {"anyOf": [DATE, {"type": "null"}]}, "blocked_by": {"type": "array", "items": IDENTITY, "maxItems": 100, "uniqueItems": True},
    "fields": {"type": "object", "maxProperties": 100},
    "snoozed_until": {"description": "One shared deferral of the Task's due reminder, as a timezone-aware future timestamp; null "
                      "clears it. It is not a state, a due date or a subscription change.", "anyOf": [{"type": "string", "maxLength": 64}, {"type": "null"}]},
    "parent_id": {"description": "The parent Task in the same owning scope (null for none). Parent and child states are independent.",
                  "anyOf": [IDENTITY, {"type": "null"}]},
}
FOLLOW = schema({"task_id": IDENTITY, "following": {"type": "boolean"}}, ("task_id", "following"))


class TaskOperations:
    def __init__(self, application, items: ItemResolver, ports: TaskPorts, *, clock=utc_clock):
        if not isinstance(items, ItemResolver):
            raise AssemblyError("Tasks need the frozen ItemResolver built by trusted assembly code")
        if not isinstance(ports, TaskPorts):
            raise AssemblyError("Tasks need the TaskPorts built by trusted assembly code")
        if not callable(clock):
            raise AssemblyError("The task clock is a callable returning an aware timestamp")
        self.application, self.items, self.ports, self.clock = application, items, ports, clock
        self.overview = TaskOverview(self, application)
        unknown = sorted(set(ports.references.types) - set(items.kinds) - set(ports.references.host_types))
        if unknown:
            raise AssemblyError("Note reference types are neither engine items nor host references: " + ", ".join(unknown))
        self.note_ref_types = list(ports.references.types)

    # -- registration ----------------------------------------------------------------------------------------------

    def contribution(self) -> CatalogContribution:
        return CatalogContribution("tasks", TASK_ENTRIES, self.descriptors())

    def descriptors(self):
        refs = self.note_references().schema
        common = dict(scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
                      resolve_namespace=task_namespace,
                      description="Manage tasks in the selected project, or use scope=global for Global-only work. Global tasks are "
                                  "not inherited by projects; only the owner and granted global agents have access.")
        reads = {
            "tasks_list": {**PAGE, "assignee_id": {"anyOf": [UUID, {"type": "null"}]}, "include_archived": {"type": "boolean"},
                           "task_type": {"enum": list(TYPES)},
                           "email_conversation_id": IDENTITY,
                           "statuses": {"type": "array", "items": {"enum": list(STATES)}}, "tags": LABELS_SCHEMA,
                           "include_closed": {"type": "boolean"}, "include_paused": {"type": "boolean"}, "blocked": {"type": "boolean"},
                           "due_before": DATE, "search": {"type": "string", "maxLength": 500},
                           "parent_id": {"anyOf": [IDENTITY, {"type": "null"}], "description": "Children of this parent, or null for top-level tasks."},
                           "snooze_filter": {"enum": ["active", "snoozed", "all"], "default": "active",
                                             "description": "Defaults to not currently snoozed. Snoozed tasks are the backlog. Closed/archived filters apply independently."}},
            "tasks_get": {"task_id": IDENTITY, "related_cursor": PAGE["cursor"]}, "tasks_thread_get": {**PAGE, "task_id": IDENTITY},
            "tasks_notes_list": {**PAGE, "task_id": IDENTITY}, "tasks_config_get": {}, "assignees_list": {},
        }
        descriptors = [self.overview.descriptor()]
        for name, properties in reads.items():
            descriptors.append(OperationDescriptor(
                name, schema({**SCOPE, **properties}, ("task_id",) if name in {"tasks_get", "tasks_notes_list", "tasks_thread_get"} else ()),
                **common, effect_kind="read", supports_signoff=False, read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow)))
        comment = {"note_refs": refs, "summary": SUMMARY, "relay": RELAY}
        mutations = {
            "tasks_create": ({"focus": schema(self.ports.focus.schema), **TASK_FIELDS, "id": IDENTITY, "note": TEXT, **comment}, ("title",)),
            "tasks_update": ({"task_id": IDENTITY, "changes": {**schema({**TASK_FIELDS, "related_task": links.EDIT}), "minProperties": 1}, "note": TEXT, **comment}, ("task_id", "changes")),
            "tasks_assign": ({"task_id": IDENTITY, "assignee_ids": ASSIGNEES}, ("task_id", "assignee_ids")),
            "tasks_comment": ({"task_id": IDENTITY, "text": BODY, **comment}, ("task_id", "text")),
            "tasks_complete": ({"task_id": IDENTITY, "status": {"enum": list(CLOSED_STATES)}, "note": TEXT, **comment}, ("task_id",)),
            "tasks_config_update": ({"config": {"type": "object"}}, ("config",)),
        }
        for name, (properties, required) in mutations.items():
            options = common
            if name in {"tasks_create", "tasks_update"}:
                options = {**common, "description": common["description"] +
                           " Set department tags from the work, independently of assignees. Marketing work: marketing. "
                           "Code, landing-page updates, design, logos, DevOps and deployment: engineering; add marketing "
                           "too when relevant. Custom scripts follow their purpose, not automatically engineering. "
                           "Use multiple departments for cross-functional work. Use the ordinary design/software tag for "
                           "software, code fixes, scripts, tests/CI, IaC, landing pages, design and visual assets, "
                           "independently of department or assignee. Preserve unrelated tags on updates. Tags come from the shared "
                           "tag catalog (tags_list); a new label is created by using it."}
            descriptors.append(OperationDescriptor(
                name, schema({**SCOPE, **properties, "rationale": TEXT}, required), **options,
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow),
                apply=self.apply, validate_targets=self.validate_prepared))
        descriptors.append(OperationDescriptor(
            TASK_FOLLOW, schema({**SCOPE, **FOLLOW.get("properties", {})}, ("task_id", "following")),
            **{**common, "description": "Follow or unfollow a task's discussion for yourself. Creators, assignees and commenters follow by "
                                        "default; an explicit unfollow stays until you follow again. Required responsibility updates "
                                        "still reach responsible assignees."},
            supports_signoff=False, prepare=self.prepare_follow, apply=self.apply_follow))
        return tuple(descriptors)

    # -- configuration ---------------------------------------------------------------------------------------------

    @staticmethod
    def configuration(ctx, uow):
        """``(config, row)``: the fixed states, the catalog reference and the scope's custom field definitions."""
        row = uow.repository("business_configs", business_id=ctx.business_id).get("tasks")
        return full_config(stored_fields(plain(row["config_json"]) if row else DEFAULT_CONFIG)), row

    # -- eligibility and graph -------------------------------------------------------------------------------------

    @staticmethod
    def graph(ctx, uow):
        graph = {task["id"]: set() for task in rows(uow, "tasks", ctx.business_id)}
        for blocker in related_rows(uow, ctx.business_id, role="blocked_by"):
            if blocker["from_type"] == blocker["to_type"] == "task" and blocker["from_id"] in graph:
                graph[blocker["from_id"]].add(blocker["to_id"])
        return graph

    @staticmethod
    def parents(ctx, uow):
        return {task["id"]: task["parent_id"] for task in rows(uow, "tasks", ctx.business_id)}

    def assignee(self, ctx, identity, uow, builder=None):
        self.ports.assignees.protect(ctx, identity, uow)
        if self.ports.assignees.owner_directed(ctx, identity, uow):
            raise DomainError("invalid_assignee", "Only the owner can assign work to Learning Applier")
        if ctx.business_id is None:
            actor = uow.repository("principals").get(identity)
            profile = uow.repository("agent_profiles", business_id=None).get(identity)
            installation = uow.repository("installation").get("service")
            allowed = actor and actor["active"] and (
                (actor["kind"] == "human" and installation and installation["owner_principal_id"] == identity)
                or (actor["kind"] == "agent" and is_global_agent(uow.connection, identity)))
            if not allowed:
                raise DomainError("invalid_assignee", "Global tasks may be assigned only to the owner or active global agents")
            if builder:
                builder.expect(ref("principals", identity, None), actor)
                builder.expect(ref("agent_profiles", identity, None), profile)
                builder.expect(ref("installation", "service", None), installation)
            return
        actor = uow.repository("principals").get(identity)
        if actor and actor["kind"] == "agent":
            # Only an active global agent takes new work, in every active business without a membership row; its principal
            # and profile are the facts this assignment depends on, so a retirement or deactivation before application
            # invalidates it. A business-home profile is history and never a new assignee.
            if not is_global_agent(uow.connection, identity) or ctx.business_id not in active_business_ids(uow.connection):
                raise DomainError("invalid_assignee", "Agent assignees must be active global agents")
            if builder:
                builder.expect(ref("principals", identity, None), actor)
                builder.expect(ref("agent_profiles", identity, None, "ecosystem"),
                               uow.repository("agent_profiles", business_id=None).get(identity))
            return
        member = uow.repository("business_memberships", business_id=ctx.business_id).get(identity)
        if not member or not member["active"] or not actor or not actor["active"] or actor["kind"] != "human":
            raise DomainError("invalid_assignee", "Assignee must be an active global agent or human member of this project")
        if builder:
            builder.expect(ref("business_memberships", identity, ctx.business_id), member)
            builder.expect(ref("principals", identity, ctx.business_id), actor)

    # -- note references -------------------------------------------------------------------------------------------

    def note_refs(self, ctx, refs, uow, builder=None, *, strict=False):
        """Resolve a note's typed references through the item resolver or the host's own reference types."""
        found, seen = [], set()
        for item in refs:
            key = (item["type"], item["id"])
            if key in seen:
                raise DomainError("validation_error", "Use distinct note references")
            seen.add(key)
            try:
                if item["type"] in self.ports.references.host_types:
                    view = self.ports.references.resolve(ctx, item, uow, builder)
                else:
                    view = self.items.resolve(ctx, item, uow, builder)
                found.append({**item, "label": view["label"], "state": view.get("state"),
                              **({"resource_type": view["resource_type"]} if item["type"] == "material" else {})})
            except DomainError as error:
                if builder or strict or error.code not in {"not_found", "forbidden", "policy_denied", "operation_unavailable"}:
                    raise
                found.append({"unavailable": True})
        return found

    def note_references(self) -> DiscussionReferences:
        """These reference ports as any item discussion (Tasks, and Actions through ``ActionOperations``) uses them."""
        return DiscussionReferences(tuple(self.note_ref_types), self.note_refs, self.message_refs)

    def message_refs(self, ctx, uow):
        def resolve(message):
            refs = list(message["refs"])
            link = self.ports.references.derived(ctx, uow, message) if message.get("item_type") == "task" else None
            if link is None or link in refs:
                return self.note_refs(ctx, refs, uow)
            if link.get("unavailable"):
                return [*self.note_refs(ctx, refs, uow), link]
            return self.note_refs(ctx, [*refs, link], uow)
        return resolve

    # -- reads -----------------------------------------------------------------------------------------------------

    def detail(self, ctx, task, uow, fields, now, *, notes=True):
        business_id, task_id = task["business_id"], task["id"]
        item = ThreadItem(business_id, "task", task_id)
        creator = uow.repository("principals").get(task.get("created_by")) if task.get("created_by") else None
        scope = uow.repository("tasks", business_id=business_id)
        blockers = events.blockers(uow, business_id, task_id)
        unresolved = [scope.get(blocker)["id"] for blocker in blockers
                      if (scope.get(blocker) or {"status": "done"})["status"] not in CLOSED_STATES]
        blocks = [row[0] for row in uow.connection.execute(
            "SELECT DISTINCT t.id FROM relationships r JOIN tasks t ON t.business_id IS r.business_id AND t.id=r.from_id "
            "WHERE r.business_id IS ? AND r.from_type='task' AND r.to_type='task' AND r.to_id=? AND r.role='blocked_by' "
            "AND t.status NOT IN ('done','cancelled') ORDER BY t.id", (business_id, task_id))]
        parent = scope.get(task["parent_id"]) if task.get("parent_id") else None
        children = [dict(row) for row in uow.connection.execute(
            "SELECT id,title,status FROM tasks WHERE business_id IS ? AND parent_id=? ORDER BY created_at,id LIMIT 51", (business_id, task_id))]
        follow = follow_state(uow, item, ctx.principal_id)
        view = {**{key: value for key, value in task.items() if key not in {"fields_json", "snoozed_until", "snooze_generation"}},
                **snooze_view(task, now),
                "created_by_name": creator.get("display_name", "") if creator else "",
                "created_by_kind": creator.get("kind", "") if creator else "",
                "tags": item_labels(uow, business_id, "task", task_id), "fields": custom_fields(task), **attributes(task),
                "thread_id": item.thread_id, "note_count": message_count(uow, item),
                "assignee_ids": events.assigned(uow, business_id, task_id), "archived": bool(task.get("archived")),
                "blocked_by": blockers, "unresolved_blockers": sorted(set(unresolved)), "blocks": blocks,
                "parent": {key: parent[key] for key in ("id", "title", "status")} if parent else None,
                "children": children[:50], "children_has_more": len(children) > 50,
                "closed": task["status"] in CLOSED_STATES, "paused": task["status"] in PAUSED_STATES,
                "following": follow["following"], "follow_reason": follow["reason"]}
        view["notes"], view["notes_next_cursor"] = [], None
        if notes:
            history = read_messages(uow, item, {"limit": 20}, "tasks_get", ctx.principal_id, resolve_refs=self.message_refs(ctx, uow),
                                    kinds=("comment",))
            view["notes"], view["notes_next_cursor"] = history["messages"], history["next_cursor"]
        return view

    def read(self, operation, ctx, arguments, uow):
        ctx = task_context(ctx, arguments, uow)
        configuration, config_row = self.configuration(ctx, uow)
        if operation == "tasks_config_get":
            return {"config": configuration, "revision": config_row["revision"] if config_row else None}
        if operation == "assignees_list":
            return self.assignees_list(ctx, uow)
        repository = uow.repository("tasks", business_id=ctx.business_id)
        now = timestamp(self.clock)
        if operation in {"tasks_thread_get", "tasks_notes_list"}:
            repository.get(arguments["task_id"], required=True)
            item = ThreadItem(ctx.business_id, "task", arguments["task_id"])
            if operation == "tasks_notes_list":
                history = read_messages(uow, item, arguments, operation, ctx.principal_id, resolve_refs=self.message_refs(ctx, uow),
                                        kinds=("comment",))
                return {"thread_id": history["thread_id"], "notes": history["messages"], "next_cursor": history["next_cursor"]}
            history = read_messages(uow, item, arguments, operation, ctx.principal_id, resolve_refs=self.message_refs(ctx, uow))
            task = repository.get(arguments["task_id"], required=True)
            people = {task["created_by"], *(note["created_by"] for note in history["messages"]), *events.assigned(uow, ctx.business_id, task["id"])}
            return {"thread_id": history["thread_id"], "participants": participants(uow, people), "notes": history["messages"],
                    "next_cursor": history["next_cursor"]}
        if operation == "tasks_get":
            task = self.detail(ctx, repository.get(arguments["task_id"], required=True), uow, configuration["fields"], now)
            task.update(links.read(self, ctx, repository.get(arguments["task_id"], required=True), uow, arguments.get("related_cursor")))
            if ctx.business_id:
                task["campaign"] = self.ports.campaigns.view(uow, ctx.business_id, task["id"], task["tags"])
            return {"task": task}
        return self.list_tasks(ctx, arguments, uow, configuration, now)

    def assignees_list(self, ctx, uow):
        directed = self.ports.assignees.owner_directed
        # Active global agents are candidates in Global and in every active business, without membership rows.
        global_agents = set(global_agent_ids(uow.connection))
        if ctx.business_id is None:
            found = uow.connection.execute(
                "SELECT p.id,p.kind,p.display_name,a.name FROM principals p LEFT JOIN agent_profiles a ON a.agent_id=p.id WHERE p.active=1 "
                "AND ((p.kind='human' AND p.id=(SELECT owner_principal_id FROM installation WHERE id='service')) OR p.kind='agent') "
                "ORDER BY p.kind,p.display_name,p.id")
            return {"assignees": [{**dict(row), "is_owner": row["kind"] == "human"} for row in found
                                  if (row["kind"] == "human" or row["id"] in global_agents) and not directed(ctx, row["id"], uow)]}
        if ctx.business_id not in active_business_ids(uow.connection):
            global_agents = set()
        found = uow.connection.execute(
            "SELECT p.id,p.kind,p.display_name,a.name,m.is_owner,m.active AS member FROM principals p "
            "LEFT JOIN business_memberships m ON m.principal_id=p.id AND m.business_id=? LEFT JOIN agent_profiles a ON a.agent_id=p.id "
            "WHERE p.active=1 AND p.kind IN ('agent','human') ORDER BY p.kind,p.display_name,p.id", (ctx.business_id,))
        return {"assignees": [{"id": row["id"], "kind": row["kind"], "display_name": row["display_name"], "name": row["name"],
                               "is_owner": row["kind"] == "human" and bool(row["is_owner"])} for row in found
                              if (row["id"] in global_agents or (row["kind"] == "human" and row["member"]))
                              and not directed(ctx, row["id"], uow)]}

    def list_tasks(self, ctx, arguments, uow, configuration, now):
        clauses, parameters = [], []
        snooze_filter = arguments.get("snooze_filter", "active")
        if snooze_filter != "all":
            active = "(snoozed_until IS NULL OR snoozed_until<=?)"
            clauses.append(active if snooze_filter == "active" else "NOT " + active)
            parameters.append(now)
        if "assignee_id" in arguments:
            assignee = arguments["assignee_id"]
            clauses.append(("" if assignee else "NOT ") + "EXISTS (SELECT 1 FROM relationships r WHERE r.business_id IS tasks.business_id "
                           "AND r.from_type='task' AND r.from_id=tasks.id AND r.role='assigned_to'" + (" AND r.to_id=?" if assignee else "") + ")")
            if assignee:
                parameters.append(assignee)
        if "parent_id" in arguments:
            clauses.append("parent_id IS ?")
            parameters.append(arguments["parent_id"])
        if not arguments.get("include_archived"):
            clauses.append("archived=0")
        states = [name for name in STATES if (not arguments.get("statuses") or name in arguments["statuses"])
                  and (arguments.get("include_closed", True) or name not in CLOSED_STATES)
                  and (arguments.get("include_paused", True) or name not in PAUSED_STATES)]
        clauses.append("status IN (" + (",".join("?" for _ in states) or "NULL") + ")")
        parameters.extend(states)
        if arguments.get("due_before"):
            clauses.append("due IS NOT NULL AND due<=?")
            parameters.append(arguments["due_before"])
        tasks = rows(uow, "tasks", ctx.business_id, where=" AND ".join(clauses), parameters=parameters)
        if "task_type" in arguments:
            tasks = [task for task in tasks if attributes(task)["task_type"] == arguments["task_type"]]
        if "email_conversation_id" in arguments:
            tasks = [task for task in tasks if attributes(task)["email_conversation_id"] == arguments["email_conversation_id"]]
        if arguments.get("tags"):
            wanted = canonical_labels(arguments["tags"])
            labels = labels_by_item(uow, ctx.business_id, "task")
            tasks = [task for task in tasks if matches(labels.get(task["id"], ()), wanted)]
        if arguments.get("search"):
            tasks = [task for task in tasks if arguments["search"].casefold() in (task["title"] + " " + task["id"]).casefold()]
        if "blocked" in arguments:
            detailed = [self.detail(ctx, task, uow, configuration["fields"], now, notes=False) for task in tasks]
            tasks = [task for task in detailed if bool(task["unresolved_blockers"]) == arguments["blocked"]]
            selected, cursor = page(tasks, arguments, ctx.business_id, "tasks_list")
            return {"tasks": selected, "next_cursor": cursor}
        selected, cursor = page(tasks, arguments, ctx.business_id, "tasks_list")
        return {"tasks": [self.detail(ctx, task, uow, configuration["fields"], now, notes=False) for task in selected], "next_cursor": cursor}

    # -- writes ----------------------------------------------------------------------------------------------------

    def prepare(self, operation, ctx, arguments, options, uow, *, team_note=True):
        """``team_note=False`` marks a caller whose ``note`` is the action's own generated record (such as campaign reply
        routing) rather than an ordinary comment, so the owner's private-channel guard does not apply to it."""
        ctx = task_context(ctx, arguments, uow)
        builder = PlanBuilder(ctx, operation, arguments, operation.replace("_", " ").title(), clock=self.clock,
                              namespace=task_namespace(ctx, arguments),
                              affected_scope={"kind": "business", "business_id": ctx.business_id} if ctx.business_id else {"kind": "global"})
        configuration, config_row = self.configuration(ctx, uow)
        fields = configuration["fields"]
        builder.expect(ref("business_configs", "tasks", ctx.business_id), config_row)
        if operation == "tasks_config_update":
            return self.prepare_config(ctx, arguments, options, uow, builder, config_row)
        if arguments.get("focus") is not None:
            self.ports.focus.creation_access(ctx, arguments["focus"], uow)
        creating = operation == "tasks_create"
        identity = arguments.get("id") or new_id() if creating else arguments["task_id"]
        before = uow.repository("tasks", business_id=ctx.business_id).get(identity)
        if creating and before is not None:
            raise DomainError("revision_conflict", "Task identity already exists", current_revision=before["revision"])
        if not creating:
            if before is None:
                raise DomainError("not_found", "Task does not exist in this scope")
            require_revision(before, options.expected_revision)
        edits = {key: value for key, value in arguments.items() if key in TASK_FIELDS} if creating else plain(arguments.get("changes", {}))
        if operation == "tasks_assign":
            edits["assignee_ids"] = arguments["assignee_ids"]
        if operation == "tasks_complete":
            status = arguments.get("status", "done")
            if status not in CLOSED_STATES:
                raise DomainError("validation_error", "Completion requires a terminal state (done or cancelled)")
            edits["status"] = status
        current = dict(before) if before else {
            "id": identity, "title": arguments["title"], "status": DEFAULT_STATE, "due": None, "fields_json": {}, "archived": False,
            "created_by": ctx.principal_id, "created_at": builder.at, "updated_at": builder.at, "kind": "ordinary", "parent_id": None,
            "snoozed_until": None, "snooze_generation": 0}
        link_edit = edits.pop("related_task", None)
        blockers = edits.pop("blocked_by", None)
        assignees = edits.pop("assignee_ids", [ctx.agent_id] if creating and ctx.agent_id else [] if creating else None)
        labels = edits.pop("tags", None)
        if "snoozed_until" in edits:
            requested = normalize_snooze(edits.pop("snoozed_until"), builder.at, current["snoozed_until"])
            if requested != current["snoozed_until"]:
                current["snoozed_until"] = requested
                if requested is not None:
                    current["snooze_generation"] += 1
        apply_attributes(current, edits)
        if link_edit is not None:
            links.edit(self, ctx, current, identity, link_edit, uow, builder)
        for key, value in edits.items():
            current["fields_json" if key == "fields" else key] = value
        validate_task(current, fields)
        if attributes(current)["email_conversation_id"] != attributes(before or {})["email_conversation_id"]:
            self.email_reference(ctx, current, uow, builder)
        parents = self.parents(ctx, uow)
        if creating:
            parents[identity] = None
        validate_parent(identity, current["parent_id"], parents)
        if current["parent_id"] and (before or {}).get("parent_id") != current["parent_id"]:
            builder.expect(ref("tasks", current["parent_id"], ctx.business_id),
                           uow.repository("tasks", business_id=ctx.business_id).get(current["parent_id"], required=True))
        if creating and current["kind"] == "owner_request":
            installation = uow.repository("installation").get("service")
            if not installation or not assignees or installation["owner_principal_id"] not in assignees:
                raise DomainError("validation_error", "Owner-request tasks must explicitly assign the installation owner")
        for assignee in assignees or ():
            self.assignee(ctx, assignee, uow, builder)
        values = {key: value for key, value in current.items() if key not in {"business_id", "revision"}}
        resource = ref("tasks", identity, ctx.business_id)
        mark = len(builder.changes)
        if before:
            values = {key: value for key, value in values.items() if key not in {"created_at", "created_by"} and before.get(key) != value}
        else:
            builder.record("tasks", identity, None, values, label=current["title"])
        if assignees is not None and operation not in {"tasks_assign", "tasks_create"}:
            builder.policy_effect(resource, "tasks_assign", "Changes the responsible assignees")
        if assignees is not None:
            targets = [{"type": "agent" if uow.repository("principals").get(assignee)["kind"] == "agent" else "person", "id": assignee}
                       for assignee in assignees]
            self.replace_edges(builder, uow, identity, "assigned_to", targets)
        if before and before["status"] not in CLOSED_STATES and current["status"] in CLOSED_STATES and operation != "tasks_complete":
            builder.policy_effect(resource, "tasks_complete", "Completes the task through a terminal state")
        if labels is not None:
            wanted = canonical_labels(labels)
            previous = [] if creating else item_labels(uow, ctx.business_id, "task", identity)
            plan_item_tags(builder, uow, "task", identity, ctx.business_id, wanted, created=creating)
            self.ports.campaigns.plan(ctx, uow, builder, identity, previous, wanted)
        if blockers is not None:
            graph = self.graph(ctx, uow)
            if creating:
                graph[identity] = set()
            validate_dependencies(identity, blockers, graph)
            for blocker_id in blockers:
                builder.expect(ref("tasks", blocker_id, ctx.business_id),
                               uow.repository("tasks", business_id=ctx.business_id).get(blocker_id, required=True))
            self.replace_edges(builder, uow, identity, "blocked_by", [{"type": "task", "id": blocker} for blocker in blockers])
        self.plan_comment(ctx, uow, builder, operation, arguments, identity, team_note=team_note)
        if before:
            # An update that changes nothing is a no-op: the Task keeps its revision (so repeating a write is idempotent).
            if values or any(change.action != "policy_effect" for change in builder.changes[mark:]):
                builder.record("tasks", identity, before, {**values, "updated_at": builder.at}, label=current["title"])
            else:
                builder.expect(resource, before)
        builder.effects.append("Updates the task in its owning scope. No agent session is started, resumed, or notified through a terminal.")
        return builder.build()

    def email_reference(self, ctx, task, uow, builder=None):
        identity = attributes(task)["email_conversation_id"]
        if identity is None:
            return
        item = self.items.resolve(ctx, {"type": "material", "id": identity}, uow, builder)
        if item["resource_type"] != "email_conversations":
            raise DomainError("validation_error", "Select an email conversation in this Task's owning scope")

    def prepare_config(self, ctx, arguments, options, uow, builder, config_row):
        require_revision(config_row, options.expected_revision)
        fields = validate_config(arguments["config"])
        for task in rows(uow, "tasks", ctx.business_id):
            validate_task(task, fields)
            builder.expect(ref("tasks", task["id"], ctx.business_id), task)
        stored = {"v": 1, "fields": fields}
        builder.record("business_configs", "tasks", config_row, {"config_json": stored} if config_row else {"domain": "tasks", "config_json": stored})
        return builder.build()

    def plan_comment(self, ctx, uow, builder, operation, arguments, identity, *, team_note=True):
        """The one discussion write of an operation: its comment, summary and typed references, with attribution."""
        plan_comment(ctx, uow, builder, ThreadItem(ctx.business_id, "task", identity),
                     note=arguments.get("text") if operation == "tasks_comment" else arguments.get("note"),
                     summary=arguments.get("summary"), refs=arguments.get("note_refs", []), relay=arguments.get("relay"),
                     resolve=lambda refs: self.note_refs(ctx, refs, uow, builder),
                     guard=(lambda: reject_owner_team_write(ctx, uow, operation)) if team_note else None,
                     operation_name="tasks_comment" if operation != "tasks_comment" else None)

    def validate_prepared(self, ctx, plan, uow):
        ctx = task_context(ctx, plan.arguments, uow)
        self.note_refs(ctx, plan.arguments.get("note_refs", ()), uow, strict=True)
        configuration, _ = self.configuration(ctx, uow)
        fields = configuration["fields"]
        if plan.operation_name == "tasks_config_update":
            proposed = plain(next(change.values["config_json"] for change in plan.changes if change.resource.resource_type == "business_configs"))
            for task in rows(uow, "tasks", ctx.business_id):
                validate_task(task, proposed["fields"])
            return
        task_id = self.planned_task(plan)
        task_change = next((change for change in plan.changes if change.resource.resource_type == "tasks"), None)
        current = uow.repository("tasks", business_id=ctx.business_id).get(task_id) or {}
        proposed = {**current, **plain(task_change.values)} if task_change else current
        validate_task(proposed, fields)
        link_edit = plan.arguments.get("changes", {}).get("related_task")
        if link_edit is not None:
            links.resolve(self, ctx, link_edit, uow)
        if attributes(proposed)["email_conversation_id"] != attributes(current)["email_conversation_id"]:
            self.email_reference(ctx, proposed, uow)
        parents = self.parents(ctx, uow)
        parents[task_id] = proposed.get("parent_id")
        validate_parent(task_id, proposed.get("parent_id"), parents)
        graph = self.graph(ctx, uow)
        graph.setdefault(task_id, set())
        blockers = set(graph[task_id])
        for change in plan.changes:
            if change.resource.resource_type == "relationships":
                edge = (uow.repository("relationships", business_id=ctx.business_id).get(change.resource.resource_id)
                        if change.action == "delete" else change.values)
                if edge["role"] == "assigned_to" and change.action != "delete":
                    self.assignee(ctx, edge["to_id"], uow)
                if edge["role"] == "blocked_by":
                    (blockers.discard if change.action == "delete" else blockers.add)(edge["to_id"])
        validate_dependencies(task_id, list(blockers), graph)

    @staticmethod
    def planned_task(plan):
        """The Task a plan writes: its recorded row, or the named Task of an update that changed nothing."""
        change = next((change for change in plan.changes if change.resource.resource_type == "tasks"), None)
        return change.resource.resource_id if change else plan.arguments["task_id"]

    @staticmethod
    def replace_edges(builder, uow, task_id, role, targets):
        source = {"type": "task", "id": task_id}
        existing = related_rows(uow, builder.ctx.business_id, source=source, role=role)
        wanted = {(target["type"], target["id"]) for target in targets}
        old = {(edge["to_type"], edge["to_id"]) for edge in existing}
        for edge in existing:
            if (edge["to_type"], edge["to_id"]) not in wanted:
                builder.record("relationships", edge["id"], edge, {}, action="delete")
        for kind, identity in sorted(wanted - old):
            create_edge(builder, source, {"type": kind, "id": identity}, role)

    def apply(self, ctx, plan, uow):
        ctx = task_context(ctx, plan.arguments, uow)
        messages = [change for change in plan.changes if change.resource.resource_type == "thread_messages"]
        if messages:
            reject_owner_team_write(ctx, uow, plan.operation_name)
        if plan.operation_name == "tasks_config_update":
            apply_records(ctx, plan, uow)
            configuration, row = self.configuration(ctx, uow)
            return {"config": configuration, "revision": row["revision"]}
        identity = self.planned_task(plan)
        before = events.state(uow, ctx.business_id, identity)
        apply_records(ctx, plan, uow)
        after = events.state(uow, ctx.business_id, identity)
        self.announce(ctx, uow, plan, before, after, [plain(change.values) for change in messages])
        for item in plan.arguments.get("note_refs", ()):
            if item["type"] == "dev_ticket":
                self.ports.incidents.link_task(uow, item["id"], ctx.business_id, identity, ctx.principal_id, timestamp(self.clock))
        if "focus" in plan.arguments:
            self.ports.focus.present_created(ctx, plan.arguments["focus"], {"type": "task", "id": identity, "business_id": ctx.business_id}, uow)
        return self.read("tasks_get", ctx, {"task_id": identity, **({"scope": "global"} if ctx.business_id is None else {})}, uow)

    def announce(self, ctx, uow, plan, before, after, comments):
        """Follows, the thread's system entry and the one event of an operation, derived from the committed difference."""
        item = ThreadItem(ctx.business_id, "task", after["row"]["id"])
        row = after["row"]
        auto_follow(uow, item, [row["created_by"]] if before is None else [], "creator")
        auto_follow(uow, item, [value for value in after["assignees"]], "assignee")
        for comment in comments:
            auto_follow(uow, item, [comment["recorder_id"]], "commenter")
            if comment["author_id"] != comment["recorder_id"]:
                auto_follow(uow, item, [comment["author_id"]], "represented")
        written = [comment["id"] for comment in comments]
        if before is not None:
            entry = events.record_change(uow, ctx.business_id, before, after, ctx.principal_id)
            written = ([entry] if entry else []) + written
        kinds = events.event_kinds(before, after, commented=bool(comments))
        if kinds:
            events.announce(uow, self.ports.routing, ctx.business_id, row, ctx.principal_id, kinds,
                            responsibility=bool({"created", "updated", "assignment_changed"} & set(kinds)),
                            targets=events.assignment_targets(uow, before, after), messages=written)

    def reconcile_snoozes(self, uow, *, now=None, limit=500):
        """The host lifecycle's one call: wake every passed snooze generation once, routed through this service's ``Routing`` port."""
        return reconcile_snoozes(uow, self.ports.routing, now=now, limit=limit)

    # -- follows ---------------------------------------------------------------------------------------------------

    def prepare_follow(self, ctx, arguments, options, uow):
        ctx = task_context(ctx, arguments, uow)
        task = uow.repository("tasks", business_id=ctx.business_id).get(arguments["task_id"])
        if task is None:
            raise DomainError("not_found", "Task does not exist in this scope")
        item = ThreadItem(ctx.business_id, "task", task["id"])
        if not can_follow(uow, item, ctx.principal_id):
            raise DomainError("forbidden", "Only an active participant of this namespace can follow its items")
        builder = PlanBuilder(ctx, "tasks_follow", arguments, "Follow task" if arguments["following"] else "Unfollow task", clock=self.clock,
                              namespace=task_namespace(ctx, arguments),
                              affected_scope={"kind": "business", "business_id": ctx.business_id} if ctx.business_id else {"kind": "global"})
        builder.expect(ref("tasks", task["id"], ctx.business_id), task)
        key = {"item_type": "task", "item_id": task["id"], "principal_id": ctx.principal_id}
        existing = uow.repository("thread_follows", business_id=ctx.business_id).get(key)
        values = {"following": int(arguments["following"]), "explicit": 1, "reason": "explicit", "updated_at": builder.at}
        if existing:
            builder.record("thread_follows", key, existing, values, label="follow")
        else:
            builder.record("thread_follows", key, None, {**key, **values, "created_at": builder.at}, label="follow")
        builder.effects.append("Changes only your own optional subscription to this task's discussion; responsibility updates are unaffected.")
        return builder.build()

    def apply_follow(self, ctx, plan, uow):
        ctx = task_context(ctx, plan.arguments, uow)
        apply_records(ctx, plan, uow)
        state = follow_state(uow, ThreadItem(ctx.business_id, "task", plan.arguments["task_id"]), ctx.principal_id)
        return {"task_id": plan.arguments["task_id"], **state}


__all__ = ["TASK_ENTRIES", "TaskOperations", "task_context", "task_namespace"]
