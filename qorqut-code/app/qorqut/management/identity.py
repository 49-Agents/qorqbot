"""One registered identity predicate for the installation owner and Ordubashy.

The private owner conversation and management intake ask the same question, on
whichever supported CLI the owner started, so the profile check lives here once. Session
IDs, `agent_activity` rows, runtime bookkeeping, workspace paths and display
names are delivery details: they never widen or narrow who a principal is.
"""

import json

from qorqut_operations.contracts import DomainError
from qorqut.agents.global_scope import active_global
from qorqut.agents.roles import ORDUBASHY_ROLE


def installation_owner(uow):
    """The registered installation owner principal, or None before registration."""
    row = uow.connection.execute("SELECT owner_principal_id FROM installation WHERE id='service'").fetchone()
    return row[0] if row else None


def ordubashy_profile(uow, identity):
    """True when `identity` is a current supported global holding the Ordubashy role.

    Current means the shared global eligibility (``global_scope.active_global``):
    an active agent principal with an active, homeless ecosystem profile. A
    business-home, inactive or malformed registration never qualifies. The
    runtime is not part of the identity: the owner may start Ordubashy with any
    supported CLI, and every session shares the same records and history.
    """
    data = active_global(uow.connection, identity)
    return data is not None and data['role']['id'] == ORDUBASHY_ROLE


def configured_ordubashy(uow):
    """The one configured coordinator principal, or None when it cannot act."""
    installation = uow.repository('installation').get('service')
    identity = installation and installation['management_coordinator_id']
    if not identity or not ordubashy_profile(uow, identity):
        return None
    return identity


def private_participants(uow):
    """The exact pair allowed in a private conversation; never client-selected."""
    return installation_owner(uow), configured_ordubashy(uow)


def require_coordinator(uow):
    """The management coordinator on any supported runtime; keeps its two distinct refusals."""
    installation = uow.repository('installation').get('service')
    if not (installation and installation['management_coordinator_id']):
        raise DomainError('management_unavailable', 'No management coordinator is configured')
    identity = configured_ordubashy(uow)
    if not identity:
        raise DomainError('management_unavailable', 'Configured coordinator is not an active Ordubashy')
    return identity


# Dev administration is owner-only; terminal callers may opt in coordinating roles.
PROTECTED_CONTROL_ROLES = frozenset({"dev"})


def protected_control_target(uow, agent_id):
    """True when the current registered profile of `agent_id` holds a protected role.

    The role comes from the stored profile, never from a caller's label, and is
    checked whatever the profile's status so a stale queued action cannot slip by.
    """
    row = uow.connection.execute("SELECT profile_json FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
    if not row:
        return False
    try:
        data = json.loads(row["profile_json"] or "{}")
    except (TypeError, ValueError):
        return False
    role = data.get("role") if isinstance(data, dict) else None
    return isinstance(role, dict) and role.get("id") in PROTECTED_CONTROL_ROLES


def is_active_installation_owner(uow, principal_id, principal_kind):
    owner = installation_owner(uow)
    if principal_kind != "human" or not owner or principal_id != owner:
        return False
    row = uow.connection.execute("SELECT active FROM principals WHERE id=?", (principal_id,)).fetchone()
    return bool(row and row["active"])


def require_protected_control(uow, principal_id, principal_kind, agent_ids, *, allow_coordinators=False):
    """Reject the whole request when an unauthorized actor targets Dev's session.

    Call before any terminal, lifecycle or administrative effect, and again at
    apply and queued dispatch. Mixed selections are rejected atomically; a
    protected target is never silently omitted.
    """
    if is_active_installation_owner(uow, principal_id, principal_kind):
        return
    if allow_coordinators and principal_kind == 'agent':
        row = uow.connection.execute("SELECT p.active,a.status,a.profile_json FROM principals p JOIN agent_profiles a "
            "ON a.agent_id=p.id WHERE p.id=?", (principal_id,)).fetchone()
        if row and row['active'] and row['status'] == 'active':
            profile = json.loads(row['profile_json'])
            if profile['role']['id'] == 'ordubashy' and profile['scope']['kind'] == 'ecosystem':
                return
    protected = sorted({identity for identity in agent_ids if protected_control_target(uow, identity)})
    if protected:
        actors = "the installation owner or Ordubashy" if allow_coordinators else "the installation owner"
        raise DomainError("forbidden", f"Only {actors} may perform this Dev control operation",
                          details={"agent_ids": protected})
