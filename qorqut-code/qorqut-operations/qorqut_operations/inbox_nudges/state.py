"""Unit-of-work state of the inbox nudge engine.

One durable need per registered agent, immutable attempts and their append-only evidence. Everything here is a function of an
open unit of work and a ``Visibility`` port: no native I/O, no clock of its own, no thread. The host reserves an attempt, sends
through its one input arbiter, and settles with exactly what the transport reported.

Eligibility rule (event-based): an inbox event warrants a notice to one native conversation when it is delivered to the agent,
unacknowledged, currently authorized, and not already announced (or possibly announced) to that conversation. A conversation is
the agent, provider and conversation id of a binding; the exact binding (generation, process, transport) still decides where
one attempt may be typed and which receipt may settle it. The coverage of a conversation is every event id recorded by the
agent's ``submitted`` and ``uncertain`` attempts for it, across its whole history; ``deferred``, ``failed``, ``unavailable`` and
``stale`` attempts cover nothing. So

* an unchanged unacknowledged backlog is never announced twice to one conversation, whatever Chat input, reply, work or time
  passes, and a newly eligible event warrants its own notice independently of older pending ones;
* a new native conversation receives the outstanding eligible events; reattaching or restarting the same conversation (a new
  exact binding) replays nothing;
* an ``uncertain`` attempt suppresses only its own events for its own conversation: it is never sent again and never holds
  back a newer event.

Pacing only bounds when a due notice may be typed: one reservation per agent, ``MINIMUM_REPEAT_SECONDS`` between two effective
notices of the agent and the bounded backoff after a notice that was definitely not typed. Nothing rearms a notice: the host's
``Observation`` is recorded with each attempt for diagnostics and decides nothing, and a need's ``cycle`` only counts its
activations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any

from qorqut_operations.contracts import DomainError, canonical_hash, new_id

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


def _chunks(values: list, size: int = 100):
    for start in range(0, len(values), size):
        yield values[start:start + size]


# ----- coverage -------------------------------------------------------------------------------------------------------------

def conversation(binding: Binding) -> tuple[str, str]:
    """The coverage identity of a binding (with its agent): the provider and native conversation, not the exact process."""
    return binding.provider, binding.conversation_id


def _covering(states: tuple[str, ...] = EFFECTIVE) -> str:
    """The event ids that the agent's attempts in ``states`` recorded for one conversation (parameters: agent, provider,
    conversation id). Membership is decided in SQL over the agent's whole history, so no coverage is materialized in Python.
    The states are the engine's own literals, so they are written inline."""
    wanted = ",".join(f"'{state}'" for state in states if state in ATTEMPT_STATES)
    return ("SELECT j.value FROM inbox_nudge_attempts a, json_each(a.event_ids_json) j WHERE a.agent_id=? "
            f"AND a.state IN ({wanted}) AND json_extract(a.binding_json,'$.provider')=? "
            "AND json_extract(a.binding_json,'$.conversation_id')=? AND j.type='integer'")


def covered_among(uow, agent_id: str, coverage: tuple[str, str], event_ids, states: tuple[str, ...] = EFFECTIVE) -> set[int]:
    """Which of the given events the conversation's attempts in ``states`` already cover (submitted or uncertain by default)."""
    found: set[int] = set()
    for chunk in _chunks(list(event_ids)):
        found |= {row[0] for row in uow.connection.execute(
            _covering(states) + " AND j.value IN (" + ",".join("?" * len(chunk)) + ")", (agent_id, *coverage, *chunk))}
    return found


def has_effect(uow, agent_id: str, coverage: tuple[str, str]) -> bool:
    """Whether that one conversation of the agent has an attempt that may have been typed."""
    return uow.connection.execute(
        "SELECT 1 FROM inbox_nudge_attempts WHERE agent_id=? AND state IN ('submitted','uncertain') "
        "AND json_extract(binding_json,'$.provider')=? AND json_extract(binding_json,'$.conversation_id')=? LIMIT 1",
        (agent_id, *coverage)).fetchone() is not None


# ----- what is pending ------------------------------------------------------------------------------------------------------

def _authorized(uow, port: Visibility, agent_id: str, rows: list) -> set[int]:
    if not rows:
        return set()
    granted = port.authorized(uow, agent_id, rows)
    return {row["id"] for row in rows} & {identity for identity in granted if type(identity) is int}


def _pending_rows(uow, agent_id: str, after: int, limit: int, coverage: tuple[str, str] | None, covered: bool) -> list:
    """Pending (unacknowledged) delivery rows of the agent after event ``after``, in id order. With ``coverage`` only the events
    that conversation has not covered (or, with ``covered``, only those it has), decided in SQL so covered rows cost no page."""
    sql, args = ("SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id "
                 "WHERE d.agent_id=? AND d.acknowledged_at IS NULL AND d.event_id>?"), [agent_id, after]
    if coverage is not None:
        # An uncorrelated subquery: SQLite evaluates it once per page, above the page's starting position only.
        sql += f" AND d.event_id {'IN' if covered else 'NOT IN'} ({_covering()} AND j.value>?)"
        args += [agent_id, *coverage, after]
    return uow.connection.execute(sql + " ORDER BY d.event_id LIMIT ?", (*args, limit)).fetchall()


def scan_pending(uow, port: Visibility, agent_id: str, *, after: int = 0, limit: int = PENDING_SAMPLE,
                 budget: int = SCAN_ROWS, coverage: tuple[str, str] | None = None, covered: bool = False) -> Scan:
    """A bounded window of the agent's pending events: its unacknowledged deliveries after event ``after``, filtered by current
    authorization, in id order. Given a conversation's ``coverage`` the window holds only the events eligible for it (not yet
    covered), or with ``covered`` only those already covered.

    Scans in bounded SQL pages so unauthorized rows never consume the sample (and covered ones are not even read), and stops
    after ``budget`` rows. The result says where the next window resumes (``next``): right after the last event a full sample
    holds, the last row inspected when the budget ran out, or 0 once the last pending row was read, so a long run of
    unauthorized rows is crossed over several passes, no authorized event past a page or sample boundary is skipped, and every
    earlier row is eventually read (and authorized) again."""
    found: list[int] = []
    cursor, read = after, 0
    while read < budget:
        page = min(PAGE, budget - read)
        rows = _pending_rows(uow, agent_id, cursor, page, coverage, covered)
        read += len(rows)
        granted = _authorized(uow, port, agent_id, rows)
        for index, row in enumerate(rows):
            cursor = row["id"]
            if row["id"] in granted:
                found.append(row["id"])
                if len(found) >= limit and (index < len(rows) - 1 or len(rows) == page):
                    return Scan(tuple(found), True, True, False, after, cursor)
        if len(rows) < page:
            # The last pending row was read. Only a window that began at the start covered every pending row; one that began
            # later is empty-handed proof only when no such pending row exists at all.
            nothing = not found and (after == 0 or not _pending_rows(uow, agent_id, 0, 1, coverage, covered))
            return Scan(tuple(found), False, after != 0 and not nothing, nothing, after, 0)
    return Scan(tuple(found), False, True, False, after, cursor)


def pending_among(uow, agent_id: str, event_ids: list[int]) -> list:
    """The pending (unacknowledged) delivery rows of the agent among the given events."""
    rows: list = []
    for chunk in _chunks(list(event_ids)):
        rows += uow.connection.execute(
            "SELECT e.* FROM inbox_events e JOIN inbox_deliveries d ON d.event_id=e.id WHERE d.agent_id=? "
            "AND d.acknowledged_at IS NULL AND e.id IN (" + ",".join("?" * len(chunk)) + ")", (agent_id, *chunk)).fetchall()
    return rows


def alive_coverage(uow, port: Visibility, agent_id: str, event_ids: list[int]) -> set[int]:
    """Which of the given events are still pending (unacknowledged) and currently authorized for the agent."""
    return _authorized(uow, port, agent_id, pending_among(uow, agent_id, event_ids))


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
    the writer lock is held. ``scan`` holds the events eligible for the binding's conversation (every authorized pending event
    when the agent is offline); ``announced`` says, when nothing is eligible, that some pending authorized event is already
    covered for that conversation (or could not be proven not to be)."""

    scan: Scan
    active: bool
    announced: bool = False


def gather(uow, port: Visibility, agent_id: str, binding: Binding | None = None) -> Facts:
    need = need_row(uow, agent_id)
    coverage = conversation(binding) if binding is not None else None
    scan = scan_pending(uow, port, agent_id, after=need["scan_after"] if need is not None else 0, coverage=coverage)
    announced = False
    if coverage is not None and not scan.ids and scan.empty:
        covered = scan_pending(uow, port, agent_id, limit=1, coverage=coverage, covered=True)
        announced = bool(covered.ids) or not covered.empty
    return Facts(scan, bool(need and need["active"]), announced)


def refresh(uow, facts: Facts, agent_id: str, now: str) -> dict | None:
    """Bring the agent's need in line with what is pending. Writes only when something changed.

    The need is active while some pending, authorized event exists (eligible or already announced); each activation is a new
    ``cycle``, which is a diagnostic only. Only a proven-empty window deactivates it; a partial one proves nothing."""
    uow.require_write()
    need = need_row(uow, agent_id)
    if facts.scan.ids or facts.announced:
        if need is None:
            uow.connection.execute("INSERT INTO inbox_nudge_needs(agent_id,cycle,active,opened_at,opened_at_first,updated_at) VALUES(?,1,1,?,?,?)",
                                   (agent_id, now, now, now))
        elif not need["active"]:
            # The first activation of a row that only held a scan position is cycle one.
            uow.connection.execute("UPDATE inbox_nudge_needs SET active=1,cycle=cycle+1,opened_at=?,reason=NULL,updated_at=? WHERE agent_id=?",
                                   (now, now, agent_id))
    elif facts.scan.empty and need is not None and need["active"]:
        _stale_reservation(uow, agent_id, "no_pending_events", now)
        uow.connection.execute("UPDATE inbox_nudge_needs SET active=0,reason=NULL,updated_at=? WHERE agent_id=?", (now, agent_id))
    return need_row(uow, agent_id)


def _resume(uow, need: dict | None, agent_id: str, position: int, now: str) -> dict | None:
    """Save where the next window of this agent's scan resumes, in the same transaction as the rest of the preparation.

    This is a position, not a permission or coverage: it caches no authorization, acknowledges and announces nothing, and starts
    no notice. An agent with no row yet gets an inactive one (cycle 0) when the position matters before any authorized event
    was found."""
    if need is None:
        if position == 0:
            return None
        uow.connection.execute("INSERT INTO inbox_nudge_needs(agent_id,cycle,active,opened_at,opened_at_first,updated_at,scan_after) "
                               "VALUES(?,0,0,?,?,?,?)", (agent_id, now, now, now, position))
    elif need["scan_after"] != position:
        uow.connection.execute("UPDATE inbox_nudge_needs SET scan_after=?,updated_at=? WHERE agent_id=?", (position, now, agent_id))
    return need_row(uow, agent_id)


def note(uow, agent_id: str, reason: str | None, now: str) -> None:
    """Remember why an active need has no notice yet (offline, minimum spacing, ...). Writes only on a change."""
    need = need_row(uow, agent_id)
    if need is not None and need["active"] and need["reason"] != (reason[:200] if reason else None):
        uow.connection.execute("UPDATE inbox_nudge_needs SET reason=?,updated_at=? WHERE agent_id=?",
                               (reason[:200] if reason else None, now, agent_id))


def _retry(uow, agent_id: str, latest: dict, key: str, now: str, policy: Policy) -> Waiting | None:
    """Pace a retry after a notice that was definitely not typed (its own bounded backoff for the same exact session)."""
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


def decide(uow, need: dict, binding: Binding | None, now: str, policy: Policy) -> str | Waiting:
    """Whether a notice of events eligible for ``binding`` may be typed now, and its kind; otherwise why it waits.

    Kinds keep their stored names with event-based meanings: ``initial`` is the agent's first notice that may have been typed,
    ``new_session`` the first one to a native conversation after another conversation of the agent had one, and ``follow_up``
    a notice of newly eligible events to a conversation that already had one."""
    if binding is None:
        return Waiting("offline")
    agent_id, key = need["agent_id"], binding.session_key
    if uow.connection.execute("SELECT 1 FROM inbox_nudge_attempts WHERE agent_id=? AND state='prepared'", (agent_id,)).fetchone():
        return Waiting("reserved")
    last_effect = uow.connection.execute(
        "SELECT prepared_at,settled_at FROM inbox_nudge_attempts WHERE agent_id=? AND state IN ('submitted','uncertain') "
        "ORDER BY sequence DESC LIMIT 1", (agent_id,)).fetchone()
    if last_effect is not None:
        due = add_seconds(last_effect["settled_at"] or last_effect["prepared_at"], policy.minimum_repeat_seconds)
        if parse_time(now) < parse_time(due):
            return Waiting("minimum_spacing", due)
    latest = uow.connection.execute("SELECT * FROM inbox_nudge_attempts WHERE agent_id=? ORDER BY sequence DESC LIMIT 1",
                                    (agent_id,)).fetchone()
    if latest is not None and (waiting := _retry(uow, agent_id, dict(latest), key, now, policy)) is not None:
        return waiting
    if last_effect is None:
        return KIND_INITIAL
    return KIND_FOLLOW_UP if has_effect(uow, agent_id, conversation(binding)) else KIND_NEW_SESSION


def prepare(uow, facts: Facts, agent_id: str, binding: Binding | Any | None, observation: Observation, now: str,
            policy: Policy) -> Reservation | Waiting:
    """Reserve one notice of the events eligible for exactly this session, or say why none is due. No native I/O happens here.

    ``observation`` is stored with the attempt for diagnostics only."""
    uow.require_write()
    exact = Binding.of(binding, agent_id) if binding is not None else None
    scan = facts.scan
    need = refresh(uow, facts, agent_id, now)
    if not scan.ids:
        _resume(uow, need, agent_id, scan.next, now)
        if not scan.empty:
            # Nothing authorized in this window and the rest of the inbox was not read: the scan resumes on the next pass.
            note(uow, agent_id, "scan_incomplete", now)
            return Waiting("scan_incomplete")
        if facts.announced:
            # Every pending authorized event is already covered for this conversation: nothing is announced again.
            note(uow, agent_id, "coalesced", now)
            return Waiting("coalesced")
        return Waiting("idle", status=IDLE)
    verdict = decide(uow, need, exact, now, policy)
    if isinstance(verdict, Waiting):
        # The position stays where this window began, so the events it found are the ones reserved once a notice is due, with
        # anything that arrives meanwhile.
        note(uow, agent_id, verdict.reason, now)
        return verdict
    # A concurrent caller may have covered some of these events since they were read: never announce one twice.
    covered = covered_among(uow, agent_id, conversation(exact), scan.ids)
    event_ids = tuple(identity for identity in scan.ids if identity not in covered)
    if not event_ids:
        note(uow, agent_id, "coalesced", now)
        return Waiting("coalesced")
    need = _resume(uow, need, agent_id, scan.next, now)
    attempt_id = new_id()
    sequence = need["next_sequence"]
    uow.connection.execute(
        "INSERT INTO inbox_nudge_attempts(id,agent_id,cycle,sequence,kind,binding_json,session_key,event_ids_json,progress_json,state,prepared_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,'prepared',?)",
        (attempt_id, agent_id, need["cycle"], sequence, verdict, json.dumps(exact.public(), sort_keys=True), exact.session_key,
         json.dumps(list(event_ids)), json.dumps(observation.public(), sort_keys=True), now))
    uow.connection.execute("UPDATE inbox_nudge_needs SET next_sequence=next_sequence+1,reason=NULL,updated_at=? WHERE agent_id=?", (now, agent_id))
    return Reservation(attempt_id, agent_id, need["cycle"], sequence, verdict, exact, event_ids)


def check(uow, port: Visibility, attempt_id: str, binding: Binding | Any) -> str | None:
    """Why this reservation may no longer be used, or ``None`` when it may. Read-only.

    A reservation is valid only for the exact binding it was made for while every event it covers that is still pending is
    still eligible (authorized and not covered for its conversation by another attempt), and at least one is. An acknowledged
    event simply drops out; a pending covered event that was revoked makes the whole reservation stale, so a delivered notice
    never records coverage for an event the agent could not see. A changed or ended session makes it stale too."""
    attempt = attempt_row(uow, attempt_id)
    if attempt is None or attempt["state"] != PREPARED:
        return "not_reserved"
    try:
        exact = Binding.of(binding)
    except DomainError:
        return "binding_invalid"
    if exact.public() != json.loads(attempt["binding_json"]):
        return "binding_changed"
    rows = pending_among(uow, attempt["agent_id"], json.loads(attempt["event_ids_json"]))
    pending = {row["id"] for row in rows}
    eligible = (_authorized(uow, port, attempt["agent_id"], rows)
                - covered_among(uow, attempt["agent_id"], conversation(exact), pending))
    if not eligible or eligible != pending:
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


def status(uow, port: Visibility, agent_id: str, *, limit: int = 5, binding: Binding | Any | None = None) -> dict:
    """What the operator may say about this agent's nudge: pending, deferred, submitted, uncertain, stale or unavailable.

    Coverage is read for the conversation of ``binding`` when the host supplies its current live binding (it must be this
    agent's), otherwise for the conversation of the agent's latest attempt. ``submitted`` (or ``uncertain``) means every pending
    authorized event is already announced (or possibly announced) to it; when some event is not, the state is ``pending`` or
    the latest undelivered attempt's state, never ``submitted``. The scans are bounded: when the announced scan could not read
    every covered event and none it read is only possibly announced, the state is ``pending`` (``scan_incomplete``) rather than
    a conclusive ``submitted``. ``pending`` counts the authorized pending events seen and is distinct from the coverage counts;
    ``more`` says either scan stopped early, so the counts are lower bounds. It reads the live pending set through the host's
    authorization, so it is never staler than the inbox, and it never claims the agent read anything: ``submitted`` is only the
    native transport's report."""
    exact = Binding.of(binding, agent_id) if binding is not None else None
    need = need_row(uow, agent_id)
    attempts = [view(dict(row)) for row in uow.connection.execute(
        "SELECT * FROM inbox_nudge_attempts WHERE agent_id=? ORDER BY sequence DESC LIMIT ?", (agent_id, max(1, limit)))]
    latest = attempts[0] if attempts else None
    if exact is not None:
        coverage = conversation(exact)
    else:
        coverage = conversation(Binding.of(latest["binding"])) if latest is not None else None
    unannounced = scan_pending(uow, port, agent_id, coverage=coverage)
    announced = (scan_pending(uow, port, agent_id, coverage=coverage, covered=True) if coverage is not None
                 else Scan((), False, False, True))
    possibly = set(announced.ids) - covered_among(uow, agent_id, coverage, announced.ids, (SUBMITTED,)) if announced.ids else set()
    reason = need["reason"] if need is not None and need["active"] else None
    state = IDLE
    if unannounced.ids or not unannounced.empty:
        state = PENDING
        if latest is not None and latest["state"] not in EFFECTIVE:
            # The latest attempt was meant for events that still wait: an undelivered one says why it was not typed.
            state = {PREPARED: PENDING, DEFERRED: STATUS_DEFERRED, FAILED: STATUS_DEFERRED, UNAVAILABLE: STATUS_UNAVAILABLE,
                     STALE: STATUS_STALE}[latest["state"]]
            reason = latest["reason"] or reason
    elif possibly:
        state = STATUS_UNCERTAIN
        reason = reason or latest["reason"]
    elif announced.incomplete:
        # Covered events the scan did not reach could be only possibly announced: nothing conclusive can be said.
        state, reason = PENDING, "scan_incomplete"
    elif announced.ids:
        state = STATUS_SUBMITTED
        reason = reason or latest["reason"]
    else:
        reason = None
    return {"agent_id": agent_id, "state": state, "reason": reason, "cycle": need["cycle"] if need else 0,
            "active": state != IDLE, "pending": len(unannounced.ids) + len(announced.ids),
            "more": unannounced.incomplete or announced.incomplete, "unannounced": len(unannounced.ids),
            "announced": len(announced.ids) - len(possibly), "possibly_announced": len(possibly),
            "opened_at": need["opened_at"] if need and need["active"] and state != IDLE else None,
            "attempts": attempts[:limit], "note": STATUS_NOTE}


__all__ = ["ATTEMPT_STATES", "Facts", "Policy", "Scan", "alive_coverage", "cancel", "candidates", "check", "conversation",
           "covered_among", "decide", "gather", "has_effect", "need_row", "note", "pending_among", "prepare", "refresh",
           "scan_pending", "settle", "status", "unsettled", "view"]
