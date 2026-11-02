"""Action operations: record, correct, withdraw, follow and the authorized reads.

An Action is one completed result in one owning namespace. ``actions_record`` keeps its v1 arguments (and adds tags and
Context pointers); an owner or the original recorder corrects or withdraws it as the next immutable revision, and every
accepted revision is queued for Context in the same transaction (see ``writer``). Reads follow business links, never
the storage namespace, exactly as in v1, and a withdrawn Action stays readable with its history.

An Action's discussion is the canonical item discussion (``qorqut_operations.discussion``). ``actions_thread_get`` is
authorized exactly like ``actions_get``, retained business read links included. ``actions_comment`` is a separate
mutation that needs its own policy and participation in the Action's owning namespace (an active human member, or the
owner for Global, or an active global agent invoking in that namespace): a read link is never a write.
A comment writes one ``thread_messages`` row, the commenter's (and a relay's represented speaker's) default follow and
one optional event; it creates no Action revision and no Context input."""

from __future__ import annotations

import base64
import json
import uuid

from qorqut_operations import local_time
from qorqut_operations.actions import models
from qorqut_operations.actions.schema import CATEGORIES
from qorqut_operations.actions.scope import (action_namespace, change_namespace, classify, evidence_visible, is_owner, links, may_change,
                                              reader_scope, scope_clause, visible)
from qorqut_operations.actions.writer import SOURCE_TYPE, change_action, new_action, settle
from qorqut_operations.application.catalog import CatalogContribution
from qorqut_operations.application.plans import (DATE, IDENTITY, PAGE, TEXT, UNSET, UUID, PlanBuilder, apply_records,
                                                 page_position, ref, require_revision, schema, utc_clock)
from qorqut_operations.application.registry import CatalogEntry, OperationDescriptor
from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import DomainError, canonical_bytes, plain
from qorqut_operations.conversations import SUMMARY, participants
from qorqut_operations.discussion.events import emit_event
from qorqut_operations.discussion.follows import ThreadItem, auto_follow, can_follow, follow_state, followers
from qorqut_operations.discussion.threads import (BODY, RELAY, DiscussionReferences, attach_event, message_view, plan_comment,
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
ACTION_ID = {"type": "string", "minLength": 1, "maxLength": 300}
BUSINESS_IDS = {"type": "array", "items": UUID, "maxItems": 50, "uniqueItems": True}
RECORD_DESCRIPTION = (
    "Record one atomic COMPLETED result (not a task description, plan, proposal, status change or failed attempt) "
    "in the canonical action log. Text is the result in at most 20 words for agents; no task, title or time is "
    "required. Omitted real_time means now; supplied real_time is always San Juan (America/Puerto_Rico) time, "
    "YYYY-MM-DD or YYYY-MM-DDTHH. Agents holding read_global_context record in Global: category is required "
    "(global-operational or business-operational); business-operational needs business_ids of currently authorized, "
    "active projects, and one inaccessible project rejects the whole call. An agent without read_global_context "
    "records only with a project selected: text only; the action is business-operational for that project. "
    "Tags come from the shared tag catalog (tags_list); a new label is created by using it, and a tag grants no access. "
    "related_objects are optional suggestions of Context objects the result concerns. Every accepted record is queued for "
    "Context as revision 1. You are the performer and recorder of your own work; never record another actor's work as "
    "yours or duplicate system receipts. " + models.SHARING)
DELEGATION_DESCRIPTION = (
    "Optional: record the result of your own COMPLETED delegated assignment. The server verifies that you own the "
    "assignment, that it completed with a recorded result and that the projects are within its scope; the performer "
    "is then unknown, you are the recorder and the assignment is kept as provenance. Each assignment's result is "
    "recorded once, and not at all when its worker already recorded its actions.")
CORRECT_DESCRIPTION = (
    "Correct an Action you recorded (the installation owner may correct any visible Action): supply the fields to change "
    "(text, category, business_ids, real_time, tags, related_objects, evidence) with expected_revision. The correction is a "
    "new immutable revision queued for Context; earlier revisions are kept. Identity, origin, performer and recorder never "
    "change. A withdrawn Action cannot be corrected: record a new one.")
WITHDRAW_DESCRIPTION = (
    "Withdraw an Action you recorded (the installation owner may withdraw any visible Action) with expected_revision. The "
    "Action and its history stay readable, marked withdrawn; the withdrawal is queued for Context as a removal and the "
    "Action accepts no later change.")
LIST_DESCRIPTION = ("List authorized completed actions by occurrence (newest first) with date (San Juan), category, "
                    "business, performer and tag filters; withdrawn actions are omitted unless requested. With a project "
                    "selected you see the actions linked to that project; without one, agents holding read_global_context "
                    "see global-operational and their accessible business-operational actions. " + models.SHARING)
THREAD_DESCRIPTION = (
    "Read one bounded page of an authorized action's discussion (oldest first within the page; next_cursor continues to older "
    "messages). Authorized exactly like actions_get: unauthorized or unknown actions are not found, and a project that can read a "
    "shared action reads its discussion. Reading never follows the action. " + models.SHARING)
COMMENT_DESCRIPTION = (
    "Add a comment to an action's discussion (it changes neither the action nor its revisions, and queues nothing for Context). "
    "Needs its own permission and an active participant invoking in the action's owning namespace: being linked to an action "
    "is read access only. "
    "You follow the discussion by default unless you explicitly unfollowed it. The registered coordinator may relay an explicit "
    "installation-owner statement or another agent's report with relay; the recorder is always kept and a relay is never approval.")
FOLLOW = schema({"action_id": ACTION_ID, "following": {"type": "boolean"}}, ("action_id", "following"))
COMMENT_EVENT = "note_added"


class ActionOperations:
    def __init__(self, application, references: DiscussionReferences, *, clock=utc_clock):
        if not isinstance(references, DiscussionReferences):
            raise AssemblyError("Action discussions need the note references of the Task service (note_references())")
        if not callable(clock):
            raise AssemblyError("The action clock is a callable returning an aware timestamp")
        self.application, self.references, self.clock = application, references, clock

    # -- registration ----------------------------------------------------------------------------------------------

    def contribution(self) -> CatalogContribution:
        return CatalogContribution("actions", ACTION_ENTRIES, self.descriptors())

    def descriptors(self):
        common = dict(scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
                      resolve_namespace=action_namespace)
        filters = {**PAGE, "since": DATE, "until": DATE, "business": UUID, "performer": UUID, "tags": LABELS_SCHEMA,
                   "categories": {"type": "array", "items": {"enum": [*CATEGORIES, "unclassified"]}, "uniqueItems": True, "maxItems": 4},
                   "origins": {"type": "array", "items": {"enum": list(models.ORIGINS)}, "uniqueItems": True, "maxItems": 4},
                   "include_withdrawn": {"type": "boolean"}}
        reads = (("actions_list", filters, (), LIST_DESCRIPTION),
                 ("actions_get", {"action_id": ACTION_ID, "revision": {"type": "integer", "minimum": 1}}, ("action_id",),
                  "Read one authorized action by action_id (its current state, or an exact earlier revision with revision). "
                  "Unauthorized or unknown actions are not found. " + models.SHARING),
                 ("actions_config_get", {}, (), "Categories, the 20-word agent rule, the San Juan time convention "
                  "(timezone, current local date/hour and server instant) and your allowed/default action scope."),
                 ("actions_thread_get", {**PAGE, "action_id": ACTION_ID}, ("action_id",), THREAD_DESCRIPTION))
        descriptors = [OperationDescriptor(name, schema(properties, required), **common, effect_kind="read", supports_signoff=False,
                                           description=description,
                                           read=lambda ctx, args, options, uow, op=name: self.read(op, ctx, args, uow))
                       for name, properties, required, description in reads]
        optional = {"tags": LABELS_SCHEMA, "related_objects": models.RELATED_OBJECTS, "evidence": models.EVIDENCE}
        descriptors.append(OperationDescriptor("actions_record", schema({
            "text": models.ACTION_TEXT, "category": {"enum": list(CATEGORIES)}, "business_ids": BUSINESS_IDS,
            "real_time": local_time.REAL_TIME_SCHEMA, "task_id": IDENTITY, **optional,
            "delegation_assignment_id": {**UUID, "description": DELEGATION_DESCRIPTION}, "rationale": TEXT}, ("text",)),
            **common, description=RECORD_DESCRIPTION, prepare=self.prepare_record, apply=self.apply,
            validate_targets=self.validate_record))
        changing = {**common, "resolve_namespace": change_namespace}
        descriptors.append(OperationDescriptor("actions_correct", schema({
            "action_id": ACTION_ID, "text": models.ACTION_TEXT, "category": {"anyOf": [{"enum": list(CATEGORIES)}, {"type": "null"}]},
            "business_ids": BUSINESS_IDS, "real_time": {"anyOf": [local_time.REAL_TIME_SCHEMA, {"type": "null"}]}, **optional,
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
            description="Follow or unfollow an action for yourself. The recorder follows by default; an explicit unfollow stays "
                        "until you follow again. Followers are told when it is corrected or withdrawn."))
        return tuple(descriptors)

    # -- shared rules ----------------------------------------------------------------------------------------------

    def occurrence(self, builder, real_time):
        if real_time is None:
            return local_time.recorded_time(builder.at)
        return local_time.parse_real_time(real_time, self.clock)

    def delegation(self, ctx, uow, args, category, businesses):
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
        if assignment is None or assignment["parent_principal_id"] != ctx.principal_id:
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
        allowed = {assignment["business_id"]} if scope.get("kind") == "business" else set(scope.get("allowed_business_ids", []))
        if (scope.get("kind") == "business" and category != "business-operational") or not set(businesses) <= allowed:
            raise DomainError("forbidden", "The action is outside the delegated assignment's project scope")
        return assignment

    @staticmethod
    def find(uow, action_id):
        return decode_row(uow.connection.execute("SELECT * FROM action_logs WHERE action_key=?", (action_id,)).fetchone())

    # -- writes ----------------------------------------------------------------------------------------------------

    def prepare_record(self, ctx, args, options, uow):
        reader_scope(ctx, uow)
        origin = models.origin_for(ctx)
        if origin not in ("agent", "manual"):
            raise DomainError("forbidden", "Only the owner and registered agents record actions directly")
        text = models.validate_text(args["text"], origin)
        category, businesses = classify(ctx, uow, args.get("category"), args.get("business_ids"))
        builder = PlanBuilder(ctx, "actions_record", args, "Record action", clock=self.clock)
        refs = dict(args.get("evidence", {}))
        if args.get("task_id"):
            task = uow.repository("tasks", business_id=ctx.business_id).get(args["task_id"])
            if task is None:
                raise DomainError("validation_error", "Linked task does not exist in this action's workspace",
                                  field_errors={"task_id": "not found"})
            builder.expect(ref("tasks", task["id"], ctx.business_id), task)
            refs["task"] = task["id"]
        assignment = self.delegation(ctx, uow, args, category, businesses)
        performer, identity, provenance = UNSET, None, {"via": "actions_record"}
        if assignment is not None:
            # A delegated result: the worker has no principal of its own, so the performer is unknown, the caller is the
            # recorder and the verified assignment is the provenance.
            performer = None
            generation = str(ctx.delegation_generation if ctx.delegation_assignment_id else assignment["execution_generation"])
            refs.update(delegation_assignment=assignment["id"], delegation_generation=generation)
            provenance["delegation_assignment"] = assignment["id"]
            if ctx.delegation_assignment_id is None:
                identity = str(uuid.uuid5(models.SOURCE_NAMESPACE, "delegation:" + assignment["id"]))
        new_action(builder, uow, text=text, category=category, businesses=businesses, origin=origin, refs=refs, performer=performer,
                   occurrence=self.occurrence(builder, args.get("real_time")), tags=args.get("tags", ()),
                   related=args.get("related_objects", ()), identity=identity, provenance=provenance)
        builder.effects.append("Records the action and queues the accepted revision for Context in the same transaction.")
        return builder.build()

    def validate_record(self, ctx, plan, uow):
        # Grants, business authorization and delegated provenance are rechecked at apply.
        reader_scope(ctx, uow)
        category, businesses = classify(ctx, uow, plan.arguments.get("category"), plan.arguments.get("business_ids"))
        self.delegation(ctx, uow, plan.arguments, category, businesses)

    def target(self, ctx, uow, args, options):
        """The Action a correction or withdrawal changes, checked for visibility, authority and the expected revision."""
        scope = reader_scope(ctx, uow)
        row = self.find(uow, args["action_id"])
        if row is None or not visible(scope, row, links(uow, row["action_key"])):
            raise DomainError("not_found", "Action does not exist")
        if not may_change(ctx, uow, row):
            raise DomainError("forbidden", "Only the original recorder (or the owner) can change an Action in its owning scope")
        require_revision(row, options.expected_revision)
        if row["withdrawn"]:
            raise DomainError("invalid_transition", "Withdrawn Actions retain history; later activity requires a new Action")
        return row

    def prepare_correct(self, ctx, args, options, uow):
        row = self.target(ctx, uow, args, options)
        builder = PlanBuilder(ctx, "actions_correct", args, "Correct action", clock=self.clock)
        current_links = links(uow, row["action_key"])
        category = args["category"] if "category" in args else row["category"]
        category, businesses = classify(ctx, uow, category, args.get("business_ids", current_links if category == "business-operational" else []))
        changes = {"category": category}
        if "text" in args:
            changes["action_text"] = models.validate_text(args["text"], models.origin_for(ctx))
        if "real_time" in args:
            occurrence = (local_time.recorded_time(row["created_at"]) if args["real_time"] is None
                          else local_time.parse_real_time(args["real_time"], self.clock))
            changes.update(occurred_at=occurrence["occurred_at"], occurred_date=occurrence["real_date"],
                           occurred_hour=occurrence["real_hour"], time_precision=occurrence["time_precision"])
        if "evidence" in args:
            changes["refs_json"] = {**{key: value for key, value in row["refs_json"].items() if key not in models.EVIDENCE_KEYS},
                                    **args["evidence"]}
        if "related_objects" in args:
            changes["related_objects_json"] = models.related_objects(args["related_objects"])
        change_action(builder, uow, row, operation="edit", changes=changes, businesses=businesses,
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
        if row is None or not visible(scope, row, links(uow, row["action_key"])) or not may_change(ctx, uow, row) or row["withdrawn"]:
            raise DomainError("forbidden", "The action can no longer be changed by this caller")
        if plan.operation_name == "actions_correct":
            change = next(change for change in plan.changes if change.resource.resource_type == "action_logs")
            category = plain(change.values).get("category", row["category"])
            linked = set(links(uow, row["action_key"]))
            for item in plan.changes:
                if item.resource.resource_type == "action_businesses":
                    business = json.loads(item.resource.resource_id)["business_id"]
                    linked = linked - {business} if item.action == "delete" else linked | {business}
            classify(ctx, uow, category, sorted(linked))

    def apply(self, ctx, plan, uow):
        apply_records(ctx, plan, uow)
        settled = settle(ctx, plan, uow)
        key = settled[0]["action_key"]
        row = self.find(uow, key)
        scope = reader_scope(ctx, uow)
        return {"action": self.view(ctx, uow, row, scope), "revision": settled[0]["revision"], "context_input_id": settled[0]["input_id"],
                **self.time_view()}

    # -- discussion ------------------------------------------------------------------------------------------------

    def commentable(self, ctx, uow, args):
        """The Action and its discussion item for a comment: readable by the caller AND in the caller's own namespace.

        A business that merely reads a shared Global Action through a retained link cannot write to it."""
        scope = reader_scope(ctx, uow)
        row = self.find(uow, args["action_id"])
        if row is None or not visible(scope, row, links(uow, row["action_key"])):
            raise DomainError("not_found", "Action does not exist")
        item = ThreadItem(row["business_id"], "action", row["id"])
        if row["business_id"] != ctx.business_id or not can_follow(uow, item, ctx.principal_id):
            raise DomainError("forbidden", "Only an active participant of an action's owning namespace can discuss it")
        return row, item

    def prepare_comment(self, ctx, args, options, uow):
        row, item = self.commentable(ctx, uow, args)
        builder = PlanBuilder(ctx, ACTION_COMMENT, args, "Comment on action", clock=self.clock)
        builder.expect(ref("action_logs", row["id"], row["business_id"]), row)
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
        item = ThreadItem(row["business_id"], "action", row["id"])
        for comment in comments:
            auto_follow(uow, item, [comment["recorder_id"]], "commenter")
            if comment["author_id"] != comment["recorder_id"]:
                auto_follow(uow, item, [comment["author_id"]], "represented")
        event = emit_event(uow, business_id=row["business_id"], source_type=SOURCE_TYPE, source_id=row["action_key"],
                           source_revision=row["revision"], actor_id=ctx.principal_id, event_type=COMMENT_EVENT,
                           summary=models.one_line(row["action_text"]), optional=followers(uow, item))
        if event is not None:
            attach_event(uow, item, [comment["id"] for comment in comments], event)
        stored = uow.connection.execute("SELECT * FROM thread_messages WHERE business_id IS ? AND id=?",
                                        (row["business_id"], comments[0]["id"])).fetchone()
        return {"action_id": row["action_key"], "thread_id": item.thread_id,
                "message": message_view(dict(stored), uow, self.references.message(ctx, uow))}

    # -- follows ---------------------------------------------------------------------------------------------------

    def prepare_follow(self, ctx, arguments, options, uow):
        scope = reader_scope(ctx, uow)
        row = self.find(uow, arguments["action_id"])
        if row is None or not visible(scope, row, links(uow, row["action_key"])):
            raise DomainError("not_found", "Action does not exist")
        item = ThreadItem(row["business_id"], "action", row["id"])
        if row["business_id"] != ctx.business_id or not can_follow(uow, item, ctx.principal_id):
            raise DomainError("forbidden", "Only an active participant of an action's owning namespace can follow it")
        builder = PlanBuilder(ctx, ACTION_FOLLOW, arguments, "Follow action" if arguments["following"] else "Unfollow action", clock=self.clock)
        builder.expect(ref("action_logs", row["id"], row["business_id"]), row)
        key = {"item_type": "action", "item_id": row["id"], "principal_id": ctx.principal_id}
        existing = uow.repository("thread_follows", business_id=ctx.business_id).get(key)
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
        return {"action_id": row["action_key"], **follow_state(uow, ThreadItem(row["business_id"], "action", row["id"]), ctx.principal_id)}

    # -- reads -----------------------------------------------------------------------------------------------------

    def time_view(self):
        return {"timezone": local_time.TIMEZONE, "now": local_time.now_view(self.clock)}

    # Newest occurrence first. Day-only entries have unknown intra-day order: they sort after that day's timed entries,
    # by identity, never by their midnight anchor.
    ORDER_COLUMNS = ("COALESCE(a.occurred_date,'')", "CASE WHEN a.time_precision='day' THEN 0 ELSE 1 END",
                     "CASE WHEN a.time_precision='day' THEN '' ELSE COALESCE(a.occurred_at,'') END", "a.action_key")

    def query_page(self, uow, scope, args, after, limit):
        """Authorized, filtered, cursor-positioned rows in SQL, fetching at most limit+1."""
        clause, parameters = scope_clause(scope)
        where = [clause]
        if args.get("business") is not None:
            where.append("EXISTS (SELECT 1 FROM action_businesses f WHERE f.action_key=a.action_key AND f.business_id=?)")
            parameters.append(args["business"])
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
            where.append("EXISTS (SELECT 1 FROM item_tags i JOIN tags t ON t.id=i.tag_id WHERE i.business_id IS a.business_id "
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
    def businesses(uow, identities):
        identities = sorted(set(identities))
        if not identities:
            return {}
        marks = ",".join("?" for _ in identities)
        return {row["id"]: {"id": row["id"], "slug": row["slug"], "name": row["name"]} for row in uow.connection.execute(
            f"SELECT id, slug, name FROM businesses WHERE id IN ({marks})", identities)}

    def view(self, ctx, uow, row, scope, *, row_links=None, people=None, names=None):
        row_links = links(uow, row["action_key"]) if row_links is None else row_links
        shown = [business for business in row_links if scope["kind"] == "owner" or business in scope["businesses"]]
        people = people if people is not None else self.principals(uow, (row["performed_by"], row["recorded_by"]))
        names = names if names is not None else self.businesses(uow, shown)
        disclosed = evidence_visible(scope, row, row_links)
        occurred = {"date": row["occurred_date"], "hour": row["occurred_hour"] if row["time_precision"] != "day" else None,
                    "precision": row["time_precision"], "time_zone": row["time_zone"]}
        if row["time_precision"] == "instant":
            occurred["instant"] = row["occurred_at"]
        return {"action_id": row["action_key"], "text": row["action_text"], "summary": models.one_line(row["action_text"]),
                "category": row["category"], "businesses": [names.get(business, {"id": business}) for business in shown],
                "performed_by": people.get(row["performed_by"]), "recorded_by": people.get(row["recorded_by"]),
                # Without a known performer the recorder is shown as recorded attribution only.
                "attribution": "performed" if row["performed_by"] else "recorded",
                "origin": row["origin"], "occurred": occurred, "recorded_at": row["created_at"], "updated_at": row["updated_at"],
                "revision": row["revision"], "withdrawn": bool(row["withdrawn"]),
                "can_change": bool(not row["withdrawn"] and may_change(ctx, uow, row) and visible(scope, row, row_links)),
                "tags": item_labels(uow, row["business_id"], "action", row["id"]) if disclosed else [],
                "related_objects": row["related_objects_json"] if disclosed else [],
                "refs": row["refs_json"] if disclosed else {},
                "legacy": {"business_id": row["business_id"], "id": row["id"]} if disclosed else None}

    def revision_view(self, ctx, uow, row, scope, revision):
        stored = uow.repository("action_revisions", business_id=row["business_id"]).get({"action_key": row["action_key"], "revision": revision})
        if stored is None:
            raise DomainError("not_found", "Action revision does not exist")
        row_links = links(uow, row["action_key"])
        disclosed = evidence_visible(scope, row, row_links)
        snapshot = stored["snapshot_json"]
        shown = [business for business in snapshot["business_ids"] if scope["kind"] == "owner" or business in scope["businesses"]]
        people = self.principals(uow, (stored["performed_by"], stored["recorded_by"]))
        return {"action_id": row["action_key"], "revision": stored["revision"], "operation": stored["operation"],
                "text": snapshot["text"], "category": snapshot["category"], "origin": snapshot["origin"], "business_ids": shown,
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
            if row is None or not visible(scope, row, links(uow, row["action_key"])):
                raise DomainError("not_found", "Action does not exist")
            item = ThreadItem(row["business_id"], "action", row["id"])
            history = read_messages(uow, item, args, operation, ctx.principal_id, resolve_refs=self.references.message(ctx, uow))
            people = {row["recorded_by"], row["performed_by"], *(message["created_by"] for message in history["messages"])}
            return {"thread_id": history["thread_id"], "action_id": row["action_key"], "participants": participants(uow, people),
                    "messages": history["messages"], "next_cursor": history["next_cursor"]}
        if operation == "actions_get":
            row = self.find(uow, args["action_id"])
            if row is None or not visible(scope, row, links(uow, row["action_key"])):
                raise DomainError("not_found", "Action does not exist")
            if "revision" in args:
                return {"action": self.revision_view(ctx, uow, row, scope, args["revision"]), **self.time_view()}
            return {"action": self.view(ctx, uow, row, scope), **self.time_view()}
        business = args.get("business")
        if business is not None and scope["kind"] != "owner" and business not in scope["businesses"]:
            raise DomainError("forbidden", "Project is outside your action scope")
        binding = ctx.principal_id + ":" + scope["kind"] + ":" + ",".join(sorted(scope["businesses"]))
        binding, after = page_position(args, binding, operation)
        limit = args.get("limit", 50)
        found = self.query_page(uow, scope, args, after, limit)
        selected = [row for row, _ in found[:limit]]
        cursor = (base64.urlsafe_b64encode(canonical_bytes({"binding": binding, "after": found[limit - 1][1]})).decode("ascii")
                  if len(found) > limit else None)
        keys = [row["action_key"] for row in selected]
        all_links = {key: [] for key in keys}
        if keys:
            marks = ",".join("?" for _ in keys)
            for row in uow.connection.execute(f"SELECT action_key, business_id FROM action_businesses WHERE action_key IN ({marks}) "
                                              "ORDER BY business_id", keys):
                all_links[row[0]].append(row[1])
        people = self.principals(uow, [value for row in selected for value in (row["performed_by"], row["recorded_by"])])
        names = self.businesses(uow, [business for row_links in all_links.values() for business in row_links])
        return {"actions": [self.view(ctx, uow, row, scope, row_links=all_links[row["action_key"]], people=people, names=names)
                            for row in selected], "next_cursor": cursor, **self.time_view()}

    def config_view(self, ctx, uow, scope):
        directory = self.businesses(uow, [business for business in scope["businesses"] if business in active_business_ids(uow.connection)])
        if scope["kind"] == "business":
            allowed, default, required = ["business-operational"], "business-operational", False
            defaults = sorted(scope["businesses"])
        elif scope["kind"] == "ecosystem":
            allowed, default, required, defaults = list(models.GLOBAL_AGENT_CATEGORIES), None, True, []
        else:
            allowed, default, required, defaults = [*CATEGORIES, None], None, False, []
        return {"categories": [{"id": category, "label": category} for category in CATEGORIES],
                "unclassified": "Unclassified (no category) is available for owner capture only",
                "agent_word_limit": models.AGENT_WORD_LIMIT, "text_max_characters": models.TEXT_LIMIT,
                "real_time": {"formats": ["YYYY-MM-DD", "YYYY-MM-DDTHH"], "zone": local_time.TIMEZONE,
                              "omitted": "now (server clock)", "future": "rejected"},
                "sharing": models.SHARING, "tags": {"source": "catalog", "operation": "tags_list"},
                "scope": {"kind": scope["kind"], "allowed_categories": allowed, "default_category": default,
                          "category_required": required, "default_business_ids": defaults,
                          "businesses": sorted(directory.values(), key=lambda item: (item["name"] or "", item["id"])),
                          "can_change_own": True, "owner": scope["kind"] == "owner" and is_owner(ctx, uow)},
                **self.time_view()}




__all__ = ["ACTION_ENTRIES", "ActionOperations", "action_namespace"]
