"""Customer, Story and Email records share the one surviving Ordubashy conversation API, through the real composed host.

The owner's private conversation on a record is with the registered Ordubashy alone (see ``test_private_conversation_global_peers``).
This module proves, with the completed Operations engine (operations-f6663c03) and the real HTTP application, that the three
record Task types need no type-specific conversation code: the same ``owner_conversation_*`` calls the dashboard makes work on a
Customer, a Story and an Email Task in a business and in Global, store one null-peer conversation per record under the same
``source:`` key shape, deliver to Ordubashy alone, list only Ordubashy as a peer and refuse every other selector.

Synthetic only: a tmp instance; no model, provider, network, native session or operational data."""

import pytest
from qorqut_operations.contracts import Principal

from customer_host_support import (code, customer, data, host, invoke, ok, operate, refused, rows,  # noqa: F401  (host is the fixture)
                                   seed_email)
from host_foundation_support import select_coordinator, set_agent_overrides

GRANTS = {"owner_conversation_get": "allow", "owner_conversation_send": "allow"}


@pytest.fixture
def ordubashy(host):
    select_coordinator(host.instance, host.agent)
    set_agent_overrides(host.instance, host.agent, GRANTS, (host.first,))
    return host.agent


def records(host, slug):
    """One record of each type in the scope, as the dashboard creates them (the create form for a business Customer)."""
    conversation = seed_email(host, host.first if slug else None, f"thread-{slug or 'global'}")
    make = lambda **fields: data(operate(host, slug, "tasks_create", **fields))["task"]
    return {"customer": customer(host, slug), "story": make(title="Relationship", task_type="story", overview="Next steps"),
            "email": make(title="Discuss", task_type="email", email_conversation_id=conversation)}


def source_of(host, record, slug):
    return {"type": "task", "id": record["id"], "business_id": host.first if slug else None}


def scoped(slug, **arguments):
    return arguments if slug else {"scope": "global", **arguments}


@pytest.mark.parametrize("slug", ["first", None])
def test_customer_story_and_email_records_use_the_same_ordubashy_conversation_api(host, ordubashy, slug):
    found = records(host, slug)
    assert {record["task_type"] for record in found.values()} == {"customer", "story", "email"}
    for kind, record in found.items():
        source = source_of(host, record, slug)
        sent = data(operate(host, slug, "owner_conversation_send", source=source, text=f"Owner on the {kind}", **scoped(slug)))
        assert sent["message"]["body"] == f"Owner on the {kind}"
        # Ordubashy takes part as an active global and answers in the same conversation, through the application the agent uses.
        agent = host.runtime.resolver.invocation_context(Principal(ordubashy, "agent"), business_id=host.first if slug else None)
        arguments = {"source": source, **({} if slug else {"scope": "global"})}
        assert ok(invoke(host, agent, "owner_conversation_send", {**arguments, "text": f"Ordubashy on the {kind}"}))["message"]
        read = data(operate(host, slug, "owner_conversation_get", source=source, **scoped(slug)))
        assert [message["body"] for message in read["messages"]] == [f"Owner on the {kind}", f"Ordubashy on the {kind}"]
        peers = data(operate(host, slug, "owner_conversation_peers", source=source, **scoped(slug)))["peers"]
        assert [(peer["agent_id"], peer["channel"]) for peer in peers] == [(ordubashy, "private")]
        # Nobody can select another agent for a record of any type.
        assert refused(operate(host, slug, "owner_conversation_get", source=source, agent_id=host.bare, **scoped(slug))) in {"forbidden", "policy_denied"}
        assert refused(operate(host, slug, "owner_conversation_send", source=source, text="x", peer_role="global-worker",
                               **scoped(slug))) == "validation_error"
    # One null-peer conversation per record, all under the same key shape; no other conversation and no forwarded notice exists.
    stored = rows(host, "SELECT source_key, peer_agent_id FROM owner_conversations ORDER BY source_key")
    prefix = f"source:{host.first if slug else 'global'}:task:"
    assert sorted(row["source_key"] for row in stored) == sorted(prefix + record["id"] for record in found.values())
    assert {row["peer_agent_id"] for row in stored} == {None}
    assert rows(host, "SELECT count(*) AS n FROM agent_messages") == [{"n": 0}]


def test_a_record_conversation_does_not_reach_an_agent_without_the_grant_or_another_scope(host, ordubashy):
    found = records(host, "first")
    source = source_of(host, found["customer"], "first")
    data(operate(host, "first", "owner_conversation_send", source=source, text="Private"))
    assert code(invoke(host, host.bare_context, "owner_conversation_get", {"source": source})) in {"forbidden", "policy_denied"}
    assert refused(operate(host, "second", "owner_conversation_get", source=source)) in {"forbidden", "not_found", "validation_error"}
