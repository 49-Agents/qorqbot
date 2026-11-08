"""Derived owner-task following: no subscription writer, backfill or scheduler."""

from qorqut.agents.global_scope import active_role_holder
from qorqut.agents.roles import ORDUBASHY_ROLE


def ordubashies(uow):
    """The current Ordubashy follower as a set: empty when none is registered; a second holder is a configuration error."""
    holder = active_role_holder(uow.connection, ORDUBASHY_ROLE)
    return {holder} if holder else set()


ALL_SCOPES = object()


def tasks(uow, *, business=ALL_SCOPES, identity=None):
    # The installation identity is authoritative, not an assignee display name
    # or every business member marked owner. Match final committed assignment.
    # The membership test belongs to that HUMAN owner, not to any agent.
    where, args = [], []
    if business is not ALL_SCOPES:
        where.append('t.business_id IS ?')
        args.append(business)
    if identity is not None:
        where.append('t.id=?')
        args.append(identity)
    return uow.connection.execute(
        "SELECT DISTINCT t.* FROM installation i "
        "JOIN principals p ON p.id=i.owner_principal_id AND p.kind='human' AND p.active=1 "
        "JOIN relationships r ON r.to_id=p.id AND r.to_type='person' "
        "AND r.from_type='task' AND r.role='assigned_to' "
        "JOIN tasks t ON t.id=r.from_id AND t.business_id IS r.business_id "
        "WHERE i.id='service' AND (t.business_id IS NULL OR EXISTS (SELECT 1 FROM business_memberships m WHERE m.principal_id=p.id AND m.business_id=t.business_id AND m.active=1))" + (' AND ' + ' AND '.join(where) if where else '') +
        ' ORDER BY t.business_id,t.created_at,t.id', args)
