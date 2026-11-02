from qorqut_operations.contracts import DomainError
from bazarlyk.management.identity import is_active_installation_owner
from bazarlyk.reminders.legacy import NAME

def legacy_admit(self, uow, row):
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
