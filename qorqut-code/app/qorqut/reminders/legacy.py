"""The retired owner-reminder tool names, kept as thin wrappers over the shared reminder service.

``owner_reminder`` (schedule/list/update/cancel) and ``owner_reminders_list`` keep their arguments and plain results, but the
target is always the installation owner and every mutation now needs the owner or the configured Ordubashy: another agent can
no longer use them to remind the owner. Every call is also authorized as the matching shared ``reminders_*`` operation (a deny
there is not bypassed here), including when a saved result is replayed. Reminders are stable rows now, so an update keeps its
reminder_id. Optional delivery adapters own admission of historical queued transport rows."""

from qorqut_agentic.behavior.tool_help import text as instruction_text
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, page_position, schema
from qorqut_operations.contracts import DomainError, plain
from qorqut.management.identity import installation_owner, is_active_installation_owner

from . import service as shared
from .views import legacy

NAME = "owner_reminder"
# The narrow owner/Ordubashy read of the owner's reminders in one scope, for the Project reader. It adds no write.
READ = "owner_reminders_list"
DEFAULT_PAGE, MAX_PAGE = 25, 100
FIELDS = {"schedule": {"action", "text", "at"}, "list": {"action"}, "update": {"action", "reminder_id", "revision", "text", "at"},
          "cancel": {"action", "reminder_id", "revision"}}
SHAPE = ("reminder_id", "revision", "text", "at", "state")
# The shared operation each legacy action is authorized as.
SHARED = {"schedule": shared.CREATE, "list": shared.LIST, "update": shared.UPDATE, "cancel": shared.CANCEL}


class LegacyReminders:
    def __init__(self, service):
        self.service, self.runtime, self.app = service, service.runtime, service.app

    def register(self):
        self.app.registry.register(OperationDescriptor(NAME, schema({
            "action": {"enum": ["schedule", "list", "update", "cancel"]},
            "text": {"type": "string", "minLength": 1, "maxLength": 240},
            "at": {"type": "string", "format": "date-time", "description": instruction_text('legacy-when-to-remind-the-owner-include-a-timezone')},
            "reminder_id": {"type": "string", "format": "uuid"},
            "revision": {"type": "integer", "minimum": 1},
        }, ("action",)), supports_signoff=False, exposure=frozenset({"mcp", "dashboard"}),
            scope_kinds=frozenset({"business", "global"}),
            resolve_namespace=lambda ctx, args: self.service.namespace(ctx), global_capability="read_global_context",
            description=instruction_text('legacy-short-alias-for-the-owner-s-own-one'),
            prepare=self.prepare, apply=self.apply, replay_validator=self.replay))
        self.app.registry.register(OperationDescriptor(READ, schema({
            "scope": {"enum": ["business", "global"]},
            "reminder_id": {"type": "string", "format": "uuid"},
            "cursor": {"type": "string", "maxLength": 2000},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE}}),
            effect_kind="read", supports_signoff=False, exposure=frozenset({"dashboard", "worker"}),
            scope_kinds=frozenset({"business", "global"}), global_capability="read_global_context",
            resolve_namespace=self.read_namespace,
            description=instruction_text('legacy-owner-and-configured-ordubashy-read-of-the-owner'),
            read=self.read))
        return self

    # ----- owner_reminder -----------------------------------------------------------------------------------------

    def manager(self, ctx, uow, operation, *, read=False):
        """The owner and the configured Ordubashy manage owner reminders; an active global worker may also read them."""
        access = self.service.caller(ctx, uow)
        if not access.manager:
            from qorqut.agents.worker_access import is_worker
            if not (read and access.kind == "agent" and is_worker(uow.connection, access.ctx.principal_id)):
                raise DomainError("forbidden", "Only the owner and the configured Ordubashy may manage owner reminders"
                                  + ("; an active global worker may read them" if read else ""))
        # The wrapper never sidesteps the shared operation's own policy decision, an explicit deny included.
        self.service.authorize_operation(access.ctx, operation, uow)
        owner = installation_owner(uow)
        if owner is None:
            raise DomainError("operation_unavailable", "No installation owner is registered", retryable=True)
        return access, owner

    def prepare(self, ctx, args, options, uow):
        access, owner = self.manager(ctx, uow, SHARED[args["action"]])
        self.run(access, owner, args, uow, apply=False)
        return PlanBuilder(access.ctx, NAME, args, "Manage owner reminder", namespace=self.service.namespace(access.ctx)).build()

    def apply(self, ctx, plan, uow):
        arguments = plain(plan.arguments)
        access, owner = self.manager(ctx, uow, SHARED[arguments["action"]])
        return self.run(access, owner, arguments, uow, apply=True)

    def replay(self, ctx, args, replay, uow):
        """Before a saved result is returned again: the caller must still be the owner or configured Ordubashy, still pass the
        shared operation's policy, and the saved reminder must still be one of the owner's."""
        args = plain(args)
        access, owner = self.manager(ctx, uow, SHARED[args["action"]])
        reminder_id = args.get("reminder_id") or plain(replay.data or {}).get("reminder_id")
        row = self.service.load(uow, reminder_id) if reminder_id else None
        if reminder_id and (row is None or row["recipient_id"] != owner or not self.service.reachable(access, row, uow)):
            raise DomainError("not_found", "No such reminder")

    def run(self, access, owner, args, uow, *, apply):
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
        access, owner = self.manager(ctx, uow, shared.LIST, read=True)
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
