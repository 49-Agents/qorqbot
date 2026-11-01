"""The one writer of inbox events and their deliveries for Operations-owned records.

An event is the immutable fact that something happened to a record; a delivery addresses it to one registered agent.
Each delivery is either *required* (a responsibility or a mandatory route: assignees, assignment targets, owners of
dependent work, the owner-task coordinator, a coordinator notice) or *optional* (a follower of a discussion). The
two sets are independent: an optional follower who has unfollowed never receives optional deliveries, and no follow
state ever removes a required one. The acting principal is never woken by its own change, exactly as in v1.

Recipient eligibility is v1's: an active registered agent whose profile reaches the record's namespace and who is
still an active member of that business (a Global record needs an ecosystem agent holding global capabilities).
The writer runs inside the caller's write transaction; a failure rolls the record and its notifications back
together."""

from __future__ import annotations

from collections.abc import Iterable
import json

from qorqut_operations.contracts import DomainError, utc_now

SUMMARY_LIMIT = 500


def eligible_agents(uow, business_id, agents: Iterable[str]) -> set[str]:
    """The registered agents that may currently be addressed about a record of this namespace (v1's filter)."""
    active = set()
    for agent in sorted({a for a in agents if a}):
        profile = uow.connection.execute(
            "SELECT a.home_business_id, a.profile_json FROM agent_profiles a JOIN principals p ON p.id=a.agent_id "
            "WHERE a.agent_id=? AND a.status='active' AND p.active=1 AND p.kind='agent'", (agent,)).fetchone()
        if profile is None:
            continue
        scope = json.loads(profile["profile_json"]).get("scope", {})
        if business_id is None:
            allowed = scope.get("kind") == "ecosystem" and bool(scope.get("global_capabilities"))
        else:
            allowed = business_id == profile["home_business_id"] or business_id in scope.get("allowed_business_ids", [])
            member = uow.connection.execute(
                "SELECT active FROM business_memberships WHERE business_id=? AND principal_id=?",
                (business_id, agent)).fetchone()
            allowed = allowed and bool(member and member["active"])
        if allowed:
            active.add(agent)
    return active


def emit_event(uow, *, business_id, source_type: str, source_id: str, source_revision, actor_id, event_type: str,
               summary: str, required: Iterable[str] = (), optional: Iterable[str] = (), visibility=None) -> int | None:
    """Write one event and its deliveries; returns the event id, or None when nobody is eligible to receive it.

    ``required`` and ``optional`` are principal ids. A principal in both sets is delivered once, as required."""
    uow.require_write()
    if not isinstance(source_type, str) or not source_type or not isinstance(source_id, str) or not source_id:
        raise DomainError("validation_error", "An event names its source record")
    if not isinstance(event_type, str) or not event_type:
        raise DomainError("validation_error", "An event has a type")
    mandatory = set(required) - {actor_id}
    discretionary = set(optional) - {actor_id} - mandatory
    recipients = eligible_agents(uow, business_id, mandatory | discretionary)
    if not recipients:
        return None
    cursor = uow.connection.execute(
        "INSERT INTO inbox_events(business_id,source_type,source_id,source_revision,actor_id,event_type,summary,"
        "visibility_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (business_id, source_type, source_id, source_revision, actor_id, event_type,
         " ".join(str(summary).split())[:SUMMARY_LIMIT] or source_type, json.dumps(visibility or {}, sort_keys=True), utc_now()))
    uow.connection.executemany(
        "INSERT INTO inbox_deliveries(agent_id,event_id,required) VALUES(?,?,?)",
        [(agent, cursor.lastrowid, int(agent in mandatory)) for agent in sorted(recipients)])
    return cursor.lastrowid
