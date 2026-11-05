"""Owner text to an agent terminal: queueing, ordering, uncertainty, secrets and recovery, with an injected terminal adapter."""
from dataclasses import replace
import json
import os

import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.telegram import store
from bazarlyk.telegram.outbox import enqueue_text
from bazarlyk.transport.agent_terminal_control import OwnerInputOutcome
from telegram_support import Env

SUBMITTED = OwnerInputOutcome("submitted", "processing_not_confirmed")


class FakeTerminal:
    """Records what would be typed. Scripted outcomes: an OwnerInputOutcome, or ``("after_checkpoint", outcome)``, or ``"crash"``."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    # The signature is deliberately strict: Telegram never passes ``allow_busy`` (that opt-in is Terminal Chat's alone), so a
    # running turn is still "composer busy" and queues here.
    def owner_input(self, row, text, *, workdir, checkpoint):
        self.calls.append({"agent": row["agent_id"], "text": text, "workdir": workdir})
        outcome = self.outcomes.pop(0) if self.outcomes else SUBMITTED
        if outcome == "crash":
            checkpoint()
            raise RuntimeError("terminal adapter failed")
        if type(outcome) is tuple:
            checkpoint()
            return outcome[1]
        if outcome.status in ("submitted", "uncertain"):
            checkpoint()
        return outcome


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


def owner_says(env, chat, text, **kwargs):
    env.api.push(env.message(chat, text, **kwargs))
    env.bridge.poll_once()
    return env.rows("SELECT * FROM telegram_owner_inputs ORDER BY created_at, rowid")[-1]


def inputs(env):
    return env.rows("SELECT * FROM telegram_owner_inputs ORDER BY created_at, rowid")


def notices(env):
    return [json.loads(row["payload_json"]) for row in env.rows("SELECT * FROM telegram_outbox WHERE source_kind='notice' ORDER BY created_at, rowid")]


def secret_request(env, chat="marketing", seconds=600):
    bridge = env.bridge
    request = {"request_key": "sms", "input_mode": "secret", "reason": "Login needs the code",
               "expires_at": store.plus(bridge.now(), seconds)}
    with bridge.write("test") as uow:
        message, row, _ = enqueue_text(bridge, uow, bridge.config.chat(chat), text="Send the SMS code", purpose="blocking_input",
                                       source_kind="agent", source_key=new_id(), agent_id=env.agents[chat], request=request)
    bridge.dispatcher.run()
    return env.one("SELECT * FROM telegram_messages WHERE id=?", message["id"])


def every_stored_text(env):
    """Everything the bridge persists in the database that could carry a body."""
    tables = ("telegram_updates", "telegram_messages", "telegram_outbox", "telegram_owner_inputs", "telegram_cleanup_jobs",
              "agent_messages", "inbox_events")
    return json.dumps({table: env.rows(f"SELECT * FROM {table}") for table in tables}, default=str)


# ----- delivery -------------------------------------------------------------------------------------------------


def test_an_owner_message_is_typed_once_with_the_canonical_correlation_and_never_to_another_agent(env):
    bridge = ready(env)
    row = owner_says(env, "founder", "please check the build")
    assert (row["state"], row["purpose"], row["agent_id"]) == ("queued", "owner_message", env.agents["founder"])
    canonical = env.one("SELECT * FROM agent_messages WHERE id=?", row["correlation_id"])
    assert canonical["recipient_id"] == env.agents["founder"]
    assert bridge.owner_inputs.run() is True
    terminal = bridge.owner_terminal
    assert len(terminal.calls) == 1
    call = terminal.calls[0]
    assert call["agent"] == env.agents["founder"] and call["workdir"] == bridge.spool.directory
    assert row["correlation_id"] in call["text"] and "please check the build" in call["text"] and canonical["body"] in call["text"]
    assert "inbox" in call["text"] and "not blanket authorization" in call["text"]
    done = env.one("SELECT * FROM telegram_owner_inputs")
    assert (done["state"], done["attempts"]) == ("submitted", 1) and done["submitted_at"]
    bridge.owner_inputs.run()
    assert len(terminal.calls) == 1  # never typed again


def test_a_reply_to_a_bot_message_is_still_input_for_the_chats_agent(env):
    bridge = ready(env)
    with bridge.write("test") as uow:
        enqueue_text(bridge, uow, bridge.config.chat("marketing"), text="Draft is ready", purpose="update", source_kind="agent",
                     source_key="one", agent_id=env.agents["marketing"])
    bridge.dispatcher.run()
    provider = env.api.sent[0]["message_id"]
    owner_says(env, "marketing", "looks good, ship it", reply_to=provider)
    bridge.owner_inputs.run()
    call = bridge.owner_terminal.calls[0]
    assert call["agent"] == env.agents["marketing"]
    assert "In reply to your earlier message" in call["text"] and "Draft is ready" in call["text"]


def test_every_owner_text_queues_direct_input_for_the_exact_agent_in_order(env):
    bridge = ready(env)
    owner_says(env, "founder", "first")
    owner_says(env, "founder", "second")
    owner_says(env, "marketing", "elsewhere")
    while bridge.owner_inputs.run():
        pass
    calls = bridge.owner_terminal.calls
    assert [c["agent"] for c in calls].count(env.agents["founder"]) == 2 and len(calls) == 3
    founder = [c["text"] for c in calls if c["agent"] == env.agents["founder"]]
    assert "first" in founder[0] and "second" in founder[1]


def test_a_busy_terminal_queues_with_one_notice_per_episode_and_the_head_blocks_the_line(env):
    bridge = ready(env)
    bridge.owner_terminal.outcomes[:] = [OwnerInputOutcome("deferred", "composer_busy")] * 2
    first = owner_says(env, "founder", "first")
    owner_says(env, "founder", "second")
    bridge.owner_inputs.run()
    rows = inputs(env)
    assert [r["state"] for r in rows] == ["queued", "queued"]
    assert rows[0]["deferral_reason"] == "composer_busy" and rows[0]["attempts"] == 1 and rows[0]["notice_sent_at"]
    assert rows[1]["deferral_reason"] is None  # behind the head: never attempted, order kept
    assert len(bridge.owner_terminal.calls) == 1
    assert len(notices(env)) == 1 and "Founder" in notices(env)[0]["text"] and "queued" in notices(env)[0]["text"]
    assert "first" not in notices(env)[0]["text"]
    # Not due yet: no attempt.
    bridge.owner_inputs.run()
    assert len(bridge.owner_terminal.calls) == 1
    assert bridge.status()["unavailable_targets"] == [{"agent_id": env.agents["founder"], "reason": "composer_busy", "queued": 1}]
    env.clock.advance(60)
    bridge.owner_inputs.run()  # deferred again: still one notice
    assert len(notices(env)) == 1
    env.clock.advance(60)
    while bridge.owner_inputs.run():
        pass
    assert [r["state"] for r in inputs(env)] == ["submitted", "submitted"]
    assert [c["text"].count("first") + c["text"].count("second") for c in bridge.owner_terminal.calls[-2:]] == [1, 1]
    assert first["id"] == inputs(env)[0]["id"]


def test_a_doubtful_outcome_is_uncertain_reported_once_and_never_typed_again(env):
    bridge = ready(env)
    bridge.owner_terminal.outcomes[:] = [OwnerInputOutcome("uncertain", "submit_unconfirmed")]
    owner_says(env, "ordubashy", "do the thing")
    bridge.owner_inputs.run()
    row = env.one("SELECT * FROM telegram_owner_inputs")
    assert (row["state"], row["error_code"]) == ("uncertain", "submit_unconfirmed")
    for _ in range(3):
        bridge.owner_inputs.run()
        env.clock.advance(120)
    assert len(bridge.owner_terminal.calls) == 1
    assert len([n for n in notices(env) if "may or may not" in n["text"]]) == 1
    assert bridge.status()["uncertain_inputs"] == 1


def test_a_crash_after_the_checkpoint_is_uncertain_and_a_restart_never_replays_it(env):
    bridge = ready(env)
    bridge.owner_terminal.outcomes[:] = ["crash"]
    owner_says(env, "founder", "half done")
    bridge.owner_inputs.run()
    assert env.one("SELECT state, error_code FROM telegram_owner_inputs") == {"state": "uncertain", "error_code": "internal_error"}
    # A process death between the checkpoint and the outcome leaves the row submitting; recovery makes it uncertain.
    other = owner_says(env, "founder", "second half")
    with bridge.write("test") as uow:
        uow.connection.execute("UPDATE telegram_owner_inputs SET state='submitting' WHERE id=?", (other["id"],))
    bridge.lock.release(); bridge.lock = None
    restarted = env.make_bridge()
    restarted.owner_terminal = bridge.owner_terminal
    env.ready(restarted)
    assert env.one("SELECT state, error_code FROM telegram_owner_inputs WHERE id=?", other["id"]) == {"state": "uncertain", "error_code": "interrupted"}
    for _ in range(3):
        restarted.owner_inputs.run()
    assert len(bridge.owner_terminal.calls) == 1
    assert len([n for n in notices(env) if "may or may not" in n["text"]]) == 2  # one per uncertain row, once each


def test_a_lost_receipt_is_retried_from_memory_and_the_text_is_never_retyped(env, monkeypatch):
    bridge = ready(env)
    owner_says(env, "founder", "hello")
    real = bridge.write
    closed = {"on": False}

    def gate(operation):
        if closed["on"] and operation == "input.settle":
            raise DomainError("maintenance", "closed")
        return real(operation)

    monkeypatch.setattr(bridge, "write", gate)
    closed["on"] = True
    bridge.owner_inputs.run()
    assert env.one("SELECT state FROM telegram_owner_inputs")["state"] == "submitting"
    bridge.owner_inputs.run()
    assert len(bridge.owner_terminal.calls) == 1
    closed["on"] = False
    bridge.owner_inputs.run()
    assert env.one("SELECT state FROM telegram_owner_inputs")["state"] == "submitted"
    assert len(bridge.owner_terminal.calls) == 1


def test_an_unsupported_terminal_fails_the_direct_path_and_says_so(env):
    bridge = ready(env)
    bridge.owner_terminal.outcomes[:] = [OwnerInputOutcome("deferred", "terminal_unsupported")]
    owner_says(env, "founder", "hello")
    bridge.owner_inputs.run()
    row = env.one("SELECT * FROM telegram_owner_inputs")
    assert (row["state"], row["error_code"]) == ("failed", "terminal_unsupported")
    assert any("could not be typed" in n["text"] and "inbox" in n["text"] for n in notices(env))
    assert env.one("SELECT count(*) n FROM agent_messages")["n"] == 1  # the canonical message is the durable record


def test_an_agent_that_became_inactive_or_rebound_is_never_typed_into(env):
    bridge = ready(env)
    owner_says(env, "founder", "hello")
    with bridge.write("test") as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (env.agents["founder"],))
    bridge.owner_inputs.run()
    assert env.one("SELECT state, error_code FROM telegram_owner_inputs") == {"state": "failed", "error_code": "agent_unavailable"}
    with bridge.write("test") as uow:
        uow.connection.execute("UPDATE principals SET active=1 WHERE id=?", (env.agents["founder"],))
    owner_says(env, "founder", "again")
    # The operator rebinds the Founder chat to another Global founder agent in the private file; the bridge verifies the new
    # binding as a new generation. What was accepted under the old binding is never typed into the new agent.
    with bridge.write("test") as uow:
        replacement = new_id()
        uow.repository("principals").insert({"id": replacement, "kind": "agent", "display_name": "Founder Two"})
        uow.repository("agent_profiles", business_id=None).insert({
            "agent_id": replacement, "principal_id": replacement, "namespace": "ecosystem", "name": "founder-two",
            "workspace": str(env.tmp / "agents" / "founder-two"), "status": "active",
            "profile_json": {"role": {"id": "founder"}, "display_name": "Founder Two"}})
    env.agents["founder"] = replacement
    env.write_config()
    assert bridge._ensure_ready(), bridge.health()
    bridge.owner_inputs.run()
    assert env.rows("SELECT state, error_code FROM telegram_owner_inputs")[-1] == {"state": "failed", "error_code": "binding_changed"}
    assert bridge.owner_terminal.calls == []


@pytest.mark.parametrize("change,code", [
    ("UPDATE agent_profiles SET workspace='{moved}' WHERE agent_id=?", "target_changed"),
    ("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.role.id','ordubashy') WHERE agent_id=?", "binding_changed"),
])
def test_a_profile_change_between_load_and_checkpoint_is_never_typed_into_the_new_target(env, change, code):
    bridge = ready(env)
    owner_says(env, "founder", "hello")
    typed = []

    class Changing:
        """The registration changes while the adapter waits: the checkpoint must refuse, so nothing is typed."""

        def owner_input(self, row, text, *, workdir, checkpoint):
            with env.database.transaction() as uow:
                uow.connection.execute(change.format(moved=env.tmp / "moved"), (row["agent_id"],))
            try:
                checkpoint()
            except Exception:  # noqa: BLE001 - the real adapter turns a failed checkpoint into a deferral
                return OwnerInputOutcome("deferred", "checkpoint_failed")
            typed.append(text)
            return OwnerInputOutcome("submitted", None)

    bridge.owner_terminal = Changing()
    bridge.owner_inputs.run()
    row = env.one("SELECT * FROM telegram_owner_inputs")
    assert row["state"] == "queued" and row["deferral_reason"] == "checkpoint_failed" and typed == []
    env.clock.advance(120)
    bridge.owner_inputs.run()
    row = env.one("SELECT * FROM telegram_owner_inputs")
    assert (row["state"], row["error_code"]) == ("failed", code) and typed == []


def test_the_control_cycle_runs_the_delivery_step(env):
    bridge = ready(env)
    owner_says(env, "founder", "hello")
    bridge.cycle()
    assert env.one("SELECT state FROM telegram_owner_inputs")["state"] == "submitted"


def test_without_an_attached_terminal_input_queues_and_nothing_is_started(env):
    bridge = ready(env)
    bridge.owner_terminal = None
    owner_says(env, "founder", "hello")
    bridge.owner_inputs.run()
    row = env.one("SELECT * FROM telegram_owner_inputs")
    assert (row["state"], row["deferral_reason"]) == ("queued", "terminal_unavailable")


# ----- secrets --------------------------------------------------------------------------------------------------


def test_a_secret_reply_reaches_only_the_terminal_and_its_traces_are_deleted(env):
    bridge = ready(env)
    prompt = secret_request(env)
    row = owner_says(env, "marketing", "482913")
    assert row["sensitive"] == 1 and row["spool_id"] and "482913" not in every_stored_text(env)
    spool_file = bridge.spool.directory / (row["spool_id"] + ".spool")
    assert spool_file.exists() and oct(spool_file.stat().st_mode & 0o777) == "0o600"
    bridge.owner_inputs.run()
    call = bridge.owner_terminal.calls[0]
    assert call["text"].endswith("\n482913") and "sms" in call["text"] and "Secret input" in call["text"]
    assert "482913" not in every_stored_text(env)
    assert env.one("SELECT state FROM telegram_owner_inputs")["state"] == "submitted"
    assert not spool_file.exists() and list(bridge.spool.directory.glob("*.paste")) == []
    # The known request and the owner's response are queued for deletion; nothing else is.
    jobs = {row["message_id"] for row in env.rows("SELECT message_id FROM telegram_cleanup_jobs")}
    assert jobs == {prompt["id"], row["message_id"]}
    while bridge.cleanup.run():
        pass
    assert {message for _, message in env.api.deleted} == {prompt["telegram_message_id"], env.one(
        "SELECT telegram_message_id FROM telegram_messages WHERE id=?", row["message_id"])["telegram_message_id"]}


def test_a_pending_secret_request_still_governs_after_a_newer_non_secret_request(env):
    bridge = ready(env)
    secret_request(env)
    env.clock.advance(seconds=5)
    request = {"request_key": "screen", "input_mode": "screen_action", "reason": "A dialog is blocking",
               "expires_at": store.plus(bridge.now(), 600)}
    with bridge.write("test") as uow:
        enqueue_text(bridge, uow, bridge.config.chat("marketing"), text="Also approve the consent screen", purpose="blocking_input",
                     source_kind="agent", source_key=new_id(), agent_id=env.agents["marketing"], request=request)
    bridge.dispatcher.run()
    env.clock.advance(seconds=5)
    row = owner_says(env, "marketing", "482913")
    assert row["sensitive"] == 1 and row["spool_id"] and "482913" not in every_stored_text(env)


def test_a_secret_that_cannot_be_typed_before_it_expires_is_discarded_and_never_resurrected(env):
    bridge = ready(env)
    prompt = secret_request(env, seconds=300)
    bridge.owner_terminal.outcomes[:] = [OwnerInputOutcome("deferred", "composer_busy")]
    row = owner_says(env, "marketing", "739201")
    bridge.owner_inputs.run()
    assert env.one("SELECT state FROM telegram_owner_inputs")["state"] == "queued"
    queued = [n["text"] for n in notices(env) if "is queued for the" in n["text"]]
    assert len(queued) == 1 and "Only a redacted marker is in the agent's inbox" in queued[0] and "also in the agent's inbox" not in queued[0]
    spool_file = bridge.spool.directory / (row["spool_id"] + ".spool")
    assert spool_file.exists()
    env.clock.advance(301)
    # Even if the spool file survives a crash, an expired secret is never typed.
    bridge.owner_inputs.run()
    assert env.one("SELECT state, error_code FROM telegram_owner_inputs") == {"state": "expired", "error_code": "expired"}
    assert not spool_file.exists() and len(bridge.owner_terminal.calls) == 1
    assert any("expired before it could be delivered" in n["text"] for n in notices(env))
    assert {r["message_id"] for r in env.rows("SELECT message_id FROM telegram_cleanup_jobs")} >= {prompt["id"], row["message_id"]}
    assert "739201" not in every_stored_text(env)


def test_an_uncertain_secret_is_deleted_reported_and_not_replayed(env):
    bridge = ready(env)
    secret_request(env)
    bridge.owner_terminal.outcomes[:] = [OwnerInputOutcome("uncertain", "paste_unverified")]
    row = owner_says(env, "marketing", "615243")
    bridge.owner_inputs.run()
    assert env.one("SELECT state FROM telegram_owner_inputs")["state"] == "uncertain"
    assert not (bridge.spool.directory / (row["spool_id"] + ".spool")).exists()
    for _ in range(3):
        bridge.owner_inputs.run()
    assert len(bridge.owner_terminal.calls) == 1
    assert env.one("SELECT count(*) n FROM telegram_cleanup_jobs")["n"] == 2
    assert "615243" not in every_stored_text(env)


def test_a_missing_spool_file_fails_the_secret_without_typing(env):
    bridge = ready(env)
    secret_request(env)
    row = owner_says(env, "marketing", "111222")
    os.unlink(bridge.spool.directory / (row["spool_id"] + ".spool"))
    bridge.owner_inputs.run()
    assert env.one("SELECT state, error_code FROM telegram_owner_inputs") == {"state": "failed", "error_code": "spool_lost"}
    assert bridge.owner_terminal.calls == []


def test_a_restart_finishes_secret_traces_a_crash_left_behind(env):
    bridge = ready(env)
    prompt = secret_request(env)
    row = owner_says(env, "marketing", "998877")
    # Crash between the terminal outcome and its cleanup: the row is final but no deletion was queued.
    with bridge.write("test") as uow:
        uow.connection.execute("UPDATE telegram_owner_inputs SET state='submitted', submitted_at=?, spool_id=spool_id WHERE id=?",
                               (bridge.now(), row["id"]))
    assert env.rows("SELECT * FROM telegram_cleanup_jobs") == []
    bridge.owner_inputs.run()
    assert {r["message_id"] for r in env.rows("SELECT message_id FROM telegram_cleanup_jobs")} == {prompt["id"], row["message_id"]}
    bridge.lock.release(); bridge.lock = None
    restarted = env.make_bridge()
    env.ready(restarted)
    assert list(restarted.spool.directory.glob("*.spool")) == []  # the startup sweep removed the orphan


def test_a_stored_but_unrouted_secret_update_keeps_its_spool_file_across_a_restart_and_is_then_delivered(env, monkeypatch):
    bridge = ready(env)
    secret_request(env)
    # The crash point after the batch and its cursor committed but before routing: the body is only in the spool file.
    monkeypatch.setattr(bridge.inbound, "process_pending", lambda: False)
    env.api.push(env.message("marketing", "553311"))
    bridge.poll_once()
    monkeypatch.undo()
    stored = env.one("SELECT state, normalized_json FROM telegram_updates")
    spool_id = json.loads(stored["normalized_json"])["spool_id"]
    assert stored["state"] == "received" and "553311" not in stored["normalized_json"] and env.rows("SELECT * FROM telegram_owner_inputs") == []
    assert (bridge.spool.directory / (spool_id + ".spool")).exists()
    bridge.lock.release(); bridge.lock = None
    restarted = env.make_bridge()
    env.ready(restarted)
    restarted.owner_terminal = FakeTerminal()
    assert (restarted.spool.directory / (spool_id + ".spool")).exists()  # the startup sweep does not treat it as an orphan
    restarted.inbound.process_pending()
    assert env.one("SELECT state, sensitive FROM telegram_owner_inputs") == {"state": "queued", "sensitive": 1}
    restarted.owner_inputs.run()
    assert restarted.owner_terminal.calls[0]["text"].endswith("\n553311") and "553311" not in every_stored_text(env)
    assert env.one("SELECT state FROM telegram_owner_inputs")["state"] == "submitted"
    assert not (restarted.spool.directory / (spool_id + ".spool")).exists()


def test_the_terminal_receipt_is_kept_on_submitted_and_uncertain_inputs_and_never_carries_text(env):
    bridge = ready(env)
    receipt = {"client": "claude", "composer": "c" * 16, "target": "a" * 64}
    bridge.owner_terminal = FakeTerminal(OwnerInputOutcome("submitted", "processing_not_confirmed", receipt),
                                         OwnerInputOutcome("uncertain", "paste_unverified", receipt),
                                         OwnerInputOutcome("deferred", "target_busy"))
    owner_says(env, "founder", "first message")
    bridge.owner_inputs.run()
    owner_says(env, "founder", "second message")
    bridge.owner_inputs.run()
    rows = inputs(env)
    assert [(row["state"], json.loads(row["receipt_json"])) for row in rows] == [("submitted", receipt), ("uncertain", receipt)]
    assert "first message" not in rows[0]["receipt_json"]
    owner_says(env, "ordubashy", "third message")
    bridge.owner_inputs.run()
    deferred = inputs(env)[-1]
    assert deferred["state"] == "queued" and deferred["receipt_json"] is None


def test_a_long_owner_text_keeps_its_identifying_line_when_the_full_header_would_not_fit(env):
    from bazarlyk.telegram.inbound import Inbound
    from bazarlyk.telegram.inbox_port import MAX_BODY
    inbound = Inbound(env.bridge)
    message = {"id": "m-1"}
    reply = {"id": "r-1", "direction": "outbound", "body": "an earlier question", "body_redacted": 0, "purpose": "reply"}
    item = {"reply": {"message_id": 5}}
    request = {"request_key": "k", "input_mode": "text", "request_reason": "because"}
    short = inbound._body(item, "hello", reply, request, False, message, None)
    assert short.startswith("[Telegram message m-1]") and "In reply to" in short and "Request k" in short and short.endswith("hello")
    text = "x" * (MAX_BODY - 40)
    tight = inbound._body(item, text, reply, request, False, message, None)
    assert tight == "[Telegram message m-1]\n" + text and len(tight) <= MAX_BODY
    assert inbound._body(item, "y" * MAX_BODY, reply, request, False, message, None) == "y" * MAX_BODY
