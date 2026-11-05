"""Durable inbox publication for automation output, questions and failure, with the program worker as origin.

Events and their deliveries are written directly in the caller's transaction (the broadcast ``_inbox`` precedent), so the
event, its deliveries and any cursor advance commit together. The origin is never Ordubashy, so its own self-suppression never hides a
delivery. Each event carries a stable ``key``: publishing the same key again returns the existing event and - for ``required_for`` -
restores a missing or informational delivery to a required one, so a notification owed to Ordubashy is never silently lost.

The inbox event keeps at most ``MAX_DATA_BYTES`` of published data inline; the complete bounded record lives in the step history."""

from __future__ import annotations

import json

from qorqut_operations.contracts import canonical_hash, utc_now

from .records import PROGRAM, SOURCE

MAX_SUMMARY = 160
MAX_DATA_BYTES = 8192
MANAGERS = "managers"       # failures, questions and recovery notices: visible to the owner and the configured Ordubashy only
READERS = "readers"         # published output: visible to every authorized reader


def worker_id(runtime, uow):
    return runtime.authority.worker_id(PROGRAM, uow)


def ordubashy(uow):
    """The configured, active Ordubashy principal id, or ``None`` (a failure then waits for the next reconciliation)."""
    from bazarlyk.management.identity import configured_ordubashy
    return configured_ordubashy(uow, require_codex=False)


def _existing(uow, automation_id, key):
    row = uow.connection.execute(
        "SELECT id FROM inbox_events WHERE source_type=? AND source_id=? AND json_extract(visibility_json,'$.key')=? LIMIT 1",
        (SOURCE, automation_id, key)).fetchone()
    return row["id"] if row else None


def subscribers(uow, automation):
    return [row["agent_id"] for row in uow.connection.execute(
        "SELECT agent_id FROM inbox_subscriptions WHERE business_id IS ? AND source_type=? AND source_id=?",
        (automation["business_id"], SOURCE, automation["id"]))]


def _deliver(uow, event, agents, required):
    for agent_id in agents:
        profile = uow.connection.execute("SELECT status FROM agent_profiles WHERE agent_id=?", (agent_id,)).fetchone()
        if profile is None or profile["status"] != "active":
            continue
        uow.connection.execute("INSERT OR IGNORE INTO inbox_deliveries(agent_id,event_id,required) VALUES(?,?,?)",
                               (agent_id, event, 1 if agent_id in required else 0))
        if agent_id in required:
            uow.connection.execute("UPDATE inbox_deliveries SET required=1 WHERE agent_id=? AND event_id=? AND required=0",
                                   (agent_id, event))


def publish(runtime, uow, automation, *, key, event_type, summary, data=None, run_id=None, required_for=(), audience=READERS,
            addressed=()):
    """Insert one event (idempotent by ``key``) and its deliveries: subscribers informationally (readers' events only),
    ``required_for`` and ``addressed`` agents as required - independent of any subscription.

    Returns ``(event_id, created)``."""
    uow.require_write()
    required = {agent for agent in (*required_for, *addressed) if agent}
    existing = _existing(uow, automation["id"], key)
    if existing is not None:
        _deliver(uow, existing, required, required)
        return existing, False
    payload = {"key": key, "run_id": run_id, "kind": event_type, "audience": audience}
    if data is not None:
        text = json.dumps(data, sort_keys=True, default=str)
        payload["data"] = data if len(text) <= MAX_DATA_BYTES else {"truncated": True, "digest": canonical_hash(data), "bytes": len(text)}
    actor = worker_id(runtime, uow)
    uow.actor_id = actor
    event = uow.connection.execute(
        "INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,visibility_json,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (automation["business_id"], SOURCE, automation["id"], actor, event_type, summary[:MAX_SUMMARY],
         json.dumps(payload, sort_keys=True, default=str), utc_now())).lastrowid
    watchers = subscribers(uow, automation) if audience == READERS else []
    _deliver(uow, event, {*watchers, *required}, required)
    return event, True
