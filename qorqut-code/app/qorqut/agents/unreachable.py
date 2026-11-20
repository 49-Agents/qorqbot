"""Unreachable workers: one inbox event to Ordubashy when a permanent worker's session cannot take input.

Inbox notices are delivered into an agent's session. The nudge engine records every attempt, so its rows already say
when a worker stopped being reachable: every attempt since the last one that may have arrived was definitely not
sent (the session could not be verified, kept refusing, or cannot be opened). Work assigned to such
a worker waits unseen. Ordubashy is told once the condition has lasted ``BLOCKED_AFTER``; he is told again only after
``REPEAT_AFTER`` while it still lasts.

A worker with no session is not, by that alone, unreachable: a managed session closes after 15 idle minutes and the next
input opens it (``native_chat/opener.py``). Only two conditions with no session are reported: the worker is **off**
(the owner ended its session or Ordubashy killed it, and something is waiting for it; repair: ``agent_terminal_start``), and
opening it **keeps failing** for a reason that repeats (``opener.REPEATING``: Claude Code missing, its earlier conversation
cannot be resumed, the session cannot start), counted in this process for ``BLOCKED_AFTER``. The repair named is the one that
works for that reason, and for the reasons only the owner can change it says so.

A worker in a running turn is reachable: a notice that waits for the turn to end is not a fault. A worker with
nothing pending is never reported, because nothing is waiting on it.

The emitted events are the only memory, so there is no new table and a repeated pass, a restart or two racing passes
cannot tell him twice within a period. Nothing about the worker changes and its session is never touched here."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from qorqut_operations.application.plans import utc_clock
from qorqut_operations.discussion.events import emit_event

from qorqut.management.identity import configured_ordubashy
from .worker_access import is_worker

SOURCE_TYPE = "agent_profiles"
EVENT_TYPE = "unreachable"
# How long a worker must have been unable to take input before Ordubashy is told, and how long until he is told again.
BLOCKED_AFTER = timedelta(minutes=20)
REPEAT_AFTER = timedelta(hours=6)
# Attempt states of the nudge engine: a notice that may have arrived, and one that was definitely not typed.
ARRIVED = ("submitted", "uncertain")
NOT_TYPED = ("deferred", "failed", "unavailable")
# A notice deferred for these reasons waits for a running turn to end or a session to finish starting or closing; the session itself
# is fine.
BUSY = frozenset({"turn_running", "session_starting", "session_closing", "host_busy"})
OFFLINE = "offline"
OFF = "off"
REASONS = {
    "host_unverified": "its session could not be verified",
    "host_rejected": "its session keeps refusing input",
    OFFLINE: "it has no running session",
    OFF: "it is turned off",
    "cli_unavailable": "the client it is set to use (Claude Code or Codex) is not installed or not usable on this computer, so its session cannot be opened",
    "cli_not_signed_in": "the client it is set to use (Claude Code or Codex) is not signed in, so its session cannot be opened",
    "resume_failed": "its earlier conversation could not be resumed, so its session cannot be opened",
    "resume_unavailable": "its earlier conversation can't be continued because its file is missing, so its session cannot be opened",
    "driver_unavailable": "its session could not be started",
}
# What Ordubashy is told to do, by reason. A repair he cannot make himself says whose it is.
REPAIRS = {
    OFF: "Turn it on with agent_terminal_start (it opens and takes what is waiting); if it cannot be started, reassign its open work.",
    "cli_unavailable": ("Only the owner can fix this: tell the owner to download the client under Settings > Agents, or to switch the "
                        "agent to the other client there. Until then reassign its open work."),
    "cli_not_signed_in": ("Only the owner can fix this: tell the owner to sign in to the client under Settings > Agents, or to switch the "
                          "agent to the other client there. Until then reassign its open work."),
    "resume_failed": ("agent_terminal_restart can try once more; if it fails again, tell the owner and reassign its open work."),
    "resume_unavailable": ("Only the owner can fix this: tell the owner to start a new conversation for it in Chat. "
                           "Until then reassign its open work."),
    "driver_unavailable": ("Try agent_terminal_restart once; if it is still unreachable, tell the owner and reassign its open work."),
}
DEFAULT_REPAIR = "Check it with agent_terminal_status and restart it with agent_terminal_restart; if it is still unreachable after that, reassign its open work."


def _instant(text) -> datetime | None:
    try:
        value = datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _failing(agent_id: str, current: datetime):
    """``(since, code)`` while opening this agent has kept failing, for a reason that repeats, for at least ``BLOCKED_AFTER``."""
    from qorqut.native_chat.opener import FAILURES
    found = FAILURES.streak(agent_id)
    if found is None:
        return None
    code, since, count = found
    if count < 2 or current - since < BLOCKED_AFTER:
        return None
    return _stamp(since), code


def blocked(connection, agent_id: str, current: datetime) -> dict | None:
    """``{"since", "reason"}`` when ``agent_id`` has pending inbox items and has been unable to take a notice for at
    least ``BLOCKED_AFTER``; otherwise None."""
    need = connection.execute("SELECT active,reason,updated_at FROM inbox_nudge_needs WHERE agent_id=?", (agent_id,)).fetchone()
    if need is None or not need["active"]:
        return None
    marks = ",".join("?" * len(ARRIVED + NOT_TYPED))
    latest = connection.execute(
        f"SELECT state,reason FROM inbox_nudge_attempts WHERE agent_id=? AND state IN ({marks}) ORDER BY sequence DESC LIMIT 1",
        (agent_id, *ARRIVED, *NOT_TYPED)).fetchone()
    desired = connection.execute("SELECT desired_state FROM native_session_owners WHERE agent_id=?", (agent_id,)).fetchone()
    failing = _failing(agent_id, current)
    if desired is not None and desired["desired_state"] == "stopped":
        since, reason = need["updated_at"], OFF
    elif failing is not None:
        since, reason = failing
    elif need["reason"] == OFFLINE:
        # Asleep and on: the next input opens it. Nothing is wrong until opening it keeps failing (above).
        return None
    elif latest is None or latest["state"] in ARRIVED or latest["reason"] in BUSY:
        return None
    else:
        arrived = connection.execute(
            f"SELECT COALESCE(MAX(sequence),0) FROM inbox_nudge_attempts WHERE agent_id=? AND state IN ({','.join('?' * len(ARRIVED))})",
            (agent_id, *ARRIVED)).fetchone()[0]
        first = connection.execute(
            f"SELECT prepared_at FROM inbox_nudge_attempts WHERE agent_id=? AND sequence>? AND state IN ({','.join('?' * len(NOT_TYPED))}) "
            "ORDER BY sequence LIMIT 1", (agent_id, arrived, *NOT_TYPED)).fetchone()
        since, reason = first["prepared_at"], latest["reason"] or latest["state"]
    began = _instant(since)
    if began is None or current - began < BLOCKED_AFTER:
        return None
    return {"since": since, "reason": reason}


def summary(name: str, found: dict, waiting: int) -> str:
    """What Ordubashy reads in his inbox: who, since when, why, how much is waiting, and the repair."""
    since = found["since"][:16].replace("T", " ")
    why = REASONS.get(found["reason"], found["reason"])
    items = f"{waiting} inbox item{'' if waiting == 1 else 's'}"
    return (f"{name} cannot receive inbox notices since {since} UTC: {why}. {items} waiting. "
            + REPAIRS.get(found["reason"], DEFAULT_REPAIR))


def reconcile(uow, *, now: datetime | None = None) -> list[dict]:
    """Tell Ordubashy about each permanent worker that is unreachable and not told within the period; returns what
    was told. Reads only, until a notice is due."""
    coordinator = configured_ordubashy(uow)
    if coordinator is None:
        return []
    current = now or utc_clock()
    told = []
    for profile in uow.connection.execute(
            "SELECT agent_id,name,revision FROM agent_profiles WHERE status='active' AND agent_id!=? ORDER BY name,agent_id",
            (coordinator,)).fetchall():
        agent_id = profile["agent_id"]
        if not is_worker(uow.connection, agent_id):
            continue
        found = blocked(uow.connection, agent_id, current)
        if found is None:
            continue
        # One notice per stretch, and again only after a full period while the stretch lasts.
        after = max(found["since"], _stamp(current - REPEAT_AFTER))
        if uow.connection.execute(
                "SELECT 1 FROM inbox_events WHERE source_type=? AND source_id=? AND event_type=? AND created_at>=? LIMIT 1",
                (SOURCE_TYPE, agent_id, EVENT_TYPE, after)).fetchone():
            continue
        waiting = uow.connection.execute(
            "SELECT COUNT(*) FROM inbox_deliveries WHERE agent_id=? AND acknowledged_at IS NULL", (agent_id,)).fetchone()[0]
        event_id = emit_event(uow, business_id=None, source_type=SOURCE_TYPE, source_id=agent_id,
                              source_revision=profile["revision"], actor_id=None, event_type=EVENT_TYPE,
                              summary=summary(profile["name"], found, waiting), required=[coordinator])
        if event_id is not None:
            told.append({"agent_id": agent_id, "event_id": event_id, **found})
    return told


__all__ = ["BLOCKED_AFTER", "EVENT_TYPE", "REPEAT_AFTER", "blocked", "reconcile", "summary"]
