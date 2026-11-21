"""Effective project access of the supported global agents.

An active global is an active agent principal with an active profile row and active
stored profile, an ecosystem scope and one of
the supported global roles; the shared Operations authority
(``qorqut_operations.global_agents``) decides it for the engine and the host alike,
and malformed profiles or wrong field types fail closed. Its project access is
every active project, derived in the reading transaction: a new project is
reachable at once and an archived one never is. The installation owner and system workers reach every active
project; role, plugin, capability and revision checks still
apply to every operation.
"""

from __future__ import annotations

from qorqut_operations.project_lifecycle import active_project_ids, owner_may_act
from qorqut_operations.contracts import DomainError
from qorqut_operations.global_agents import current_global_row


def global_profile_data(row) -> dict | None:
    """The stored profile of an active supported global, or None for anything else.

    ``row`` is an ``agent_profiles`` row joined with its principal's ``kind`` and
    ``active`` columns, and a malformed stored profile fails closed. The shared Operations validator decides.
    """
    return current_global_row(row)


def active_global(connection, agent_id: str | None) -> dict | None:
    """The stored profile data of `agent_id` when it is an active supported global."""
    if not agent_id:
        return None
    row = connection.execute(
        "SELECT a.agent_id, a.status, a.profile_json, p.kind, p.active "
        "FROM agent_profiles a JOIN principals p ON p.id=a.agent_id WHERE a.agent_id=?", (agent_id,)).fetchone()
    return global_profile_data(row)


def active_role_holders(connection, role: str) -> list[str]:
    """Every active supported global holding ``role`` right now, in a stable order."""
    return [row["agent_id"] for row in connection.execute(
        "SELECT a.agent_id, a.status, a.profile_json, p.kind, p.active FROM agent_profiles a "
        "JOIN principals p ON p.id=a.agent_id WHERE a.status='active' ORDER BY a.agent_id")
        if (data := global_profile_data(row)) is not None and data["role"]["id"] == role]


def active_role_count(connection, role: str) -> int:
    """How many active supported globals hold ``role`` right now."""
    return len(active_role_holders(connection, role))


def active_role_holder(connection, role: str) -> str | None:
    """The one active supported global holding ``role``, or None when none is registered.

    The roster holds at most one active registration per role, so a second holder is a configuration error, never a
    choice; nothing is provisioned and no other role stands in."""
    holders = [row["agent_id"] for row in connection.execute(
        "SELECT a.agent_id, a.status, a.profile_json, p.kind, p.active FROM agent_profiles a "
        "JOIN principals p ON p.id=a.agent_id WHERE a.status='active' ORDER BY a.agent_id")
        if (data := global_profile_data(row)) is not None and data["role"]["id"] == role]
    if len(holders) > 1:
        raise DomainError("invalid_configuration", f"More than one active {role} agent is registered")
    return holders[0] if holders else None


def effective_project_ids(connection, agent_id: str | None) -> frozenset[str]:
    """Every active project for an active supported global; nothing for any other identity."""
    return active_project_ids(connection) if active_global(connection, agent_id) is not None else frozenset()


def reaches_project(connection, agent_id: str | None, project_id: str | None) -> bool:
    """True when an active supported global can reach this active project right now."""
    return bool(project_id) and project_id in effective_project_ids(connection, agent_id)


def active_project_participant(connection, project_id: str, principal_id: str | None) -> bool:
    """An eligible current participant in an active project's work.

    The installation owner and active system workers qualify for every active project; agents
    qualify only as active supported globals.
    """
    if not principal_id or not project_id:
        return False
    principal = connection.execute("SELECT kind, active FROM principals WHERE id=?", (principal_id,)).fetchone()
    if principal is None:
        return False
    if principal["kind"] == "agent":
        return reaches_project(connection, principal_id, project_id)
    if principal["kind"] == "human":
        return owner_may_act(connection, principal_id, project_id)
    if principal["kind"] == "system":
        return bool(principal["active"]) and project_id in active_project_ids(connection)
    return False
