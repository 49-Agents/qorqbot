"""Compact Focus and related-item projection of a Customer Task: kind and state label only, never the profile or its notes.

``qorqut_operations.records.resources.RelatedResources`` resolves every related Task (Focus cards, item links) and
``FocusService`` builds the card from it. For ``task_type == 'customer'`` the compact card and item carry ``customer_kind`` and the
free-text ``customer_state`` label; ordinary and Story/Email Tasks are unchanged; ``customer_profile`` (company, contact, email,
phone, website, notes) is never part of a compact projection.

SYNTHETIC STORED SHAPE. This module runs on any engine: the baseline engine does not create Customer Tasks, so the check stores the
shape the design gives the typed fields (under the Task's ``$task`` attributes) directly into a synthetic Task row, and reads it
through the real host Focus path. It proves only the host's projection of that shape. The engine-backed proof (Customers created
through ``POST .../task`` and the operation route of the completed Operations engine, then read through Focus, related-Task rows,
children, overview and activity) is ``test_host_customer_links.py::test_every_compact_projection_shows_kind_and_state_and_never_the_profile``.
No model, native session, provider, network or operational data.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from qorqut_operations.contracts import InvocationContext, InvocationOptions, Principal, new_id, plain
from qorqut_operations.records.resources import _task_projection

from bazarlyk.agents.models import GLOBAL_CAPABILITIES
from host_foundation_support import build_instance, create_registered_global, register_sources

NOTES = "PRIVATE-CUSTOMER-NOTES prefers morning calls"
PROFILE = {"company": "Acme Ltd", "contact_name": "A. Buyer", "email": "buyer@example.test", "phone": "+000",
           "website": "https://example.test", "notes": NOTES}


@pytest.fixture
def host(tmp_path):
    instance, ids = build_instance(tmp_path, businesses=("first",))
    first = ids["first"]
    register_sources(instance, first, "first")
    create_registered_global(instance, "ordubashy")
    instance.runtime.freeze_registry()
    owner = InvocationContext(Principal(instance.owner, "human"), allowed_business_ids=frozenset({first}),
                              global_capabilities=GLOBAL_CAPABILITIES, source="dashboard")
    try:
        yield SimpleNamespace(instance=instance, first=first, owner=owner, business_owner=instance.human(first))
    finally:
        try:
            instance.runtime.release_instance()
        finally:
            instance.database.close()


def call(host, ctx, name, arguments):
    return host.instance.runtime.application.invoke(ctx, name, arguments, InvocationOptions(idempotency_key=new_id()))


def ok(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def task(host, title, attributes=None):
    """An ordinary Task; ``attributes`` replaces its stored ``$task`` attributes with the synthetic shape under test."""
    created = ok(call(host, host.business_owner, "tasks_create", {"title": title}))["task"]
    if attributes is not None:
        with host.instance.database.transaction() as uow:
            uow.connection.execute("UPDATE tasks SET fields_json=? WHERE id=?", (json.dumps({"$task": attributes}), created["id"]))
    return created["id"]


def card(host, identity):
    source = {"type": "task", "id": identity, "business_id": host.first}
    presented = ok(call(host, host.owner, "focus_present", {
        "scope": "global", "source": source, "reason": "Needs a look", "requested_response": "Decide"}))["item"]
    listed = ok(call(host, host.owner, "focus_get", {"scope": "global", "item_id": presented["id"]}))["item"]
    return presented, listed


def test_a_customer_card_shows_its_kind_and_state_label_and_nothing_of_the_profile(host):
    identity = task(host, "Acme Ltd", {"task_type": "customer", "customer_kind": "company", "customer_state": "Prospect",
                                       "customer_profile": PROFILE})
    presented, listed = card(host, identity)
    for shown in (presented, listed):
        source = shown["resolved_source"]
        assert (source["task_type"], source["customer_kind"], source["customer_state"]) == ("customer", "company", "Prospect")
        text = json.dumps(shown)
        assert "customer_profile" not in source and NOTES not in text and "buyer@example.test" not in text
        assert not {"company", "contact_name", "email", "phone", "website", "notes"} & set(source)


def test_a_person_customer_without_a_state_has_an_empty_label_and_ordinary_tasks_gain_no_customer_keys(host):
    person = task(host, "Jo Buyer", {"task_type": "customer", "customer_kind": "person"})
    _, listed = card(host, person)
    source = listed["resolved_source"]
    assert (source["task_type"], source["customer_kind"], source["customer_state"]) == ("customer", "person", "")
    for attributes, expected in ((None, "task"), ({"task_type": "story", "overview": "Overview"}, "story"), ({"task_type": "email"}, "email")):
        _, listed = card(host, task(host, "Ordinary", attributes))
        source = listed["resolved_source"]
        assert source["task_type"] == expected and not {"customer_kind", "customer_state"} & set(source)


@pytest.mark.parametrize("row, expected", [
    ({"fields_json": {}}, {"task_type": "task"}),
    ({"fields_json": {"$task": {"task_type": "story", "overview": "x"}}}, {"task_type": "story"}),
    ({"fields_json": {"$task": {"task_type": "customer", "customer_kind": "company", "customer_state": "Won",
                                "customer_profile": PROFILE}}},
     {"task_type": "customer", "customer_kind": "company", "customer_state": "Won"}),
    ({"fields_json": {"$task": {"task_type": "customer", "customer_kind": "person"}}},
     {"task_type": "customer", "customer_kind": "person", "customer_state": ""}),
])
def test_the_related_item_projection_is_compact(row, expected):
    assert _task_projection(row) == expected
