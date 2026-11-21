"""Action operations: record, correct, withdraw, follow and the authorized reads.

An Action is one completed result in one owning namespace. ``actions_record`` keeps its v1 arguments (and adds tags and
Context pointers); an owner or the original recorder corrects or withdraws it as the next immutable revision, and every
accepted revision is queued for Context in the same transaction (see ``writer``). Reads follow project links, never
the storage namespace, exactly as in v1, and a withdrawn Action stays readable with its history.

``actions_record`` may link one Task by ``task_id``. The owner and global agents record in Global whatever project the
work was for, so their Task is found by its id anywhere in their reach (``linked_task``) and a category and projects they
leave out are taken from it (``classified``); a caller recording in its own project links a Task of that project.

An Action's discussion is the canonical item discussion (``qorqut_operations.discussion``). ``actions_thread_get`` is
authorized exactly like ``actions_get``, retained project read links included. ``actions_comment`` is a separate
mutation that needs its own policy and participation in the Action's owning namespace (an active human member, or the
owner for Global, or an active global agent invoking in that namespace): a read link is never a write.
A comment writes one ``thread_messages`` row, the commenter's (and a relay's represented speaker's) default follow and
one optional event; it creates no Action revision and no Context input."""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

import base64
import json
import uuid

from qorqut_operations import local_time
from qorqut_operations.actions import models
from qorqut_operations.actions.lookup import ActionLookup
from qorqut_operations.actions.ports import require_on_revision
from qorqut_operations.actions.schema import CATEGORIES
from qorqut_operations.actions.scope import (action_namespace, change_namespace, classification_from_task, classify, evidence_visible,
                                              is_owner, links, may_change, reader_scope, scope_clause, visible)
from qorqut_operations.actions.writer import SOURCE_TYPE, change_action, new_action, settle
from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.plans import (DATE, IDENTITY, PAGE, TEXT, UNSET, UUID, PlanBuilder, apply_records,
                                                 page_position, ref, require_revision, schema, utc_clock)
from qorqut_operations.application.registry import CatalogEntry, OperationDescriptor
from qorqut_operations.project_lifecycle import active_project_ids
from qorqut_operations.contracts import DomainError, canonical_bytes, plain
from qorqut_operations.global_agents import is_coordinator
from qorqut_operations.conversations import SUMMARY, participants
from qorqut_operations.discussion.events import emit_event
from qorqut_operations.discussion.follows import ThreadItem, auto_follow, can_follow, follow_state, followers
from qorqut_operations.discussion.threads import (BODY, RELAY, DiscussionReferences, attach_event, attribution, message_view, plan_comment,
                                                  read_messages)
from qorqut_operations.storage.assembly import AssemblyError
from qorqut_operations.storage.repositories import decode_row
from qorqut_operations.tags.catalog import LABELS_SCHEMA, distinct_labels, item_labels, label_key
from qorqut_operations.team_channel import reject_owner_team_write

ACTION_READS = ("actions_list", "actions_get", "actions_config_get", "actions_thread_get")
ACTION_MUTATIONS = ("actions_record", "actions_correct", "actions_withdraw", "actions_comment")
ACTION_FOLLOW = "actions_follow"
ACTION_COMMENT = "actions_comment"
ACTION_ENTRIES = (
    *(CatalogEntry(name, False, "read") for name in ACTION_READS),
    *(CatalogEntry(name, True, "records") for name in ACTION_MUTATIONS),
    CatalogEntry(ACTION_FOLLOW, False, "records"),
)
ACTION_ID = IDENTITY
PROJECT_IDS = {"type": "array", "items": UUID, "maxItems": 50, "uniqueItems": True}
RECORD_DESCRIPTION = (
    instruction_text('operations-record-one-atomic-completed-result-not-a-task') + models.SHARING)
DELEGATION_DESCRIPTION = (
    instruction_text('operations-optional-record-the-result-of-your-own-completed'))
CORRECT_DESCRIPTION = (
    instruction_text('operations-correct-an-action-you-recorded-the-installation-owner'))
WITHDRAW_DESCRIPTION = (
    instruction_text('operations-withdraw-an-action-you-recorded-the-installation-owner'))
LIST_DESCRIPTION = (instruction_text('operations-list-authorized-completed-actions-by-occurrence-newest-first') + models.SHARING)
THREAD_DESCRIPTION = (
    instruction_text('operations-read-one-bounded-page-of-an-authorized-action') + models.SHARING)
COMMENT_DESCRIPTION = (
    instruction_text('operations-add-a-comment-to-an-action-s-discussion'))
FOLLOW = schema({"action_id": ACTION_ID, "following": {"type": "boolean"}}, ("action_id", "following"))
COMMENT_EVENT = "note_added"
# The most scopes named when a linked Task's id is held in several (an id reused before ids were unique).
CANDIDATES = 20


class ActionOperations:
    def __init__(self, application, references: DiscussionReferences, *, tasks, on_revision, clock=utc_clock):
        if not isinstance(references, DiscussionReferences):
            raise AssemblyError("Action discussions need the note references of the Task service (note_references())")
        # Duck-typed: the Task service imports this package's scope rules, so its class is not imported here.
        if not callable(getattr(getattr(tasks, "lookup", None), "find", None)):
            raise AssemblyError("Actions need the Task service, whose lookup finds the Task an Action links")
        if not callable(clock):
            raise AssemblyError("The action clock is a callable returning an aware timestamp")
        self.application, self.references, self.clock = application, references, clock
        self.tasks = tasks
        self.on_revision = require_on_revision(on_revision)

    # -- registration ----------------------------------------------------------------------------------------------

    def contribution(self) -> CatalogContribution:
        return CatalogContribution("actions", ACTION_ENTRIES, self.descriptors())

    def descriptors(self):
        common = dict(scope_kinds=frozenset({"project", "global"}), global_capability="read_global_context",
                      resolve_namespace=action_namespace)
        filters = {**PAGE, "since": DATE, "until": DATE, "project": UUID, "performer": UUID, "tags": LABELS_SCHEMA,
                   "categories": {"type": "array", "items": {"enum": [*CATEGORIES, "unclassified"]}, "uniqueItems": True, "maxItems": 4},
                   "origins": {"type": "array", "items": {"enum": list(models.ORIGINS)}, "uniqueItems": True, "maxItems": 4},
                   "include_withdrawn": {"type": "boolean"}}
        reads = (("actions_list", filters, (), LIST_DESCRIPTION),
                 ("actions_get", {"action_id": ACTION_ID, "revision": {"type": "integer", "minimum": 1}}, ("action_id",),
                  "Read one authorized action by action_id (its current state, or an exact earlier revision with revision). "
                  "Unauthorized or unknown actions are not found. " + models.SHARING),
                 ("actions_config_get", {}, (), "Categories, the 20-word agent rule, the installation time convention "
                  "(timezone, current local date/hour and server instant) and your allowed/default action scope."),
                 ("actions_thread_get", {**PAGE, "action_id": ACTION_ID}, ("action_id",), THREAD_DESCRIPTION))
        descriptors = [OperationDescriptor(name, schema(properties, required), **common, effect_kind="read", supports_signoff=False,
                                           description=description,
                                           read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow))
                       for name, properties, required, description in reads]
        optional = {"tags": LABELS_SCHEMA, "related_objects": models.RELATED_OBJECTS, "evidence": models.EVIDENCE}
        descriptors.append(OperationDescriptor("actions_record", schema({
            "text": models.ACTION_TEXT, "category": {"enum": list(CATEGORIES)}, "project_ids": PROJECT_IDS,
            "real_time": local_time.REAL_TIME_SCHEMA, "task_id": IDENTITY, "performed_by": UUID, "relay": RELAY, **optional,
            "delegation_assignment_id": {**UUID, "description": DELEGATION_DESCRIPTION}, "rationale": TEXT}, ("text",)),
            **common, description=RECORD_DESCRIPTION, prepare=self.prepare_record, apply=self.apply,
            validate_targets=self.validate_record))
        changing = {**common, "resolve_namespace": change_namespace, "locate_scope": ActionLookup(), "locate_all_sources": True}
        descriptors.append(OperationDescriptor("actions_correct", schema({
            "action_id": ACTION_ID, "text": models.ACTION_TEXT, "category": {"anyOf": [{"enum": list(CATEGORIES)}, {"type": "null"}]},
            "project_ids": PROJECT_IDS, "real_time": {"anyOf": [local_time.REAL_TIME_SCHEMA, {"type": "null"}]}, **optional,
            "rationale": TEXT}, ("action_id",)), **changing, description=CORRECT_DESCRIPTION, prepare=self.prepare_correct,
            apply=self.apply, validate_targets=self.validate_change))
        descriptors.append(OperationDescriptor("actions_withdraw", schema({"action_id": ACTION_ID, "rationale": TEXT}, ("action_id",)),
            **changing, description=WITHDRAW_DESCRIPTION, prepare=self.prepare_withdraw, apply=self.apply,
            validate_targets=self.validate_change))
        descriptors.append(OperationDescriptor(ACTION_COMMENT, schema({
            "action_id": ACTION_ID, "text": BODY, "summary": SUMMARY, "note_refs": self.references.schema, "relay": RELAY,
            "rationale": TEXT}, ("action_id", "text")), **changing, description=COMMENT_DESCRIPTION, prepare=self.prepare_comment,
            apply=self.apply_comment, validate_targets=self.validate_comment))
        descriptors.append(OperationDescriptor(ACTION_FOLLOW, FOLLOW, **changing, supports_signoff=False,
            prepare=self.prepare_follow, apply=self.apply_follow,
            description=instruction_text('operations-follow-or-unfollow-an-action-for-yourself-the')))
        return tuple(descriptors)

    # -- shared rules ----------------------------------------------------------------------------------------------

    def occurrence(self, builder, real_time):
        if real_time is None:
            return local_time.recorded_time(builder.at)
        return local_time.parse_real_time(real_time, self.clock)

    def delegation(self, ctx, uow, args, category, projects):
        """The verified assignment whose result this records, or None for the caller's own work."""
        requested = args.get("delegation_assignment_id")
        worker = ctx.delegation_assignment_id
        if worker is not None and requested not in (None, worker):
            raise DomainError("forbidden", "A delegated worker records only its own assignment's results")
        identity = worker or requested
        if identity is None:
            return None
        provider = self.application.delegation
        assignment = provider.assignment(uow, identity) if provider is not None else None
        # The coordinator controls every parent's assignment, so it also records the result of one another agent created.
        if assignment is None or (assignment["parent_principal_id"] != ctx.principal_id
                                  and (worker is not None or not is_coordinator(uow.connection, ctx.principal_id))):
            raise DomainError("not_found", "Delegated assignment does not exist")
        if worker is None:
            if assignment["state"] != "completed" or not (assignment["result_json"] or {}).get("summary"):
                raise DomainError("validation_error", "Only a completed assignment with a recorded result can be recorded",
                                  field_errors={"delegation_assignment_id": "not completed"})
            recorded = uow.connection.execute(
                "SELECT 1 FROM action_logs WHERE json_extract(refs_json,'$.delegation_assignment')=? LIMIT 1", (assignment["id"],)).fetchone()
            if recorded:
                raise DomainError("conflict", "This assignment's result is already recorded")
        scope = assignment["scope_json"]
        allowed = {assignment["project_id"]} if scope.get("kind") == "project" else set(scope.get("allowed_project_ids", []))
        if (scope.get("kind") == "project" and category != "project-operational") or not set(projects) <= allowed:
            raise DomainError("forbidden", "The action is outside the delegated assignment's project scope")
        return assignment

    @staticmethod
    def find(uow, action_id):
        return decode_row(uow.connection.execute("SELECT * FROM action_logs WHERE id=?", (action_id,)).fetchone())

    def linked_task(self, ctx, uow, args):
        """The stored Task that ``task_id`` names, or ``None`` when the record links none.

        A caller that records in its own project links a Task of that project, and a delegated worker one of the Action's
        own scope, as before. The owner and a global agent record in Global whatever project the work was for, so their
        Task is looked for in their whole reach (``TaskLookup.find``: Global, and every allowed project where they may read
        Tasks). An id held in several of those scopes means the one the call names, a project in ``project_ids`` or
        Global by ``category`` global-operational, when it names exactly one; nothing outside the reach is disclosed."""
        task_id = args.get("task_id")
        if not task_id:
            return None
        if action_namespace(ctx, {}) != "global" or ctx.delegation_assignment_id is not None:
            task = uow.repository("tasks", project_id=ctx.project_id).get(task_id)
            if task is None:
                raise DomainError("validation_error", "Linked task does not exist in this action's workspace",
                                  field_errors={"task_id": "not found"})
            return task
        task_id, places = self.tasks.lookup.find(ctx, uow, task_id)
        if len(places) > 1:
            named = set(args.get("project_ids") or ())
            if args.get("category") == "global-operational":
                named.add(None)
            chosen = [place for place in places if place["project_id"] in named]
            if len(chosen) != 1:
                raise DomainError(
                    "validation_error", "This task id is in more than one project; name the project of the one you mean in "
                    "project_ids (category global-operational for the Global one)",
                    field_errors={"project_ids": "required: the task id is in more than one project"},
                    details={"task_id": task_id, "candidates": places[:CANDIDATES]})
            places = chosen
        if not places:
            raise DomainError("validation_error", "Linked task does not exist, or is in a project outside your reach",
                              field_errors={"task_id": "not found"})
        return uow.repository("tasks", project_id=places[0]["project_id"]).get(task_id, required=True)

    def classified(self, ctx, uow, args):
        """``(category, projects, task)`` of a record, when it is prepared and again when its prepared plan is validated.

        The linked Task is found first, because what a Global recorder left out of the classification is taken from it
        (``classification_from_task``). ``classify`` then authorizes the result exactly as if the caller had passed it."""
        task = self.linked_task(ctx, uow, args)
        category, project_ids = args.get("category"), args.get("project_ids")
        if task is not None and action_namespace(ctx, {}) == "global":
            category, project_ids = classification_from_task(category, project_ids, task["project_id"])
        return (*classify(ctx, uow, category, project_ids), task)

    # -- writes ----------------------------------------------------------------------------------------------------

    def prepare_record(self, ctx, args, options, uow):
        reader_scope(ctx, uow)
        origin = models.origin_for(ctx)
        if origin not in ("agent", "manual"):
            raise DomainError("forbidden", "Only the owner and registered agents record actions directly")
        text = models.validate_text(args["text"], origin)
        category, projects, task = self.classified(ctx, uow, args)
        builder = PlanBuilder(ctx, "actions_record", args, "Record action", clock=self.clock)
        refs = dict(args.get("evidence", {}))
        if task is not None:
            builder.expect(ref("tasks", task["id"], task["project_id"]), task)
            refs["task"] = task["id"]
        assignment = self.delegation(ctx, uow, args, category, projects)
        performer, identity, provenance = UNSET, None, {"via": "actions_record"}
        if args.get("relay") is not None or args.get("performed_by") not in (None, ctx.principal_id):
            performer, reported = self.reported_performer(ctx, args, uow)
            provenance["relay"] = reported
            refs["reported_performer"] = performer
        if assignment is not None:
            # A delegated result: the worker has no principal of its own, so the performer is unknown, the caller is the
            # recorder and the verified assignment is the provenance.
            performer = None
            generation = str(ctx.delegation_generation if ctx.delegation_assignment_id else assignment["execution_generation"])
            refs.update(delegation_assignment=assignment["id"], delegation_generation=generation)
            provenance["delegation_assignment"] = assignment["id"]
            if ctx.delegation_assignment_id is None:
                identity = str(uuid.uuid5(models.SOURCE_NAMESPACE, "delegation:" + assignment["id"]))
        new_action(builder, uow, text=text, category=category, projects=projects, origin=origin, refs=refs, performer=performer,
                   occurrence=self.occurrence(builder, args.get("real_time")), tags=args.get("tags", ()),
                   related=args.get("related_objects", ()), identity=identity, provenance=provenance)
        builder.effects.append("Records the action and queues the accepted revision for Context in the same transaction.")
        return builder.build()

    def validate_record(self, ctx, plan, uow):
        # Grants, project authorization, the linked Task's place and delegated provenance are rechecked at apply.
        reader_scope(ctx, uow)
        category, projects, _ = self.classified(ctx, uow, plan.arguments)
        self.delegation(ctx, uow, plan.arguments, category, projects)
        if plan.arguments.get("relay") is not None or plan.arguments.get("performed_by") not in (None, ctx.principal_id):
            self.reported_performer(ctx, plan.arguments, uow)

    @staticmethod
    def reported_performer(ctx, args, uow):
        relay = args.get("relay")
        if not relay or relay.get("kind") not in {"explicit_owner_statement", "agent_report"}:
            raise DomainError("validation_error", "Recording another performer's completed work requires their explicit statement or report")
        if args.get("delegation_assignment_id") or ctx.delegation_assignment_id:
            raise DomainError("validation_error", "Use either a reported performer or a verified delegated assignment")
        performer, reported = attribution(ctx, uow, relay)
        if args.get("performed_by", performer) != performer:
            raise DomainError("validation_error", "performed_by must match the reported speaker")
        return performer, reported

    def target(self, ctx, uow, args, options):
        """The Action a correction or withdrawal changes, checked for visibility, authority and the expected revision."""
        scope = reader_scope(ctx, uow)
        row = self.find(uow, args["action_id"])
        if row is None or not visible(scope, row, links(uow, row["id"])):
            raise DomainError("not_found", "Action does not exist")
        if not may_change(ctx, uow, row):
            raise DomainError("forbidden", "Only the original recorder, owner or configured coordinator can change an accessible Action in its owning scope")
        require_revision(row, options.expected_revision)
        if row["withdrawn"]:
            raise DomainError("invalid_transition", "Withdrawn Actions retain history; later activity requires a new Action")
        return row

    def prepare_correct(self, ctx, args, options, uow):
        row = self.target(ctx, uow, args, options)
        builder = PlanBuilder(ctx, "actions_correct", args, "Correct action", clock=self.clock)
        current_links = links(uow, row["id"])
        category = args["category"] if "category" in args else row["category"]
        category, projects = classify(ctx, uow, category, args.get("project_ids", current_links if category == "project-operational" else []))
        changes = {"category": category}
        if "text" in args:
            changes["action_text"] = models.validate_text(args["text"], models.origin_for(ctx))
        if "real_time" in args:
            occurrence = (local_time.recorded_time(row["created_at"]) if args["real_time"] is None
                          else local_time.parse_real_time(args["real_time"], self.clock))
            changes.update(occurred_at=occurrence["occurred_at"], occurred_date=occurrence["real_date"],
                           occurred_hour=occurrence["real_hour"], time_precision=occurrence["time_precision"],
                           time_zone=occurrence["time_zone"])
        if "evidence" in args:
            changes["refs_json"] = {**{key: value for key, value in row["refs_json"].items() if key not in models.EVIDENCE_KEYS},
                                    **args["evidence"]}
        if "related_objects" in args:
            changes["related_objects_json"] = models.related_objects(args["related_objects"])
        change_action(builder, uow, row, operation="edit", changes=changes, projects=projects,
                      tags=args.get("tags"), provenance={"via": "actions_correct"})
        builder.effects.append("Corrects the action as its next immutable revision and queues it for Context in the same transaction.")
        return builder.build()

    def prepare_withdraw(self, ctx, args, options, uow):
        row = self.target(ctx, uow, args, options)
        builder = PlanBuilder(ctx, "actions_withdraw", args, "Withdraw action", clock=self.clock)
        change_action(builder, uow, row, operation="remove", changes={"withdrawn": 1}, provenance={"via": "actions_withdraw"})
        builder.effects.append("Withdraws the action; its history stays and the removal is queued for Context in the same transaction.")
        return builder.build()

    def validate_change(self, ctx, plan, uow):
        scope = reader_scope(ctx, uow)
        row = self.find(uow, plan.arguments["action_id"])
        if row is None or not visible(scope, row, links(uow, row["id"])) or not may_change(ctx, uow, row) or row["withdrawn"]:
            raise DomainError("forbidden", "The action can no longer be changed by this caller")
        if plan.operation_name == "actions_correct":
            change = next(change for change in plan.changes if change.resource.resource_type == "action_logs")
            category = plain(change.values).get("category", row["category"])
            linked = set(links(uow, row["id"]))
            for item in plan.changes:
                if item.resource.resource_type == "action_projects":
                    project = json.loads(item.resource.resource_id)["project_id"]
                    linked = linked - {project} if item.action == "delete" else linked | {project}
            classify(ctx, uow, category, sorted(linked))

    def apply(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        settled = settle(ctx, plan, uow, on_revision=self.on_revision)
        row = self.find(uow, settled[0]["action_id"])
        scope = reader_scope(ctx, uow)
        return {"action": self.view(ctx, uow, row, scope), "revision": settled[0]["revision"], "context_input_id": settled[0]["input_id"],
                **self.time_view()}

    # -- discussion ------------------------------------------------------------------------------------------------

    def commentable(self, ctx, uow, args):
        """The Action and its discussion item for a comment: readable by the caller AND in the caller's own namespace.

        A project that merely reads a shared Global Action through a retained link cannot write to it."""
        scope = reader_scope(ctx, uow)
        row = self.find(uow, args["action_id"])
        if row is None or not visible(scope, row, links(uow, row["id"])):
            raise DomainError("not_found", "Action does not exist")
        item = ThreadItem(row["project_id"], "action", row["id"])
        if row["project_id"] != ctx.project_id or not can_follow(uow, item, ctx.principal_id):
            raise DomainError("forbidden", "Only an active participant of an action's owning namespace can discuss it")
        return row, item

    def prepare_comment(self, ctx, args, options, uow):
        row, item = self.commentable(ctx, uow, args)
        builder = PlanBuilder(ctx, ACTION_COMMENT, args, "Comment on action", clock=self.clock)
        builder.expect(ref("action_logs", row["id"], row["project_id"]), row)
        plan_comment(ctx, uow, builder, item, note=args["text"], summary=args.get("summary"), refs=args.get("note_refs", []),
                     relay=args.get("relay"), resolve=lambda refs: self.references.resolve(ctx, refs, uow, builder),
                     guard=lambda: reject_owner_team_write(ctx, uow, ACTION_COMMENT))
        builder.effects.append("Adds a comment to the action's discussion. The action, its revisions and the Context queue are unchanged.")
        return builder.build()

    def validate_comment(self, ctx, plan, uow):
        self.commentable(ctx, uow, plan.arguments)
        self.references.resolve(ctx, plan.arguments.get("note_refs", ()), uow, strict=True)

    def apply_comment(self, ctx, plan, uow):
        comments = [plain(change.values) for change in plan.changes if change.resource.resource_type == "thread_messages"]
        apply_records(ctx, plan, uow)
        row = self.find(uow, plan.arguments["action_id"])
        item = ThreadItem(row["project_id"], "action", row["id"])
        for comment in comments:
            auto_follow(uow, item, [comment["recorder_id"]], "commenter")
            if comment["author_id"] != comment["recorder_id"]:
                auto_follow(uow, item, [comment["author_id"]], "represented")
        event = emit_event(uow, project_id=row["project_id"], source_type=SOURCE_TYPE, source_id=row["id"],
                           source_revision=row["revision"], actor_id=ctx.principal_id, event_type=COMMENT_EVENT,
                           summary=models.one_line(row["action_text"]), optional=followers(uow, item))
        if event is not None:
            attach_event(uow, item, [comment["id"] for comment in comments], event)
        stored = uow.connection.execute("SELECT * FROM thread_messages WHERE project_id IS ? AND id=?",
                                        (row["project_id"], comments[0]["id"])).fetchone()
        return {"action_id": row["id"], "thread_id": item.thread_id,
                "message": message_view(dict(stored), uow, self.references.message(ctx, uow))}

    # -- follows ---------------------------------------------------------------------------------------------------

    def prepare_follow(self, ctx, arguments, options, uow):
        scope = reader_scope(ctx, uow)
        row = self.find(uow, arguments["action_id"])
        if row is None or not visible(scope, row, links(uow, row["id"])):
            raise DomainError("not_found", "Action does not exist")
        item = ThreadItem(row["project_id"], "action", row["id"])
        if row["project_id"] != ctx.project_id or not can_follow(uow, item, ctx.principal_id):
            raise DomainError("forbidden", "Only an active participant of an action's owning namespace can follow it")
        builder = PlanBuilder(ctx, ACTION_FOLLOW, arguments, "Follow action" if arguments["following"] else "Unfollow action", clock=self.clock)
        builder.expect(ref("action_logs", row["id"], row["project_id"]), row)
        key = {"item_type": "action", "item_id": row["id"], "principal_id": ctx.principal_id}
        existing = uow.repository("thread_follows", project_id=ctx.project_id).get(key)
        values = {"following": int(arguments["following"]), "explicit": 1, "reason": "explicit", "updated_at": builder.at}
        if existing:
            builder.record("thread_follows", key, existing, values, label="follow")
        else:
            builder.record("thread_follows", key, None, {**key, **values, "created_at": builder.at}, label="follow")
        builder.effects.append("Changes only your own optional subscription to this action's updates.")
        return builder.build()

    def apply_follow(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        row = self.find(uow, plan.arguments["action_id"])
        return {"action_id": row["id"], **follow_state(uow, ThreadItem(row["project_id"], "action", row["id"]), ctx.principal_id)}

    # -- reads -----------------------------------------------------------------------------------------------------

    def time_view(self):
        return {"timezone": local_time.TIMEZONE, "now": local_time.now_view(self.clock)}

    # Newest occurrence first. Day-only entries have unknown intra-day order: they sort after that day's timed entries,
    # by identity, never by their midnight anchor.
    ORDER_COLUMNS = ("COALESCE(a.occurred_date,'')", "CASE WHEN a.time_precision='day' THEN 0 ELSE 1 END",
                     "CASE WHEN a.time_precision='day' THEN '' ELSE COALESCE(a.occurred_at,'') END", "a.id")

    def query_page(self, uow, scope, args, after, limit):
        """Authorized, filtered, cursor-positioned rows in SQL, fetching at most limit+1."""
        clause, parameters = scope_clause(scope)
        where = [clause]
        if args.get("project") is not None:
            where.append("EXISTS (SELECT 1 FROM action_projects f WHERE f.action_id=a.id AND f.project_id=?)")
            parameters.append(args["project"])
        if args.get("since"):
            where.append("COALESCE(a.occurred_date,'') >= ?")
            parameters.append(args["since"])
        if args.get("until"):
            where.append("a.occurred_date IS NOT NULL AND a.occurred_date <= ?")
            parameters.append(args["until"])
        if args.get("categories"):
            named = [value for value in args["categories"] if value != "unclassified"]
            options = ([f"a.category IN ({','.join('?' for _ in named)})"] if named else []) + \
                      (["a.category IS NULL"] if "unclassified" in args["categories"] else [])
            where.append("(" + " OR ".join(options) + ")")
            parameters.extend(named)
        if args.get("origins"):
            where.append(f"a.origin IN ({','.join('?' for _ in args['origins'])})")
            parameters.extend(args["origins"])
        if args.get("performer"):
            where.append("COALESCE(a.performed_by, a.recorded_by) = ?")
            parameters.append(args["performer"])
        if not args.get("include_withdrawn"):
            where.append("a.withdrawn=0")
        for label in distinct_labels(args.get("tags", ())):
            where.append("EXISTS (SELECT 1 FROM item_tags i JOIN tags t ON t.id=i.tag_id WHERE i.project_id IS a.project_id "
                         "AND i.item_type='action' AND i.item_id=a.id AND t.label_key=?)")
            parameters.append(label_key(label))
        columns = ", ".join(self.ORDER_COLUMNS)
        if after is not None:
            if len(after) != 4 or not all(isinstance(value, (str, int)) for value in after):
                raise DomainError("validation_error", "Invalid or differently scoped list cursor")
            where.append(f"({columns}) < (?, ?, ?, ?)")
            parameters.extend(after)
        sorts = ", ".join(f"{column} AS sort_{index}" for index, column in enumerate(self.ORDER_COLUMNS))
        found = uow.connection.execute(
            f"SELECT a.*, {sorts} FROM action_logs a WHERE {' AND '.join(where)} "
            f"ORDER BY {', '.join(column + ' DESC' for column in self.ORDER_COLUMNS)} LIMIT ?", [*parameters, limit + 1]).fetchall()
        result = []
        for row in found:
            decoded = decode_row(row)
            result.append((decoded, [decoded.pop(f"sort_{index}") for index in range(len(self.ORDER_COLUMNS))]))
        return result

    @staticmethod
    def principals(uow, identities):
        identities = sorted({identity for identity in identities if identity})
        if not identities:
            return {}
        marks = ",".join("?" for _ in identities)
        return {row["id"]: {"id": row["id"], "kind": row["kind"], "name": row["display_name"]} for row in uow.connection.execute(
            f"SELECT id, kind, display_name FROM principals WHERE id IN ({marks})", identities)}

    @staticmethod
    def projects(uow, identities):
        identities = sorted(set(identities))
        if not identities:
            return {}
        marks = ",".join("?" for _ in identities)
        return {row["id"]: {"id": row["id"], "slug": row["slug"], "name": row["name"]} for row in uow.connection.execute(
            f"SELECT id, slug, name FROM projects WHERE id IN ({marks})", identities)}

    def view(self, ctx, uow, row, scope, *, row_links=None, people=None, names=None):
        row_links = links(uow, row["id"]) if row_links is None else row_links
        shown = [project for project in row_links if scope["kind"] == "owner" or project in scope["projects"]]
        people = people if people is not None else self.principals(uow, (row["performed_by"], row["recorded_by"]))
        names = names if names is not None else self.projects(uow, shown)
        disclosed = evidence_visible(scope, row, row_links)
        occurred = {"date": row["occurred_date"], "hour": row["occurred_hour"] if row["time_precision"] != "day" else None,
                    "precision": row["time_precision"], "time_zone": row["time_zone"]}
        if row["time_precision"] == "instant":
            occurred["instant"] = row["occurred_at"]
        return {"action_id": row["id"], "text": row["action_text"], "summary": models.one_line(row["action_text"]),
                "category": row["category"], "projects": [names.get(project, {"id": project}) for project in shown],
                "performed_by": people.get(row["performed_by"]), "recorded_by": people.get(row["recorded_by"]),
                # Without a known performer the recorder is shown as recorded attribution only.
                "attribution": "performed" if row["performed_by"] else "recorded",
                "origin": row["origin"], "occurred": occurred, "recorded_at": row["created_at"], "updated_at": row["updated_at"],
                "revision": row["revision"], "withdrawn": bool(row["withdrawn"]),
                "can_change": bool(not row["withdrawn"] and may_change(ctx, uow, row) and visible(scope, row, row_links)),
                "tags": item_labels(uow, row["project_id"], "action", row["id"]) if disclosed else [],
                "related_objects": row["related_objects_json"] if disclosed else [],
                "refs": row["refs_json"] if disclosed else {},
                "project_id": row["project_id"] if disclosed else None}

    def revision_view(self, ctx, uow, row, scope, revision):
        stored = uow.repository("action_revisions", project_id=row["project_id"]).get({"action_id": row["id"], "revision": revision})
        if stored is None:
            raise DomainError("not_found", "Action revision does not exist")
        row_links = links(uow, row["id"])
        disclosed = evidence_visible(scope, row, row_links)
        snapshot = stored["snapshot_json"]
        shown = [project for project in snapshot["project_ids"] if scope["kind"] == "owner" or project in scope["projects"]]
        people = self.principals(uow, (stored["performed_by"], stored["recorded_by"]))
        return {"action_id": row["id"], "revision": stored["revision"], "operation": stored["operation"],
                "text": snapshot["text"], "category": snapshot["category"], "origin": snapshot["origin"], "project_ids": shown,
                "tags": [tag["name"] for tag in snapshot["tag_snapshots"]] if disclosed else [],
                "related_objects": snapshot["related_objects"] if disclosed else [],
                "refs": snapshot["refs"] if disclosed else {}, "occurred": snapshot["occurred"], "withdrawn": snapshot["withdrawn"],
                "recorded_by": people.get(stored["recorded_by"]), "performed_by": people.get(stored["performed_by"]),
                "recorded_at": stored["created_at"]}

    def read(self, operation, ctx, args, uow):
        scope = reader_scope(ctx, uow)
        if operation == "actions_config_get":
            return self.config_view(ctx, uow, scope)
        if operation == "actions_thread_get":
            row = self.find(uow, args["action_id"])
            if row is None or not visible(scope, row, links(uow, row["id"])):
                raise DomainError("not_found", "Action does not exist")
            item = ThreadItem(row["project_id"], "action", row["id"])
            history = read_messages(uow, item, args, operation, ctx.principal_id, resolve_refs=self.references.message(ctx, uow))
            people = {row["recorded_by"], row["performed_by"], *(message["created_by"] for message in history["messages"])}
            return {"thread_id": history["thread_id"], "action_id": row["id"], "participants": participants(uow, people),
                    "messages": history["messages"], "next_cursor": history["next_cursor"]}
        if operation == "actions_get":
            row = self.find(uow, args["action_id"])
            if row is None or not visible(scope, row, links(uow, row["id"])):
                raise DomainError("not_found", "Action does not exist")
            if "revision" in args:
                return {"action": self.revision_view(ctx, uow, row, scope, args["revision"]), **self.time_view()}
            return {"action": self.view(ctx, uow, row, scope), **self.time_view()}
        project = args.get("project")
        if project is not None and scope["kind"] != "owner" and project not in scope["projects"]:
            raise DomainError("forbidden", "Project is outside your action scope")
        binding = ctx.principal_id + ":" + scope["kind"] + ":" + ",".join(sorted(scope["projects"]))
        binding, after = page_position(args, binding, operation)
        limit = args.get("limit", 50)
        found = self.query_page(uow, scope, args, after, limit)
        selected = [row for row, _ in found[:limit]]
        cursor = (base64.urlsafe_b64encode(canonical_bytes({"binding": binding, "after": found[limit - 1][1]})).decode("ascii")
                  if len(found) > limit else None)
        keys = [row["id"] for row in selected]
        all_links = {key: [] for key in keys}
        if keys:
            marks = ",".join("?" for _ in keys)
            for row in uow.connection.execute(f"SELECT action_id, project_id FROM action_projects WHERE action_id IN ({marks}) "
                                              "ORDER BY project_id", keys):
                all_links[row[0]].append(row[1])
        people = self.principals(uow, [value for row in selected for value in (row["performed_by"], row["recorded_by"])])
        names = self.projects(uow, [project for row_links in all_links.values() for project in row_links])
        return {"actions": [self.view(ctx, uow, row, scope, row_links=all_links[row["id"]], people=people, names=names)
                            for row in selected], "next_cursor": cursor, **self.time_view()}

    def config_view(self, ctx, uow, scope):
        directory = self.projects(uow, [project for project in scope["projects"] if project in active_project_ids(uow.connection)])
        if scope["kind"] == "project":
            allowed, default, required = ["project-operational"], "project-operational", False
            defaults = sorted(scope["projects"])
        elif scope["kind"] == "ecosystem":
            allowed, default, required, defaults = list(models.GLOBAL_AGENT_CATEGORIES), None, True, []
        else:
            allowed, default, required, defaults = [*CATEGORIES, None], None, False, []
        return {"categories": [{"id": category, "label": category} for category in CATEGORIES],
                "unclassified": "Unclassified (no category) is available for owner capture only",
                "agent_word_limit": models.AGENT_WORD_LIMIT, "text_max_characters": models.TEXT_LIMIT,
                "real_time": {"formats": ["YYYY-MM-DD", "YYYY-MM-DDTHH"], "zone": local_time.TIMEZONE,
                              "omitted": "now (server clock)", "future": "rejected",
                              "nonexistent_hour": "rejected", "repeated_hour": "first occurrence"},
                "sharing": models.SHARING, "tags": {"source": "catalog", "operation": "tags_list"},
                "scope": {"kind": scope["kind"], "allowed_categories": allowed, "default_category": default,
                          "category_required": required, "default_project_ids": defaults,
                          "projects": sorted(directory.values(), key=lambda item: (item["name"] or "", item["id"])),
                          "can_change_own": True, "owner": scope["kind"] == "owner" and is_owner(ctx, uow)},
                **self.time_view()}




__all__ = ["ACTION_ENTRIES", "ActionOperations", "action_namespace"]
