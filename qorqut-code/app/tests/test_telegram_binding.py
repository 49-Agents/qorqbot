"""The pinned installation and Qorqut owner (Manager decision 971): a mismatch fails closed, never repins by itself, and an
operator's edit of the private file is the only way to a new binding generation."""
from datetime import timedelta
import json

import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.telegram import store
from bazarlyk.telegram.normalize import normalize_update
from bazarlyk.telegram.notify import notify
from bazarlyk.telegram.outbox import enqueue_text
from telegram_support import BOT_ID, START, Env
from test_telegram_owner_input import FakeTerminal

ORIGINAL_TEXT = "accepted under the original owner"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.bridge.owner_terminal = FakeTerminal()
    yield e
    e.close()


def ready(env):
    bridge = env.ready()
    bridge.owner_terminal = env.bridge.owner_terminal
    return bridge


def restart(env, old):
    """A new process over the same database and private file."""
    old.stop(timeout=5)
    bridge = env.make_bridge()
    bridge.owner_terminal = old.owner_terminal
    bridge.load_config()
    return bridge


def second_owner(env):
    other = new_id()
    with env.database.transaction() as uow:
        uow.repository("principals").insert({"id": other, "kind": "human", "display_name": "Second owner"})
    return other


def set_database_owner(env, principal):
    with env.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET owner_principal_id=?", (principal,))


def repin(env, principal):
    """The trusted operator makes another human the installation owner and edits the private file to match."""
    set_database_owner(env, principal)
    env.owner_id = principal
    env.write_config()


def state(env):
    return env.one("SELECT * FROM telegram_state")


def senders(env):
    return [row["sender_id"] for row in env.rows("SELECT sender_id FROM agent_messages ORDER BY created_at, rowid")]


def founder_text(bridge, text, **kwargs):
    with bridge.write("test") as uow:
        return enqueue_text(bridge, uow, bridge.config.chat("founder"), text=text, purpose="update", source_kind="agent",
                            source_key=new_id(), agent_id=bridge.config.chat("founder").agent_id, **kwargs)


# ----- the pins in configuration ----------------------------------------------------------------------------------


def test_an_enabled_configuration_without_both_pins_never_starts_and_nothing_is_detected_instead(env):
    for name in ("expected_installation_id", "expected_owner_principal_id"):
        env.write_config(mutate=lambda data, name=name: data.pop(name))
        bridge = env.make_bridge()
        bridge.start()
        assert "incomplete" in bridge.health()["config"]
        assert bridge._threads == {} and not bridge._ready.is_set()
        assert env.rows("SELECT * FROM telegram_state") == [] and env.api.calls == []


def test_first_activation_needs_both_pins_to_match_the_authoritative_records(env):
    other = "00000000-0000-4000-8000-0000000000d4"
    for field, code in (("expected_owner_principal_id", "owner_binding_mismatch"),
                        ("expected_installation_id", "installation_binding_mismatch")):
        env.write_config(mutate=lambda data, field=field: data.update({field: other}))
        bridge = env.make_bridge()
        bridge.load_config()
        assert bridge._ensure_ready() is False
        assert bridge.health() == {"binding": code}
        assert env.rows("SELECT * FROM telegram_state") == [] and env.api.calls == []
    env.write_config()
    bridge = env.make_bridge()
    bridge.load_config()
    assert bridge._ensure_ready() is True
    pinned = state(env)
    assert (pinned["installation_id"], pinned["owner_principal_id"], pinned["config_generation"]) == (
        env.installation_id, env.owner_id, 1)
    assert pinned["binding_effective_at"] == pinned["activation_at"]


# ----- a mismatch fails closed ------------------------------------------------------------------------------------


def test_a_changed_installation_owner_never_receives_the_original_telegram_owners_messages(env):
    bridge = ready(env)
    pinned = state(env)
    other = second_owner(env)
    set_database_owner(env, other)
    env.api.push(env.message("founder", "still me, the original Telegram owner"))
    # Ready was reached earlier and nothing has cycled since: polling itself revalidates the pin.
    assert bridge.poll_once() == 5.0
    assert env.api.calls.count("get_updates") == 0
    assert env.rows("SELECT * FROM telegram_updates") == [] and senders(env) == []
    assert not bridge._ready.is_set() and bridge.health() == {"binding": "owner_binding_mismatch"}
    # Status names the refusal without an identifier and repairs nothing; a cycle and a restart do not repin either.
    assert bridge.status()["binding"] == {"state": "owner_binding_mismatch", "generation": 1,
                                          "effective_at": pinned["binding_effective_at"]}
    bridge.cycle()
    assert state(env) == pinned
    restarted = restart(env, bridge)
    assert restarted._ensure_ready() is False and restarted.health() == {"binding": "owner_binding_mismatch"}
    assert restarted.poll_once() == 1.0 and env.api.calls.count("get_updates") == 0
    assert state(env) == pinned and senders(env) == []
    # A mismatch before readiness is refused the same way when the state already exists.
    assert restarted.cycle() > 0 and not restarted._ready.is_set()


def test_a_mismatch_closes_every_effect_and_only_local_housekeeping_and_status_continue(env):
    bridge = ready(env)
    founder_text(bridge, "queued before the mismatch")
    env.api.push(env.message("marketing", "hello marketing"))
    bridge.poll_once()
    inbound = env.one("SELECT id FROM telegram_messages WHERE direction='inbound'")
    with bridge.write("test") as uow:
        store.enqueue_cleanup(uow, inbound["id"], "test", now=bridge.now())
    assert env.one("SELECT state FROM telegram_owner_inputs")["state"] == "queued"
    set_database_owner(env, second_owner(env))
    calls = list(env.api.calls)
    with pytest.raises(DomainError) as refused:
        bridge.dispatcher.dispatch_one()
    assert refused.value.code == "owner_binding_mismatch"
    assert not bridge._ready.is_set()
    bridge._ready.set()  # a stale ready flag must not let the next effect through: each one reads the pin again
    with pytest.raises(DomainError) as refused:
        bridge.cleanup.one()
    assert refused.value.code == "owner_binding_mismatch"
    with pytest.raises(DomainError) as refused:  # direct input reads the pinned authority again for the delivery itself
        bridge.owner_inputs.run()
    assert refused.value.code == "owner_binding_mismatch"
    for action in (lambda uow: enqueue_text(bridge, uow, bridge.config.chat("founder"), text="a tool send", purpose="update",
                                            source_kind="agent", source_key=new_id(), agent_id=env.agents["founder"]),
                   lambda uow: notify(bridge, uow, "owner", {"event_key": "disk", "title": "Disk", "text": "Full", "reason": "x"})):
        with pytest.raises(DomainError) as refused:
            with bridge.write("test") as uow:
                action(uow)
        assert refused.value.code == "owner_binding_mismatch"
    assert bridge.owner_terminal.calls == [] and env.api.sent == [] and env.api.deleted == []
    assert env.api.calls == calls
    assert env.one("SELECT state FROM telegram_owner_inputs")["state"] == "queued"
    # A whole cycle fails closed too, and status still reports the counts without a body.
    bridge.cycle()
    assert not bridge._ready.is_set() and bridge.health() == {"binding": "owner_binding_mismatch"}
    status = bridge.status()
    assert status["binding"]["state"] == "owner_binding_mismatch" and "queued before" not in json.dumps(status)
    assert env.api.calls == calls


def test_local_spool_expiry_runs_throughout_a_mismatch(env):
    bridge = ready(env)
    spool_id = bridge.spool.put("SECRET-CODE-1234", store.plus(bridge.now(), 30))
    path = bridge.spool.directory / (spool_id + ".spool")
    assert path.exists()
    set_database_owner(env, second_owner(env))
    bridge.cycle()
    assert not bridge._ready.is_set()
    env.clock.advance(61)
    bridge.cycle()
    assert not path.exists() and bridge.health() == {"binding": "owner_binding_mismatch"}
    assert bridge.status()["binding"]["state"] == "owner_binding_mismatch"


def test_a_different_installation_is_refused_and_never_adopts_this_state(env):
    bridge = ready(env)
    before = state(env)
    other = new_id()
    # Editing the configured installation is not a repin.
    env.write_config(mutate=lambda data: data.update(expected_installation_id=other))
    assert bridge._ensure_ready() is False and bridge.health() == {"binding": "installation_binding_mismatch"}
    assert state(env) == before
    # A restored foreign database (its record and the file both name another installation) is refused as well.
    with env.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET installation_id=?", (other,))
    fresh = restart(env, bridge)
    assert fresh._ensure_ready() is False and fresh.health() == {"binding": "installation_binding_mismatch"}
    assert state(env) == before and fresh.poll_once() == 1.0
    assert env.api.calls.count("get_updates") == 0 and env.api.sent == []


# ----- restart, repin and generations -----------------------------------------------------------------------------


def test_an_unchanged_restart_keeps_the_generation_effective_time_and_cursor(env):
    bridge = ready(env)
    env.api.push(env.message("founder", "hello"))
    bridge.poll_once()
    before = state(env)
    assert before["config_generation"] == 1 and before["poll_offset"] > 0
    env.clock.advance(3600)
    restarted = restart(env, bridge)
    assert restarted._ensure_ready() is True
    after = state(env)
    for column in ("config_generation", "binding_effective_at", "activation_at", "poll_offset", "owner_principal_id",
                   "installation_id", "binding_fingerprint"):
        assert after[column] == before[column]
    # An unrelated setting changes neither the generation nor the cutoff.
    env.write_config(mutate=lambda data: data.update(dashboard_origin="http://qorqut:9999"))
    assert restarted._ensure_ready() is True
    assert state(env)["config_generation"] == 1 and state(env)["binding_effective_at"] == before["binding_effective_at"]


def test_an_explicit_owner_edit_permits_only_new_generation_and_new_second_traffic(env):
    bridge = ready(env)
    env.api.push(env.message("founder", ORIGINAL_TEXT))
    bridge.poll_once()
    assert senders(env) == [env.owner_id]
    before = state(env)
    assert before["poll_offset"] > 0
    original_owner = env.owner_id
    other = second_owner(env)
    repin(env, other)
    assert bridge._ensure_ready() is True, bridge.health()
    after = state(env)
    assert after["config_generation"] == 2 and after["owner_principal_id"] == other
    assert after["activation_at"] == before["activation_at"] and after["poll_offset"] == before["poll_offset"]
    assert after["installation_id"] == before["installation_id"]
    # The clock stands exactly on a whole second: the effective time is the next whole UTC second, never that second.
    assert after["binding_effective_at"] == "2026-10-01T12:00:01.000000Z"
    assert store.parse(after["binding_effective_at"]) > START
    prior = env.message("founder", "PRIOR-SECOND-BACKLOG", at=START - timedelta(seconds=1))
    prior_second = env.message("founder", "SAME-SECOND-BACKLOG", at=START)
    env.api.push(prior)
    env.api.push(prior_second)
    env.clock.advance(1)
    env.api.push(env.message("founder", "new-generation traffic", at=env.clock.now))
    bridge.poll_once()
    updates = {row["update_id"]: row for row in env.rows("SELECT * FROM telegram_updates")}
    for update in (prior, prior_second):
        row = updates[update["update_id"]]
        assert (row["state"], row["reason"]) == ("ignored", "before_binding_effective")
        assert "text" not in json.loads(row["normalized_json"])
    accepted = env.rows("SELECT sender_id, body FROM agent_messages ORDER BY created_at, rowid")
    assert [row["sender_id"] for row in accepted] == [original_owner, other]
    assert "new-generation traffic" in accepted[-1]["body"] and "BACKLOG" not in json.dumps(accepted)
    # What was accepted before keeps its canonical attribution; it is not rewritten to the new owner.
    assert "BACKLOG" not in json.dumps(env.rows("SELECT * FROM telegram_messages"))


def test_stored_old_traffic_and_queued_input_cannot_execute_after_a_repin(env):
    bridge = ready(env)
    env.api.push(env.message("founder", ORIGINAL_TEXT))
    bridge.poll_once()
    queued = env.one("SELECT * FROM telegram_owner_inputs")
    assert queued["state"] == "queued" and queued["config_generation"] == 1 and queued["owner_principal_id"] == env.owner_id
    # Fetched and stored but not yet routed when the operator repins (a crash or race between the two steps).
    stored = env.message("marketing", "STORED-BUT-NOT-ROUTED")
    items = bridge.inbound.prepare([normalize_update(stored)])
    assert items[0]["binding"]["generation"] == 1
    with bridge.write("test") as uow:
        store.store_batch(uow, BOT_ID, items, now=bridge.now(), polled_at=bridge.now())
    original_owner = env.owner_id
    other = second_owner(env)
    repin(env, other)
    env.clock.advance(2)
    assert bridge._ensure_ready() is True and state(env)["config_generation"] == 2
    bridge.inbound.process_pending()
    row = env.one("SELECT * FROM telegram_updates WHERE update_id=?", stored["update_id"])
    assert (row["state"], row["reason"]) == ("ignored", "binding_changed")
    assert "text" not in json.loads(row["normalized_json"]) and "STORED-BUT-NOT-ROUTED" not in json.dumps(env.rows("SELECT * FROM agent_messages"))
    # The delivery accepted under generation 1 is closed: never typed, never reattributed, never retargeted.
    bridge.owner_inputs.run()
    closed = env.one("SELECT * FROM telegram_owner_inputs")
    assert (closed["state"], closed["error_code"]) == ("failed", "binding_changed") and bridge.owner_terminal.calls == []
    assert closed["owner_principal_id"] == original_owner and closed["config_generation"] == 1
    assert senders(env) == [original_owner]


def test_an_effect_already_running_keeps_the_old_identity_and_never_authorizes_another(env):
    bridge = ready(env)
    founder_text(bridge, "one")
    founder_text(bridge, "two")
    original = {row["id"]: row for row in env.rows("SELECT * FROM telegram_outbox")}
    other = second_owner(env)
    real = env.api.send_message

    def send(chat_id, text, reply_to_message_id=None):
        result = real(chat_id, text, reply_to_message_id)
        if text == "one":  # the operator repins while this send is on the wire
            repin(env, other)
        return result

    env.api.send_message = send
    assert bridge.dispatcher.dispatch_one() is True
    sent = env.one("SELECT * FROM telegram_outbox WHERE state='sent'")
    assert sent["config_generation"] == 1 and sent["binding_fingerprint"] == original[sent["id"]]["binding_fingerprint"]
    assert bridge._ensure_ready() is True and state(env)["config_generation"] == 2
    while bridge.dispatcher.dispatch_one():
        pass
    second = env.one("SELECT * FROM telegram_outbox WHERE state!='sent'")
    assert (second["state"], second["error_code"]) == ("failed", "binding_changed")
    assert [item["text"] for item in env.api.sent] == ["one"]


def test_an_identity_that_moves_after_the_claim_and_before_the_send_sends_nothing(env):
    bridge = ready(env)
    founder_text(bridge, "one")
    real = bridge.verify_frozen

    def moved(uow, frozen):
        raise DomainError("owner_binding_mismatch", "moved")

    bridge.verify_frozen = moved
    with pytest.raises(DomainError):
        bridge.dispatcher.dispatch_one()
    bridge.verify_frozen = real
    row = env.one("SELECT * FROM telegram_outbox")
    assert row["state"] == "queued" and row["error_code"] == "owner_binding_mismatch" and env.api.sent == []


def test_history_is_limited_to_the_exact_chat_and_agent_binding_and_keeps_prior_identities(env):
    bridge = ready(env)
    chat = bridge.config.chat("founder")
    old, new, elsewhere = env.agents["founder"], new_id(), env.agents["marketing"]
    with bridge.write("test") as uow:
        uow.repository("principals").insert({"id": new, "kind": "agent", "display_name": "Founder Two"})
        now = bridge.now()
        for agent, body in ((old, "old agent text"), (new, "new agent text")):
            store.insert_message(uow, direction="outbound", chat=chat, purpose="reply", body=body, now=now, agent_id=agent)
        from dataclasses import replace
        store.insert_message(uow, direction="outbound", chat=replace(chat, chat_id=-9999), purpose="reply", body="other chat text",
                             now=now, agent_id=old)
        store.insert_message(uow, direction="outbound", chat=chat, purpose="reply", body="wrong agent text", now=now, agent_id=elsewhere)
    with bridge.read() as uow:
        activation = state(env)["activation_at"]
        texts = {agent: [row["body"] for row in store.history(uow, chat, agent, activation_at=activation, limit=10)]
                 for agent in (old, new)}
    assert texts == {old: ["old agent text"], new: ["new agent text"]}
    assert env.one("SELECT count(*) n FROM telegram_messages WHERE body LIKE '%text'")["n"] == 4
