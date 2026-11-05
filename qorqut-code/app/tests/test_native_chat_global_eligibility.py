"""Native Chat operates only for an active supported global (Manager review 2400).

Contract: ``native_chat.store.active_profile`` and ``observed_agents`` admit an identity only when
``agents.global_scope.global_profile_data`` holds for its principal-joined row: an active agent principal, an active profile
row and stored profile, ecosystem scope, a supported global role and no business home; a malformed profile fails closed. The
returned profile keeps its shape. ``any_status=True`` still returns the historical row for the separately authorized
transcript reader. Chat and the input arbiter read the store directly, so a deactivated principal is refused before anything
is admitted or typed.

Real: the stock host assembly, the owner's catalog publication and ``agents_create``, and the native Chat graph of
``native_chat_support`` with its scripted pane. Synthetic: the tmp instance and the stored-row edits each case makes. No
agent, terminal client, model, provider, network or operational data is used.
"""

import json

import pytest

from qorqut_operations.contracts import DomainError

from bazarlyk.native_chat import store
from bazarlyk.native_chat.contracts import InputRequest, TYPE_DIRECT
from host_foundation_support import (build_instance, create_worker, historical_business_agent, historical_founder,
                                     register_sources)
from native_chat_support import Native, count, rows

PUBLIC = {"agent_id", "name", "workspace", "profile_json", "revision", "home_business_id", "business_slug"}


def stored(instance, agent_id):
    with instance.database.read() as uow:
        return dict(uow.connection.execute(
            "SELECT a.status, a.home_business_id, a.profile_json, p.kind, p.active FROM agent_profiles a "
            "JOIN principals p ON p.id=a.agent_id WHERE a.agent_id=?", (agent_id,)).fetchone())


def restore(instance, agent_id, saved):
    with instance.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET status=?, home_business_id=?, profile_json=? WHERE agent_id=?",
                               (saved["status"], saved["home_business_id"], saved["profile_json"], agent_id))
        uow.connection.execute("UPDATE principals SET kind=?, active=? WHERE id=?", (saved["kind"], saved["active"], agent_id))


def edited(profile_json, **changes):
    return json.dumps({**json.loads(profile_json), **changes})


def reads(instance, agent_id):
    with instance.database.read() as uow:
        return (store.active_profile(uow.connection, agent_id, instance.paths),
                {row["agent_id"] for row in store.observed_agents(uow.connection, instance.paths)},
                store.active_profile(uow.connection, agent_id, instance.paths, any_status=True))


def test_only_an_active_supported_global_is_an_active_profile_or_observed_and_history_still_reads(tmp_path):
    instance, ids = build_instance(tmp_path)
    register_sources(instance, ids["first"], "first")
    try:
        agent = create_worker(instance, "colleague")
        accepted, observed, historical = reads(instance, agent)
        assert set(accepted) == PUBLIC and accepted["agent_id"] == agent and accepted["home_business_id"] is None
        assert observed == {agent} and historical == accepted
        # The retired Founder is stored active with an active principal, yet is never an active profile or observed; its
        # historical transcript read keeps the same public shape.
        founder = historical_founder(instance)
        retired, seen, history = reads(instance, founder)
        assert retired is None and founder not in seen and set(history) == PUBLIC and history["agent_id"] == founder
        saved = stored(instance, agent)
        cases = {
            "inactive principal": ("UPDATE principals SET active=0 WHERE id=?", ()),
            "wrong-kind principal": ("UPDATE principals SET kind='system' WHERE id=?", ()),
            "inactive stored status": ("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?",
                                       (edited(saved["profile_json"], status="inactive"),)),
            "business scope without a home": ("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?",
                                              (edited(saved["profile_json"], scope={"kind": "business", "business_id": ids["first"]}),)),
            "retired role": ("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?",
                             (edited(saved["profile_json"], role={**json.loads(saved["profile_json"])["role"], "id": "business-agent"}),)),
            "malformed scope and role": ("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?",
                                         (edited(saved["profile_json"], scope="ecosystem", role=["global-marketing"]),)),
            # Storage admits only valid JSON; a valid document that is not a profile object still fails closed.
            "non-object profile": ("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?", ('["global-marketing"]',)),
        }
        for name, (sql, parameters) in cases.items():
            with instance.database.transaction() as uow:
                uow.connection.execute(sql, (*parameters, agent))
            active, observed, historical = reads(instance, agent)
            assert active is None and agent not in observed and not observed, name
            # The historical read is unchanged: the same public shape, with no active credentials required.
            assert set(historical) == PUBLIC and historical["agent_id"] == agent, name
            restore(instance, agent, saved)
            assert reads(instance, agent)[0] == accepted, name
        # A business-home identity, even with an active stored status and its old membership, is history only.
        legacy = historical_business_agent(instance, ids["first"])
        active, observed, historical = reads(instance, legacy)
        assert active is None and legacy not in observed and set(historical) == PUBLIC and historical["home_business_id"] == ids["first"]
        # Retirement: an inactive profile row is not active, and history still reads it.
        with instance.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", (agent,))
        active, observed, historical = reads(instance, agent)
        assert active is None and agent not in observed and historical["agent_id"] == agent
    finally:
        try:
            instance.runtime.release_instance()
        finally:
            instance.database.close()


def deactivate(native):
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (native.agent,))


def test_chat_and_the_arbiter_refuse_a_deactivated_principal_before_anything_is_admitted_or_typed(tmp_path):
    native = Native(tmp_path)
    native.publish()
    pin = native.pin()
    deactivate(native)
    with pytest.raises(DomainError) as refused:
        native.chat.send(native.owner, native.agent, "Anyone there?", "chat-key-000001", pin)
    assert refused.value.code == "not_found"
    with pytest.raises(DomainError) as state:
        native.chat.state(native.owner, native.agent)
    assert state.value.code == "not_found"
    relay = native.arbiter.submit(InputRequest(native.agent, TYPE_DIRECT, "relay-source-key", "From a teammate."))
    assert (relay.state, relay.reason) == ("rejected", "agent_unavailable")
    native.pass_(2)
    assert count(native.runtime, "native_inputs") == 0 and native.pane.typed() == []


def test_an_input_admitted_while_active_is_never_typed_after_the_principal_is_deactivated(tmp_path):
    native = Native(tmp_path)
    native.publish()
    sent = native.chat.send(native.owner, native.agent, "Please summarize", "chat-key-000001", native.pin())
    assert sent["input"]["state"] == "waiting" and native.pane.typed() == []
    deactivate(native)
    native.pass_(2)                                                    # the backend no longer observes the identity
    assert native.pane.typed() == []
    assert native.arbiter.dispatch(native.agent) is False              # a direct dispatch refuses before delivery
    assert rows(native.runtime, "SELECT state, reason FROM native_inputs") == [{"state": "undelivered", "reason": "agent_inactive"}]
    assert native.pane.typed() == [] and native.queued == []
