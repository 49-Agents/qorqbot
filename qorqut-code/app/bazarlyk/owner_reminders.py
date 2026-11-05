"""Owner reminders: a transport-neutral tool over the existing durable timed outbox."""
from datetime import datetime, timezone

import base64

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, canonical_bytes, new_id, plain
from qorqut_operations.application.plans import PlanBuilder, page_position, schema
from bazarlyk.management.identity import configured_ordubashy
from bazarlyk.telegram import store
from bazarlyk.telegram.outbox import enqueue_text

NAME = "owner_reminder"
# The narrow owner/Ordubashy read of the reminders already stored for one scope. It adds no write, no delivery and
# no second reminder store: it projects the existing outbox rows' safe view only.
READ = "owner_reminders_list"
DEFAULT_PAGE, MAX_PAGE = 25, 100


class OwnerReminders:
    def __init__(self, runtime):
        self.app, self.bridge = runtime.application, runtime.telegram

    def register(self):
        self.app.registry.register(OperationDescriptor(NAME, schema({
            "action": {"enum": ["schedule", "list", "update", "cancel"]},
            "text": {"type": "string", "minLength": 1, "maxLength": 240},
            "at": {"type": "string", "format": "date-time", "description": "When to remind the owner; include a timezone offset."},
            "reminder_id": {"type": "string", "format": "uuid"},
            "revision": {"type": "integer", "minimum": 1},
        }, ("action",)), supports_signoff=False, exposure=frozenset({"mcp", "dashboard"}),
            scope_kinds=frozenset({"business", "global"}),
            resolve_namespace=lambda ctx, args: self.namespace(ctx), global_capability="read_global_context",
            description="When the owner asks to be reminded, schedule one concise reminder with text and at. "
                        "Delivery runs automatically even when you are offline. List your reminders, or update/cancel "
                        "a queued reminder using its reminder_id and revision. No delivery setup is needed.",
            prepare=self.prepare, apply=self.apply))
        self.app.registry.register(OperationDescriptor(READ, schema({
            "scope": {"enum": ["business", "global"]},
            "reminder_id": {"type": "string", "format": "uuid"},
            "cursor": {"type": "string", "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE}}),
            effect_kind="read", supports_signoff=False, exposure=frozenset({"dashboard", "worker"}),
            scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
            resolve_namespace=self.read_namespace,
            description="Owner and configured Ordubashy read of the reminders already scheduled for the selected project "
                        "(or Global from the root route). Safe stored fields only; no delivery, update or cancel.",
            read=self.list_reminders))
        self.bridge.admit_hooks[NAME] = self.admit
        return self

    def caller(self, ctx, uow):
        ctx = self.app.current_context(ctx, uow)
        installation = uow.repository("installation").get("service")
        owner = ctx.principal_kind == "human" and installation["owner_principal_id"] == ctx.principal_id
        if not owner and (ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None):
            raise DomainError("forbidden", "Only the owner and authorized agents may manage owner reminders")
        return ctx, owner

    @staticmethod
    def namespace(ctx):
        return "business:" + ctx.business_id if ctx.business_id else "global"

    @staticmethod
    def read_namespace(ctx, args):
        """The one reminder namespace of this read: the selected project, or Global exactly."""
        if args.get("scope") == "global":
            return "global"
        if ctx.business_id is None:
            return "global"
        return "business:" + ctx.business_id

    def read_caller(self, ctx, uow):
        """The owner, or the one configured active Ordubashy: the Project reminder read has no wider audience."""
        ctx, owner = self.caller(ctx, uow)
        if not owner and ctx.principal_id != configured_ordubashy(uow, require_codex=False):
            raise DomainError("forbidden", "Project reminders belong to the installation owner and the configured Ordubashy")
        return ctx, owner

    def list_reminders(self, ctx, args, options, uow):
        """Bounded, stable read of the stored reminders of exactly one scope; never touches delivery state."""
        ctx, owner = self.read_caller(ctx, uow)
        namespace = self.read_namespace(ctx, args)
        if args.get("scope") == "global" and ctx.business_id is not None:
            raise DomainError("validation_error", "Global reminders are read from the root route")
        if args.get("scope") == "business" and ctx.business_id is None:
            raise DomainError("scope_required", "Business reminders require a selected project")
        binding, after = page_position(args, namespace, READ)
        if after is not None and (len(after) != 2 or any(not isinstance(value, str) for value in after)):
            raise DomainError("validation_error", "Invalid or differently scoped reminder cursor")
        clause, parameters = "source_kind=? AND source_key LIKE ?", [NAME, namespace + "/%"]
        if args.get("reminder_id"):
            clause += " AND id=?"
            parameters.append(args["reminder_id"])
        if not owner:
            clause += " AND agent_id=?"
            parameters.append(ctx.principal_id)
        if after is not None:
            clause += " AND (created_at,id) < (?,?)"
            parameters.extend(after)
        limit = args.get("limit", DEFAULT_PAGE)
        rows = uow.connection.execute(
            "SELECT id,created_at FROM telegram_outbox WHERE " + clause + " ORDER BY created_at DESC,id DESC LIMIT ?",
            [*parameters, limit + 1]).fetchall()
        items = [self.view(store.outbox(uow, row["id"])) for row in rows[:limit]]
        cursor = (base64.urlsafe_b64encode(canonical_bytes(
            {"binding": binding, "after": [rows[limit - 1]["created_at"], rows[limit - 1]["id"]]})).decode("ascii")
            if len(rows) > limit else None)
        return {"reminders": items, "next_cursor": cursor}

    def row(self, ctx, owner, args, uow):
        row = store.outbox(uow, args["reminder_id"])
        if row is None or row["source_kind"] != NAME or (not owner and (
                row["agent_id"] != ctx.principal_id or row["source_key"].split("/", 1)[0] != self.namespace(ctx))):
            raise DomainError("not_found", "No such reminder")
        if row["revision"] != args["revision"]:
            raise DomainError("revision_conflict", "Reminder changed; list reminders and use the current revision")
        if row["state"] != "queued":
            raise DomainError("invalid_transition", "Only a reminder still waiting for delivery can be changed")
        return row

    def validate(self, ctx, args, uow):
        ctx, owner = self.caller(ctx, uow)
        action = args["action"]
        fields = {"schedule": {"action", "text", "at"}, "list": {"action"},
                  "update": {"action", "reminder_id", "revision", "text", "at"},
                  "cancel": {"action", "reminder_id", "revision"}}[action]
        if set(args) != fields:
            raise DomainError("validation_error", "Required fields for " + action + ": " + ", ".join(sorted(fields)))
        row, at = None, None
        if action in {"update", "cancel"}:
            row = self.row(ctx, owner, args, uow)
        if action in {"schedule", "update"}:
            if not args["text"].strip():
                raise DomainError("validation_error", "Reminder text cannot be blank")
            try:
                when = datetime.fromisoformat(args["at"].replace("Z", "+00:00"))
                if when.tzinfo is None or when.utcoffset() is None:
                    raise ValueError()
                when = when.astimezone(timezone.utc)
            except (ValueError, OverflowError):
                raise DomainError("validation_error", "Reminder time must include a timezone offset") from None
            if when <= self.bridge.clock():
                raise DomainError("validation_error", "Choose a reminder time in the future")
            at = when.isoformat(timespec="microseconds").replace("+00:00", "Z")
            try:
                self.bridge.current_config()
                self.bridge.check_binding(uow)
            except DomainError:
                raise DomainError("operation_unavailable", "Reminder delivery is unavailable; no reminder was scheduled", retryable=True) from None
        return ctx, owner, row, at

    def prepare(self, ctx, args, options, uow):
        ctx, *_ = self.validate(ctx, args, uow)
        return PlanBuilder(ctx, NAME, args, "Manage owner reminder", namespace=self.namespace(ctx)).build()

    @staticmethod
    def view(row):
        return {"reminder_id": row["id"], "revision": row["revision"], "text": row["payload_json"]["text"],
                "at": row["part"], "state": "scheduled" if row["state"] == "queued" else row["state"]}

    def apply(self, ctx, plan, uow):
        args = plain(plan.arguments)
        ctx, owner, old, at = self.validate(ctx, args, uow)
        if args["action"] == "list":
            clause, params = ("", []) if owner else (" AND agent_id=? AND source_key LIKE ?", [ctx.principal_id, self.namespace(ctx) + "/%"])
            # Active reminders first, then recent receipts. No private destination metadata is returned.
            rows = uow.connection.execute("SELECT id FROM telegram_outbox WHERE source_kind=?" + clause +
                " ORDER BY CASE WHEN state='queued' THEN 0 ELSE 1 END,next_attempt_at,created_at DESC LIMIT 100", [NAME, *params])
            return {"reminders": [self.view(store.outbox(uow, r["id"])) for r in rows]}
        if old:
            store.set_outbox(uow, old, state="cancelled", updated_at=self.bridge.now(), error_code="owner_cancelled",
                                error_message="Reminder cancelled or replaced")
            if args["action"] == "cancel":
                return self.view(store.outbox(uow, old["id"]))
        _, row, _ = enqueue_text(self.bridge, uow, self.bridge.config.chat("urgent"), text=args["text"].strip(),
            purpose="notice", source_kind=NAME, source_key=self.namespace(ctx) + "/" + new_id(), part=at,
            agent_id=ctx.principal_id, next_attempt_at=at)
        self.bridge.wake()
        return self.view(row)

    def admit(self, uow, row):
        """Recheck the creator's current permission immediately before the scheduled send."""
        namespace = row["source_key"].split("/", 1)[0]
        business = namespace.removeprefix("business:") if namespace != "global" else None
        try:
            ctx = self.app.policy.context_for(row["agent_id"], business_id=business, review_business_id=None, uow=uow)
            self.caller(ctx, uow)
            decision = self.app.authorize(ctx, self.app.registry.get(NAME), [], uow, namespace=namespace)
            if decision.mode != "allow":
                raise DomainError("forbidden", "Reminder permission was revoked")
        except DomainError:
            return "cancelled", "permission_revoked", "Reminder creator is no longer authorized"
        return None
