"""Task records, configuration, discussion, assignment and follows: the v1 operations on the new Task model.

Every operation keeps its v1 name, namespace rule (one owning business, or Global for the owner and granted global
agents), prepare/apply authority, revision checks and idempotency. What changed is the model underneath: five fixed
states, one shared ``snoozed_until``, a parent Task, labels from the shared catalog, and a single discussion writer
(``thread_messages``) for comments, summaries, typed references and the system entries that describe a change. Events
are written by the operation that changed the Task, in its own transaction; see ``events``.

The Task is the one record of a piece of work, so the rules that keep it true are checked where it is written (see
``lifecycle``), and ``on_change`` lets the host update the records that point at a Task in the same transaction.

Host-specific behaviour (who may be assigned, Focus presentation, the incident link, host note
references, owner-work routing) arrives through ``TaskPorts``."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.plans import (DATE, IDENTITY, PAGE, TEXT, UUID, PlanBuilder, apply_records, page, ref,
                                                 require_revision, rows, schema, timestamp, utc_clock)
from qorqut_operations.application.registry import CatalogEntry, OperationDescriptor
from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import DomainError, new_id, plain
from qorqut_operations.conversations import SUMMARY, participants
from qorqut_operations.discussion.follows import ThreadItem, auto_follow, can_follow, follow_state
from qorqut_operations.discussion.threads import (AUDIENCE, BODY, FOR_OWNER, RELAY, DiscussionReferences, message_count, plan_comment,
                                                  read_messages)
from qorqut_operations.global_agents import global_agent_ids, is_global_agent
from qorqut_operations.items import ItemResolver
from qorqut_operations.relationships import create_edge, related_rows
from qorqut_operations.storage.assembly import AssemblyError
from qorqut_operations.tags.catalog import LABELS_SCHEMA, distinct_labels, item_labels, labels_by_item, matches, plan_item_tags
from qorqut_operations.tasks import customers, events, lifecycle, links
from qorqut_operations.tasks.kinds import KIND_INPUT, normalize_arguments, project_result, public_task
from qorqut_operations.tasks.associated import TaskAssociatedRecords
from qorqut_operations.tasks.history import HistoricalReferences
from qorqut_operations.tasks.models import (DEFAULT_CONFIG, DEFAULT_STATE, full_config, normalize_snooze, snooze_view, stored_fields,
                                            validate_config, validate_dependencies, validate_parent, validate_task)
from qorqut_operations.tasks.overview import TaskOverview
from qorqut_operations.tasks.ports import TaskPorts
from qorqut_operations.tasks.relocation import TaskRelocation, moved_location
from qorqut_operations.tasks.scope import task_context, task_namespace
from qorqut_operations.tasks.schema import CLOSED_STATES, PAUSED_STATES, STATES
from qorqut_operations.tasks.snooze import reconcile_snoozes
from qorqut_operations.tasks.types import (KIND_SCHEMA, NEXT_STEP_SCHEMA, PROFILE_SCHEMA, STATE_SCHEMA, STATUS_LINE_SCHEMA, TYPES,
                                           apply_attributes, attributes, custom_fields)
from qorqut_operations.team_channel import reject_owner_team_write

TASK_READS = ("tasks_list", "tasks_get", "tasks_thread_get", "tasks_notes_list", "tasks_config_get", "assignees_list", "workers_list", "tasks_overview",
              "tasks_associated_records")
TASK_MUTATIONS = ("tasks_create", "tasks_update", "tasks_assign", "tasks_comment", "tasks_complete", "tasks_delegate",
                  "tasks_config_update")
TASK_FOLLOW = "tasks_follow"
TASK_ENTRIES = (
    *(CatalogEntry(name, False, "read") for name in TASK_READS),
    *(CatalogEntry(name, True, "records") for name in TASK_MUTATIONS),
    CatalogEntry(TASK_FOLLOW, False, "records"),
)

ASSIGNEES = {"type": "array", "items": UUID, "maxItems": 100}
SCOPE = {"scope": {"enum": ["business", "global"]}}
TASK_FIELDS = {
    "task_type": {"enum": list(TYPES), "description": instruction_text('operations-task-for-an-actionable-step-story-for-an')},
    "overview": {"type": "string", "maxLength": 20000, "description": instruction_text('operations-maintained-current-summary-and-next-steps-history-stays')},
    "email_conversation_id": {"anyOf": [IDENTITY, {"type": "null"}], "description": instruction_text('operations-historical-binding-of-an-email-task-kept-read')},
    "customer_kind": KIND_SCHEMA, "customer_state": STATE_SCHEMA, "customer_profile": PROFILE_SCHEMA,
    "status_line": STATUS_LINE_SCHEMA, "next_step": NEXT_STEP_SCHEMA,
    "kind": KIND_INPUT, "title": {"type": "string", "minLength": 1, "maxLength": 1000},
    "status": {"enum": list(STATES)}, "assignee_ids": ASSIGNEES, "worker_ids": {**ASSIGNEES, "uniqueItems": True}, "archived": {"type": "boolean"}, "tags": LABELS_SCHEMA,
    "due": {"anyOf": [DATE, {"type": "null"}]}, "blocked_by": {"type": "array", "items": IDENTITY, "maxItems": 100, "uniqueItems": True},
    "fields": {"type": "object", "maxProperties": 100},
    "snoozed_until": {"description": instruction_text('operations-one-shared-deferral-of-the-task-s-due'), "anyOf": [{"type": "string", "maxLength": 64}, {"type": "null"}]},
    "parent_id": {"description": instruction_text('operations-the-parent-task-in-the-same-owning-scope'),
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
        self.associated = TaskAssociatedRecords(self)
        # The host adds the adapters for the references it owns; a Task move itself is a host-registered operation.
        self.relocation = TaskRelocation(self)
        self.history = HistoricalReferences(self)
        unknown = sorted(set(ports.references.types) - set(items.kinds) - set(ports.references.host_types))
        if unknown:
            raise AssemblyError("Note reference types are neither engine items nor host references: " + ", ".join(unknown))
        self.note_ref_types = list(ports.references.types)
        # ``on_change(ctx, uow, before, after)``: the host's one hook on every committed Task write, called inside the
        # writing transaction with the stored state before (``None`` for a new Task) and after. The host uses it to keep the
        # records that point at a Task (board entries, Focus cards, reminders) in step with it.
        self.on_change = None

    # -- moved Tasks -----------------------------------------------------------------------------------------------

    def missing(self, ctx, uow, task_id) -> DomainError:
        """The refusal for a Task absent from the selected scope: a plain ``not_found``, or ``record_moved`` when the Task
        was relocated and this caller may read it where it is now (nothing is disclosed otherwise, and nothing is written)."""
        location = moved_location(uow, ctx.business_id, task_id)
        if location is not None:
            try:
                links.resolve(self, ctx, {"business_id": location, "task_id": task_id}, uow)
                project = uow.repository("businesses").get(location)
            except DomainError:
                project = None
            if project is not None:
                return DomainError("record_moved", "This Task moved to another project",
                                   details={"destination": {"id": project["id"], "name": project.get("name", project["slug"]),
                                                            "slug": project["slug"]},
                                            "task": {"type": "task", "id": task_id, "business_id": location}})
        return DomainError("not_found", "Task does not exist in this scope")

    def require(self, ctx, uow, task_id):
        """The Task of the selected scope, or the ``missing`` refusal."""
        row = uow.repository("tasks", business_id=ctx.business_id).get(task_id)
        if row is None:
            raise self.missing(ctx, uow, task_id)
        return row

    # -- registration ----------------------------------------------------------------------------------------------

    def contribution(self) -> CatalogContribution:
        return CatalogContribution("tasks", TASK_ENTRIES, self.descriptors())

    def descriptors(self):
        refs = self.note_references().schema
        common = dict(scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
                      project_result=project_result,
                      resolve_namespace=task_namespace,
                      description=instruction_text('operations-manage-tasks-in-the-selected-project-or-use'))
        reads = {
            "tasks_list": {**PAGE, "assignee_id": {"anyOf": [UUID, {"type": "null"}]}, "include_archived": {"type": "boolean"},
                           "task_type": {"enum": list(TYPES)},
                           "email_conversation_id": IDENTITY,
                           "statuses": {"type": "array", "items": {"enum": list(STATES)}}, "tags": LABELS_SCHEMA,
                           "include_closed": {"type": "boolean"}, "include_paused": {"type": "boolean"}, "blocked": {"type": "boolean"},
                           "due_before": DATE, "search": {"type": "string", "maxLength": 500},
                           "parent_id": {"anyOf": [IDENTITY, {"type": "null"}], "description": instruction_text('operations-children-of-this-parent-or-null-for-top')},
                           "snooze_filter": {"enum": ["active", "snoozed", "all"], "default": "active",
                                             "description": instruction_text('operations-defaults-to-not-currently-snoozed-snoozed-tasks-are')}},
            "tasks_get": {"task_id": IDENTITY, "related_cursor": PAGE["cursor"],
                          "notes_limit": {"type": "integer", "minimum": 0, "maximum": 10, "default": 7,
                                          "description": instruction_text('operations-comments-per-page-newest-first-0-omits-them')},
                          "notes_cursor": PAGE["cursor"], "audience": AUDIENCE},
            "tasks_thread_get": {**PAGE, "task_id": IDENTITY, "audience": AUDIENCE},
            "tasks_notes_list": {**PAGE, "task_id": IDENTITY, "audience": AUDIENCE},
            "tasks_config_get": {}, "assignees_list": {}, "workers_list": {},
        }
        descriptors = [self.overview.descriptor(), self.associated.descriptor()]
        for name, properties in reads.items():
            descriptors.append(OperationDescriptor(
                name, schema({**SCOPE, **properties}, ("task_id",) if name in {"tasks_get", "tasks_notes_list", "tasks_thread_get"} else ()),
                **common, effect_kind="read", supports_signoff=False, read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow)))
        comment = {"note_refs": refs, "summary": SUMMARY, "relay": RELAY, "for_owner": FOR_OWNER}
        mutations = {
            "tasks_create": ({"focus": schema(self.ports.focus.schema), **TASK_FIELDS, "id": IDENTITY, "note": TEXT, **comment,
                              "allow_duplicate": lifecycle.ALLOW_DUPLICATE, "owner_requested": lifecycle.OWNER_REQUESTED}, ("title",)),
            "tasks_update": ({"task_id": IDENTITY, "changes": {**schema({**TASK_FIELDS, "related_task": links.EDIT}), "minProperties": 1}, "note": TEXT, **comment,
                              "owner_requested": lifecycle.OWNER_REQUESTED}, ("task_id", "changes")),
            "tasks_assign": ({"task_id": IDENTITY, "assignee_ids": ASSIGNEES}, ("task_id", "assignee_ids")),
            "tasks_comment": ({"task_id": IDENTITY, "text": BODY, **comment}, ("task_id", "text")),
            "tasks_complete": ({"task_id": IDENTITY, "status": {"enum": list(CLOSED_STATES)}, "note": TEXT, **comment,
                                "children": lifecycle.CHILDREN, "result": lifecycle.RESULT}, ("task_id",)),
            "tasks_delegate": ({"task_id": IDENTITY, "worker_id": UUID, "brief": TEXT, "note_refs": refs}, ("task_id", "worker_id")),
            "tasks_config_update": ({"config": {"type": "object"}}, ("config",)),
        }
        for name, (properties, required) in mutations.items():
            options = common
            if name in {"tasks_create", "tasks_update"}:
                options = {**common, "description": common["description"] +
                           instruction_text('operations-tags-are-ordinary-labels-that-group-work-they')}
            if name == "tasks_delegate":
                options = {**common, "description": lifecycle.DELEGATE}
            descriptors.append(OperationDescriptor(
                name, schema({**SCOPE, **properties, "rationale": TEXT}, required), **options,
                normalize_arguments=normalize_arguments if name in {"tasks_create", "tasks_update"} else None,
                prepare=lambda ctx, args, options, uow, op=name: self.prepare(op, ctx, args, options, uow),
                apply=self.apply, validate_targets=self.validate_prepared))
        descriptors.append(OperationDescriptor(
            TASK_FOLLOW, schema({**SCOPE, **FOLLOW.get("properties", {})}, ("task_id", "following")),
            **{**common, "description": instruction_text('operations-follow-or-unfollow-a-task-s-discussion-for')},
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

    def assignee(self, ctx, identity, uow, builder=None, *, worker=False):
        if worker and self.ports.assignees.worker_excluded(ctx, identity, uow):
            raise DomainError("invalid_worker", "This principal cannot be a Worker")
        if not worker:
            self.ports.assignees.protect(ctx, identity, uow)
        if not worker and self.ports.assignees.owner_directed(ctx, identity, uow):
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
        if worker:
            installation = uow.repository("installation").get("service")
            if not actor or actor["kind"] != "human" or not installation or installation["owner_principal_id"] != identity:
                raise DomainError("invalid_worker", "Only the installation owner or eligible agents may be Workers")
            if builder:
                builder.expect(ref("installation", "service", None), installation)
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
        """The display resolver of a stored message's references: read-only, in the scope the note was written in (see
        ``tasks.history``). Writes and strict validation use ``note_refs`` and never follow a relocated target."""
        def resolve(message):
            refs = list(message["refs"])
            shown = self.history.show(ctx, refs, uow, message.get("reference_namespace"))
            link = self.ports.references.derived(ctx, uow, message) if message.get("item_type") == "task" else None
            if link is None or link in refs:
                return shown
            if link.get("unavailable"):
                return [*shown, link]
            return [*shown, *self.history.show(ctx, [link], uow)]
        return resolve

    # -- reads -----------------------------------------------------------------------------------------------------

    def detail(self, ctx, task, uow, fields, now, *, notes=True, notes_page=None):
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
        view = {**{key: value for key, value in task.items() if key not in {"fields_json", "worker_ids_json", "snoozed_until", "snooze_generation"}},
                **snooze_view(task, now),
                "created_by_name": creator.get("display_name", "") if creator else "",
                "created_by_kind": creator.get("kind", "") if creator else "",
                "tags": item_labels(uow, business_id, "task", task_id), "fields": custom_fields(task), **attributes(task),
                "thread_id": item.thread_id, "note_count": message_count(uow, item),
                **self.worker_projection(uow, task),
                "assignee_ids": events.assigned(uow, business_id, task_id), "archived": bool(task.get("archived")),
                "blocked_by": blockers, "unresolved_blockers": sorted(set(unresolved)), "blocks": blocks,
                "parent": {key: parent[key] for key in ("id", "title", "status")} if parent else None,
                "children": children[:50], "children_has_more": len(children) > 50,
                "closed": task["status"] in CLOSED_STATES, "paused": task["status"] in PAUSED_STATES,
                "stalled": lifecycle.stalled(task, now),
                "following": follow["following"], "follow_reason": follow["reason"]}
        view["notes"], view["notes_next_cursor"] = [], None
        notes_page = notes_page or {}
        limit = notes_page.get("notes_limit", 7)
        if notes and limit:
            # Newest comments first; notes_next_cursor continues into older ones.
            cursor = {"cursor": notes_page["notes_cursor"]} if notes_page.get("notes_cursor") else {}
            history = read_messages(uow, item, {"limit": limit, **cursor}, "tasks_get", ctx.principal_id,
                                    resolve_refs=self.message_refs(ctx, uow), kinds=("comment",),
                                    audience=notes_page.get("audience"))
            view["notes"], view["notes_next_cursor"] = history["messages"][::-1], history["next_cursor"]
            if "hidden" in history:
                view["notes_hidden"] = history["hidden"]
        return public_task(self.project_input(ctx, [view], uow)[0] if 'input_version' not in view else view)

    def project_input(self, ctx, tasks, uow):
        signoffs = getattr(self.application, 'signoffs', None)
        return signoffs.sources.project_tasks(ctx, tasks, uow) if signoffs else tasks

    @staticmethod
    def worker_projection(uow, task):
        from qorqut_operations.tasks.workers import project_workers
        return project_workers(uow, task)

    def read(self, operation, ctx, arguments, uow):
        ctx = task_context(ctx, arguments, uow)
        if operation == "tasks_associated_records":
            return self.associated.read(ctx, arguments, uow)
        configuration, config_row = self.configuration(ctx, uow)
        if operation == "tasks_config_get":
            return {"config": configuration, "revision": config_row["revision"] if config_row else None,
                    "customer_states": customers.read(uow, ctx.business_id)[0]}
        if operation in {"assignees_list", "workers_list"}:
            return self.assignees_list(ctx, uow, workers=operation == "workers_list")
        repository = uow.repository("tasks", business_id=ctx.business_id)
        now = timestamp(self.clock)
        if operation in {"tasks_thread_get", "tasks_notes_list"}:
            self.require(ctx, uow, arguments["task_id"])
            item = ThreadItem(ctx.business_id, "task", arguments["task_id"])
            if operation == "tasks_notes_list":
                history = read_messages(uow, item, arguments, operation, ctx.principal_id, resolve_refs=self.message_refs(ctx, uow),
                                        kinds=("comment",), audience=arguments.get("audience"))
                return {"thread_id": history["thread_id"], "notes": history["messages"], "next_cursor": history["next_cursor"],
                        **({"hidden": history["hidden"]} if "hidden" in history else {})}
            history = read_messages(uow, item, arguments, operation, ctx.principal_id, resolve_refs=self.message_refs(ctx, uow),
                                    audience=arguments.get("audience"))
            task = self.require(ctx, uow, arguments["task_id"])
            people = {task["created_by"], *(note["created_by"] for note in history["messages"]), *events.assigned(uow, ctx.business_id, task["id"])}
            return {"thread_id": history["thread_id"], "participants": participants(uow, people), "notes": history["messages"],
                    "next_cursor": history["next_cursor"], **({"hidden": history["hidden"]} if "hidden" in history else {})}
        if operation == "tasks_get":
            task = self.require(ctx, uow, arguments["task_id"])
            detail = self.detail(ctx, task, uow, configuration["fields"], now, notes_page=arguments)
            detail.update(links.read(self, ctx, task, uow, arguments.get("related_cursor")))
            return {"task": detail}
        return self.list_tasks(ctx, arguments, uow, configuration, now)

    def assignees_list(self, ctx, uow, *, workers=False):
        directed = self.ports.assignees.worker_excluded if workers else self.ports.assignees.owner_directed
        result_key = "workers" if workers else "assignees"
        # Active global agents are candidates in Global and in every active business, without membership rows.
        global_agents = set(global_agent_ids(uow.connection))
        if ctx.business_id is None:
            found = uow.connection.execute(
                "SELECT p.id,p.kind,p.display_name,a.name FROM principals p LEFT JOIN agent_profiles a ON a.agent_id=p.id WHERE p.active=1 "
                "AND ((p.kind='human' AND p.id=(SELECT owner_principal_id FROM installation WHERE id='service')) OR p.kind='agent') "
                "ORDER BY p.kind,p.display_name,p.id")
            return {result_key: [{**dict(row), "is_owner": row["kind"] == "human"} for row in found
                                  if (row["kind"] == "human" or row["id"] in global_agents) and not directed(ctx, row["id"], uow)]}
        if ctx.business_id not in active_business_ids(uow.connection):
            global_agents = set()
        installation = uow.repository("installation").get("service")
        owner = installation["owner_principal_id"] if installation else None
        found = uow.connection.execute(
            "SELECT p.id,p.kind,p.display_name,a.name,m.is_owner,m.active AS member FROM principals p "
            "LEFT JOIN business_memberships m ON m.principal_id=p.id AND m.business_id=? LEFT JOIN agent_profiles a ON a.agent_id=p.id "
            "WHERE p.active=1 AND p.kind IN ('agent','human') ORDER BY p.kind,p.display_name,p.id", (ctx.business_id,))
        return {result_key: [{"id": row["id"], "kind": row["kind"], "display_name": row["display_name"], "name": row["name"],
                               "is_owner": row["kind"] == "human" and (row["id"] == owner if workers else bool(row["is_owner"]))} for row in found
                              if (row["id"] in global_agents or (row["kind"] == "human" and row["member"]
                                                                  and (not workers or row["id"] == owner)))
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
        if arguments.get("due_before"):
            clauses.append("due IS NOT NULL AND due<=?")
            parameters.append(arguments["due_before"])
        tasks = rows(uow, "tasks", ctx.business_id, where=" AND ".join(clauses), parameters=parameters)
        if "task_type" in arguments:
            tasks = [task for task in tasks if attributes(task)["task_type"] == arguments["task_type"]]
        if "email_conversation_id" in arguments:
            tasks = [task for task in tasks if attributes(task)["email_conversation_id"] == arguments["email_conversation_id"]]
        if arguments.get("tags"):
            wanted = distinct_labels(arguments["tags"])
            labels = labels_by_item(uow, ctx.business_id, "task")
            tasks = [task for task in tasks if matches(labels.get(task["id"], ()), wanted)]
        if arguments.get("search"):
            tasks = [task for task in tasks if arguments["search"].casefold() in (task["title"] + " " + task["id"]).casefold()]
        # Status is a projection of explicit pending Signoffs; filter before paging, in one batch.
        tasks = [task for task in self.project_input(ctx, tasks, uow) if task['status'] in states]
        if "blocked" in arguments:
            detailed = [self.detail(ctx, task, uow, configuration["fields"], now, notes=False) for task in tasks]
            tasks = [task for task in detailed if bool(task["unresolved_blockers"]) == arguments["blocked"]]
            selected, cursor = page(tasks, arguments, ctx.business_id, "tasks_list")
            return {"tasks": selected, "next_cursor": cursor}
        selected, cursor = page(tasks, arguments, ctx.business_id, "tasks_list")
        return {"tasks": [self.detail(ctx, task, uow, configuration["fields"], now, notes=False) for task in selected], "next_cursor": cursor}

    # -- writes ----------------------------------------------------------------------------------------------------

    def prepare(self, operation, ctx, arguments, options, uow, *, team_note=True):
        """``team_note=False`` marks a caller whose ``note`` is the action's own generated record (such as a host handoff
        message) rather than an ordinary comment, so the owner's private-channel guard does not apply to it."""
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
                raise self.missing(ctx, uow, identity)
            # A comment adds to the thread and edits no field, so it needs no prior read of the Task. A caller that
            # does name a revision is still held to it, and the plan pins the row it was prepared against.
            if operation != "tasks_comment" or options.expected_revision is not None:
                require_revision(before, options.expected_revision)
        edits = {key: value for key, value in arguments.items() if key in TASK_FIELDS} if creating else plain(arguments.get("changes", {}))
        if edits.get('status') == 'needs_input':
            raise DomainError('validation_error', 'Needs input is derived from pending Signoffs. Use signoffs_input_create with source, or signoffs_source_link for an existing approval.')
        if operation == "tasks_assign":
            edits["assignee_ids"] = arguments["assignee_ids"]
        if operation == "tasks_complete":
            status = arguments.get("status", "done")
            if status not in CLOSED_STATES:
                raise DomainError("validation_error", "Completion requires a terminal state (done or cancelled)")
            edits["status"] = status
            if arguments.get("result") is not None:
                # The outcome is recorded once: it is the Task's status line.
                edits["status_line"] = arguments["result"]
        if operation == "tasks_delegate":
            if before["status"] in CLOSED_STATES:
                raise DomainError("validation_error", "This Task is closed; reopen it before delegating it")
            # One recorded worker: the agent doing the work. The brief is the Task's own comment, so the hand-over and the
            # work it describes are one record.
            edits["worker_ids"] = [arguments["worker_id"]]
        current = dict(before) if before else {
            "id": identity, "title": arguments["title"], "status": DEFAULT_STATE, "due": None, "fields_json": {}, "archived": False,
            "created_by": ctx.principal_id, "created_at": builder.at, "updated_at": builder.at, "kind": "ordinary", "parent_id": None,
            "snoozed_until": None, "snooze_generation": 0, "worker_ids_json": [], "latest_completed_at": None}
        link_edit = edits.pop("related_task", None)
        blockers = edits.pop("blocked_by", None)
        assignees = edits.pop("assignee_ids", [ctx.agent_id] if creating and ctx.agent_id else [] if creating else None)
        workers = edits.pop("worker_ids", None)
        if workers is not None:
            if len(workers) != len(set(workers)) or len(workers) > 100:
                raise DomainError("validation_error", "Workers must be unique and at most 100")
            for identity_worker in workers:
                self.assignee(ctx, identity_worker, uow, builder, worker=True)
            current["worker_ids_json"] = sorted(workers)
        labels = edits.pop("tags", None)
        if "snoozed_until" in edits:
            requested = normalize_snooze(edits.pop("snoozed_until"), builder.at, current["snoozed_until"])
            if requested != current["snoozed_until"]:
                current["snoozed_until"] = requested
                if requested is not None:
                    current["snooze_generation"] += 1
        states, states_row = customers.read(uow, ctx.business_id) if "customer_state" in edits else ([], None)
        apply_attributes(current, edits, states)
        if link_edit is not None:
            links.edit(self, ctx, current, identity, link_edit, uow, builder)
        for key, value in edits.items():
            current["fields_json" if key == "fields" else key] = value
        if current["status"] == "done" and (before is None or before["status"] != "done"):
            current["latest_completed_at"] = builder.at
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
        closing = lifecycle.check(ctx, uow, arguments, before, current,
                                  blocked_by=blockers if blockers is not None else events.blockers(uow, ctx.business_id, identity))
        if creating and current["kind"] == "owner_request":
            installation = uow.repository("installation").get("service")
            if not installation or not assignees or installation["owner_principal_id"] not in assignees:
                raise DomainError("validation_error", "request_to_owner tasks must explicitly assign the installation owner")
        for assignee in assignees or ():
            self.assignee(ctx, assignee, uow, builder)
        label = customers.introduced(attributes(before or {})["customer_state"], attributes(current)["customer_state"])
        if label is not None:
            customers.append(builder, states_row, states, label)
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
            plan_item_tags(builder, uow, "task", identity, ctx.business_id, distinct_labels(labels), created=creating)
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
        # Open work under a Task closed with ``children: close`` closes with it, after the Task's own record so that record
        # stays the plan's first Task change.
        for child in closing:
            builder.record("tasks", child["id"], child, lifecycle.closed_values(child, current["status"], builder.at),
                           label=child["title"])
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
                     note=arguments.get({"tasks_comment": "text", "tasks_delegate": "brief"}.get(operation, "note"))
                     or (arguments.get("result") if operation == "tasks_complete" else None),
                     summary=arguments.get("summary"), refs=arguments.get("note_refs", []), relay=arguments.get("relay"),
                     resolve=lambda refs: self.note_refs(ctx, refs, uow, builder),
                     guard=(lambda: reject_owner_team_write(ctx, uow, operation)) if team_note else None,
                     operation_name="tasks_comment" if operation != "tasks_comment" else None,
                     # A delegation brief is for the worker. What closes the work is its outcome, which the owner reads.
                     for_owner=False if operation == "tasks_delegate" else arguments.get("for_owner"),
                     outcome=operation == "tasks_complete")

    def validate_prepared(self, ctx, plan, uow):
        ctx = task_context(ctx, plan.arguments, uow)
        self.note_refs(ctx, plan.arguments.get("note_refs", ()), uow, strict=True)
        configuration, _ = self.configuration(ctx, uow)
        fields = configuration["fields"]
        if plan.operation_name == "tasks_config_update":
            proposed = plain(next(change.values["config_json"] for change in plan.changes
                                  if change.resource.resource_type == "business_configs" and change.resource.resource_id == "tasks"))
            for task in rows(uow, "tasks", ctx.business_id):
                validate_task(task, proposed["fields"])
            return
        task_id = self.planned_task(plan)
        task_change = next((change for change in plan.changes if change.resource.resource_type == "tasks"), None)
        current = uow.repository("tasks", business_id=ctx.business_id).get(task_id) or {}
        proposed = {**current, **plain(task_change.values)} if task_change else current
        validate_task(proposed, fields)
        worker_changes = plan.arguments.get("worker_ids") if plan.operation_name == "tasks_create" else plan.arguments.get("changes", {}).get("worker_ids")
        if plan.operation_name == "tasks_delegate":
            worker_changes = [plan.arguments["worker_id"]]
        if worker_changes is not None:
            for worker_id in worker_changes:
                self.assignee(ctx, worker_id, uow, worker=True)
        customers.check(plan, uow, ctx.business_id, attributes(current)["customer_state"], attributes(proposed)["customer_state"])
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
        # Tasks closed along with this one (``children: close``): each keeps its own thread entry and host hook; the one
        # event of the operation is the parent's.
        carried = {change.resource.resource_id: events.state(uow, ctx.business_id, change.resource.resource_id)
                   for change in plan.changes if change.resource.resource_type == "tasks" and change.action == "update"
                   and change.resource.resource_id != identity}
        apply_records(ctx, plan, uow)
        after = events.state(uow, ctx.business_id, identity)
        self.announce(ctx, uow, plan, before, after, [plain(change.values) for change in messages])
        for child_id, earlier in carried.items():
            later = events.state(uow, ctx.business_id, child_id)
            events.record_change(uow, ctx.business_id, earlier, later, ctx.principal_id)
            if self.on_change is not None:
                self.on_change(ctx, uow, earlier, later)
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
        named = sorted(set(events.workers(after)) - set(events.workers(before)))
        if named and self.ports.boards is not None:
            # Before the event is routed, so a board the host fills here is already a recipient of this change.
            self.ports.boards.workers_named(ctx, {"type": "task", "id": row["id"], "business_id": ctx.business_id},
                                            row, named, uow)
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
                            targets=events.assignment_targets(uow, before, after), messages=written,
                            change=events.change(before, after))
        if self.on_change is not None:
            self.on_change(ctx, uow, before, after)

    def reconcile_snoozes(self, uow, *, now=None, limit=500):
        """The host lifecycle's one call: wake every passed snooze generation once, routed through this service's ``Routing`` port."""
        return reconcile_snoozes(uow, self.ports.routing, now=now, limit=limit)

    # -- follows ---------------------------------------------------------------------------------------------------

    def prepare_follow(self, ctx, arguments, options, uow):
        ctx = task_context(ctx, arguments, uow)
        task = self.require(ctx, uow, arguments["task_id"])
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
