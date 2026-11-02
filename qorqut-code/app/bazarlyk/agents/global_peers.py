"""Explicit global peer authority, independent of private account-mail authority."""
import json
from qorqut_operations.contracts import DomainError
from .global_scope import active_global
from .roles import GLOBAL_MARKETING_ROLE

CAPABILITY = "coordinate_businesses"
PEER_ROLES = frozenset({"ordubashy", "global-marketing"})


def marketing_peer(uow, identity):
    """True for the current supported Global Marketing holding the peer capability."""
    profile = active_global(uow.connection, identity)
    return (profile is not None and profile['role']['id'] == GLOBAL_MARKETING_ROLE
            and CAPABILITY in profile['scope'].get('global_capabilities', []))


def marketing_role(uow, identity):
    """True for any agent holding the Global Marketing role, whether or not its peer authority is still current."""
    row = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (identity,)).fetchone()
    return bool(row) and json.loads(row['profile_json'])['role']['id'] == 'global-marketing'


def global_peer(uow, identity):
    from bazarlyk.management.identity import configured_ordubashy
    return identity == configured_ordubashy(uow, require_codex=False) or marketing_peer(uow, identity)


def protect_peer_control(ctx, target, uow):
    """Ordubashy and Global Marketing never administer each other; that belongs to the owner.

    Messages remain ordinary coordination, and control between agents of the same role is unchanged.
    """
    if ctx.principal_kind != 'agent' or ctx.principal_id == target:
        return
    rows = uow.connection.execute("SELECT agent_id,profile_json FROM agent_profiles WHERE agent_id IN (?,?)", (ctx.principal_id, target)).fetchall()
    roles = {r['agent_id']: json.loads(r['profile_json'])['role']['id'] for r in rows}
    actor, other = roles.get(ctx.principal_id), roles.get(target)
    if actor in PEER_ROLES and other in PEER_ROLES and actor != other:
        raise DomainError('forbidden', 'Global peers cannot administer each other; the owner controls peer sessions and profiles')


# Explicit release-plan selections only; this does not edit any live profile.
LEGACY_MARKETING_DENIALS = frozenset({
    "agents_activity_list", "business_email_conversation_get", "business_email_conversations_list",
    "broadcast_recipients", "broadcasts_cancel", "broadcasts_create", "broadcasts_get",
    "broadcasts_list", "broadcasts_pause", "broadcasts_resume", "broadcasts_send_now", "broadcasts_update",
})
