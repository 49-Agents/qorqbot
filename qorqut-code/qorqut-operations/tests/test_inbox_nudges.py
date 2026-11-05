"""The inbox nudge engine over a synthetic host: durable need, immutable attempts, exact receipts and event-based eligibility."""

import json

import pytest

from qorqut_operations.contracts import DomainError
from qorqut_operations.inbox_nudges import (NUDGE_TEXT, Binding, Observation, Outcome, Policy, Reservation, Waiting,
                                            source_key)
from nudge_support import AGENT_A, AGENT_B, SHORT_AGENT, NudgeHost, binding

EARLIER = "2026-10-11T12:00:00.000000Z"


@pytest.fixture
def host(tmp_path):
    return NudgeHost(tmp_path)


def submitted(attempt_binding, **extra):
    return Outcome("submitted", "typed", {"input_id": "input-1", "binding": attempt_binding.public(), **extra})


def reserve(host, agent=AGENT_A, current=None, observation=None):
    result = host.nudges.prepare(agent, current if current is not None else binding(agent), observation)
    assert isinstance(result, Reservation), result
    return result


def deliver(host, agent=AGENT_A, current=None, observation=None):
    reservation = reserve(host, agent, current, observation)
    assert host.nudges.validate(reservation.attempt_id, reservation.binding)
    host.nudges.settle(reservation.attempt_id, submitted(reservation.binding))
    return reservation


def same_conversation(generation, *, pid=5151, agent=AGENT_A):
    """A new exact binding (generation, process, binding id) of the SAME native conversation as ``binding(agent)``: a reattach
    or a backend restart, not a new conversation."""
    first = binding(agent)
    return Binding(f"binding-{agent}-reattach-{generation}", agent, first.provider, first.conversation_id, generation,
                   first.transport, pid, "654321")


def historical(host, agent, attempts, *, cycle=3):
    """Rows as an earlier engine left them (cycles, progress observations, kinds), inserted as they are: nothing is migrated,
    cleared or reseeded. ``attempts`` holds (state, binding, event ids, cycle) tuples."""
    with host.database.transaction() as uow:
        uow.connection.execute("INSERT INTO inbox_nudge_needs(agent_id,cycle,active,opened_at,opened_at_first,updated_at,next_sequence) "
                               "VALUES(?,?,0,?,?,?,?)", (agent, cycle, EARLIER, EARLIER, EARLIER, len(attempts) + 1))
        for sequence, (state, current, event_ids, attempt_cycle) in enumerate(attempts, 1):
            uow.connection.execute(
                "INSERT INTO inbox_nudge_attempts(id,agent_id,cycle,sequence,kind,binding_json,session_key,event_ids_json,progress_json,"
                "state,reason,prepared_at,settled_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"historical-{sequence:04d}", agent, attempt_cycle, sequence, "initial" if sequence == 1 else "follow_up",
                 json.dumps(current.public(), sort_keys=True), current.session_key, json.dumps(list(event_ids)),
                 json.dumps({"work": 7, "user": 2, "working": True}, sort_keys=True), state, "typed", EARLIER, EARLIER))


# -- one need, coalescing ---------------------------------------------------------------------------------------------------

def test_events_coalesce_into_one_reservation_and_a_later_event_gets_its_own_notice(host):
    first, second, third = (host.event([AGENT_A]) for _ in range(3))
    reservation = reserve(host)
    assert reservation.kind == "initial" and reservation.event_ids == (first, second, third)
    assert reservation.text == NUDGE_TEXT and reservation.source_key == source_key(reservation.attempt_id)
    # While the notice is reserved no second one can be made.
    assert host.nudges.prepare(AGENT_A, binding()) == Waiting("reserved")
    host.nudges.validate(reservation.attempt_id, reservation.binding)
    host.nudges.settle(reservation.attempt_id, submitted(reservation.binding))
    host.clock.advance(3600)
    assert host.nudges.prepare(AGENT_A, binding()).reason == "coalesced"      # the announced backlog is not announced again
    later = host.event([AGENT_A])                                              # ... but a new event is, alone
    follow = reserve(host)
    assert follow.kind == "follow_up" and follow.event_ids == (later,)
    assert len(host.attempts()) == 2 and host.count("inbox_nudge_needs") == 1


def test_an_event_arriving_after_the_reservation_is_left_for_the_next_batch(host):
    first = host.event([AGENT_A])
    reservation = reserve(host)
    late = host.event([AGENT_A])
    assert host.nudges.validate(reservation.attempt_id, reservation.binding)         # still exactly the reserved batch
    host.nudges.settle(reservation.attempt_id, submitted(reservation.binding))
    assert reservation.event_ids == (first,)
    host.clock.advance(61)
    assert reserve(host).event_ids == (late,)


def test_the_notice_names_no_event_and_a_need_stores_no_summary(host):
    host.event([AGENT_A], summary="TOP-SECRET-SUMMARY")
    reservation = deliver(host)
    assert "TOP-SECRET" not in NUDGE_TEXT
    with host.database.read() as uow:
        stored = "".join(str(tuple(row)) for table in ("inbox_nudge_needs", "inbox_nudge_attempts", "inbox_nudge_evidence")
                         for row in uow.connection.execute(f"SELECT * FROM {table}"))
    assert "TOP-SECRET" not in stored and reservation.attempt_id in stored


def test_nothing_pending_is_idle_and_creates_no_rows(host):
    assert host.nudges.prepare(AGENT_A, binding()) == Waiting("idle", status="idle")
    assert host.count("inbox_nudge_needs") == host.count("inbox_nudge_attempts") == 0
    assert host.nudges.candidates([AGENT_A, AGENT_B]) == []
    host.event([AGENT_B])
    assert host.nudges.candidates([AGENT_A, AGENT_B, SHORT_AGENT]) == [AGENT_B]


# -- authorization ---------------------------------------------------------------------------------------------------------

def test_only_currently_authorized_events_are_covered_and_unavailable_ones_stay_silent(host):
    hidden, visible = host.event([AGENT_A]), host.event([AGENT_A])
    host.visibility.denied.add((AGENT_A, hidden))
    reservation = reserve(host)
    assert reservation.event_ids == (visible,)
    with host.database.read() as uow:
        assert json.loads(uow.connection.execute("SELECT event_ids_json FROM inbox_nudge_attempts").fetchone()[0]) == [visible]
    assert hidden != visible


def test_a_need_with_only_unauthorized_events_is_idle(host):
    event = host.event([AGENT_A])
    host.visibility.denied.add((AGENT_A, event))
    assert host.nudges.prepare(AGENT_A, binding()).reason == "idle"
    assert host.nudges.status(AGENT_A)["state"] == "idle"


def test_authorization_is_asked_in_a_read_not_inside_the_writer(host):
    host.event([AGENT_A])
    reserve(host)
    assert host.visibility.asked and not any(host.visibility.in_write)


def test_revoked_access_cancels_the_reservation_before_input(host):
    event = host.event([AGENT_A])
    reservation = reserve(host)
    host.visibility.denied.add((AGENT_A, event))
    assert host.nudges.validate(reservation.attempt_id, reservation.binding) is False
    (attempt,) = host.attempts()
    assert attempt["state"] == "stale" and attempt["reason"] == "coverage_handled"
    assert host.nudges.unsettled() == []


def test_acknowledgement_before_input_cancels_the_reservation_and_ends_the_cycle(host):
    event = host.event([AGENT_A])
    reservation = reserve(host)
    host.acknowledge(AGENT_A, event)
    assert host.nudges.validate(reservation.attempt_id, reservation.binding) is False
    assert host.attempts()[0]["state"] == "stale"
    assert host.nudges.prepare(AGENT_A, binding()).reason == "idle"
    assert host.nudges.status(AGENT_A)["state"] == "idle"


def test_an_unrelated_acknowledgement_keeps_the_reservation_valid(host):
    handled, remaining = host.event([AGENT_A]), host.event([AGENT_A])
    reservation = reserve(host)
    host.acknowledge(AGENT_A, handled)
    assert host.nudges.validate(reservation.attempt_id, reservation.binding) is True
    assert host.attempts()[0]["state"] == "prepared" and remaining in reservation.event_ids


def test_partial_revocation_before_input_cancels_the_notice_so_it_never_covers_the_revoked_event(host):
    revoked, remaining = host.event([AGENT_A]), host.event([AGENT_A])
    reservation = reserve(host)
    host.visibility.denied.add((AGENT_A, revoked))
    assert host.nudges.validate(reservation.attempt_id, reservation.binding) is False
    assert host.attempts()[0]["state"] == "stale" and host.attempts()[0]["reason"] == "coverage_handled"
    # The next pass reserves exactly what is eligible now; the revoked event is not recorded as announced.
    again = deliver(host)
    assert again.event_ids == (remaining,) and again.kind == "initial"
    # When access returns, the event was never announced to this conversation, so it gets its own notice.
    host.visibility.denied.discard((AGENT_A, revoked))
    host.clock.advance(61)
    assert reserve(host).event_ids == (revoked,)
    assert all(row["acknowledged_at"] is None for row in host.deliveries())


def test_the_engine_never_changes_acknowledgement_state(host):
    host.event([AGENT_A])
    host.event([AGENT_A, AGENT_B])
    before = host.deliveries()
    deliver(host)
    host.nudges.prepare(AGENT_B, binding(AGENT_B))
    host.nudges.status(AGENT_A)
    assert host.deliveries() == before


# -- exact bindings and generations -----------------------------------------------------------------------------------------

def test_a_changed_session_aborts_the_old_reservation(host):
    host.event([AGENT_A])
    reservation = reserve(host)
    assert host.nudges.validate(reservation.attempt_id, binding(generation=2)) is False
    assert host.attempts()[0]["state"] == "stale" and host.attempts()[0]["reason"] == "binding_changed"


def test_a_new_native_conversation_gets_the_outstanding_batch_not_a_replay_of_a_receipt(host):
    event = host.event([AGENT_A])
    old = deliver(host)
    host.clock.advance(61)
    new = reserve(host, current=binding(generation=2))                   # generation 2 is another native conversation here
    assert new.kind == "new_session" and new.binding.generation == 2 and new.attempt_id != old.attempt_id
    assert new.event_ids == (event,) and new.binding.conversation_id != old.binding.conversation_id
    # The old generation's receipt can no longer settle anything for the new attempt.
    with pytest.raises(DomainError) as error:
        host.nudges.settle(new.attempt_id, submitted(old.binding))
    assert error.value.code == "invalid_outcome"
    host.nudges.settle(new.attempt_id, submitted(new.binding))
    assert [row["state"] for row in host.attempts()] == ["submitted", "submitted"]


def test_reattaching_or_restarting_the_same_conversation_replays_nothing_and_old_bindings_cannot_type(host):
    old_event = host.event([AGENT_A])
    old = deliver(host)
    host.clock.advance(3600)
    host.restart()                                                       # the backend restarts; nothing in memory survives
    reattached = same_conversation(2)
    assert host.nudges.prepare(AGENT_A, reattached).reason == "coalesced"
    new_event = host.event([AGENT_A])
    reservation = reserve(host, current=reattached)
    assert reservation.event_ids == (new_event,) and reservation.kind == "follow_up" and old_event not in reservation.event_ids
    # The old exact binding can neither pass the check before input nor settle the new attempt.
    assert host.nudges.validate(reservation.attempt_id, old.binding) is False
    assert host.attempts()[-1]["state"] == "stale" and host.attempts()[-1]["reason"] == "binding_changed"
    again = reserve(host, current=reattached)
    with pytest.raises(DomainError) as error:
        host.nudges.settle(again.attempt_id, submitted(old.binding))
    assert error.value.code == "invalid_outcome"


def test_agent_ids_of_any_shape_are_exact_and_other_agents_bindings_are_refused(host):
    host.event([SHORT_AGENT])
    reservation = reserve(host, SHORT_AGENT)
    assert reservation.agent_id == SHORT_AGENT and reservation.binding.agent_id == SHORT_AGENT
    with pytest.raises(DomainError) as error:
        host.nudges.prepare(AGENT_A, binding(SHORT_AGENT))
    assert error.value.code == "invalid_binding"
    for bad in ({"agent_id": ""}, {"provider": "other"}, {"generation": 0}, {"transport": "pipe"}):
        values = {**binding().public(), **bad}
        with pytest.raises(DomainError):
            Binding.of(values)
    assert Binding.of(binding().public()) == binding()


# -- delivery states --------------------------------------------------------------------------------------------------------

def test_a_deferred_notice_changes_nothing_else_and_retries_only_after_its_backoff(host):
    event = host.event([AGENT_A])
    reservation = reserve(host)
    host.nudges.settle(reservation.attempt_id, Outcome("deferred", "composer_occupied",
                                                       {"binding": reservation.binding.public()}))
    assert host.nudges.status(AGENT_A)["state"] == "deferred"
    waiting = host.nudges.prepare(AGENT_A, binding())
    assert waiting.reason == "retry_later" and waiting.not_before
    host.clock.advance(6)
    second = reserve(host)
    assert second.kind == "initial" and second.event_ids == (event,)
    # A second deferral doubles the wait, bounded by the cap.
    host.nudges.settle(second.attempt_id, Outcome("deferred", "composer_occupied", {"binding": second.binding.public()}))
    host.clock.advance(6)
    assert host.nudges.prepare(AGENT_A, binding()).reason == "retry_later"
    host.clock.advance(5)
    assert isinstance(host.nudges.prepare(AGENT_A, binding()), Reservation)


def test_unsupported_transport_stays_visibly_unavailable_and_never_claims_support(host):
    host.event([AGENT_A])
    reservation = reserve(host)
    host.nudges.settle(reservation.attempt_id, Outcome("unavailable", "no_safe_submission",
                                                       {"binding": reservation.binding.public()}))
    view = host.nudges.status(AGENT_A)
    assert view["state"] == "unavailable" and view["reason"] == "no_safe_submission" and "does not mean the agent read" in view["note"]


def test_uncertain_effects_are_never_resent_and_never_hold_back_a_newer_event(host):
    host.event([AGENT_A])
    reservation = reserve(host)
    host.nudges.settle(reservation.attempt_id, Outcome("uncertain", "claim_without_result", {"binding": reservation.binding.public()}))
    assert host.nudges.status(AGENT_A)["state"] == "uncertain"
    assert [row["attempt_id"] for row in host.nudges.unsettled()] == [reservation.attempt_id]
    host.clock.advance(3600)
    assert host.nudges.prepare(AGENT_A, binding()).reason == "coalesced"     # possibly announced: never replayed
    newer = host.event([AGENT_A])
    follow = reserve(host)
    assert follow.event_ids == (newer,) and follow.kind == "follow_up"
    host.nudges.settle(follow.attempt_id, submitted(follow.binding))
    # Exact evidence later shows it was in fact submitted; that is the only allowed change, and a repeat is a no-op.
    first = host.nudges.settle(reservation.attempt_id, submitted(reservation.binding))
    assert host.nudges.settle(reservation.attempt_id, submitted(reservation.binding)) == first
    assert host.attempts()[0]["state"] == "submitted" and host.nudges.unsettled() == []
    with pytest.raises(DomainError) as error:
        host.nudges.settle(reservation.attempt_id, Outcome("failed", "late", {"binding": reservation.binding.public()}))
    assert error.value.code == "outcome_conflict"
    host.clock.advance(3600)
    assert host.nudges.prepare(AGENT_A, binding()).reason == "coalesced" and len(host.attempts()) == 2


def test_a_repeated_identical_receipt_is_a_noop_and_a_conflicting_one_is_refused(host):
    host.event([AGENT_A])
    reservation = reserve(host)
    first = host.nudges.settle(reservation.attempt_id, submitted(reservation.binding))
    again = host.nudges.settle(reservation.attempt_id, submitted(reservation.binding))
    assert first == again and host.count("inbox_nudge_evidence") == 1
    with pytest.raises(DomainError):
        host.nudges.settle(reservation.attempt_id, Outcome("deferred", "composer_occupied", {"binding": reservation.binding.public()}))


def test_a_receipt_for_another_session_cannot_settle_the_attempt(host):
    host.event([AGENT_A])
    reservation = reserve(host)
    with pytest.raises(DomainError) as error:
        host.nudges.settle(reservation.attempt_id, submitted(binding(generation=9)))
    assert error.value.code == "invalid_outcome" and host.attempts()[0]["state"] == "prepared"


def test_restart_keeps_reservations_uncertainty_and_the_need(host):
    host.event([AGENT_A])
    reservation = reserve(host)
    restarted = host.restart()
    assert [row["state"] for row in restarted.unsettled(AGENT_A)] == ["prepared"]
    assert restarted.prepare(AGENT_A, binding()).reason == "reserved"
    restarted.settle(reservation.attempt_id, Outcome("uncertain", "restart_claim", {"binding": reservation.binding.public()}))
    again = host.restart()
    host.clock.advance(3600)
    assert again.prepare(AGENT_A, binding()).reason == "coalesced"
    assert again.status(AGENT_A)["state"] == "uncertain"


def test_an_unclaimed_reservation_after_a_restart_can_be_cancelled_without_a_trace_of_input(host):
    host.event([AGENT_A])
    reservation = reserve(host)
    host.restart().cancel(reservation.attempt_id, "restart_unclaimed")
    (attempt,) = host.attempts()
    assert attempt["state"] == "stale" and attempt["reason"] == "restart_unclaimed"
    host.clock.advance(1)
    assert isinstance(host.nudges.prepare(AGENT_A, binding()), Reservation)


# -- offline, manual binding ------------------------------------------------------------------------------------------------

def test_an_offline_agent_keeps_a_durable_pending_need_until_a_manual_binding(host):
    event = host.event([AGENT_A])
    for _ in range(3):
        host.clock.advance(3600)
        assert host.nudges.prepare(AGENT_A, None).reason == "offline"
    assert host.count("inbox_nudge_attempts") == 0
    view = host.nudges.status(AGENT_A)
    assert view["state"] == "pending" and view["reason"] == "offline" and view["active"]
    first = reserve(host)
    assert first.kind == "initial" and first.event_ids == (event,)


# -- event-based eligibility -----------------------------------------------------------------------------------------------

def test_an_announced_backlog_is_never_repeated_whatever_chat_work_or_time_passes(host):
    host.event([AGENT_A])
    deliver(host, observation=Observation(work=3, user=1))
    for step in range(1, 6):
        host.clock.advance(24 * 3600)
        busy = Observation(work=3 + step, user=1 + step, working=step % 2 == 0)
        assert host.nudges.prepare(AGENT_A, binding(), busy).reason == "coalesced"
    assert len(host.attempts()) == 1


def test_a_new_event_is_announced_alone_while_the_old_one_stays_pending_and_the_counters_do_not_move(host):
    old = host.event([AGENT_A])
    same = Observation(work=1, user=1)
    deliver(host, observation=same)
    host.clock.advance(120)
    new = host.event([AGENT_A])
    follow = reserve(host, observation=same)
    assert follow.kind == "follow_up" and follow.event_ids == (new,)
    host.nudges.settle(follow.attempt_id, submitted(follow.binding))
    host.clock.advance(3600)
    assert host.nudges.prepare(AGENT_A, binding(), same).reason == "coalesced"
    assert {row["acknowledged_at"] for row in host.deliveries()} == {None} and old not in follow.event_ids


def test_the_observation_is_a_recorded_diagnostic_and_decides_nothing(host):
    host.event([AGENT_A])
    deliver(host, observation=Observation(work=9, user=4, working=True))
    assert json.loads(host.attempts()[0]["progress_json"]) == {"work": 9, "user": 4, "working": True}
    host.clock.advance(61)
    new = host.event([AGENT_A])
    neutral = reserve(host)                                             # the host may pass nothing at all
    assert neutral.event_ids == (new,) and json.loads(host.attempts()[1]["progress_json"]) == Observation().public()


def test_events_arriving_during_the_minimum_spacing_wait_and_are_then_announced_together(host):
    host.event([AGENT_A])
    deliver(host)
    host.clock.advance(10)
    second = host.event([AGENT_A])
    waiting = host.nudges.prepare(AGENT_A, binding())
    assert waiting.reason == "minimum_spacing" and waiting.not_before
    host.clock.advance(20)
    third = host.event([AGENT_A])
    assert host.nudges.prepare(AGENT_A, binding()).reason == "minimum_spacing"
    host.clock.advance(31)
    assert reserve(host).event_ids == (second, third)


def test_the_spacing_floor_is_configurable_but_never_enables_a_timer_rearm(tmp_path):
    host = NudgeHost(tmp_path, policy=Policy(minimum_repeat_seconds=0))
    host.event([AGENT_A])
    deliver(host, observation=Observation(work=1, user=1))
    assert host.nudges.prepare(AGENT_A, binding(), Observation(work=1, user=1)).reason == "coalesced"
    new = host.event([AGENT_A])
    assert reserve(host).event_ids == (new,)                             # no spacing: a new event is announced at once
    with pytest.raises(ValueError):
        Policy(minimum_repeat_seconds=-1)


def test_a_notice_that_was_not_typed_is_retried_and_its_events_are_then_announced_once(host):
    event = host.event([AGENT_A])
    first = reserve(host, observation=Observation(work=1, user=1, working=True))
    host.nudges.settle(first.attempt_id, Outcome("deferred", "composer_occupied", {"binding": first.binding.public()}))
    host.clock.advance(6)
    retry = reserve(host, observation=Observation(work=1, user=1, working=True))
    assert retry.kind == "initial" and retry.event_ids == (event,)       # a no-effect attempt covers nothing
    host.nudges.settle(retry.attempt_id, submitted(retry.binding))
    for work in (2, 3, 4):
        host.clock.advance(3600)
        assert host.nudges.prepare(AGENT_A, binding(), Observation(work=work, user=work)).reason == "coalesced"


def test_handled_coverage_ends_the_need_and_the_next_event_starts_a_new_activation(host):
    first = host.event([AGENT_A])
    deliver(host, observation=Observation(work=1, user=1))
    host.acknowledge(AGENT_A, first)
    assert host.nudges.prepare(AGENT_A, binding()).reason == "idle"
    host.clock.advance(61)
    host.event([AGENT_A])
    second = reserve(host, observation=Observation(work=1, user=1))
    assert second.kind == "follow_up" and second.cycle == 2               # the cycle counts activations; it gates nothing


def test_handling_announced_events_neither_rearms_nor_holds_back_newer_ones(host):
    first = host.event([AGENT_A])
    deliver(host, observation=Observation(work=1, user=1))
    newer = host.event([AGENT_A])
    host.acknowledge(AGENT_A, first)
    host.clock.advance(61)
    reservation = reserve(host, observation=Observation(work=1, user=1))
    assert reservation.cycle == 1 and reservation.event_ids == (newer,)


def test_historical_attempts_of_every_cycle_cover_their_events_without_migration(host):
    old_a, old_b, old_c, newer = (host.event([AGENT_A]) for _ in range(4))
    other = binding(generation=7)                                        # another conversation of the same agent
    historical(host, AGENT_A, [("submitted", binding(), [old_a], 1), ("uncertain", binding(), [old_b], 2),
                               ("failed", binding(), [old_c], 2), ("submitted", other, [old_c, newer], 3)])
    reservation = reserve(host)
    # Earlier cycles still count; a failed attempt and another conversation's notice cover nothing for this one.
    assert reservation.event_ids == (old_c, newer) and reservation.kind == "follow_up" and reservation.sequence == 5
    assert host.need()["cycle"] == 4                                     # nothing was cleared or reseeded
    host.nudges.settle(reservation.attempt_id, submitted(reservation.binding))
    host.clock.advance(61)
    assert host.nudges.prepare(AGENT_A, binding()).reason == "coalesced"
    # Coverage is per conversation: the other one was never told of the first two events.
    assert reserve(host, current=other).event_ids == (old_a, old_b)


def test_concurrent_preparations_never_reserve_or_announce_the_same_events_twice(tmp_path):
    from qorqut_operations.inbox_nudges import state

    host = NudgeHost(tmp_path, policy=Policy(minimum_repeat_seconds=0))
    event = host.event([AGENT_A])
    with host.database.read() as uow:
        early, late = (state.gather(uow, host.visibility, AGENT_A, binding()) for _ in range(2))
    assert early.scan.ids == late.scan.ids == (event,)
    with host.database.transaction() as uow:
        first = state.prepare(uow, early, AGENT_A, binding(), Observation(), host.nudges.now(), Policy(minimum_repeat_seconds=0))
    with host.database.transaction() as uow:
        assert state.prepare(uow, late, AGENT_A, binding(), Observation(), host.nudges.now(),
                             Policy(minimum_repeat_seconds=0)) == Waiting("reserved")
    host.nudges.settle(first.attempt_id, submitted(first.binding))
    # Read before the first notice was settled, the second caller's facts still name the event: it is not reserved again.
    with host.database.transaction() as uow:
        assert state.prepare(uow, late, AGENT_A, binding(), Observation(), host.nudges.now(),
                             Policy(minimum_repeat_seconds=0)) == Waiting("coalesced")
    assert len(host.attempts()) == 1


def test_a_full_sample_resumes_right_after_its_last_event_so_no_page_tail_is_skipped(tmp_path):
    host = NudgeHost(tmp_path, policy=Policy(minimum_repeat_seconds=0))
    first, last = host.bulk(AGENT_A, 150)                                # one page holds more than one sample
    batch = deliver(host)
    assert batch.event_ids == tuple(range(first, first + 100))
    assert host.need()["scan_after"] == first + 99
    rest = deliver(host)
    assert rest.event_ids == tuple(range(first + 100, last + 1))         # the very next pass, not after a wrap
    assert host.nudges.prepare(AGENT_A, binding()).reason == "coalesced"


def test_a_covered_prefix_larger_than_the_scan_budget_never_delays_a_new_event(host):
    first, last = host.bulk(AGENT_A, 2500)
    historical(host, AGENT_A, [("submitted", binding(), list(range(first, last + 1)), 1)])
    assert host.nudges.prepare(AGENT_A, binding()).reason == "coalesced"
    new = host.event([AGENT_A])
    host.visibility.asked.clear()
    reservation = reserve(host)
    assert reservation.event_ids == (new,)
    # The eligible scan never read the covered rows: no authorization was asked for any of them.
    assert not any(first <= identity <= last for _, asked in host.visibility.asked for identity in asked)


def test_a_large_backlog_is_announced_in_bounded_batches_each_event_once(tmp_path):
    host = NudgeHost(tmp_path, policy=Policy(minimum_repeat_seconds=0))
    first, last = host.bulk(AGENT_A, 260)
    hidden = set(range(first, last + 1, 3))                              # unauthorized rows interleaved with visible ones
    host.visibility.denied |= {(AGENT_A, identity) for identity in hidden}
    announced = []
    for _ in range(10):
        result = host.nudges.prepare(AGENT_A, binding())
        if isinstance(result, Waiting):
            break
        assert len(result.event_ids) <= 100
        host.nudges.settle(result.attempt_id, submitted(result.binding))
        announced += result.event_ids
    assert result.reason == "coalesced"
    assert sorted(announced) == [identity for identity in range(first, last + 1) if identity not in hidden]
    assert len(announced) == len(set(announced))


# -- several recipients -----------------------------------------------------------------------------------------------------

def test_business_and_global_recipients_have_independent_needs_and_receipts(host):
    shared = host.event([AGENT_A, AGENT_B, SHORT_AGENT])
    private = host.event([AGENT_B], business=None, source="messages", visibility={"participants": [AGENT_B]})
    a, b, short = (deliver(host, agent) for agent in (AGENT_A, AGENT_B, SHORT_AGENT))
    assert (a.event_ids, b.event_ids, short.event_ids) == ((shared,), (shared, private), (shared,))
    host.acknowledge(AGENT_B, shared, private)
    assert host.nudges.status(AGENT_B)["state"] == "idle"
    assert host.nudges.status(AGENT_A)["state"] == "submitted" and host.nudges.status(SHORT_AGENT)["state"] == "submitted"
    assert host.count("inbox_nudge_needs") == 3


# -- bounded reads and immutability -----------------------------------------------------------------------------------------

def test_coverage_is_bounded_and_unauthorized_rows_do_not_consume_it(host):
    hidden = [host.event([AGENT_A]) for _ in range(150)]
    visible = [host.event([AGENT_A]) for _ in range(120)]
    for event in hidden:
        host.visibility.denied.add((AGENT_A, event))
    reservation = reserve(host)
    assert reservation.event_ids == tuple(visible[:100])


def test_attempts_and_evidence_are_immutable_and_never_deleted(host):
    host.event([AGENT_A])
    deliver(host)
    for statement in ("UPDATE inbox_nudge_attempts SET kind='follow_up'", "UPDATE inbox_nudge_attempts SET binding_json='{}'",
                      "UPDATE inbox_nudge_attempts SET state='failed'", "DELETE FROM inbox_nudge_attempts",
                      "UPDATE inbox_nudge_evidence SET state='failed'", "DELETE FROM inbox_nudge_evidence",
                      "UPDATE inbox_nudge_needs SET cycle=0", "DELETE FROM inbox_nudge_needs",
                      "UPDATE inbox_nudge_needs SET agent_id='x'"):
        with pytest.raises(DomainError):
            with host.database.transaction() as uow:
                uow.connection.execute(statement)


def test_one_reservation_per_agent_is_a_storage_invariant(host):
    host.event([AGENT_A])
    reserve(host)
    with pytest.raises(DomainError):
        with host.database.transaction() as uow:
            uow.connection.execute(
                "INSERT INTO inbox_nudge_attempts(id,agent_id,cycle,sequence,kind,binding_json,session_key,event_ids_json,progress_json,state,prepared_at)"
                " VALUES('duplicate-reservation',?,1,9,'initial','{}',?,'[]','{}','prepared','2026-10-12T12:00:00Z')", (AGENT_A, "0" * 64))


def test_status_distinguishes_every_state_without_claiming_the_agent_read(host):
    host.event([AGENT_A])
    assert host.nudges.status(AGENT_A)["state"] == "pending"
    reservation = reserve(host)
    assert host.nudges.status(AGENT_A)["state"] == "pending"
    host.nudges.settle(reservation.attempt_id, submitted(reservation.binding))
    view = host.nudges.status(AGENT_A)
    assert view["state"] == "submitted" and "does not mean the agent read its inbox" in view["note"]
    assert view["attempts"][0]["binding"] == reservation.binding.public()


def test_status_never_calls_a_whole_inbox_announced_because_the_last_notice_was_submitted(host):
    host.event([AGENT_A])
    deliver(host)
    host.event([AGENT_A])
    view = host.nudges.status(AGENT_A)
    assert view["state"] == "pending" and (view["pending"], view["unannounced"], view["announced"]) == (2, 1, 1)
    host.clock.advance(61)
    uncertain = reserve(host)
    host.nudges.settle(uncertain.attempt_id, Outcome("uncertain", "claim_without_result", {"binding": uncertain.binding.public()}))
    view = host.nudges.status(AGENT_A)
    assert view["state"] == "uncertain" and (view["unannounced"], view["announced"], view["possibly_announced"]) == (0, 1, 1)
    assert view["pending"] == 2 and host.nudges.status(AGENT_A, limit=1)["attempts"][0]["state"] == "uncertain"


def test_status_reads_coverage_for_the_live_binding_the_host_supplies(host):
    host.event([AGENT_A])
    deliver(host)                                                        # announced to the old conversation
    host.clock.advance(10)
    fresh = binding(generation=2)                                        # a new live conversation, still inside the spacing
    assert host.nudges.prepare(AGENT_A, fresh).reason == "minimum_spacing"
    assert host.nudges.status(AGENT_A)["state"] == "submitted"           # no binding: the latest attempt's conversation
    view = host.nudges.status(AGENT_A, binding=fresh)
    assert (view["state"], view["reason"]) == ("pending", "minimum_spacing")
    assert (view["unannounced"], view["announced"], view["possibly_announced"], view["pending"]) == (1, 0, 0, 1)
    assert host.nudges.status(AGENT_A, binding=fresh.public())["state"] == "pending"
    reattached = host.nudges.status(AGENT_A, binding=same_conversation(2))
    assert reattached["state"] == "submitted" and (reattached["unannounced"], reattached["announced"]) == (0, 1)
    with pytest.raises(DomainError) as error:
        host.nudges.status(AGENT_A, binding=binding(AGENT_B))
    assert error.value.code == "invalid_binding"


def test_status_is_not_conclusive_when_an_uncertain_tail_lies_beyond_the_bounded_scan(tmp_path):
    host = NudgeHost(tmp_path, policy=Policy(minimum_repeat_seconds=0))
    first, last = host.bulk(AGENT_A, 150)
    head = deliver(host)                                                 # the first sample: submitted
    tail = reserve(host)                                                 # the rest: only possibly typed
    host.nudges.settle(tail.attempt_id, Outcome("uncertain", "claim_without_result", {"binding": tail.binding.public()}))
    assert (len(head.event_ids), len(tail.event_ids)) == (100, 50)
    view = host.nudges.status(AGENT_A)
    assert (view["state"], view["reason"], view["more"]) == ("pending", "scan_incomplete", True)
    assert (view["unannounced"], view["announced"], view["possibly_announced"]) == (0, 100, 0)
    host.acknowledge(AGENT_A, *range(first, first + 60))                # every covered event now fits in one sample
    view = host.nudges.status(AGENT_A)
    assert (view["state"], view["more"], view["announced"], view["possibly_announced"]) == ("uncertain", False, 40, 50)
    host.acknowledge(AGENT_A, *tail.event_ids)
    view = host.nudges.status(AGENT_A)
    assert (view["state"], view["more"], view["announced"], view["possibly_announced"]) == ("submitted", False, 40, 0)


def test_status_shows_stale_after_a_cancelled_reservation_and_follows_live_acknowledgement(host):
    event = host.event([AGENT_A])
    reservation = reserve(host)
    assert host.nudges.validate(reservation.attempt_id, binding(generation=2)) is False
    view = host.nudges.status(AGENT_A)
    assert view["state"] == "stale" and view["reason"] == "binding_changed" and view["pending"] == 1
    # The stale reservation never blocks the exact session that is now current.
    assert isinstance(host.nudges.prepare(AGENT_A, binding(generation=2)), Reservation)
    host.acknowledge(AGENT_A, event)
    assert host.nudges.status(AGENT_A)["state"] == "idle"


# -- bounded scan continuation ----------------------------------------------------------------------------------------------

def hidden_backlog(host, rows=2000):
    """``rows`` pending events the agent may not see, followed by one it may."""
    first, last = host.bulk(AGENT_A, rows)
    host.visibility.denied |= {(AGENT_A, identity) for identity in range(first, last + 1)}
    return first, last, host.event([AGENT_A])


def test_an_authorized_event_beyond_the_scan_budget_is_eventually_reserved_and_the_position_survives_restart(host):
    first, last, visible = hidden_backlog(host)
    assert (first, last, visible) == (1, 2000, 2001)
    # The first window spends its whole row budget on inaccessible rows: nothing is reserved, nothing is claimed idle, and the
    # position is saved in an inactive row that holds no cycle.
    assert host.nudges.prepare(AGENT_A, binding()) == Waiting("scan_incomplete")
    need = host.need()
    assert need["scan_after"] == 2000 and need["active"] == 0 and need["cycle"] == 0 and need["next_sequence"] == 1
    assert host.nudges.status(AGENT_A)["state"] == "pending"
    host.restart()                                                      # nothing in memory survives
    reservation = host.nudges.prepare(AGENT_A, binding())
    assert isinstance(reservation, Reservation) and reservation.event_ids == (visible,) and reservation.kind == "initial"
    need = host.need()
    assert need["cycle"] == 1 and need["active"] == 1 and need["scan_after"] == 0     # first activation is cycle one; the pass wrapped
    assert host.count("inbox_nudge_attempts") == 1 and host.count("inbox_nudge_evidence") == 0
    assert host.nudges.validate(reservation.attempt_id, reservation.binding)           # authorization was asked for again
    assert host.deliveries()[-1]["acknowledged_at"] is None                           # a position is not an acknowledgement


def test_a_scan_position_never_announces_or_reannounces_anything(host):
    hidden_backlog(host)
    same = Observation(work=5, user=5)
    assert host.nudges.prepare(AGENT_A, binding(), same) == Waiting("scan_incomplete")
    assert host.count("inbox_nudge_attempts") == 0 and host.need()["next_sequence"] == 1
    reservation = reserve(host, observation=same)
    host.nudges.settle(reservation.attempt_id, submitted(reservation.binding))
    # Every later pass crosses the inaccessible rows again, and none of them offers a second notice of the announced event.
    verdicts = []
    for step in range(6):
        host.clock.advance(3600)
        verdicts.append(host.nudges.prepare(AGENT_A, binding(), Observation(work=6 + step, user=6 + step)))
    assert all(isinstance(verdict, Waiting) for verdict in verdicts)
    assert {verdict.reason for verdict in verdicts} <= {"scan_incomplete", "coalesced"}
    assert len(host.attempts()) == 1 and host.need()["next_sequence"] == 2


def test_a_partial_window_that_finds_nothing_never_clears_an_outstanding_need(host):
    hidden_backlog(host)
    deliver_state = None
    for _ in range(2):
        result = host.nudges.prepare(AGENT_A, binding())
        deliver_state = result if isinstance(result, Reservation) else deliver_state
    host.nudges.settle(deliver_state.attempt_id, submitted(deliver_state.binding))
    host.clock.advance(3600)
    # The pass has wrapped to the start: its window is again 2000 inaccessible rows. It proves nothing about the event beyond it.
    assert host.nudges.prepare(AGENT_A, binding()) == Waiting("scan_incomplete")
    assert host.need()["active"] == 1 and host.need()["cycle"] == 1 and host.need()["scan_after"] == 2000
    assert host.nudges.status(AGENT_A)["state"] != "idle"
    # The tail after the position holds only the announced event, which is not read again; the inaccessible rows before it
    # still prove nothing, so the scan returns to the start and the need stays.
    assert host.nudges.prepare(AGENT_A, binding()) == Waiting("scan_incomplete")
    assert len(host.attempts()) == 1 and host.need()["active"] == 1 and host.need()["scan_after"] == 0


def test_earlier_entries_are_read_again_so_a_later_visibility_change_is_noticed(host):
    first, last, visible = hidden_backlog(host)
    reservation = None
    for _ in range(2):
        result = host.nudges.prepare(AGENT_A, binding())
        reservation = result if isinstance(result, Reservation) else reservation
    host.nudges.settle(reservation.attempt_id, submitted(reservation.binding))
    host.acknowledge(AGENT_A, visible)                                   # the covered event is handled; only denied rows remain
    host.clock.advance(3600)
    assert host.nudges.prepare(AGENT_A, binding()) == Waiting("scan_incomplete")      # window from the start: all denied
    assert host.need()["active"] == 1 and host.need()["scan_after"] == 2000
    assert host.nudges.prepare(AGENT_A, binding()) == Waiting("scan_incomplete")      # the tail is empty: cannot prove emptiness
    assert host.need()["active"] == 1 and host.need()["scan_after"] == 0              # ... so the scan returns to the start
    host.visibility.denied.discard((AGENT_A, 5))                                      # an earlier entry became visible
    again = host.nudges.prepare(AGENT_A, binding())
    assert isinstance(again, Reservation) and again.event_ids == (5,) and again.kind == "follow_up"
    assert host.need()["cycle"] == 1                                      # never proven empty: the same activation


def test_emptiness_is_proved_only_by_no_pending_rows_or_a_complete_scan_from_the_start(host):
    first, last, visible = hidden_backlog(host)
    assert host.nudges.prepare(AGENT_A, binding()) == Waiting("scan_incomplete")      # position now 2000
    host.acknowledge(AGENT_A, *range(first, visible + 1))
    # A window after the position that finds nothing, with no pending rows at all, is empty: the position is cleared and no
    # cycle is ever opened.
    assert host.nudges.prepare(AGENT_A, binding()) == Waiting("idle", status="idle")
    need = host.need()
    assert need["scan_after"] == 0 and need["active"] == 0 and need["cycle"] == 0
    assert host.nudges.status(AGENT_A)["state"] == "idle" and host.attempts() == []


def test_a_complete_scan_from_the_start_that_finds_only_inaccessible_rows_is_empty(host):
    first, last = host.bulk(AGENT_A, 50)
    host.visibility.denied |= {(AGENT_A, identity) for identity in range(first, last + 1)}
    assert host.nudges.prepare(AGENT_A, binding()) == Waiting("idle", status="idle")
    assert host.count("inbox_nudge_needs") == 0


def test_cycle_zero_exists_only_while_the_need_is_inactive(host):
    hidden_backlog(host)
    host.nudges.prepare(AGENT_A, binding())
    with pytest.raises(DomainError):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE inbox_nudge_needs SET active=1 WHERE agent_id=?", (AGENT_A,))
