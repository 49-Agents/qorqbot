"""The one writer of inbox events and their deliveries for Operations-owned records.

An event is the immutable fact that something happened to a record; a delivery addresses it to one registered agent.
Each delivery is either *required* (a responsibility or a mandatory route: assignees, assignment targets, owners of
dependent work, the owner-task coordinator, a coordinator notice) or *optional* (a follower of a discussion). The
two sets are independent: an optional follower who has unfollowed never receives optional deliveries, and no follow
state ever removes a required one. The acting principal is never woken by its own change, exactly as in v1.

Recipient eligibility: only an active global agent is addressed, in Global when it holds a global capability and in
every active business without a membership row. Business-home profiles are never addressed.
The writer runs inside the caller's write transaction; a failure rolls the record and its notifications back
together."""

from __future__ import annotations

from collections.abc import Iterable
import json

from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import DomainError, utc_now
from qorqut_operations.global_agents import global_profile

SUMMARY_LIMIT = 500


def eligible_agents(uow, business_id, agents: Iterable[str]) -> set[str]:
    """The registered agents that may currently be addressed about a record of this namespace.

    Only an active global agent is addressable: in Global with a global capability, and in every active business without
    a membership row. A business-home or non-ecosystem profile is never addressable, whatever its membership rows say."""
    active, businesses = set(), None
    for agent in sorted({a for a in agents if a}):
        found = global_profile(uow.connection, agent)
        if found is None:
            continue
        if business_id is None:
            allowed = bool(found["profile"]["scope"].get("global_capabilities"))
        else:
            businesses = active_business_ids(uow.connection) if businesses is None else businesses
            allowed = business_id in businesses
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
