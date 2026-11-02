"""The retired owner-reminder tool names, kept as thin wrappers over the shared reminder service.

``owner_reminder`` (schedule/list/update/cancel) and ``owner_reminders_list`` keep their arguments and plain results, but the
target is always the installation owner and every mutation now needs the owner or the configured Ordubashy: another agent can
no longer use them to remind the owner. Reminders are stable rows now, so an update keeps its reminder_id. Nothing here
touches the Telegram outbox except admitting a queued legacy row, which the shared service has already taken over."""
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, page_position, schema
from qorqut_operations.contracts import DomainError, plain
from bazarlyk.management.identity import installation_owner, is_active_installation_owner

from .views import legacy

NAME = "owner_reminder"
# The narrow owner/Ordubashy read of the owner's reminders in one scope, for the Project reader. It adds no write.
READ = "owner_reminders_list"
DEFAULT_PAGE, MAX_PAGE = 25, 100
FIELDS = {"schedule": {"action", "text", "at"}, "list": {"action"}, "update": {"action", "reminder_id", "revision", "text", "at"},
          "cancel": {"action", "reminder_id", "revision"}}
SHAPE = ("reminder_id", "revision", "text", "at", "state")


class LegacyReminders:
    def __init__(self, service):
        self.service, self.runtime, self.app = service, service.runtime, service.app

    def register(self):
        self.app.registry.register(OperationDescriptor(NAME, schema({
            "action": {"enum": ["schedule", "list", "update", "cancel"]},
            "text": {"type": "string", "minLength": 1, "maxLength": 240},
            "at": {"type": "string", "format": "date-time", "description": "When to remind the owner; include a timezone offset."},
            "reminder_id": {"type": "string", "format": "uuid"},
            "revision": {"type": "integer", "minimum": 1},
        }, ("action",)), supports_signoff=False, exposure=frozenset({"mcp", "dashboard"}),
            scope_kinds=frozenset({"business", "global"}),
            resolve_namespace=lambda ctx, args: self.service.namespace(ctx), global_capability="read_global_context",
            description="Short alias for the owner's own one-off reminders; prefer the reminders_* tools, which also support "
                        "recurrence and other recipients. When the owner asks to be reminded, schedule one concise reminder with text and "
                        "at; list, update or cancel with reminder_id and revision. Only the owner and Ordubashy may use it.",
            prepare=self.prepare, apply=self.apply))
        self.app.registry.register(OperationDescriptor(READ, schema({
            "scope": {"enum": ["business", "global"]},
            "reminder_id": {"type": "string", "format": "uuid"},
            "cursor": {"type": "string", "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE}}),
            effect_kind="read", supports_signoff=False, exposure=frozenset({"dashboard", "worker"}),
            scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
            resolve_namespace=self.read_namespace,
            description="Owner and configured Ordubashy read of the owner's reminders in the selected project (or Global from the "
                        "root route). Safe stored fields only; no delivery, update or cancel.",
            read=self.read))
        self.runtime.telegram.admit_hooks[NAME] = self.admit
        return self

    # ----- owner_reminder -----------------------------------------------------------------------------------------

    def manager(self, ctx, uow):
        access = self.service.caller(ctx, uow)
        if not access.manager:
            raise DomainError("forbidden", "Only the owner and the configured Ordubashy may manage owner reminders")
        owner = installation_owner(uow)
        if owner is None:
            raise DomainError("operation_unavailable", "No installation owner is registered", retryable=True)
        return access, owner

    def prepare(self, ctx, args, options, uow):
        access, owner = self.manager(ctx, uow)
        self.run(access, owner, args, uow, apply=False)
        return PlanBuilder(access.ctx, NAME, args, "Manage owner reminder", namespace=self.service.namespace(access.ctx)).build()

    def apply(self, ctx, plan, uow):
        access, owner = self.manager(ctx, uow)
        return self.run(access, owner, plain(plan.arguments), uow, apply=True)

    def run(self, access, owner, args, uow, *, apply):
        from . import service as shared
        action = args["action"]
        if set(args) != FIELDS[action]:
            raise DomainError("validation_error", "Required fields for " + action + ": " + ", ".join(sorted(FIELDS[action])))
        if action == "list":
            return self.listing(owner, uow) if apply else None
        if action == "schedule":
            result = self.service.mutate(shared.CREATE, access, {"text": args["text"], "at": args["at"], "recipient_id": owner}, uow, apply=apply)
        else:
            row = self.service.load(uow, args["reminder_id"])
            if row is None or row["recipient_id"] != owner:
                raise DomainError("not_found", "No such reminder")
            if action == "cancel":
                result = self.service.mutate(shared.CANCEL, access, {"reminder_id": row["id"], "revision": args["revision"]}, uow, apply=apply)
            else:
                result = self.service.mutate(shared.UPDATE, access, {"reminder_id": row["id"], "revision": args["revision"],
                                                                     "text": args["text"], "at": args["at"]}, uow, apply=apply)
        return {key: result["reminder"][key] for key in SHAPE} if apply else None

    def listing(self, owner, uow):
        """Active reminders first, then the most recent finished ones; no destination metadata."""
        rows = uow.connection.execute(
            "SELECT * FROM reminders WHERE recipient_id=? ORDER BY CASE WHEN state IN ('scheduled','due') THEN 0 ELSE 1 END, "
            "COALESCE(CASE WHEN state='due' THEN due_at END, next_at, due_at, created_at) , id LIMIT 100", (owner,)).fetchall()
        return {"reminders": [legacy(row) for row in rows]}

    # ----- owner_reminders_list -------------------------------------------------------------------------------------

    @staticmethod
    def read_namespace(ctx, args):
        """The one reminder namespace of this read: the selected project, or Global exactly."""
        if args.get("scope") == "global" or ctx.business_id is None:
            return "global"
        return "business:" + ctx.business_id

    def read(self, ctx, args, options, uow):
        """Bounded, stable read of the owner's reminders in exactly one scope; never touches delivery state."""
        access, owner = self.manager(ctx, uow)
        ctx = access.ctx
        namespace = self.read_namespace(ctx, args)
        if args.get("scope") == "global" and ctx.business_id is not None:
            raise DomainError("validation_error", "Global reminders are read from the root route")
        if args.get("scope") == "business" and ctx.business_id is None:
            raise DomainError("scope_required", "Business reminders require a selected project")
        binding, after = page_position(args, namespace, READ)
        where, values = "recipient_id=? AND business_id IS ?", [owner, ctx.business_id if namespace != "global" else None]
        if args.get("reminder_id"):
            where += " AND id=?"
            values.append(args["reminder_id"])
        items, cursor = self.service.scan(uow, where, values, "created_at", True, binding, after, args.get("limit", DEFAULT_PAGE),
                                          lambda row: True)
        return {"reminders": [legacy(row) for row in items], "next_cursor": cursor}

    # ----- queued legacy Telegram rows --------------------------------------------------------------------------------

    def admit(self, uow, row):
        """Admission of a still-queued legacy ``owner_reminder`` outbox row.

        A row the offline transform imported is delivered by the shared service and is cancelled here, so one reminder can
        never send twice. A row that was never imported keeps its original rule: recheck the creator's current permission."""
        if self.service.load(uow, row["id"]) is not None:
            return "cancelled", "reminder_migrated", "Reminder is delivered by the shared reminder service"
        namespace = row["source_key"].split("/", 1)[0]
        business = namespace.removeprefix("business:") if namespace != "global" else None
        try:
            ctx = self.app.policy.context_for(row["agent_id"], business_id=business, review_business_id=None, uow=uow)
            ctx = self.app.current_context(ctx, uow)
            owner = is_active_installation_owner(uow, ctx.principal_id, ctx.principal_kind)
            if not owner and (ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None):
                raise DomainError("forbidden", "Only the owner and authorized agents may manage owner reminders")
            decision = self.app.authorize(ctx, self.app.registry.get(NAME), [], uow, namespace=namespace)
            if decision.mode != "allow":
                raise DomainError("forbidden", "Reminder permission was revoked")
        except DomainError:
            return "cancelled", "permission_revoked", "Reminder creator is no longer authorized"
        return None
