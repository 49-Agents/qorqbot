"""Effective project access of the supported global agents.

An active global is an active agent principal with an active profile row and active
stored profile, no project home, an ecosystem scope without a project and one of
the supported global roles; the shared Operations authority
(``qorqut_operations.global_agents``) decides it for the engine and the host alike,
and malformed profiles or wrong field types fail closed. Its project access is
every active project, derived in the reading transaction: a new project is
reachable at once and an archived one never is. A saved ``allowed_project_ids``
list is not authority and agent membership rows are not consulted. Human and system
memberships keep their meaning; role, plugin, capability and revision checks still
apply to every operation.
"""

from __future__ import annotations

from qorqut_operations.project_lifecycle import active_project_ids
from qorqut_operations.contracts import DomainError, freeze, plain
from qorqut_operations.global_agents import current_global_row

from .models import AgentProfile


def global_profile_data(row) -> dict | None:
    """The stored profile of an active supported global, or None for anything else.

    ``row`` is an ``agent_profiles`` row joined with its principal's ``kind`` and
    ``active`` columns. A project home disqualifies the row whatever its status, and
    a malformed stored profile fails closed. The shared Operations validator decides.
    """
    return current_global_row(row)


def active_global(connection, agent_id: str | None) -> dict | None:
    """The stored profile data of `agent_id` when it is an active supported global."""
    if not agent_id:
        return None
    row = connection.execute(
        "SELECT a.agent_id, a.home_project_id, a.status, a.profile_json, p.kind, p.active "
        "FROM agent_profiles a JOIN principals p ON p.id=a.agent_id WHERE a.agent_id=?", (agent_id,)).fetchone()
    return global_profile_data(row)


def active_role_holders(connection, role: str) -> list[str]:
    """Every active supported global holding ``role`` right now, in a stable order."""
    return [row["agent_id"] for row in connection.execute(
        "SELECT a.agent_id, a.home_project_id, a.status, a.profile_json, p.kind, p.active FROM agent_profiles a "
        "JOIN principals p ON p.id=a.agent_id WHERE a.status='active' AND a.home_project_id IS NULL ORDER BY a.agent_id")
        if (data := global_profile_data(row)) is not None and data["role"]["id"] == role]


def active_role_count(connection, role: str) -> int:
    """How many active supported globals hold ``role`` right now."""
    return len(active_role_holders(connection, role))


def active_role_holder(connection, role: str) -> str | None:
    """The one active supported global holding ``role``, or None when none is registered.

    The roster holds at most one active registration per role, so a second holder is a configuration error, never a
    choice; nothing is provisioned and no other role stands in."""
    holders = [row["agent_id"] for row in connection.execute(
        "SELECT a.agent_id, a.home_project_id, a.status, a.profile_json, p.kind, p.active FROM agent_profiles a "
        "JOIN principals p ON p.id=a.agent_id WHERE a.status='active' AND a.home_project_id IS NULL ORDER BY a.agent_id")
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


def with_effective_scope(profile: AgentProfile, connection) -> AgentProfile:
    """The profile whose ecosystem scope lists the current active projects.

    This is the read projection existing consumers expect; it is never written back
    as authority.
    """
    if profile.scope.kind != "ecosystem":
        return profile
    data = plain(profile.data)
    data["scope"]["allowed_project_ids"] = sorted(active_project_ids(connection))
    return AgentProfile(freeze(data))


def active_project_participant(connection, project_id: str, principal_id: str | None) -> bool:
    """An eligible current participant in an active project's work.

    Humans and system/integration principals need their active membership; agents
    qualify only as active supported globals. A project-home identity never does.
    """
    if not principal_id or not project_id:
        return False
    principal = connection.execute("SELECT kind, active FROM principals WHERE id=?", (principal_id,)).fetchone()
    if principal is None:
        return False
    if principal["kind"] == "agent":
        return reaches_project(connection, principal_id, project_id)
    member = connection.execute("SELECT active FROM project_memberships WHERE project_id=? AND principal_id=?",
                                (project_id, principal_id)).fetchone()
    return bool(member and member["active"])
