"""Owner comments can join business task and Story discussions.

Other shared discussion writes still require the private owner channel.
Formal decisions retain their separate authorization and decision paths.
"""

from qorqut_operations.contracts import DomainError

CHANNEL = "owner_ordubashy"
PRIVATE_OPERATION = "owner_conversation_send"
MESSAGE = ("Owner comments belong to the private conversation with Ordubashy; "
           "use owner_conversation_send")


def installation_owner_id(uow):
    """The registered installation owner principal, or None before registration."""
    row = uow.repository("installation").get("service")
    return row["owner_principal_id"] if row else None


def is_installation_owner(ctx, uow):
    """True only for the human principal registered as the installation owner.

    Agents, system/worker principals, and humans who are merely business owners
    or teammates keep their existing team writes.
    """
    if ctx.principal_kind != "human":
        return False
    owner = installation_owner_id(uow)
    return owner is not None and owner == ctx.principal_id


def channel_required(operation=None):
    """The contract's shared-channel refusal for an installation-owner comment.

    `operation` names the refused team write, so a client can keep the right
    draft while switching channels; `operation` in the payload stays the private
    operation to use instead.
    """
    details = {"channel": CHANNEL, "operation": PRIVATE_OPERATION}
    if operation:
        details["refused_operation"] = operation
    return DomainError("channel_required", MESSAGE, details=details)


def reject_owner_team_write(ctx, uow, operation):
    """Refuse `operation`'s shared-channel write when the actor is the owner."""
    if ctx.business_id and operation in {'tasks_comment', 'stories_note_add', 'focus_reply'}:
        return  # Business task/story group discussion is also open to the owner.
    if is_installation_owner(ctx, uow):
        raise channel_required(operation)
