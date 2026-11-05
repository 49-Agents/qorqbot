"""The owner-attention tick reads first: a dormant tick takes no write unit, any possible work takes the original path."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from qorqut_operations.signoffs.notifications import EASTERN, instant
from signoff_support import SyntheticHost
from support import HUMAN
from test_signoffs import FORM, create_input

OWNER_ONLY = dict(FORM, response_policy="owner_only")
TABLES = ("signoff_notifications", "signoff_notification_schedule", "signoff_attention")


@pytest.fixture
def host(tmp_path):
    return SyntheticHost(tmp_path)


@pytest.fixture
def units(host, monkeypatch):
    """The operations of every write unit the notification service opens from now on."""
    calls, original = [], host.database.transaction

    def counted(*args, **kwargs):
        calls.append(kwargs.get("operation"))
        return original(*args, **kwargs)

    monkeypatch.setattr(host.database, "transaction", counted)
    return calls


def at(host, value):
    host.now = value


def shift(host, **delta):
    host.now = (instant(host.now) + timedelta(**delta)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def tick(host):
    host.service.notifications.tick()


def settle(host):
    """Move to 08:00 Eastern and let the day's summary entry be written, so later ticks are idle unless attention is due."""
    at(host, "2026-10-12T12:00:00.000000Z")
    tick(host)


def rows(host, table, order):
    with host.database.read() as uow:
        return [tuple(row) for row in uow.connection.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def deliveries(host):
    return host.service.notifications.deliveries(host.owner())["items"]


def schedule(host):
    return rows(host, "signoff_notification_schedule", "owner_id")


def test_dormant_ticks_take_no_write_unit_until_the_eastern_summary_is_due(host, units):
    # 07:59 EDT: nothing is pending and no summary can be due.
    for _ in range(3):
        tick(host)
    assert units == [] and schedule(host) == []
    # 08:00 EDT: the owner has no entry for today, so the original path runs once even though no notification results.
    at(host, "2026-10-12T12:00:00.000000Z")
    tick(host)
    assert units == ["signoffs_notification_tick"]
    assert schedule(host) == [(HUMAN, "2026-10-12")] and rows(host, "signoff_notifications", "id") == []
    for _ in range(3):
        tick(host)
    assert len(units) == 1
    # The next Eastern day: still the same date at 23:59 EDT, then 07:59 EDT is only the day's start; 08:00 is the boundary.
    at(host, "2026-10-13T03:59:00.000000Z")
    tick(host)
    at(host, "2026-10-13T11:59:00.000000Z")
    tick(host)
    assert len(units) == 1
    at(host, "2026-10-13T12:00:00.000000Z")
    tick(host)
    assert len(units) == 2 and schedule(host) == [(HUMAN, "2026-10-13")]


def test_daily_summary_keeps_its_eastern_boundaries_across_daylight_saving_changes(host, units):
    for before, boundary, date in (("2026-10-31T11:59:00.000000Z", "2026-10-31T12:00:00.000000Z", "2026-10-31"),
                                   ("2026-11-01T12:59:00.000000Z", "2026-11-01T13:00:00.000000Z", "2026-11-01"),   # EST begins
                                   ("2027-03-13T12:59:00.000000Z", "2027-03-13T13:00:00.000000Z", "2027-03-13"),
                                   ("2027-03-14T11:59:00.000000Z", "2027-03-14T12:00:00.000000Z", "2027-03-14")):  # EDT begins
        before_count = len(units)
        at(host, before)
        tick(host)
        assert len(units) == before_count
        at(host, boundary)
        tick(host)
        assert len(units) == before_count + 1 and schedule(host) == [(HUMAN, date)]
        tick(host)
        assert len(units) == before_count + 1


def test_ordinary_attention_is_summarised_once_per_day_and_nothing_between(host, units):
    ask = create_input(host, OWNER_ONLY, attention={"notify": True})
    del units[:]
    tick(host)                                                # 07:59 EDT: a non-urgent alert waits for the summary
    assert units == [] and deliveries(host) == []
    at(host, "2026-10-12T12:00:00.000000Z")
    tick(host)
    assert units == ["signoffs_notification_tick"]
    summary = deliveries(host)
    assert [d["kind"] for d in summary] == ["summary"] and summary[0]["items"][0]["id"] == ask["id"]
    tick(host)
    shift(host, hours=6)
    tick(host)
    assert len(units) == 1 and len(rows(host, "signoff_notifications", "id")) == 1
    at(host, "2026-10-13T12:00:00.000000Z")
    tick(host)
    assert len(units) == 2 and len(rows(host, "signoff_notifications", "id")) == 2
    assert [d["kind"] for d in deliveries(host)] == ["summary"]            # the newest reminder replaces the backlog


def test_urgent_attention_is_due_at_once_then_hourly_without_idle_write_units(host, units):
    settle(host)
    ask = create_input(host, OWNER_ONLY, attention={"notify": True, "urgent": True, "reason": "Blocked on you"})
    del units[:]
    tick(host)
    assert units == ["signoffs_notification_tick"]
    assert [d["kind"] for d in deliveries(host)] == ["individual"] and deliveries(host)[0]["items"][0]["id"] == ask["id"]
    shift(host, minutes=59)
    tick(host)
    tick(host)
    assert len(units) == 1 and len(rows(host, "signoff_notifications", "id")) == 1
    shift(host, minutes=2)                                                   # the hour has passed
    tick(host)
    assert len(units) == 2 and len(rows(host, "signoff_notifications", "id")) == 2
    assert len(deliveries(host)) == 1                                        # one live alert; the older one was dismissed


def test_blocking_attention_follows_the_same_cadence(host, units):
    settle(host)
    create_input(host, OWNER_ONLY, attention={"notify": True, "blocking": True, "reason": "Needed to continue"})
    del units[:]
    tick(host)
    tick(host)
    assert units == ["signoffs_notification_tick"] and [d["kind"] for d in deliveries(host)] == ["individual"]


def test_snoozed_attention_is_dormant_until_it_expires(host, units):
    settle(host)
    ask = create_input(host, OWNER_ONLY, attention={"notify": True, "urgent": True, "reason": "Blocked on you"})
    host.service.notifications.snooze(host.owner(), ask["id"], "2026-10-12T14:00:00Z")      # two hours from now
    del units[:]
    tick(host)
    shift(host, hours=1)
    tick(host)
    assert units == [] and deliveries(host) == []
    at(host, "2026-10-12T14:00:01.000000Z")
    tick(host)
    assert units == ["signoffs_notification_tick"] and [d["items"][0]["id"] for d in deliveries(host)] == [ask["id"]]


def test_alerts_that_cannot_notify_or_are_no_longer_pending_take_no_write_unit(host, units):
    settle(host)
    quiet = create_input(host, OWNER_ONLY, key="quiet", attention={"notify": False, "urgent": True, "reason": "Muted"})
    done = create_input(host, OWNER_ONLY, key="done", attention={"notify": True, "urgent": True, "reason": "Answered"})
    response = host.service.respond(host.owner(), done["id"], proposal_revision=done["proposal_revision"],
        payload_hash=done["payload_hash"], expected_status_revision=done["status_revision"], idempotency_key="answer",
        notes="Done", selected_choices=["a"])
    assert response.status == "ok" and host.row(done["id"])["state"] == "answered"
    assert host.row(quiet["id"])["state"] == "pending"
    del units[:]
    for _ in range(3):
        tick(host)
    assert units == [] and rows(host, "signoff_notifications", "id") == []


def test_work_that_appears_after_an_empty_tick_is_processed_on_the_next_tick(host, units):
    settle(host)
    del units[:]
    tick(host)
    assert units == []
    ask = create_input(host, OWNER_ONLY, attention={"notify": True, "urgent": True, "reason": "Blocked on you"})
    del units[:]
    tick(host)
    assert units == ["signoffs_notification_tick"] and deliveries(host)[0]["items"][0]["id"] == ask["id"]


def test_concurrent_ticks_deliver_a_due_alert_at_most_once(host):
    settle(host)
    create_input(host, OWNER_ONLY, attention={"notify": True, "urgent": True, "reason": "Blocked on you"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: tick(host), range(4)))
    assert len(rows(host, "signoff_notifications", "id")) == 1 and len(deliveries(host)) == 1


def test_unreadable_attention_state_is_treated_as_possible_work(host):
    create_input(host, OWNER_ONLY, attention={"notify": True, "urgent": True, "reason": "Blocked on you"})
    notifications = host.service.notifications
    current = instant(host.now)
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE signoff_attention SET next_notify_at=?", ("not a timestamp",))
    assert notifications._may_have_due_work(current, current.astimezone(EASTERN)) is True
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE signoff_attention SET next_notify_at=?, snoozed_until=?", ("2026-10-12T11:00:00Z", "garbled"))
    assert notifications._may_have_due_work(current, current.astimezone(EASTERN)) is True


def test_the_preflight_never_misses_work_the_original_path_would_do(host, monkeypatch):
    """Every instant across both daylight-saving changes: whenever the forced original path changes anything, the preflight said so."""
    notifications = host.service.notifications
    create_input(host, OWNER_ONLY, key="urgent", attention={"notify": True, "urgent": True, "reason": "Blocked on you"})
    create_input(host, OWNER_ONLY, key="ordinary", attention={"notify": True})
    snoozed = create_input(host, OWNER_ONLY, key="snoozed", attention={"notify": True, "urgent": True, "reason": "Later"})
    create_input(host, OWNER_ONLY, key="muted", attention={"notify": False, "urgent": True, "reason": "Muted"})
    answered = create_input(host, OWNER_ONLY, key="answered", attention={"notify": True, "urgent": True, "reason": "Done"})
    assert host.service.respond(host.owner(), answered["id"], proposal_revision=answered["proposal_revision"],
        payload_hash=answered["payload_hash"], expected_status_revision=answered["status_revision"], idempotency_key="answer",
        notes="Done", selected_choices=["a"]).status == "ok"
    host.service.notifications.snooze(host.owner(), snoozed["id"], "2026-10-12T20:00:00Z")
    original = type(notifications)._may_have_due_work
    forced = []
    monkeypatch.setattr(notifications, "_may_have_due_work", lambda current, eastern: forced.append(True) or True)
    flagged = changed = steps = 0
    for start, end, minutes in ((datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc), datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc), 20),
                                (datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc), datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc), 20),
                                (datetime(2027, 3, 13, 6, 0, tzinfo=timezone.utc), datetime(2027, 3, 15, 15, 0, tzinfo=timezone.utc), 20)):
        moment = start
        while moment <= end:
            at(host, moment.isoformat(timespec="microseconds").replace("+00:00", "Z"))
            current = instant(host.now)
            expected = original(notifications, current, current.astimezone(EASTERN))
            before = [rows(host, table, "1") for table in TABLES]
            tick(host)                                                          # the original path, unconditionally
            after = [rows(host, table, "1") for table in TABLES]
            did = before != after
            assert expected or not did, f"preflight missed work at {host.now}"
            flagged, changed, steps = flagged + expected, changed + did, steps + 1
            moment += timedelta(minutes=minutes)
    assert len(forced) == steps and 0 < changed <= flagged < steps          # real work happened, and idle instants were skipped
