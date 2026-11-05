"""Event-based inbox notice coverage, through the public engine API over the synthetic host.

An event warrants a notice when it is delivered to the agent, unacknowledged, currently authorized and not already announced
(or possibly announced) to the same agent + provider + native conversation. Chat inputs, replies, work counters and elapsed
time neither rearm an unchanged backlog nor hold back a new event; ``Observation`` is diagnostic only. Submitted and uncertain
attempts are the coverage, across every cycle and without migration; deferred, failed, unavailable and stale attempts cover
nothing. Reading, notifying and settling never acknowledge anything.
"""

import json

import pytest

from qorqut_operations.contracts import DomainError, canonical_hash
from qorqut_operations.inbox_nudges import (FAILED_RETRY_SECONDS, MINIMUM_REPEAT_SECONDS, PENDING_SAMPLE, Binding, InboxNudges,
                                            Observation, Outcome, Policy, Reservation, Waiting)
from nudge_support import AGENT_A, AGENT_B, NudgeHost

HISTORY_TIME = "2026-10-11T12:00:00.000000Z"  # a day before the synthetic clock starts
COUNTERS = (None, Observation(), Observation(work=4, user=2, working=True))


@pytest.fixture
def host(tmp_path):
    return NudgeHost(tmp_path)


def session(agent=AGENT_A, *, conversation="conversation-x", generation=1, provider="claude", pid=4242, started="123456"):
    """One exact native binding. Coverage belongs to agent + provider + conversation; the rest only identifies the attempt."""
    return Binding(f"binding-{agent}-{provider}-{conversation}-{generation}", agent, provider, conversation, generation,
                   "terminal" if provider == "claude" else "codex_queue", pid, started)


def receipt(state, attempt_binding, reason="typed", **extra):
    return Outcome(state, reason, {"binding": attempt_binding.public(), **extra})


def reserve(host, current, observation=None):
    result = host.nudges.prepare(current.agent_id, current, observation)
    assert isinstance(result, Reservation), result
    return result


def announce(host, current, observation=None):
    """Reserve, validate immediately before input and settle one notice as submitted."""
    reservation = reserve(host, current, observation)
    assert host.nudges.validate(reservation.attempt_id, current)
    host.nudges.settle(reservation.attempt_id, receipt("submitted", current, input_id="input-" + reservation.attempt_id))
    return reservation


def quiet(host, current, observation=None):
    """No notice is due for ``current``: nothing is reserved."""
    before = len(host.attempts(current.agent_id))
    result = host.nudges.prepare(current.agent_id, current, observation)
    assert isinstance(result, Waiting), result
    assert len(host.attempts(current.agent_id)) == before
    return result


def ids(reservation):
    assert len(set(reservation.event_ids)) == len(reservation.event_ids), reservation.event_ids
    return set(reservation.event_ids)


def drain(host, current, *, patience=4, limit=80):
    """Announce to ``current`` pass after pass (each after the spacing) until ``patience`` passes in a row offer nothing.

    Returns every announced batch. A run that never stops offering notices fails: that is repeated announcement."""
    batches, idle = [], 0
    for _ in range(limit):
        host.clock.advance(MINIMUM_REPEAT_SECONDS + 1)
        result = host.nudges.prepare(current.agent_id, current)
        if isinstance(result, Waiting):
            idle += 1
            if idle >= patience:
                return batches
            continue
        idle = 0
        assert 0 < len(result.event_ids) <= PENDING_SAMPLE
        assert host.nudges.validate(result.attempt_id, current)
        host.nudges.settle(result.attempt_id, receipt("submitted", current, input_id="input-" + result.attempt_id))
        batches.append(result.event_ids)
    raise AssertionError(f"notices kept coming after {len(batches)} batches")


def announced_once(batches):
    """Every event of the batches, asserting none was announced twice."""
    flat = [event for batch in batches for event in batch]
    assert len(flat) == len(set(flat)), "an event was announced twice to the same conversation"
    return set(flat)


def unacknowledged(host, agent=AGENT_A):
    return {row["event_id"] for row in host.deliveries() if row["agent_id"] == agent and row["acknowledged_at"] is None}


def seed(host, agent, attempts, *, cycle, active=True):
    """Store a need and attempts exactly as an earlier engine release left them: rows and evidence only, no migration, no
    service call. ``attempts`` are ``(cycle, state, binding, event ids)``, oldest first."""
    with host.database.transaction() as uow:
        uow.connection.execute(
            "INSERT INTO inbox_nudge_needs(agent_id,cycle,active,opened_at,opened_at_first,updated_at,next_sequence) "
            "VALUES(?,?,?,?,?,?,?)", (agent, cycle, int(active), HISTORY_TIME, HISTORY_TIME, HISTORY_TIME, len(attempts) + 1))
        for sequence, (attempt_cycle, state, attempt_binding, event_ids) in enumerate(attempts, 1):
            attempt_id = f"history-{agent}-{sequence}"
            if state == "stale":
                evidence = {"state": "stale", "reason": "history",
                            "receipt": {"effect": "none", "binding": attempt_binding.public()}}
            else:
                evidence = receipt(state, attempt_binding, "history").evidence(attempt_binding)
            outcome = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
            uow.connection.execute(
                "INSERT INTO inbox_nudge_attempts(id,agent_id,cycle,sequence,kind,binding_json,session_key,event_ids_json,"
                "progress_json,state,reason,prepared_at,settled_at,outcome_json) VALUES(?,?,?,?,'initial',?,?,?,?,?,'history',?,?,?)",
                (attempt_id, agent, attempt_cycle, sequence, json.dumps(attempt_binding.public(), sort_keys=True),
                 attempt_binding.session_key, json.dumps(list(event_ids)), json.dumps(Observation().public(), sort_keys=True),
                 state, HISTORY_TIME, HISTORY_TIME, outcome))
            uow.connection.execute(
                "INSERT INTO inbox_nudge_evidence(attempt_id,evidence_hash,state,outcome_json,recorded_at) VALUES(?,?,?,?,?)",
                (attempt_id, canonical_hash(evidence), state, outcome, HISTORY_TIME))


# -- new events versus an announced backlog ---------------------------------------------------------------------------------

@pytest.mark.parametrize("counters", COUNTERS)
def test_a_new_event_is_announced_beside_an_old_announced_unacknowledged_one_with_counters_unchanged(host, counters):
    current = session()
    old = host.event([AGENT_A])
    assert announce(host, current, counters).event_ids == (old,)
    host.clock.advance(3600)
    new = host.event([AGENT_A])
    # No artificial delay: the last notice is long past, so the new event is announced on this pass, and only it.
    assert announce(host, current, counters).event_ids == (new,)
    host.clock.advance(3600)
    quiet(host, current, counters)
    assert unacknowledged(host) == {old, new}


def test_a_new_event_is_not_held_back_while_the_first_notice_is_being_worked_on(host):
    # The reported failure: a busy first notice and a Chat reply that never advances the work counter.
    current, busy = session(), Observation(work=7, user=3, working=True)
    old = host.event([AGENT_A])
    announce(host, current, busy)
    host.clock.advance(MINIMUM_REPEAT_SECONDS + 1)
    new = host.event([AGENT_A])
    assert announce(host, current, busy).event_ids == (new,)
    assert unacknowledged(host) == {old, new}


def test_an_unchanged_backlog_is_never_announced_again_whatever_chat_work_or_time_does(host):
    current = session()
    backlog = {host.event([AGENT_A]) for _ in range(3)}
    assert ids(announce(host, current, Observation(work=0, user=0, working=True))) == backlog
    # Work finishing, further direct Chat inputs, replies, an agent that says it will defer, and days passing.
    for step in range(1, 8):
        host.clock.advance(6 * 3600)
        quiet(host, current, Observation(work=step, user=step // 2, working=step % 2 == 1))
    quiet(host, current)
    assert len(host.attempts()) == 1 and unacknowledged(host) == backlog


def test_a_burst_is_one_notice_and_arrivals_during_the_spacing_follow_as_one_more(host):
    current = session()
    burst = {host.event([AGENT_A]) for _ in range(5)}
    assert ids(announce(host, current)) == burst
    later = set()
    for _ in range(4):
        host.clock.advance(10)
        later.add(host.event([AGENT_A]))
        quiet(host, current)                                  # inside the 60 s spacing: pending, not lost
    host.clock.advance(MINIMUM_REPEAT_SECONDS + 1 - 40)
    assert ids(announce(host, current)) == later
    host.clock.advance(3600)
    quiet(host, current)
    assert len(host.attempts()) == 2 and unacknowledged(host) == burst | later


def test_the_spacing_is_kept_between_effective_notices_and_then_delivers(host):
    current = session()
    host.event([AGENT_A])
    announce(host, current)
    host.clock.advance(10)
    new = host.event([AGENT_A])
    quiet(host, current)
    host.clock.advance(MINIMUM_REPEAT_SECONDS - 11)           # 59 s after the notice
    quiet(host, current)
    host.clock.advance(2)                                     # 61 s
    assert announce(host, current).event_ids == (new,)


def test_an_event_arriving_after_the_reservation_is_left_for_the_next_batch(host):
    current = session()
    first = host.event([AGENT_A])
    reservation = reserve(host, current)
    second = host.event([AGENT_A])
    assert host.nudges.validate(reservation.attempt_id, current)
    host.nudges.settle(reservation.attempt_id, receipt("submitted", current))
    assert reservation.event_ids == (first,)
    host.clock.advance(MINIMUM_REPEAT_SECONDS + 1)
    assert announce(host, current).event_ids == (second,)


def test_a_late_delivery_of_an_older_event_is_still_announced(host):
    current = session()
    older = host.event([AGENT_B])                             # created first, delivered to this agent only later
    newer = host.event([AGENT_A])
    assert announce(host, current).event_ids == (newer,)
    host.clock.advance(MINIMUM_REPEAT_SECONDS + 1)
    with host.database.transaction() as uow:
        uow.connection.execute("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)", (AGENT_A, older))
    assert announce(host, current).event_ids == (older,)


def test_an_earlier_event_that_becomes_visible_after_a_later_one_was_announced_is_announced(host):
    current = session()
    earlier = host.event([AGENT_A])
    host.visibility.denied.add((AGENT_A, earlier))
    later = host.event([AGENT_A])
    assert announce(host, current).event_ids == (later,)
    host.clock.advance(MINIMUM_REPEAT_SECONDS + 1)
    host.visibility.denied.discard((AGENT_A, earlier))
    assert announce(host, current).event_ids == (earlier,)


# -- acknowledgement and authorization ---------------------------------------------------------------------------------------

def test_a_partial_acknowledgement_before_input_keeps_the_notice_and_delivery_acknowledges_nothing(host):
    current = session()
    handled, remaining = host.event([AGENT_A]), host.event([AGENT_A])
    reservation = reserve(host, current)
    host.acknowledge(AGENT_A, handled)
    assert host.nudges.validate(reservation.attempt_id, current)
    host.nudges.settle(reservation.attempt_id, receipt("submitted", current))
    assert unacknowledged(host) == {remaining}
    host.clock.advance(3600)
    quiet(host, current)


def test_full_acknowledgement_or_revocation_before_input_cancels_the_notice_and_leaves_no_coverage(host):
    current = session()
    acknowledged, revoked = host.event([AGENT_A]), host.event([AGENT_A])
    reservation = reserve(host, current)
    host.acknowledge(AGENT_A, acknowledged)
    host.visibility.denied.add((AGENT_A, revoked))
    assert host.nudges.validate(reservation.attempt_id, current) is False
    assert host.attempts()[0]["state"] == "stale"
    quiet(host, current)
    # Nothing was typed, so the restored event was never announced to this conversation.
    host.visibility.denied.discard((AGENT_A, revoked))
    assert announce(host, current).event_ids == (revoked,)
    assert unacknowledged(host) == {revoked}


def test_revoking_and_restoring_access_to_an_announced_event_does_not_announce_it_again(host):
    current = session()
    announced = host.event([AGENT_A])
    announce(host, current)
    host.visibility.denied.add((AGENT_A, announced))
    host.clock.advance(3600)
    quiet(host, current)
    host.visibility.denied.discard((AGENT_A, announced))
    host.clock.advance(3600)
    quiet(host, current)
    new = host.event([AGENT_A])
    assert announce(host, current).event_ids == (new,)


# -- coverage belongs to agent + provider + conversation ---------------------------------------------------------------------

def test_restarting_the_backend_or_reattaching_the_same_conversation_never_replays_announced_events(host):
    announced = host.event([AGENT_A])
    announce(host, session())
    host.restart()
    for generation, pid in ((2, 5151), (3, 6161)):
        host.clock.advance(3600)
        quiet(host, session(generation=generation, pid=pid, started=f"70000{generation}"))
    reattached = session(generation=3, pid=6161, started="700003")
    new = host.event([AGENT_A])
    assert announce(host, reattached).event_ids == (new,)
    host.clock.advance(3600)
    quiet(host, reattached)
    assert unacknowledged(host) == {announced, new}


def test_a_genuinely_new_conversation_receives_the_outstanding_batch(host):
    first, second = host.event([AGENT_A]), host.event([AGENT_A])
    announce(host, session())
    host.clock.advance(3600)
    fresh = session(conversation="conversation-y", generation=2, pid=5151)
    assert ids(announce(host, fresh)) == {first, second}
    host.clock.advance(3600)
    quiet(host, fresh)
    quiet(host, session())


def test_coverage_is_per_provider_and_per_agent_not_per_conversation_text(host):
    shared = host.event([AGENT_A, AGENT_B])
    announce(host, session())
    host.clock.advance(3600)
    assert announce(host, session(provider="codex", generation=2, pid=5151)).event_ids == (shared,)
    assert announce(host, session(AGENT_B)).event_ids == (shared,)


def test_an_offline_agent_stays_pending_and_a_manual_start_gets_only_what_its_conversation_was_not_told(host):
    original = session()
    announced = host.event([AGENT_A])
    announce(host, original)
    new = host.event([AGENT_A])
    for _ in range(3):
        host.clock.advance(3600)
        assert isinstance(host.nudges.prepare(AGENT_A, None, Observation()), Waiting)
    assert len(host.attempts()) == 1
    # A manual start that resumes the same native conversation under a new exact binding.
    resumed = session(generation=2, pid=5151, started="654321")
    assert announce(host, resumed).event_ids == (new,)
    host.clock.advance(3600)
    quiet(host, resumed)
    # A manual start of a fresh conversation.
    fresh = session(conversation="conversation-y", generation=3, pid=6161)
    assert ids(announce(host, fresh)) == {announced, new}
    assert unacknowledged(host) == {announced, new}


def test_a_reservation_for_an_old_exact_binding_cannot_be_typed_into_its_replacement(host):
    event = host.event([AGENT_A])
    old, replacement = session(), session(generation=2, pid=5151, started="654321")
    stale = reserve(host, old)
    assert host.nudges.validate(stale.attempt_id, replacement) is False
    assert host.attempts()[0]["state"] == "stale"
    current = reserve(host, replacement)                      # nothing was typed: the event is still unannounced here
    assert current.event_ids == (event,)
    with pytest.raises(DomainError) as error:
        host.nudges.settle(current.attempt_id, receipt("submitted", old))
    assert error.value.code == "invalid_outcome" and host.attempts()[-1]["state"] == "prepared"
    assert host.nudges.validate(current.attempt_id, replacement)
    host.nudges.settle(current.attempt_id, receipt("submitted", replacement))
    host.clock.advance(3600)
    quiet(host, old)


# -- delivery outcomes --------------------------------------------------------------------------------------------------------

def test_a_failed_first_notice_covers_nothing_and_retries_after_its_backoff(host):
    current = session()
    event = host.event([AGENT_A])
    failed = reserve(host, current)
    host.nudges.settle(failed.attempt_id, receipt("failed", current, "transport_refused"))
    quiet(host, current)
    host.clock.advance(FAILED_RETRY_SECONDS + 1)
    assert announce(host, current).event_ids == (event,)


def test_no_effect_outcomes_retry_a_new_event_within_the_bounded_backoff_without_replaying_announced_ones(host):
    current = session()
    announced = host.event([AGENT_A])
    announce(host, current)
    host.clock.advance(3600)
    new = host.event([AGENT_A])
    for state in ("failed", "unavailable", "deferred", "failed", "failed", "failed"):
        attempt = reserve(host, current)
        assert attempt.event_ids == (new,)
        host.nudges.settle(attempt.attempt_id, receipt(state, current, "not_typed"))
        quiet(host, current)
        host.clock.advance(Policy().backoff_cap_seconds + 1)   # the backoff never exceeds its cap
    assert announce(host, current).event_ids == (new,)
    host.clock.advance(3600)
    quiet(host, current)
    assert unacknowledged(host) == {announced, new}


def test_an_uncertain_notice_suppresses_only_its_own_events_for_its_own_conversation(host):
    current = session()
    uncertain = host.event([AGENT_A])
    attempt = reserve(host, current)
    host.nudges.settle(attempt.attempt_id, receipt("uncertain", current, "claim_without_result"))
    host.clock.advance(3600)
    quiet(host, current)                                      # never replayed blindly to the same conversation
    new = host.event([AGENT_A])
    assert announce(host, current).event_ids == (new,)       # ... and it bans nothing else there
    host.clock.advance(3600)
    quiet(host, session(generation=2, pid=5151, started="654321"))
    assert ids(announce(host, session(conversation="conversation-y", generation=3, pid=6161))) == {uncertain, new}


def test_later_receipt_reconciliation_of_an_uncertain_notice_is_exact_and_idempotent(host):
    current = session()
    uncertain = host.event([AGENT_A])
    attempt = reserve(host, current)
    host.nudges.settle(attempt.attempt_id, receipt("uncertain", current, "claim_without_result"))
    new = host.event([AGENT_A])
    host.clock.advance(MINIMUM_REPEAT_SECONDS + 1)
    assert announce(host, current).event_ids == (new,)
    evidence = host.count("inbox_nudge_evidence")
    reconciled = host.nudges.settle(attempt.attempt_id, receipt("submitted", current, input_id="input-1"))
    assert reconciled["state"] == "submitted"
    assert host.nudges.settle(attempt.attempt_id, receipt("submitted", current, input_id="input-1")) == reconciled
    assert host.count("inbox_nudge_evidence") == evidence + 1 and host.nudges.unsettled(AGENT_A) == []
    with pytest.raises(DomainError) as error:
        host.nudges.settle(attempt.attempt_id, receipt("failed", current, "late"))
    assert error.value.code == "outcome_conflict"
    host.clock.advance(3600)
    quiet(host, current)
    assert unacknowledged(host) == {uncertain, new}


# -- historical attempts, no migration ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("active", (True, False))
def test_historical_attempts_across_cycles_suppress_their_events_without_migration(host, active):
    current, elsewhere = session(), session(conversation="conversation-old", pid=999)
    e1, e2, e3, e4, e5 = (host.event([AGENT_A]) for _ in range(5))
    seed(host, AGENT_A, [
        (1, "submitted", current, [e1]),
        (1, "failed", current, [e2]),                         # definitely not typed: covers nothing
        (2, "uncertain", current, [e3]),
        (2, "stale", current, [e2]),                          # cancelled before input: covers nothing
        (3, "submitted", elsewhere, [e4]),                    # another conversation's notice covers nothing here
    ], cycle=3, active=active)
    e6 = host.event([AGENT_A])
    history = host.attempts()
    assert announced_once(drain(host, current)) == {e2, e4, e5, e6}
    assert host.attempts()[:len(history)] == history          # neither cleared, reseeded nor rewritten
    assert announced_once(drain(host, elsewhere)) == {e1, e2, e3, e5, e6}
    assert unacknowledged(host) == {e1, e2, e3, e4, e5, e6}


def test_coverage_history_is_not_capped_to_recent_attempts(host):
    current = session()
    events = [host.event([AGENT_A]) for _ in range(150)]
    seed(host, AGENT_A, [(1 + index // 10, "submitted", current, [event]) for index, event in enumerate(events)], cycle=15)
    new = host.event([AGENT_A])
    assert drain(host, current) == [(new,)]


def test_a_covered_prefix_beyond_the_scan_budget_does_not_starve_a_later_event(host):
    current = session()
    first, last = host.bulk(AGENT_A, 2100)
    seed(host, AGENT_A, [(1, "submitted", current, list(range(start, min(start + 100, last + 1))))
                         for start in range(first, last + 1, 100)], cycle=1)
    new = host.event([AGENT_A])
    assert drain(host, current) == [(new,)]
    newer = host.event([AGENT_A])
    assert drain(host, current) == [(newer,)]


# -- bounded batches across samples and pages ---------------------------------------------------------------------------------

def test_a_backlog_beyond_one_batch_is_announced_in_bounded_batches_each_event_once(host):
    current = session()
    first, last = host.bulk(AGENT_A, 2 * PENDING_SAMPLE + 1)
    withheld = first + PENDING_SAMPLE + 10                    # unauthorized until every other event has been announced
    host.visibility.denied.add((AGENT_A, withheld))
    batches = drain(host, current)
    assert announced_once(batches) == set(range(first, last + 1)) - {withheld} and len(batches) >= 2
    host.visibility.denied.discard((AGENT_A, withheld))
    assert drain(host, current) == [(withheld,)]
    # A fresh conversation is told about the whole outstanding backlog, again in bounded batches.
    assert announced_once(drain(host, session(conversation="conversation-y", generation=2, pid=5151))) == set(range(first, last + 1))
    assert len(unacknowledged(host)) == last - first + 1


def test_an_unauthorized_prefix_beyond_the_scan_budget_does_not_skip_or_repeat_later_events(host):
    current = session()
    hidden_first, hidden_last = host.bulk(AGENT_A, 2000)
    host.visibility.denied |= {(AGENT_A, identity) for identity in range(hidden_first, hidden_last + 1)}
    first, last = host.bulk(AGENT_A, 150)
    assert announced_once(drain(host, current)) == set(range(first, last + 1))


def test_covered_unauthorized_and_eligible_events_interleaved_across_pages_reach_exactly_the_eligible(host):
    current = session()
    first, last = host.bulk(AGENT_A, 450)
    denied = {identity for identity in range(first, last + 1) if identity % 3 == 0}
    covered = sorted(identity for identity in range(first, last + 1) if identity % 3 == 1)
    eligible = set(range(first, last + 1)) - denied - set(covered)
    host.visibility.denied |= {(AGENT_A, identity) for identity in denied}
    seed(host, AGENT_A, [(1, "submitted", current, covered[start:start + 100]) for start in range(0, len(covered), 100)], cycle=1)
    assert announced_once(drain(host, current)) == eligible
    granted = max(denied)
    host.visibility.denied.discard((AGENT_A, granted))
    assert drain(host, current) == [(granted,)]


# -- one reservation, crash boundaries ----------------------------------------------------------------------------------------

def test_several_schedulers_share_one_reservation_and_one_native_input_key(host):
    current = session()
    event = host.event([AGENT_A])
    first, second = host.nudges, InboxNudges(host.database, host.visibility, clock=host.clock)
    reservation = first.prepare(AGENT_A, current, Observation())
    assert isinstance(reservation, Reservation)
    for scheduler in (first, second):
        assert isinstance(scheduler.prepare(AGENT_A, current, Observation()), Waiting)
        assert isinstance(scheduler.prepare(AGENT_A, session(conversation="conversation-y", generation=2, pid=5151)), Waiting)
    (pending,) = second.unsettled(AGENT_A)
    assert (pending["attempt_id"], pending["source_key"], pending["event_ids"]) == (reservation.attempt_id, reservation.source_key, [event])
    assert second.validate(reservation.attempt_id, current)
    outcome = receipt("submitted", current, input_id="input-1")
    assert first.settle(reservation.attempt_id, outcome) == second.settle(reservation.attempt_id, outcome)
    assert host.count("inbox_nudge_evidence") == 1 and len(host.attempts()) == 1
    host.clock.advance(3600)
    quiet(host, current)


def test_a_crash_before_the_native_claim_leaves_a_reservation_that_can_be_cancelled_and_reissued(host):
    current = session()
    event = host.event([AGENT_A])
    reservation = reserve(host, current)
    restarted = host.restart()
    (pending,) = restarted.unsettled(AGENT_A)
    assert pending["state"] == "prepared" and pending["source_key"] == reservation.source_key and pending["event_ids"] == [event]
    assert isinstance(restarted.prepare(AGENT_A, current), Waiting)          # still held: no second reservation
    assert restarted.cancel(reservation.attempt_id, "restart_unclaimed")["state"] == "stale"
    assert host.count("inbox_nudge_evidence") == 1
    again = announce(host, current)
    assert again.event_ids == (event,) and again.source_key != reservation.source_key
    assert unacknowledged(host) == {event}


def test_a_crash_after_the_native_claim_settles_from_the_receipt_and_never_resends(host):
    current = session()
    announced = host.event([AGENT_A])
    reservation = reserve(host, current)
    assert host.nudges.validate(reservation.attempt_id, current)
    # The native input was claimed and submitted, then the backend stopped before settling.
    restarted = host.restart()
    (pending,) = restarted.unsettled(AGENT_A)
    outcome = receipt("submitted", current, input_id="input-1")
    settled = restarted.settle(pending["attempt_id"], outcome)
    assert settled["state"] == "submitted" and restarted.settle(pending["attempt_id"], outcome) == settled
    assert restarted.cancel(pending["attempt_id"], "late_cancel")["state"] == "submitted"   # an effect is never rewritten
    reattached = session(generation=2, pid=5151, started="654321")
    host.clock.advance(3600)
    quiet(host, reattached)
    new = host.event([AGENT_A])
    assert announce(host, reattached).event_ids == (new,)
    assert unacknowledged(host) == {announced, new}


def test_a_crash_with_uncertain_claim_evidence_stays_uncertain_and_blocks_only_its_own_events(host):
    current = session()
    uncertain = host.event([AGENT_A])
    reservation = reserve(host, current)
    assert host.nudges.validate(reservation.attempt_id, current)
    host.restart().settle(reservation.attempt_id, receipt("uncertain", current, "claim_without_result"))
    restarted = host.restart()
    assert [row["state"] for row in restarted.unsettled(AGENT_A)] == ["uncertain"]
    reattached = session(generation=2, pid=5151, started="654321")
    host.clock.advance(3600)
    quiet(host, current)
    quiet(host, reattached)
    new = host.event([AGENT_A])
    assert announce(host, reattached).event_ids == (new,)
    final = restarted.settle(reservation.attempt_id, receipt("submitted", current, input_id="input-1"))
    assert final["state"] == "submitted" and restarted.unsettled(AGENT_A) == []
    host.clock.advance(3600)
    quiet(host, reattached)
    assert unacknowledged(host) == {uncertain, new}


# -- status ---------------------------------------------------------------------------------------------------------------------

def test_status_distinguishes_unannounced_events_from_announced_pending_work_and_uncertainty(host):
    current = session()
    first = host.event([AGENT_A])
    announce(host, current)
    view = host.nudges.status(AGENT_A)
    assert view["state"] == "submitted" and view["pending"] == 1
    host.clock.advance(10)
    second = host.event([AGENT_A])
    quiet(host, current)                                      # held by the spacing, not yet announced
    view = host.nudges.status(AGENT_A)
    assert view["state"] == "pending" and view["active"] and view["pending"] == 2
    host.clock.advance(MINIMUM_REPEAT_SECONDS)
    attempt = reserve(host, current)
    host.nudges.settle(attempt.attempt_id, receipt("uncertain", current, "claim_without_result"))
    view = host.nudges.status(AGENT_A)
    assert view["state"] == "uncertain" and view["pending"] == 2
    third = host.event([AGENT_A])
    view = host.nudges.status(AGENT_A)
    assert view["state"] == "pending" and view["pending"] == 3   # the pending count is the inbox, not the coverage
    host.acknowledge(AGENT_A, first, second, third)
    view = host.nudges.status(AGENT_A)
    assert view["state"] == "idle" and view["pending"] == 0
