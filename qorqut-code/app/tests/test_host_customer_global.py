"""Global Customer records through the real composed host: the root operation route, the stored rows, and separate choices per scope.

Qualification of the completed Operations engine (operations-f6663c03) with the host for the Global owning scope. The dashboard's
Global calls are ``POST /api/root/operations/<name>`` with no scope argument for Tasks (``workCall(null, ...)``); the stored
vocabulary row of Global has ``business_id`` NULL. Business choices and Global choices never mix, a case/Unicode variant of an offered
label returns that scope's first display spelling, archive/unarchive never changes a label, and a refused or retried write behaves as
it does in a business.

Synthetic only: a tmp instance; no model, provider, network, native session or operational data."""

import pytest
from qorqut_operations.contracts import new_id

from customer_host_support import (NOTES, PROFILE, choices, customer, data, host, operate, post, read, refused, rows, snapshot,  # noqa: F401
                                   stored_attributes, vocabulary)


def update(host, task, **changes):
    return data(operate(host, None, "tasks_update", task_id=task["id"], changes=changes, expected_revision=task["revision"]))["task"]


def test_a_global_customer_persists_updates_independently_and_keeps_its_choices_apart_from_every_business(host):
    lead = customer(host, None, customer_state="Lead")
    assert lead["business_id"] is None and (lead["customer_kind"], lead["customer_state"], lead["customer_profile"]) == ("company", "Lead", PROFILE)
    assert read(host, lead["id"], None)["customer_profile"] == PROFILE
    assert vocabulary(host, None)["config"] == {"v": 1, "states": ["Lead"]} and stored_attributes(host, lead["id"])["customer_profile"] == PROFILE
    assert choices(host, None) == ["Lead"] and choices(host, "first") == [] and choices(host, "second") == []

    # Each scope offers only its own labels and keeps its own first spelling of an equal label.
    customer(host, "first", title="First's", customer_state="lead")
    customer(host, "second", title="Second's", customer_state="LEAD")
    customer(host, None, title="Global too", customer_state=" lEAD ")
    assert (choices(host, None), choices(host, "first"), choices(host, "second")) == (["Lead"], ["lead"], ["LEAD"])
    assert vocabulary(host, None)["config"]["states"] == ["Lead"] and vocabulary(host, host.first)["config"]["states"] == ["lead"]

    # State and profile update independently under the read revision; Unicode variants resolve to the first spelling.
    labelled = update(host, lead, customer_state="Café Active")
    assert (labelled["customer_state"], labelled["customer_profile"]) == ("Café Active", PROFILE) and labelled["revision"] == lead["revision"] + 1
    reprofiled = update(host, labelled, customer_profile={"email": "ops@example.test"})
    assert (reprofiled["customer_profile"], reprofiled["customer_state"]) == ({"email": "ops@example.test"}, "Café Active")
    assert update(host, reprofiled, customer_state="CAFÉ active")["customer_state"] == "Café Active"
    assert choices(host, None) == ["Lead", "Café Active"] and NOTES not in str(read(host, lead["id"], None))

    # Archive and unarchive are their own flag and never touch the label or the status.
    archived = update(host, update(host, reprofiled, customer_state="Café Active"), archived=True)
    assert archived["archived"] is True and (archived["customer_state"], archived["status"]) == ("Café Active", "open")
    restored = update(host, archived, archived=False)
    assert restored["archived"] is False and restored["customer_state"] == "Café Active"
    assert choices(host, None) == ["Lead", "Café Active"]            # a label never leaves the choices


@pytest.mark.parametrize("changes", [
    {"customer_kind": "organisation"},
    {"customer_profile": {"fax": "1"}},
    {"customer_profile": {"notes": "n" * 20001}},
    {"customer_state": "s" * 121},
    {"customer_state": "Brand new label", "task_type": "story"},
    {"customer_state": "Brand new label", "due": "not-a-date"},
])
def test_a_refused_global_write_changes_no_task_and_appends_no_label(host, changes):
    record = customer(host, None, customer_state="Lead")
    before = snapshot(host)
    assert refused(operate(host, None, "tasks_update", task_id=record["id"], changes=changes, expected_revision=record["revision"])) \
        in {"validation_error", "invalid_plan"}
    assert refused(operate(host, None, "tasks_create", **{"title": "Never", "task_type": "customer", **changes})) in {"validation_error", "invalid_plan"}
    assert snapshot(host) == before and choices(host, None) == ["Lead"]


def test_stale_and_replayed_global_writes_append_and_revise_once(host):
    record = customer(host, None, customer_state="Lead")
    newer = update(host, record, customer_profile={"company": "Acme"})
    before = snapshot(host)
    assert refused(operate(host, None, "tasks_update", task_id=record["id"], changes={"customer_state": "Late"},
                           expected_revision=record["revision"])) == "revision_conflict"
    assert snapshot(host) == before and choices(host, None) == ["Lead"]
    key = new_id()
    body = dict(task_id=newer["id"], changes={"customer_state": "Won"}, expected_revision=newer["revision"], idempotency_key=key)
    one, two = operate(host, None, "tasks_update", **body), operate(host, None, "tasks_update", **body)
    assert one[0] == two[0] == 200 and one[1]["data"]["task"]["revision"] == two[1]["data"]["task"]["revision"] == newer["revision"] + 1
    assert choices(host, None) == ["Lead", "Won"]
    created = [post(host, "root/operations/tasks_create", title="Once", task_type="customer", customer_state="Once", idempotency_key=key + "-c")
               for _ in range(2)]
    assert [status for status, _ in created] == [200, 200] and created[0][1]["data"]["task"]["id"] == created[1][1]["data"]["task"]["id"]
    assert len(rows(host, "SELECT id FROM tasks WHERE title='Once'")) == 1 and choices(host, None) == ["Lead", "Won", "Once"]
