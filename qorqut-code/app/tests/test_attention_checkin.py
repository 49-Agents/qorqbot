"""Synthetic checks for the exact legacy Focus check-in identification and its explicit pause reconciliation.

The first group exercises the pure classifier. The composed check then builds the current synthetic host fixture
(``host_foundation_support.build_instance`` plus ``register_sources``, ``create_registered_global``,
``select_coordinator`` and ``owner_global_context``, the pattern used by ``test_crm_host_composition``) over a disposable
SQLite store, and drives the real identity, read and pause path: the configured Ordubashy, the existing
``broadcasts_list`` authority, the canonical repository projection and the revision-bound ``broadcasts_pause``. It seeds
more than 200 Global broadcast rows so the exact check-in is older than the newest page, then proves report-only silence,
one-shot pausing, repeat no-op, a concurrent-edit refusal, post-write verification failure and the missing-coordinator
refusal. No live instance, service, provider, model, browser, network or loopback call is used.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id, plain, utc_now

from bazarlyk.migration import attention_checkin
from bazarlyk.migration.attention_checkin import (
    AMBIGUOUS, CANCELLED, ELIGIBLE, LEGACY_BROADCAST_MESSAGE, LEGACY_BROADCAST_NAME, PAUSED, UNRELATED,
    classify, reconcile, report)

from host_foundation_support import (build_instance, create_registered_global, owner_global_context, register_sources,
                                     select_coordinator)

OWNER = "00000000-0000-4000-8000-000000000001"
ORDUBASHY = "00000000-0000-4000-8000-000000000010"
OTHER_AGENT = "00000000-0000-4000-8000-000000000011"
BUSINESS = "00000000-0000-4000-8000-000000000020"

# Frozen independently from the baseline dashboard source (``js/agentpane.js``, ``AGPAD_PING_MESSAGE``).
EXPECTED_MESSAGE = (
    "Scheduled check-in from the owner. Review the items on the Focus board (focus_get) and the open tasks assigned "
    "to me. Take on and complete every item you can. Keep each task's status current: doing for what you are working "
    "on, waiting for what you are waiting on others for, and blocked for what you are waiting on me for. If you are "
    "blocked on an item, reply in that item's thread and state clearly why you are blocked and what would unblock "
    "you. Assign me tasks that I can do to unblock you and put them on my Focus (focus_present). Clear done tasks "
    "from the Focus pane (focus_set_archived)."
)


def broadcast(identity=None, **changes):
    """One stored Global broadcast row as the canonical repository projection decodes it."""
    row = {"id": identity or new_id(), "business_id": None, "name": LEGACY_BROADCAST_NAME, "delivery": "inbox",
           "message": LEGACY_BROADCAST_MESSAGE,
           "recipients_json": [{"agent_id": ORDUBASHY, "business_id": None}],
           "timing_json": {"kind": "interval", "every": 5, "unit": "hours"},
           "status": "active", "revision": 1, "created_at": "2026-10-01T00:00:00Z"}
    row.update(changes)
    return row


# --------------------------------------------------------------------------------------------------------------------
# The pure classifier
# --------------------------------------------------------------------------------------------------------------------

def test_frozen_name_and_wording_are_the_baseline_built_in():
    assert LEGACY_BROADCAST_NAME == "Focus check-in"
    assert LEGACY_BROADCAST_MESSAGE == EXPECTED_MESSAGE
    assert len(LEGACY_BROADCAST_MESSAGE) == 593


def test_normalized_wording_ignores_line_wrapping_but_not_words():
    wrapped = LEGACY_BROADCAST_MESSAGE.replace("the open tasks", "the open\n\ttasks").replace("Keep ", " Keep\r\n ")
    assert attention_checkin.normalize_message(wrapped) == attention_checkin.normalize_message(LEGACY_BROADCAST_MESSAGE)
    assert attention_checkin.normalize_message(LEGACY_BROADCAST_MESSAGE + " Please hurry.") != \
        attention_checkin.normalize_message(LEGACY_BROADCAST_MESSAGE)


def test_active_exact_match_is_the_only_eligible_classification():
    item = classify(broadcast(), ordubashy_id=ORDUBASHY)
    assert item["classification"] == ELIGIBLE
    assert item["reasons"] == []
    assert item["recipient_ids"] == [ORDUBASHY]
    assert item["timing"] == {"kind": "interval", "every": 5, "unit": "hours"}
    assert "message" not in item


def test_exact_matches_already_paused_or_cancelled_stay_retained():
    assert classify(broadcast(status="paused"), ordubashy_id=ORDUBASHY)["classification"] == PAUSED
    assert classify(broadcast(status="cancelled"), ordubashy_id=ORDUBASHY)["classification"] == CANCELLED


def test_customized_wording_is_ambiguous_and_never_paused():
    item = classify(broadcast(message=LEGACY_BROADCAST_MESSAGE + " Please hurry."), ordubashy_id=ORDUBASHY)
    assert item["classification"] == AMBIGUOUS
    assert item["reasons"] == ["wording"]
    assert classify(broadcast(message=LEGACY_BROADCAST_MESSAGE.upper()), ordubashy_id=ORDUBASHY)["reasons"] == ["wording"]


def test_customized_recipient_sets_are_ambiguous_and_never_paused():
    extra = [{"agent_id": ORDUBASHY, "business_id": None}, {"agent_id": OTHER_AGENT, "business_id": None}]
    assert classify(broadcast(recipients_json=extra), ordubashy_id=ORDUBASHY)["reasons"] == ["recipients"]
    assert classify(broadcast(recipients_json=[{"agent_id": OTHER_AGENT, "business_id": None}]),
                    ordubashy_id=ORDUBASHY)["reasons"] == ["recipients"]
    assert classify(broadcast(recipients_json=[{"agent_id": ORDUBASHY, "business_id": BUSINESS}]),
                    ordubashy_id=ORDUBASHY)["reasons"] == ["recipients"]


def test_same_name_terminal_input_delivery_is_ambiguous():
    item = classify(broadcast(delivery="input"), ordubashy_id=ORDUBASHY)
    assert item["classification"] == AMBIGUOUS
    assert item["reasons"] == ["delivery"]


def test_other_broadcasts_are_unrelated():
    item = classify(broadcast(name="Daily check-in"), ordubashy_id=ORDUBASHY)
    assert item["classification"] == UNRELATED
    assert item["reasons"] == ["name"]
    assert classify(broadcast(name="Focus check-in (old)"), ordubashy_id=ORDUBASHY)["classification"] == UNRELATED


def test_a_business_scoped_same_name_record_is_outside_the_global_helper():
    item = classify(broadcast(business_id=BUSINESS), ordubashy_id=ORDUBASHY)
    assert item["classification"] == UNRELATED
    assert item["reasons"] == ["scope"]


def test_whitespace_reflowed_stored_wording_still_matches_the_built_in():
    stored = LEGACY_BROADCAST_MESSAGE.replace(". ", ".\n    ")
    assert classify(broadcast(message=stored), ordubashy_id=ORDUBASHY)["classification"] == ELIGIBLE


def test_interval_is_reported_but_never_selects_or_disqualifies_a_record():
    # The preparation record could not read the live interval; wording and recipient set identify the built-in check-in.
    for timing in ({"kind": "interval", "every": 4, "unit": "hours"},
                   {"kind": "interval", "every": 1, "unit": "days"},
                   {"kind": "cron", "schedule": "0 9 * * *", "timezone": "UTC"}):
        item = classify(broadcast(timing_json=timing), ordubashy_id=ORDUBASHY)
        assert item["classification"] == ELIGIBLE
        assert item["timing"] == timing


def test_no_configured_ordubashy_reports_an_ambiguous_candidate():
    item = classify(broadcast(), ordubashy_id=None)
    assert item["classification"] == AMBIGUOUS
    assert item["reasons"] == ["no_configured_ordubashy"]


def test_report_counts_classifications_and_orders_items_stably():
    rows = [broadcast("00000000-0000-4000-8000-000000000030"),
            broadcast("00000000-0000-4000-8000-000000000031", message=LEGACY_BROADCAST_MESSAGE + " Changed."),
            broadcast("00000000-0000-4000-8000-000000000032", name="Daily check-in"),
            broadcast("00000000-0000-4000-8000-000000000033", status="paused")]
    result = report(rows, ordubashy_id=ORDUBASHY)
    assert result["scanned"] == 4
    assert result["coordinator_id"] == ORDUBASHY
    assert result["counts"] == {ELIGIBLE: 1, PAUSED: 1, CANCELLED: 0, AMBIGUOUS: 1, UNRELATED: 1}
    assert result["eligible"] == ["00000000-0000-4000-8000-000000000030"]
    assert [item["classification"] for item in result["items"]] == [ELIGIBLE, PAUSED, AMBIGUOUS, UNRELATED]


# --------------------------------------------------------------------------------------------------------------------
# The composed store: real identity, read and pause path over disposable data
# --------------------------------------------------------------------------------------------------------------------

def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def insert_broadcast(instance, *, name="Synthetic reminder", message="Other scheduled reminder.", status="active",
                     created_at=None):
    """One synthetic Global row seeded directly, as the fixture's documents are; the read path still decodes it."""
    when = created_at or utc_now()
    with instance.database.transaction() as uow:
        return dict(uow.repository("broadcasts", business_id=None).insert({
            "id": new_id(), "sender_id": instance.owner, "created_by": instance.owner, "name": name,
            "delivery": "inbox", "message": message,
            "recipients_json": [{"agent_id": instance.registered["ordubashy"], "business_id": None}],
            "timing_json": {"kind": "interval", "every": 5, "unit": "hours"}, "status": status,
            "created_at": when, "updated_at": when}))


def create_check_in(instance):
    """The retired built-in schedule as the real ``broadcasts_create`` operation stores it."""
    created = instance.invoke(owner_global_context(instance), "broadcasts_create", {"scope": "global", "definition": {
        "name": LEGACY_BROADCAST_NAME, "delivery": "inbox", "message": LEGACY_BROADCAST_MESSAGE,
        "recipients": [instance.registered["ordubashy"]],
        "timing": {"kind": "interval", "every": 5, "unit": "hours"}}})
    assert created.status == "ok", created.error
    return plain(created.data)["records"][0]


def row_states(instance):
    with instance.database.read() as uow:
        return {row["id"]: (row["status"], row["revision"])
                for row in uow.connection.execute("SELECT id,status,revision FROM broadcasts")}


class InterceptedPauses:
    """The real composed application, with one named pause step edited to simulate a concurrent owner change."""

    def __init__(self, application, database, *, bump=(), corrupt=()):
        self.application, self.database = application, database
        self.bump, self.corrupt = set(bump), set(corrupt)

    def invoke(self, context, operation, arguments=None, options=None):
        identity = (arguments or {}).get("broadcast_id")
        if operation == "broadcasts_pause" and identity in self.bump:
            with self.database.transaction() as uow:  # an owner edit between the inventory and the pause
                uow.connection.execute("UPDATE broadcasts SET revision=revision+1 WHERE id=?", (identity,))
        outcome = self.application.invoke(context, operation, arguments, options)
        if operation == "broadcasts_pause" and identity in self.corrupt and outcome.status == "ok":
            with self.database.transaction() as uow:  # an owner edit after the pause, before verification
                uow.connection.execute("UPDATE broadcasts SET recipients_json=? WHERE id=?",
                                       (json.dumps([{"agent_id": new_id(), "business_id": None}]), identity))
        return outcome


def shim(instance, application):
    """The runtime surface ``reconcile`` reads, so only the write step can be intercepted without a fake store."""
    return SimpleNamespace(database=instance.database, root=instance.runtime.root, application=application,
                           require_authority=instance.runtime.require_authority)


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    """One composed synthetic host with its registered global Ordubashy selected as the configured coordinator."""
    instance, ids = build_instance(tmp_path_factory.mktemp("attention-checkin"))
    try:
        register_sources(instance, ids["first"], "first")
        select_coordinator(instance, create_registered_global(instance, "ordubashy"))
        yield instance
    finally:
        instance.runtime.release_instance()
        instance.database.close()


def test_composed_identification_and_pause_reconciliation(store):
    instance = store
    check_in = create_check_in(instance)
    base = datetime.fromisoformat(check_in["created_at"].replace("Z", "+00:00"))
    older = base - timedelta(minutes=30)
    ambiguous = insert_broadcast(instance, name=LEGACY_BROADCAST_NAME,
                                 message=LEGACY_BROADCAST_MESSAGE + " Please hurry.",
                                 created_at=stamp(older - timedelta(minutes=2)))
    already_paused = insert_broadcast(instance, name=LEGACY_BROADCAST_NAME, message=LEGACY_BROADCAST_MESSAGE,
                                      status="paused", created_at=stamp(older - timedelta(minutes=4)))
    unrelated = insert_broadcast(instance, created_at=stamp(older - timedelta(minutes=6)))
    # More than 200 newer rows: the exact check-in is older than the newest page the operation itself would list.
    with instance.database.transaction() as uow:
        repository = uow.repository("broadcasts", business_id=None)
        for index in range(205):
            when = stamp(base + timedelta(seconds=1 + index))
            repository.insert({"id": new_id(), "sender_id": instance.owner, "created_by": instance.owner,
                               "name": "Synthetic reminder %03d" % index, "delivery": "inbox",
                               "message": "Other scheduled reminder.",
                               "recipients_json": [{"agent_id": instance.registered["ordubashy"], "business_id": None}],
                               "timing_json": {"kind": "interval", "every": 5, "unit": "hours"}, "status": "active",
                               "created_at": when, "updated_at": when})
    before = row_states(instance)
    assert len(before) == 209

    # Report-only: the older exact check-in is found beyond the newest 200 rows; nothing is written.
    dry_run = reconcile(instance.runtime)
    assert dry_run["apply"] is False and dry_run["applied"] == [] and dry_run["verified"] == []
    assert dry_run["scanned"] == 209
    assert dry_run["counts"] == {ELIGIBLE: 1, PAUSED: 1, CANCELLED: 0, AMBIGUOUS: 1, UNRELATED: 206}
    assert dry_run["eligible"] == [check_in["id"]]
    assert row_states(instance) == before
    assert "Please hurry." not in json.dumps(dry_run) and "Other scheduled reminder." not in json.dumps(dry_run)
    assert all("message" not in item for item in dry_run["items"])
    assert reconcile(instance.runtime, apply=1)["applied"] == []  # only an explicit boolean True writes
    assert row_states(instance) == before

    # The page bound fails as incomplete instead of reporting a partial inventory (the store spans two pages).
    original_bound = attention_checkin.MAX_PAGES
    attention_checkin.MAX_PAGES = 1
    try:
        with pytest.raises(DomainError) as bound:
            reconcile(instance.runtime)
    finally:
        attention_checkin.MAX_PAGES = original_bound
    assert bound.value.code == "migration_incomplete" and bound.value.details["scanned"] == 200
    assert row_states(instance) == before

    # Apply once: only the exact active check-in is paused, everything else keeps its state and revision.
    applied = reconcile(instance.runtime, apply=True)
    assert applied["apply"] is True
    assert applied["applied"] == [check_in["id"]] and applied["verified"] == [check_in["id"]]
    assert applied["counts"] == dry_run["counts"]
    after = row_states(instance)
    assert after[check_in["id"]] == ("paused", 2)
    assert after[already_paused["id"]] == ("paused", 1)
    for identity in (ambiguous["id"], unrelated["id"]):
        assert after[identity] == ("active", 1)
    assert all(state == ("active", 1) for identity, state in after.items()
               if identity not in (check_in["id"], already_paused["id"], ambiguous["id"], unrelated["id"]))

    # Repeated apply: the retained paused state is left alone and nothing is written again.
    again = reconcile(instance.runtime, apply=True)
    assert again["apply"] is True and again["applied"] == [] and again["verified"] == []
    assert again["counts"] == {ELIGIBLE: 0, PAUSED: 2, CANCELLED: 0, AMBIGUOUS: 1, UNRELATED: 206}
    assert row_states(instance) == after

    # A concurrent owner edit between the scan and the pause refuses that record instead of forcing the pause.
    changed = create_check_in(instance)
    intercept = InterceptedPauses(instance.runtime.application, instance.database, bump={changed["id"]})
    with pytest.raises(DomainError) as refused:
        reconcile(shim(instance, intercept), apply=True)
    assert refused.value.code == "migration_incomplete"
    assert refused.value.details["broadcast_id"] == changed["id"]
    assert list(refused.value.details["applied"]) == []
    assert row_states(instance)[changed["id"]][0] == "active"
    assert reconcile(instance.runtime, apply=True)["applied"] == [changed["id"]]

    # An edit after the pause is caught by the verification read instead of being reported as retired.
    corrupted = create_check_in(instance)
    intercept = InterceptedPauses(instance.runtime.application, instance.database, corrupt={corrupted["id"]})
    with pytest.raises(DomainError) as unverified:
        reconcile(shim(instance, intercept), apply=True)
    assert unverified.value.code == "migration_verification_failed"
    assert unverified.value.details["broadcast_id"] == corrupted["id"]

    # Without a configured Ordubashy the real identity path yields no exact match; applying refuses up front.
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
    try:
        missing = reconcile(instance.runtime)
        assert missing["coordinator_id"] is None and missing["counts"][ELIGIBLE] == 0
        assert all(item["reasons"] == ["no_configured_ordubashy"]
                   for item in missing["items"] if item["name"] == LEGACY_BROADCAST_NAME)
        with pytest.raises(DomainError) as unavailable:
            reconcile(instance.runtime, apply=True)
        assert unavailable.value.code == "management_unavailable"
    finally:
        select_coordinator(instance, instance.registered["ordubashy"])