"""Unit-of-work state of the inbox nudge engine.

One durable need per registered agent, immutable attempts and their append-only evidence. Everything here is a function of an
open unit of work and a ``Visibility`` port: no native I/O, no clock of its own, no thread. The host reserves an attempt, sends
through its one input arbiter, and settles with exactly what the transport reported.

Rearm rule (frozen): a notice is an advisory about events that are still pending and currently authorized. In one pending cycle
(the interval in which some covered event is still pending), one exact native session receives

* one first notice;
* one follow-up after the work that was already running at that first notice finishes (``Observation.work`` advances); and
* one more after each further direct Chat input the host positively observed has been worked through.

A timer never rearms a notice; ``MINIMUM_REPEAT_SECONDS`` is only a lower bound between two notices. A nudge's own answer
does not advance ``Observation.work``, so it cannot ask for another. Acknowledgement or revocation of every covered event ends
the cycle; new events join the cycle while any covered event is pending (they are coalesced into the next permitted notice).
An exact new session (a new generation) gets its own advisory, never a replay of an old session's receipt. Anything that may
have reached a session is ``uncertain`` and is never sent again to that session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any

from qorqut_operations.contracts import DomainError, canonical_hash, new_id, plain

from .contracts import (ATTEMPT_STATES, DEFERRED, DEFERRED_RETRY_SECONDS, EFFECTIVE, FAILED, FAILED_RETRY_SECONDS, IDLE, KIND_FOLLOW_UP,
                        KIND_INITIAL, KIND_NEW_SESSION, MINIMUM_REPEAT_SECONDS, PENDING, PENDING_SAMPLE, PREPARED, SCAN_ROWS,
                        STALE, STATUS_DEFERRED, STATUS_NOTE, STATUS_STALE, STATUS_SUBMITTED, STATUS_UNAVAILABLE,
                        STATUS_UNCERTAIN, SUBMITTED, UNAVAILABLE, UNAVAILABLE_RETRY_SECONDS, UNCERTAIN, Binding, Observation,
                        Outcome, Reservation, Waiting, source_key)
from .ports import Visibility

PAGE = 200
BACKOFF_CAP_SECONDS = 300
TRAILING = 12
NO_EFFECT_RETRY = {DEFERRED: DEFERRED_RETRY_SECONDS, FAILED: FAILED_RETRY_SECONDS, UNAVAILABLE: UNAVAILABLE_RETRY_SECONDS}


@dataclass(frozen=True)
class Policy:
    """Pacing only. None of these starts a notice; each is a lower bound on how soon a permitted one may follow."""

    minimum_repeat_seconds: float = MINIMUM_REPEAT_SECONDS
    backoff_cap_seconds: float = BACKOFF_CAP_SECONDS

    def __post_init__(self):
        for value in (self.minimum_repeat_seconds, self.backoff_cap_seconds):
            if type(value) not in (int, float) or not 0 <= value < float("inf"):
                raise ValueError("Nudge pacing values are finite nonnegative numbers")


@dataclass(frozen=True)
class Scan:
    """One bounded window over an agent's pending deliveries, resumed from the need's saved ``scan_after``.

    Only a window that started at the beginning and read to the last pending row can prove that nothing authorized is pending;
    a window that stopped early or started in the middle of the inbox proves nothing about the rows it did not read."""

    ids: tuple[int, ...]  # authorized pending event ids found in this window, ascending, at most the coverage sample
    more: bool  # the sample is full: further authorized events may exist
    incomplete: bool  # the window was not the whole pending set (it stopped early or started after the beginning)
    empty: bool = False  # no authorized event is pending, proven (see above) -- or there are no pending rows at all
    after: int = 0  # the event id this window started after
    next: int = 0  # the event id the next window starts after; 0 once the last pending row was read (the pass wrapped)


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def seconds_between(later: str, earlier: str) -> float:
    return (parse_time(later) - parse_time(earlier)).total_seconds()


def add_seconds(text: str, seconds: float) -> str:
    return (parse_time(text) + timedelta(seconds=seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _row(cursor_row) -> dict | None:
    return dict(cursor_row) if cursor_row is not None else None


def need_row(uow, agent_id: str) -> dict | None:
    return _row(uow.connection.execute("SELECT * FROM inbox_nudge_needs WHERE agent_id=?", (agent_id,)).fetchone())


def attempt_row(uow, attempt_id: str) -> dict | None:
    return _row(uow.connection.execute("SELECT * FROM inbox_nudge_attempts WHERE id=?", (attempt_id,)).fetchone())


# ----- what is pending ------------------------------------------------------------------------------------------------------

def _authorized(uow, port: Visibility, agent_id: str, rows: list) -> set[int]:
    if not rows:
        return set()
    granted = port.authorized(uow, agent_id, rows)
    return {row["id"] for row in rows} & {identity for identity in granted if type(identity) is int}


def scan_pending(uow, port: Visibility, agent_id: str, *, after: int = 0, limit: int = PENDING_SAMPLE,
                 budget: int = SCAN_ROWS) -> Scan:
    """A bounded window of the agent's pending events: its unacknowledged deliveries after event ``after``, filtered by current
    authorization, in id order.

    Scans in bounded SQL pages so unauthorized rows never consume the sample, and stops after ``budget`` rows. The result says
    where the next window resumes (``next``): the last row inspected, or 0 once the last pending row was read, so a long run of
    unauthorized rows is crossed over several passes and every earlier row is eventually read (and authorized) again."""
    found: list[int] = []
    cursor, read = after, 0
    while read < budget and len(found) < limit:
        page = min(PAGE, budget - read)
        rows = uow.connection.execute(
            "SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
            "WHERE d.agent_id=? AND d.acknowledged_at IS NULL AND e.id>? ORDER BY e.id LIMIT ?",
            (agent_id, cursor, page)).fetchall()
        read += len(rows)
        if rows:
            cursor = rows[-1]["id"]
            granted = _authorized(uow, port, agent_id, rows)
            found += [row["id"] for row in rows if row["id"] in granted][:limit - len(found)]
        if len(rows) < page:
            # The last pending row was read. Only a window that began at the start covered every pending row; one that began
            # later is empty-handed proof only when no pending row exists at all.
            nothing = not found and (after == 0 or uow.connection.execute(
                "SELECT 1 FROM inbox_deliveries WHERE agent_id=? AND acknowledged_at IS NULL LIMIT 1", (agent_id,)).fetchone() is None)
            return Scan(tuple(found), False, after != 0 and not nothing, nothing, after, 0)
    full = len(found) >= limit
    return Scan(tuple(found), full, True, False, after, cursor)


def alive_coverage(uow, port: Visibility, agent_id: str, event_ids: list[int]) -> set[int]:
    """Which of the given events are still pending (unacknowledged) and currently authorized for the agent."""
    alive: set[int] = set()
    for start in range(0, len(event_ids), 100):
        chunk = event_ids[start:start + 100]
        rows = uow.connection.execute(
            "SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id WHERE d.agent_id=? "
            "AND d.acknowledged_at IS NULL AND e.id IN (" + ",".join("?" * len(chunk)) + ")", (agent_id, *chunk)).fetchall()
        alive |= _authorized(uow, port, agent_id, rows)
    return alive


# ----- evidence and attempts ------------------------------------------------------------------------------------------------

def _record(uow, attempt: dict, state: str, reason: str, evidence: dict, now: str) -> bool:
    """Append exact evidence and move the attempt, once. A repeat of the same evidence changes nothing."""
    digest = canonical_hash(evidence)
    if uow.connection.execute("SELECT 1 FROM inbox_nudge_evidence WHERE attempt_id=? AND evidence_hash=?",
                              (attempt["id"], digest)).fetchone():
        return False
    uow.connection.execute("INSERT INTO inbox_nudge_evidence(attempt_id,evidence_hash,state,outcome_json,recorded_at) VALUES(?,?,?,?,?)",
                           (attempt["id"], digest, state, json.dumps(evidence, sort_keys=True, separators=(",", ":")), now))
    uow.connection.execute("UPDATE inbox_nudge_attempts SET state=?,reason=?,settled_at=COALESCE(settled_at,?),outcome_json=? WHERE id=?",
                           (state, reason[:200], now, json.dumps(evidence, sort_keys=True, separators=(",", ":")), attempt["id"]))
    return True


def cancel(uow, attempt_id: str, reason: str, now: str) -> dict:
    """Cancel an unsettled reservation before any input. Only a ``prepared`` attempt can be cancelled; anything else is returned
    unchanged (an attempt that may have been typed is never rewritten)."""
    uow.require_write()
    attempt = attempt_row(uow, attempt_id)
    if attempt is None:
        raise DomainError("not_found", "Nudge attempt is unavailable")
    if attempt["state"] == PREPARED:
        binding = Binding.of(json.loads(attempt["binding_json"]))
        _record(uow, attempt, STALE, reason, {"state": STALE, "reason": reason,
                                              "receipt": {"effect": "none", "binding": binding.public()}}, now)
    return view(attempt_row(uow, attempt_id))


def settle(uow, attempt_id: str, outcome: Outcome, now: str) -> dict:
    """Record exactly what the native transport reported for one reservation.

    The receipt must name the attempt's own exact binding: a late receipt of an older generation can never settle, release or
    replace a newer session's attempt. A settled effect is final; the one allowed change is later exact evidence that an
    ``uncertain`` attempt was in fact submitted."""
    uow.require_write()
    attempt = attempt_row(uow, attempt_id)
    if attempt is None:
        raise DomainError("not_found", "Nudge attempt is unavailable")
    binding = Binding.of(json.loads(attempt["binding_json"]))
    evidence = outcome.evidence(binding)
    seen = uow.connection.execute("SELECT 1 FROM inbox_nudge_evidence WHERE attempt_id=? AND evidence_hash=?",
                                  (attempt_id, canonical_hash(evidence))).fetchone()
    if seen is None:
        if not (attempt["state"] == PREPARED or (attempt["state"] == UNCERTAIN and outcome.state == SUBMITTED)):
            raise DomainError("outcome_conflict", "A settled nudge effect cannot be replaced by different evidence")
        _record(uow, attempt, outcome.state, outcome.reason, evidence, now)
    return view(attempt_row(uow, attempt_id))


def unsettled(uow, agent_id: str | None = None, states: tuple[str, ...] = (PREPARED, UNCERTAIN)) -> list[dict]:
    """Attempts that need reconciliation with the native input record: reservations and uncertain deliveries by default."""
    bad = [state for state in states if state not in ATTEMPT_STATES]
    if bad or not states:
        raise DomainError("validation_error", "Name attempt states to look for")
    # The states are validated names, written as literals so the partial index on reservations can serve the lookup.
    wanted = " OR ".join(f"state='{state}'" for state in states)
    rows = uow.connection.execute(f"SELECT * FROM inbox_nudge_attempts WHERE ({wanted})" + (" AND agent_id=?" if agent_id else "")
                                  + " ORDER BY agent_id,sequence", (agent_id,) if agent_id else ()).fetchall()
    return [view(dict(row)) for row in rows]


def view(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {"attempt_id": row["id"], "agent_id": row["agent_id"], "cycle": row["cycle"], "sequence": row["sequence"],
            "kind": row["kind"], "state": row["state"], "reason": row["reason"], "source_key": source_key(row["id"]),
            "binding": json.loads(row["binding_json"]), "event_ids": json.loads(row["event_ids_json"]),
            "progress": json.loads(row["progress_json"]), "prepared_at": row["prepared_at"], "settled_at": row["settled_at"],
            "outcome": json.loads(row["outcome_json"]) if row["outcome_json"] else None}


# ----- the need -------------------------------------------------------------------------------------------------------------

def _stale_reservation(uow, agent_id: str, reason: str, now: str) -> None:
    row = uow.connection.execute("SELECT id FROM inbox_nudge_attempts WHERE agent_id=? AND state='prepared'", (agent_id,)).fetchone()
    if row is not None:
        cancel(uow, row["id"], reason, now)


@dataclass(frozen=True)
class Facts:
    """What the host's authorization said about one agent, gathered in a read unit of work so no authorization work runs while
    the writer lock is held. ``latest_alive`` says whether any event the cycle's latest notice covered is still pending
    (``None`` when there is no such notice)."""

    scan: Scan
    cycle: int | None
    active: bool
    latest_alive: bool | None


def gather(uow, port: Visibility, agent_id: str) -> Facts:
    need = need_row(uow, agent_id)
    scan = scan_pending(uow, port, agent_id, after=need["scan_after"] if need is not None else 0)
    alive = None
    if need is not None and need["active"] and scan.ids:
        latest = uow.connection.execute("SELECT event_ids_json FROM inbox_nudge_attempts WHERE agent_id=? AND cycle=? "
                                        "ORDER BY sequence DESC LIMIT 1", (agent_id, need["cycle"])).fetchone()
        if latest is not None:
            alive = bool(alive_coverage(uow, port, agent_id, json.loads(latest["event_ids_json"])))
    return Facts(scan, need["cycle"] if need is not None else None, bool(need and need["active"]), alive)


def refresh(uow, facts: Facts, agent_id: str, now: str) -> tuple[dict | None, Scan]:
    """Bring the agent's need in line with what is pending. Writes only when something changed.

    The need is active while any pending, authorized event exists. A new cycle starts when it becomes active again, or when
    every event the latest notice covered has been acknowledged or revoked (that notice's coverage is stale)."""
    uow.require_write()
    scan, need = facts.scan, need_row(uow, agent_id)
    if not scan.ids:
        if not scan.empty:
            # A partial or tail window found nothing authorized: that proves nothing about the rows it did not read, so an
            # outstanding need stays and the scan simply resumes where this window stopped.
            return _resume(uow, need, agent_id, scan, now), scan
        if need is not None and need["active"]:
            _stale_reservation(uow, agent_id, "no_pending_events", now)
            uow.connection.execute("UPDATE inbox_nudge_needs SET active=0,reason=NULL,updated_at=? WHERE agent_id=?", (now, agent_id))
        return _resume(uow, need_row(uow, agent_id), agent_id, scan, now), scan
    if need is None:
        uow.connection.execute("INSERT INTO inbox_nudge_needs(agent_id,cycle,active,opened_at,opened_at_first,updated_at) VALUES(?,1,1,?,?,?)",
                               (agent_id, now, now, now))
    elif not need["active"]:
        # The first activation of a row that only held a scan position is cycle one.
        uow.connection.execute("UPDATE inbox_nudge_needs SET active=1,cycle=cycle+1,opened_at=?,reason=NULL,updated_at=? WHERE agent_id=?",
                               (now, now, agent_id))
    elif facts.latest_alive is False and facts.cycle == need["cycle"]:
        _stale_reservation(uow, agent_id, "coverage_handled", now)
        uow.connection.execute("UPDATE inbox_nudge_needs SET cycle=cycle+1,opened_at=?,reason=NULL,updated_at=? WHERE agent_id=?",
                               (now, now, agent_id))
    return _resume(uow, need_row(uow, agent_id), agent_id, scan, now), scan


def _resume(uow, need: dict | None, agent_id: str, scan: Scan, now: str) -> dict | None:
    """Save where the next window of this agent's scan resumes, in the same transaction as the rest of the preparation.

    This is a position, not a permission: it caches no authorization and acknowledges nothing, and it neither starts a cycle
    nor spends or rearms a notice allowance. An agent with no row yet gets an inactive one (cycle 0) when the position matters
    before any authorized event was found."""
    if need is None:
        if scan.next == 0:
            return None
        uow.connection.execute("INSERT INTO inbox_nudge_needs(agent_id,cycle,active,opened_at,opened_at_first,updated_at,scan_after) "
                               "VALUES(?,0,0,?,?,?,?)", (agent_id, now, now, now, scan.next))
    elif need["scan_after"] != scan.next:
        uow.connection.execute("UPDATE inbox_nudge_needs SET scan_after=?,updated_at=? WHERE agent_id=?", (scan.next, now, agent_id))
    return need_row(uow, agent_id)


def note(uow, agent_id: str, reason: str | None, now: str) -> None:
    """Remember why an active need has no notice yet (offline, waiting for progress, ...). Writes only on a change."""
    need = need_row(uow, agent_id)
    if need is not None and need["active"] and need["reason"] != (reason[:200] if reason else None):
        uow.connection.execute("UPDATE inbox_nudge_needs SET reason=?,updated_at=? WHERE agent_id=?",
                               (reason[:200] if reason else None, now, agent_id))


def _retry(uow, agent_id: str, latest: dict, key: str, now: str, policy: Policy) -> Waiting | None:
    """Pace a retry after a notice that was definitely not typed (its own backoff: it never rearms a delivered notice)."""
    if latest["state"] not in NO_EFFECT_RETRY or latest["session_key"] != key or latest["settled_at"] is None:
        return None
    streak = 0
    for row in uow.connection.execute("SELECT state,session_key FROM inbox_nudge_attempts WHERE agent_id=? ORDER BY sequence DESC LIMIT ?",
                                      (agent_id, TRAILING)):
        if row["state"] not in NO_EFFECT_RETRY or row["session_key"] != key:
            break
        streak += 1
    delay = min(policy.backoff_cap_seconds, NO_EFFECT_RETRY[latest["state"]] * 2 ** max(0, streak - 1))
    due = add_seconds(latest["settled_at"], delay)
    return Waiting("retry_later", due) if parse_time(now) < parse_time(due) else None


def decide(uow, need: dict, binding: Binding | None, observation: Observation, now: str, policy: Policy) -> str | Waiting:
    """The kind of notice permitted now for ``binding``, or why none is."""
    if binding is None:
        return Waiting("offline")
    agent_id, cycle, key = need["agent_id"], need["cycle"], binding.session_key
    latest = uow.connection.execute("SELECT * FROM inbox_nudge_attempts WHERE agent_id=? AND cycle=? ORDER BY sequence DESC LIMIT 1",
                                    (agent_id, cycle)).fetchone()
    if latest is not None and latest["state"] == PREPARED:
        return Waiting("reserved")
    last_effect = uow.connection.execute(
        "SELECT prepared_at,settled_at FROM inbox_nudge_attempts WHERE agent_id=? AND state IN ('submitted','uncertain') "
        "ORDER BY sequence DESC LIMIT 1", (agent_id,)).fetchone()
    if last_effect is not None:
        due = add_seconds(last_effect["settled_at"] or last_effect["prepared_at"], policy.minimum_repeat_seconds)
        if parse_time(now) < parse_time(due):
            return Waiting("minimum_spacing", due)
    effective = [dict(row) for row in uow.connection.execute(
        "SELECT * FROM inbox_nudge_attempts WHERE agent_id=? AND cycle=? AND state IN ('submitted','uncertain') ORDER BY sequence",
        (agent_id, cycle))]
    same = [row for row in effective if row["session_key"] == key]
    if any(row["state"] == UNCERTAIN for row in same):
        return Waiting("uncertain_unreconciled")
    if latest is not None and (waiting := _retry(uow, agent_id, dict(latest), key, now, policy)) is not None:
        return waiting
    if not same:
        return KIND_NEW_SESSION if effective else KIND_INITIAL
    first, last = json.loads(same[0]["progress_json"]), json.loads(same[-1]["progress_json"])
    allowance = 1 + (1 if first["working"] else 0) + max(0, observation.user - first["user"])
    if len(same) >= allowance:
        return Waiting("coalesced")
    if observation.work <= last["work"]:
        return Waiting("awaiting_progress")
    return KIND_FOLLOW_UP


def prepare(uow, facts: Facts, agent_id: str, binding: Binding | Any | None, observation: Observation, now: str,
            policy: Policy) -> Reservation | Waiting:
    """Reserve one notice for exactly this session, or say why none is due. No native I/O happens here."""
    uow.require_write()
    exact = Binding.of(binding, agent_id) if binding is not None else None
    need, scan = refresh(uow, facts, agent_id, now)
    if not scan.ids:
        if not scan.empty:
            # Nothing authorized in this window and the rest of the inbox was not read: the scan resumes on the next pass.
            note(uow, agent_id, "scan_incomplete", now)
            return Waiting("scan_incomplete")
        return Waiting("idle", status=IDLE)
    verdict = decide(uow, need, exact, observation, now, policy)
    if isinstance(verdict, Waiting):
        note(uow, agent_id, verdict.reason, now)
        return verdict
    attempt_id = new_id()
    sequence = need["next_sequence"]
    uow.connection.execute(
        "INSERT INTO inbox_nudge_attempts(id,agent_id,cycle,sequence,kind,binding_json,session_key,event_ids_json,progress_json,state,prepared_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,'prepared',?)",
        (attempt_id, agent_id, need["cycle"], sequence, verdict, json.dumps(exact.public(), sort_keys=True), exact.session_key,
         json.dumps(list(scan.ids)), json.dumps(observation.public(), sort_keys=True), now))
    uow.connection.execute("UPDATE inbox_nudge_needs SET next_sequence=next_sequence+1,reason=NULL,updated_at=? WHERE agent_id=?", (now, agent_id))
    return Reservation(attempt_id, agent_id, need["cycle"], sequence, verdict, exact, scan.ids)


def check(uow, port: Visibility, attempt_id: str, binding: Binding | Any) -> str | None:
    """Why this reservation may no longer be used, or ``None`` when it may. Read-only.

    A reservation is valid only for the exact binding it was made for, in the cycle it was made in, while some event it covers
    is still pending and authorized. Acknowledgement, revocation, a changed or ended session all make it stale."""
    attempt = attempt_row(uow, attempt_id)
    if attempt is None or attempt["state"] != PREPARED:
        return "not_reserved"
    try:
        exact = Binding.of(binding)
    except DomainError:
        return "binding_invalid"
    if exact.public() != json.loads(attempt["binding_json"]):
        return "binding_changed"
    need = need_row(uow, attempt["agent_id"])
    if need is None or not need["active"] or need["cycle"] != attempt["cycle"]:
        return "cycle_ended"
    if not alive_coverage(uow, port, attempt["agent_id"], json.loads(attempt["event_ids_json"])):
        return "coverage_handled"
    return None


# ----- reads ----------------------------------------------------------------------------------------------------------------

def candidates(uow, agent_ids: list[str]) -> list[str]:
    """The given agents that have something pending or an active need, using the indexed per-agent lookups only."""
    found = []
    for agent_id in agent_ids:
        need = uow.connection.execute("SELECT active FROM inbox_nudge_needs WHERE agent_id=?", (agent_id,)).fetchone()
        if (need is not None and need["active"]) or uow.connection.execute(
                "SELECT 1 FROM inbox_deliveries WHERE agent_id=? AND acknowledged_at IS NULL LIMIT 1", (agent_id,)).fetchone():
            found.append(agent_id)
    return found


def status(uow, port: Visibility, agent_id: str, *, limit: int = 5) -> dict:
    """What the operator may say about this agent's nudge: pending, deferred, submitted, uncertain, stale or unavailable.

    It reads the live pending set through the host's authorization, so it is never staler than the inbox, and it never claims
    the agent read anything: ``submitted`` is only the native transport's report."""
    facts = gather(uow, port, agent_id)
    need = need_row(uow, agent_id)
    attempts = [view(dict(row)) for row in uow.connection.execute(
        "SELECT * FROM inbox_nudge_attempts WHERE agent_id=? ORDER BY sequence DESC LIMIT ?", (agent_id, limit))]
    state, reason = IDLE, None
    if facts.scan.ids or not facts.scan.empty:
        state = PENDING
        latest = next((row for row in attempts if need is not None and row["cycle"] == need["cycle"]), None)
        if need is not None and need["active"] and latest is not None and facts.latest_alive is not False:
            state = {PREPARED: PENDING, SUBMITTED: STATUS_SUBMITTED, UNCERTAIN: STATUS_UNCERTAIN, DEFERRED: STATUS_DEFERRED,
                     FAILED: STATUS_DEFERRED, UNAVAILABLE: STATUS_UNAVAILABLE, STALE: STATUS_STALE}[latest["state"]]
            # A delivered notice keeps the reason the next one is held back; an undelivered one says why it was not typed.
            reason = need["reason"] if latest["state"] in EFFECTIVE + (PREPARED,) and need["reason"] else latest["reason"]
        elif need is not None and need["active"]:
            reason = need["reason"]
    return {"agent_id": agent_id, "state": state, "reason": reason, "cycle": need["cycle"] if need else 0,
            "active": state != IDLE, "pending": len(facts.scan.ids), "more": facts.scan.more,
            "opened_at": need["opened_at"] if need and need["active"] and state != IDLE else None, "attempts": attempts,
            "note": STATUS_NOTE}


__all__ = ["ATTEMPT_STATES", "Facts", "Policy", "Scan", "alive_coverage", "cancel", "candidates", "check", "decide", "gather",
           "need_row", "note", "prepare", "refresh", "scan_pending", "settle", "status", "unsettled", "view"]
