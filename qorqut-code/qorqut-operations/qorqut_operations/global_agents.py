"""Current global agents and the business access they derive from the active business directory.

This module is the one current-global eligibility authority of the engine and the host; it imports nothing from the
host. A current global is an active ``agent`` principal whose registered profile row is active, has no home business
and stores a decoded profile that is itself active, has an ecosystem scope without a business and holds one of the
supported global roles. Missing or malformed profile JSON and wrong field types fail closed. Its business access is
every business active in the reading transaction. The profile's stored ``allowed_business_ids`` list and its own
membership rows are not consulted: a business registered later is included immediately and an archived business is
excluded. A missing, inactive or retired principal or profile, an unknown or retired role, and any business-home
profile is never global, so a retained historical row cannot regain eligibility here; history readers that present
retained records do not use this predicate.

Humans and registered system workers keep their membership and owner rules, and this module says nothing about them.
All-business access is business scope only: role and plugin operation grants and global capabilities are still decided
separately."""

from __future__ import annotations

import json

from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import plain
from qorqut_operations.roles import GLOBAL_WORKER_ROLE, LEARNING_APPLIER_ROLE, ORDUBASHY_ROLE

# The only roles a current global may hold. Retired and unknown role ids stay readable in history, never current.
SUPPORTED_GLOBAL_ROLES = frozenset({ORDUBASHY_ROLE, LEARNING_APPLIER_ROLE, GLOBAL_WORKER_ROLE})
# The one role several current globals may hold at once; every other supported role has at most one active holder.
REPEATABLE_GLOBAL_ROLES = frozenset({GLOBAL_WORKER_ROLE})

_GLOBAL_PROFILES = ("SELECT a.agent_id, a.revision, a.status, a.home_business_id, a.profile_json, p.kind, p.active "
                    "FROM agent_profiles a JOIN principals p ON p.id=a.agent_id")


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def current_global_data(profile) -> dict | None:
    """The decoded stored profile when it describes a current supported global, or None.

    ``profile`` is stored profile JSON text or an already decoded mapping; the result is a fresh plain dict. Only the
    fields this predicate reads are checked, and a present field of the wrong type rejects the profile. Input too
    deeply nested to decode or copy is refused like any other malformed profile."""
    try:
        data = json.loads(profile) if isinstance(profile, (str, bytes, bytearray)) else plain(profile)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("status") != "active":
        return None
    role, scope = data.get("role"), data.get("scope")
    if not isinstance(role, dict) or not isinstance(role.get("id"), str) or role["id"] not in SUPPORTED_GLOBAL_ROLES:
        return None
    if not isinstance(scope, dict) or scope.get("kind") != "ecosystem" or scope.get("business_id") is not None:
        return None
    if any(key in scope and not _strings(scope[key]) for key in ("allowed_business_ids", "global_capabilities")):
        return None
    return data


def current_global_row(row) -> dict | None:
    """The decoded profile of a current supported global registration row, or None for anything else.

    ``row`` is an ``agent_profiles`` row joined with its principal's ``kind`` and ``active`` columns. A business home
    disqualifies the row whatever its status."""
    if row is None or row["home_business_id"] is not None or row["status"] != "active":
        return None
    if row["kind"] != "agent" or row["active"] not in (1, True):
        return None
    return current_global_data(row["profile_json"])


def global_profile(connection, agent_id) -> dict | None:
    if not agent_id:
        return None
    row = connection.execute(_GLOBAL_PROFILES + " WHERE a.agent_id=?", (agent_id,)).fetchone()
    profile = current_global_row(row)
    return {"agent_id": row["agent_id"], "revision": row["revision"], "profile": profile} if profile is not None else None


def is_global_agent(connection, agent_id) -> bool:
    return global_profile(connection, agent_id) is not None


def is_global_worker(connection, agent_id) -> bool:
    """True for a current global holding the repeatable worker role. Workers read what Ordubashy reads; this predicate
    admits them to read paths only, never to a write."""
    found = global_profile(connection, agent_id)
    return found is not None and found["profile"]["role"]["id"] == GLOBAL_WORKER_ROLE


def is_coordinator(connection, principal_id) -> bool:
    profile = global_profile(connection, principal_id)
    installation = connection.execute("SELECT management_coordinator_id FROM installation WHERE id='service'").fetchone()
    return bool(profile and installation and installation[0] == principal_id
                and profile['profile']['role']['id'] == ORDUBASHY_ROLE)


def global_agent_ids(connection) -> list[str]:
    return [row["agent_id"] for row in connection.execute(
        _GLOBAL_PROFILES + " WHERE a.status='active' AND a.home_business_id IS NULL ORDER BY a.agent_id")
        if current_global_row(row) is not None]


def effective_business_ids(connection, agent_id) -> frozenset:
    return active_business_ids(connection) if is_global_agent(connection, agent_id) else frozenset()


__all__ = ["REPEATABLE_GLOBAL_ROLES", "SUPPORTED_GLOBAL_ROLES", "current_global_data", "current_global_row", "effective_business_ids",
           "global_agent_ids", "global_profile", "is_global_agent", "is_coordinator"]
