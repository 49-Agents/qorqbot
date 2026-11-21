"""Derived owner-task following: no subscription writer, backfill or scheduler."""

from qorqut.agents.global_scope import active_role_holder
from qorqut.agents.roles import ORDUBASHY_ROLE


def ordubashies(uow):
    """The current Ordubashy follower as a set: empty when none is registered; a second holder is a configuration error."""
    holder = active_role_holder(uow.connection, ORDUBASHY_ROLE)
    return {holder} if holder else set()


ALL_SCOPES = object()


def tasks(uow, *, project=ALL_SCOPES, identity=None):
    # The installation identity is authoritative, not an assignee display name
    # or every project member marked owner. Match final committed assignment.
    where, args = [], []
    if project is not ALL_SCOPES:
        where.append('t.project_id IS ?')
        args.append(project)
    if identity is not None:
        where.append('t.id=?')
        args.append(identity)
    return uow.connection.execute(
        "SELECT DISTINCT t.* FROM installation i "
        "JOIN principals p ON p.id=i.owner_principal_id AND p.kind='human' AND p.active=1 "
        "JOIN relationships r ON r.to_id=p.id AND r.to_type='person' "
        "AND r.from_type='task' AND r.role='assigned_to' "
        "JOIN tasks t ON t.id=r.from_id AND t.project_id IS r.project_id "
        "WHERE i.id='service' AND (t.project_id IS NULL OR EXISTS (SELECT 1 FROM projects pr WHERE pr.id=t.project_id AND pr.archived_at IS NULL))" + (' AND ' + ' AND '.join(where) if where else '') +
        ' ORDER BY t.project_id,t.created_at,t.id', args)
