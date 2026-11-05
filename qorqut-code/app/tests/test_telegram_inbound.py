"""Inbound owner text in the agent chats: literal text, refusals, media, replies, secrets and canonical inbox handoff."""
from datetime import timedelta
import json

import pytest

from qorqut_operations.contracts import new_id
from bazarlyk.telegram import store
from bazarlyk.telegram.outbox import enqueue_text
from telegram_support import CHAT_IDS, OWNER_TG
from test_telegram_owner_input import env, every_stored_text, inputs, notices, owner_says, ready, secret_request  # noqa: F401


def canonical_bodies(env):
    return [row["body"] for row in env.rows("SELECT body FROM agent_messages ORDER BY created_at, rowid")]


def test_owner_text_is_literal_crlf_becomes_lf_and_tabs_and_newlines_survive(env):
    ready(env)
    owner_says(env, "founder", "line one\r\nline two\r\n\tindented\n\nend  ")
    body = canonical_bodies(env)[0]
    assert body.endswith("line one\nline two\n\tindented\n\nend  ") and "\r" not in body
    inbound = env.one("SELECT * FROM telegram_messages WHERE direction='inbound'")
    assert inbound["body"] == "line one\nline two\n\tindented\n\nend  " and inbound["purpose"] == "owner"


@pytest.mark.parametrize("text,reason", [
    ("hello\x1b[201~inject", "control_characters"),
    ("nul\x00byte", "control_characters"),
    ("bell\x07", "control_characters"),
    ("c1 control \x9b", "control_characters"),
    ("x" * 16001, "too_long"),
    ("   \n\t ", "empty"),
])
def test_unsafe_or_empty_text_is_refused_with_one_notice_and_no_handoff(env, text, reason):
    ready(env)
    env.api.push(env.message("founder", text))
    env.bridge.poll_once()
    assert env.rows("SELECT * FROM agent_messages") == [] and inputs(env) == []
    update = env.one("SELECT state, reason FROM telegram_updates")
    assert (update["state"], update["reason"]) == ("done", "refused_" + reason)
    assert len(notices(env)) == 1
    assert env.one("SELECT body FROM telegram_messages WHERE direction='inbound'")["body"] == f"[not delivered: {reason}]"
    assert text[:20] not in json.dumps(notices(env)) or reason == "empty"


def test_exactly_16000_characters_are_accepted(env):
    ready(env)
    owner_says(env, "founder", "y" * 16000)
    assert len(inputs(env)) == 1
    assert canonical_bodies(env)[0].endswith("y" * 16000) or canonical_bodies(env)[0] == "y" * 16000


def test_media_passes_only_the_caption_and_a_marker_with_one_notice_and_no_download(env):
    ready(env)
    env.api.push(env.message("marketing", "see the screenshot", extra={"photo": [{"file_id": "F1"}], "text": None}))
    update = env.message("marketing", None, extra={"caption": "see the screenshot", "photo": [{"file_id": "F1"}]})
    env.api.updates.clear()
    env.api.push(update)
    env.bridge.poll_once()
    body = canonical_bodies(env)[0]
    assert "see the screenshot" in body and "[Telegram attachment not delivered: photo]" in body
    assert len([n for n in notices(env) if "Attachments are not supported" in n["text"]]) == 1
    assert "get_file" not in env.api.calls and "F1" not in every_stored_text(env)


def test_a_bare_attachment_is_a_marker_only_message_and_an_empty_message_is_refused(env):
    ready(env)
    env.api.push(env.message("marketing", None, extra={"voice": {"file_id": "V"}, "document": {"file_id": "D"}}))
    env.bridge.poll_once()
    assert "[Telegram attachment not delivered: document, voice]" in canonical_bodies(env)[0]
    env.api.push(env.message("marketing", None))
    env.bridge.poll_once()
    assert len(canonical_bodies(env)) == 1
    assert env.one("SELECT reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")["reason"] == "refused_empty"


def test_an_inactive_agent_gets_a_refusal_and_no_message(env):
    bridge = ready(env)
    with bridge.write("test") as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (env.agents["marketing"],))
    env.api.push(env.message("marketing", "hello?"))
    bridge.poll_once()
    assert env.rows("SELECT * FROM agent_messages") == [] and inputs(env) == []
    assert any("not active" in n["text"] for n in notices(env))


def test_replying_to_a_text_request_answers_it_and_a_plain_message_does_not(env):
    bridge = ready(env)
    request = {"request_key": "which", "input_mode": "text", "reason": "Pick one", "expires_at": store.plus(bridge.now(), 3600)}
    with bridge.write("test") as uow:
        message, _, _ = enqueue_text(bridge, uow, bridge.config.chat("founder"), text="Which region?", purpose="blocking_input",
                                     source_kind="agent", source_key=new_id(), agent_id=env.agents["founder"], request=request)
    bridge.dispatcher.run()
    provider = env.api.sent[0]["message_id"]
    owner_says(env, "founder", "unrelated note")
    assert env.one("SELECT request_state FROM telegram_messages WHERE id=?", message["id"])["request_state"] == "pending"
    row = owner_says(env, "founder", "eu-west", reply_to=provider)
    assert env.one("SELECT request_state FROM telegram_messages WHERE id=?", message["id"])["request_state"] == "answered"
    body = env.one("SELECT body FROM agent_messages WHERE id=?", row["correlation_id"])["body"]
    assert "[Request which (text): Pick one]" in body and "eu-west" in body
    assert 'In reply to your earlier message' in body and "Which region?" in body


def test_a_reply_to_a_message_the_bridge_does_not_know_is_still_input_for_the_chats_agent(env):
    ready(env)
    row = owner_says(env, "ordubashy", "about that old thing", reply_to=999999)
    assert row["agent_id"] == env.agents["ordubashy"]
    assert "not in the bridge history" in canonical_bodies(env)[0]


def test_a_reply_in_one_chat_never_redirects_to_another_chats_agent(env):
    bridge = ready(env)
    with bridge.write("test") as uow:
        enqueue_text(bridge, uow, bridge.config.chat("marketing"), text="Marketing update", purpose="update", source_kind="agent",
                     source_key="m1", agent_id=env.agents["marketing"])
    bridge.dispatcher.run()
    provider = env.api.sent[0]["message_id"]
    row = owner_says(env, "founder", "replying across chats", reply_to=provider)
    assert row["agent_id"] == env.agents["founder"]
    assert "not in the bridge history" in canonical_bodies(env)[0]


def test_the_canonical_message_and_the_direct_input_share_one_correlation_atomically(env):
    ready(env)
    row = owner_says(env, "founder", "one message")
    message = env.one("SELECT * FROM agent_messages")
    assert row["correlation_id"] == message["id"] and row["inbox_event_id"] == str(message["event_id"])
    event = env.one("SELECT * FROM inbox_events WHERE id=?", message["event_id"])
    assert event["source_type"] == "agent_messages" and event["actor_id"] == env.one("SELECT owner_principal_id o FROM installation")["o"]
    delivery = env.one("SELECT * FROM inbox_deliveries WHERE event_id=?", message["event_id"])
    assert delivery["agent_id"] == env.agents["founder"]


# ----- secrets --------------------------------------------------------------------------------------------------


def test_all_owner_text_in_a_chat_is_sensitive_until_the_first_response_then_ordinary_again(env):
    ready(env)
    secret_request(env)
    first = owner_says(env, "marketing", "not even a reply, but sensitive")
    assert first["sensitive"] == 1
    env.clock.advance(2)  # Telegram dates have one-second resolution: a later message is one sent after the answer
    second = owner_says(env, "marketing", "an ordinary message afterwards")
    assert second["sensitive"] == 0 and second["spool_id"] is None
    stored = every_stored_text(env)
    assert "not even a reply" not in stored and "an ordinary message afterwards" in stored
    bodies = [row["body"] for row in env.rows("SELECT body FROM agent_messages ORDER BY created_at, rowid")]
    assert "not even a reply" not in json.dumps(bodies) and "Secret input" in bodies[0]
    # Before submission the redacted canonical text says the value is queued, never that it was already delivered.
    assert "queued for direct delivery" in bodies[0] and "was delivered directly" not in bodies[0]


def test_a_reply_to_a_secret_prompt_after_it_expired_stays_sensitive_and_is_refused(env):
    bridge = ready(env)
    prompt = secret_request(env, seconds=120)
    env.clock.advance(121)
    bridge.dispatcher.run()
    owner_says_row = None
    env.api.push(env.message("marketing", "the code was 246810", reply_to=prompt["telegram_message_id"]))
    bridge.poll_once()
    assert env.rows("SELECT * FROM agent_messages") == [] and inputs(env) == []
    stored = env.one("SELECT * FROM telegram_messages WHERE direction='inbound'")
    assert stored["sensitive"] == 1 and stored["body"] is None and stored["body_redacted"] == 1
    assert "246810" not in every_stored_text(env)
    assert list(bridge.spool.directory.glob("*.spool")) == []
    assert any("expired or was already answered" in n["text"] for n in notices(env))
    # The refused response is queued for deletion together with the expired prompt.
    jobs = {row["message_id"] for row in env.rows("SELECT message_id FROM telegram_cleanup_jobs")}
    assert stored["id"] in jobs and prompt["id"] in jobs
    assert owner_says_row is None


def test_secret_text_with_control_characters_is_refused_without_a_spool_file_or_any_stored_body(env):
    bridge = ready(env)
    secret_request(env)
    env.api.push(env.message("marketing", "12\x1b[31m34"))
    bridge.poll_once()
    assert inputs(env) == [] and list(bridge.spool.directory.glob("*.spool")) == []
    assert "12" not in [row["body"] for row in env.rows("SELECT body FROM telegram_messages WHERE direction='inbound'")]
    assert "\x1b" not in every_stored_text(env)
    assert env.one("SELECT request_state FROM telegram_messages WHERE purpose='blocking_input'")["request_state"] == "pending"


def test_a_secret_reply_never_reaches_the_update_table_history_or_traces(env):
    bridge = ready(env)
    secret_request(env)
    owner_says(env, "marketing", "s3cr3t-value-778")
    assert "s3cr3t-value-778" not in every_stored_text(env)
    assert "s3cr3t-value-778" not in json.dumps(bridge.status(), default=str)
    update = env.one("SELECT normalized_json FROM telegram_updates ORDER BY update_id DESC LIMIT 1")
    assert "text" not in json.loads(update["normalized_json"]) and json.loads(update["normalized_json"])["sensitive"] is True


# ----- secret classification is fixed by when the owner sent the text -----------------------------------------------

LATE = "SECRET_LATE_DELIVERY_98221"


def late_update(env, text, *, chat="marketing", **kwargs):
    """An update built now (its Telegram date is now) but pushed and ingested later."""
    return env.message(chat, text, **kwargs)


def test_text_sent_while_a_secret_request_was_open_stays_secret_when_polling_is_delayed_past_its_expiry(env):
    bridge = ready(env)
    secret_request(env, seconds=600)
    sent_during = late_update(env, LATE)
    env.clock.advance(601)
    env.api.push(sent_during)
    bridge.poll_once()
    assert LATE not in every_stored_text(env)
    stored = env.one("SELECT * FROM telegram_messages WHERE direction='inbound'")
    assert stored["sensitive"] == 1 and stored["body"] is None and stored["body_redacted"] == 1
    assert inputs(env) == [] and env.rows("SELECT * FROM agent_messages") == []
    assert list(bridge.spool.directory.glob("*.spool")) == []
    row = env.one("SELECT reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")
    assert row["reason"] == "refused_expired"
    assert any("expired" in json.loads(r["payload_json"])["text"] for r in env.rows("SELECT payload_json FROM telegram_outbox WHERE source_kind='notice'"))


@pytest.mark.parametrize("closing", ["answered", "cancelled"])
def test_text_sent_while_the_request_was_open_is_never_reclassified_after_it_closed_or_the_service_restarted(env, closing):
    bridge = ready(env)
    prompt = secret_request(env, seconds=600)
    opened = env.clock.now
    env.clock.advance(1)
    if closing == "answered":
        answer = owner_says(env, "marketing", "the first answer")
        assert answer["sensitive"] == 1
    else:
        with bridge.write("test") as uow:
            store.close_request(uow, store.message(uow, prompt["id"]), "cancelled", bridge.now())
    env.clock.advance(5)
    bridge.lock.release()
    bridge.lock = None
    restarted = env.make_bridge()
    env.ready(restarted)
    # Sent while the request was open (by its own date), yet only read after it closed and the service restarted.
    sent_during = late_update(env, LATE, at=opened + timedelta(milliseconds=500), update_id=env.next_update_id() + 50)
    env.api.push(sent_during)
    restarted.poll_once()
    assert LATE not in every_stored_text(env)
    late = env.one("SELECT * FROM telegram_messages WHERE update_id=?", sent_during["update_id"])
    assert late["sensitive"] == 1 and late["body"] is None
    assert env.one("SELECT reason FROM telegram_updates WHERE update_id=?", sent_during["update_id"])["reason"].startswith("refused_")


def test_text_sent_after_the_request_ended_is_ordinary_and_a_reply_to_the_prompt_is_secret_at_any_time(env):
    bridge = ready(env)
    prompt = secret_request(env, seconds=120)
    env.clock.advance(200)
    bridge.dispatcher.run()
    ordinary = owner_says(env, "marketing", "an unrelated note long after the request expired")
    assert ordinary["sensitive"] == 0
    env.api.push(env.message("marketing", LATE, reply_to=prompt["telegram_message_id"]))
    bridge.poll_once()
    assert LATE not in every_stored_text(env) and "an unrelated note" in every_stored_text(env)
    assert env.one("SELECT sensitive, body FROM telegram_messages WHERE direction='inbound' ORDER BY rowid DESC LIMIT 1")["sensitive"] == 1


def test_a_text_at_the_exact_closing_second_is_treated_as_inside_the_request(env):
    bridge = ready(env)
    secret_request(env, seconds=600)
    first = owner_says(env, "marketing", "the answer")
    tied = env.message("marketing", LATE, at=env.clock.now)
    env.api.push(tied)
    bridge.poll_once()
    assert first["sensitive"] == 1 and LATE not in every_stored_text(env)


SAME_SECOND = "SECRET_SAME_SECOND_72811"


def request_with_microseconds(env, seconds=600):
    """A secret request queued at a moment with a nonzero microsecond part, as a real clock has."""
    env.clock.advance(1, microseconds=100000)
    prompt = secret_request(env, seconds=seconds)
    assert prompt["occurred_at"].endswith(".100000Z")
    return prompt


def test_an_answer_sent_later_in_the_second_the_request_started_is_sensitive_and_only_spooled(env):
    bridge = ready(env)
    request_with_microseconds(env)
    env.clock.advance(microseconds=600000)  # 0.7 s into the second; its Telegram date is that second, 0.1 s before the request
    env.api.push(env.message("marketing", SAME_SECOND))
    bridge.poll_once()
    assert SAME_SECOND not in every_stored_text(env)
    stored = env.one("SELECT * FROM telegram_messages WHERE direction='inbound'")
    assert stored["sensitive"] == 1 and stored["body"] is None and stored["body_redacted"] == 1
    (queued,) = inputs(env)
    assert queued["sensitive"] == 1 and queued["spool_id"] and queued["state"] == "queued"
    assert bridge.spool.take(queued["spool_id"], bridge.now()) == SAME_SECOND  # the only copy is the private spool file
    assert env.one("SELECT reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")["reason"] is None


def test_the_same_second_answer_read_after_expiry_and_a_restart_is_discarded_and_never_delivered(env):
    bridge = ready(env)
    request_with_microseconds(env, seconds=60)
    env.clock.advance(microseconds=600000)
    answer = env.message("marketing", SAME_SECOND)
    env.clock.advance(61)
    bridge.lock.release()
    bridge.lock = None
    restarted = env.make_bridge()
    env.ready(restarted)
    env.api.push(answer)
    restarted.poll_once()
    assert SAME_SECOND not in every_stored_text(env)
    stored = env.one("SELECT * FROM telegram_messages WHERE direction='inbound'")
    assert stored["sensitive"] == 1 and stored["body"] is None and stored["body_redacted"] == 1
    assert inputs(env) == [] and env.rows("SELECT * FROM agent_messages") == []
    assert list(restarted.spool.directory.glob("*.spool")) == []
    assert env.one("SELECT reason FROM telegram_updates ORDER BY update_id DESC LIMIT 1")["reason"] == "refused_expired"


def test_a_text_dated_the_second_before_the_request_started_stays_ordinary(env):
    bridge = ready(env)
    request_with_microseconds(env)
    env.api.push(env.message("marketing", "ORDINARY_BEFORE_REQUEST", at=env.clock.now - timedelta(seconds=1)))
    bridge.poll_once()
    assert env.one("SELECT sensitive, body FROM telegram_messages WHERE direction='inbound'") == {
        "sensitive": 0, "body": "ORDINARY_BEFORE_REQUEST"}


@pytest.mark.parametrize("gap,ordinary", [(5, True), (0, False)])
def test_a_text_after_the_secret_answer_in_the_same_fetched_batch_is_ordinary_unless_sent_in_the_same_second(env, gap, ordinary):
    bridge = ready(env)
    secret_request(env, seconds=600)
    env.clock.advance(1)
    code = env.message("marketing", LATE)
    env.clock.advance(gap)
    follow_up = env.message("marketing", "FOLLOW_UP_IN_BATCH_4410")
    env.api.push(code)
    env.api.push(follow_up)
    bridge.poll_once()  # one fetched batch: the request is only closed in the database when the first text is routed
    assert LATE not in every_stored_text(env)
    first, second = env.rows("SELECT * FROM telegram_messages WHERE direction='inbound' ORDER BY rowid")
    assert first["sensitive"] == 1 and first["body"] is None
    if ordinary:
        assert second["sensitive"] == 0 and second["body"] == "FOLLOW_UP_IN_BATCH_4410"
        assert [row["sensitive"] for row in inputs(env)] == [1, 0]
        assert any("FOLLOW_UP_IN_BATCH_4410" in body for body in canonical_bodies(env))
    else:
        assert second["sensitive"] == 1 and second["body"] is None and "FOLLOW_UP_IN_BATCH_4410" not in every_stored_text(env)
        assert [row["sensitive"] for row in inputs(env)] == [1]
    assert spool_files(bridge) == {inputs(env)[0]["spool_id"]}


def test_a_secret_caption_with_an_attachment_gets_the_one_unsupported_attachment_notice_and_keeps_no_body(env):
    bridge = ready(env)
    secret_request(env, seconds=600)
    env.api.push(env.message("marketing", None, extra={"caption": LATE, "photo": [{"file_id": "F9"}]}))
    bridge.poll_once()
    assert LATE not in every_stored_text(env) and "F9" not in every_stored_text(env)
    assert len([n for n in notices(env) if "Attachments are not supported" in n["text"]]) == 1
    assert inputs(env)[0]["sensitive"] == 1 and inputs(env)[0]["spool_id"]
    assert LATE not in json.dumps(notices(env))


def test_updates_the_bridge_will_not_route_keep_no_body_in_the_database(env):
    bridge = ready(env)
    marker = "UNROUTED_BODY_5521"
    old = env.clock.now - timedelta(hours=3)
    for update in (
            env.message("founder", marker + " stranger", sender=OWNER_TG + 1),
            env.message("founder", marker + " bot", sender=OWNER_TG, is_bot=True),
            env.message("founder", marker + " channel-style", sender_chat=True),
            env.message("founder", marker + " before activation", at=old),
            env.message("urgent", marker + " urgent chat"),
            env.message("founder", marker + " service", extra={"new_chat_title": "x"}),
            {"update_id": env.next_update_id(), "message": {"message_id": 9, "date": int(env.clock.now.timestamp()),
                                                            "chat": {"id": 424242, "type": "supergroup"}, "from": {"id": OWNER_TG},
                                                            "text": marker + " unknown chat"}}):
        env.api.push(update)
    bridge.poll_once()
    assert marker not in every_stored_text(env) and len(env.rows("SELECT * FROM telegram_updates")) == 7
    assert env.rows("SELECT * FROM agent_messages") == []


def test_owner_text_the_bridge_refuses_keeps_only_the_refusal(env):
    bridge = ready(env)
    env.api.push(env.message("founder", "before\x1b[201~ CONTROL_TEXT_7788"))
    env.api.push(env.message("founder", "z" * 16001 + " LONG_TEXT_7788"))
    bridge.poll_once()
    stored = every_stored_text(env)
    assert "CONTROL_TEXT_7788" not in stored and "LONG_TEXT_7788" not in stored
    reasons = [r["reason"] for r in env.rows("SELECT reason FROM telegram_updates ORDER BY update_id")]
    assert reasons == ["refused_control_characters", "refused_too_long"]


# ----- spool retention needs no Telegram --------------------------------------------------------------------------


def spool_file(bridge, text, expires_at):
    return bridge.spool.put(text, expires_at)


def spool_files(bridge):
    return {path.stem for path in bridge.spool.directory.glob("*.spool")}


def test_expired_and_orphaned_secret_files_are_erased_at_startup_even_when_verification_fails(env):
    from bazarlyk.telegram.api import TelegramApiError
    bridge = ready(env)
    secret_request(env, seconds=600)
    owned = owner_says(env, "marketing", "SPOOLED_OWNED_VALUE")["spool_id"]
    expired = spool_file(bridge, "SPOOLED_EXPIRED", store.plus(bridge.now(), 30))
    orphan = spool_file(bridge, "SPOOLED_ORPHAN", store.plus(bridge.now(), 600))
    env.clock.advance(31)
    # A new process whose Telegram verification fails: it never becomes ready, yet the expired file and the orphan are erased
    # and the still-valid file that a queued input owns is kept.
    env.api.script("get_me", *[TelegramApiError("unreachable", "network down")] * 3)
    restarted = env.make_bridge()
    restarted.load_config()
    restarted.sweep_spool(startup=True)
    assert spool_files(restarted) == {owned}
    assert restarted.cycle() > 0 and not restarted._ready.is_set()
    assert spool_files(restarted) == {owned}


def test_start_erases_expired_secret_files_before_it_decides_the_integration_is_disabled_or_broken(env):
    bridge = ready(env)
    leftover = spool_file(bridge, "SPOOLED_LEFTOVER", store.plus(bridge.now(), 5))
    env.clock.advance(10)
    env.write_config(enabled=False)
    disabled = env.make_bridge()
    disabled.start()
    assert leftover not in spool_files(bridge) and disabled._threads == {}
    again = spool_file(bridge, "SPOOLED_AGAIN", store.plus(bridge.now(), 5))
    env.clock.advance(10)
    env.config_path.write_text("{ not json")
    broken = env.make_bridge()
    broken.start()
    assert again not in spool_files(bridge) and broken._threads == {} and "config" in broken.health()


@pytest.mark.parametrize("broken", [False, True])
def test_a_process_that_started_disabled_or_unconfigured_keeps_expiring_secret_files_while_it_lives(env, broken):
    import time
    bridge = ready(env)
    secret_request(env, seconds=60)
    kept = owner_says(env, "marketing", "SPOOLED_OWNED_VALUE")["spool_id"]  # still valid, and a queued input owns it
    if broken:
        env.config_path.write_text("{ not json")
    else:
        env.write_config(enabled=False)
    process = env.make_bridge()
    process.timings["spool_sweep_interval"] = 0.02
    process.start()
    try:
        # A still-valid file at start is kept, and something watches it: there is no control loop to do so.
        assert kept in spool_files(bridge) and list(process._threads) == ["spool"]
        env.clock.advance(120)
        deadline = time.monotonic() + 5
        while kept in spool_files(bridge) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert kept not in spool_files(bridge)
        while process._threads["spool"].is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not process._threads["spool"].is_alive()  # nothing left to watch: the thread ends by itself
    finally:
        process.stop()
    assert process._threads == {}


def test_a_running_but_not_ready_bridge_keeps_erasing_expired_secrets_periodically(env):
    from bazarlyk.telegram.api import TelegramApiError
    bridge = ready(env)
    soon = spool_file(bridge, "SPOOLED_SOON", store.plus(bridge.now(), 120))
    env.api.script("get_me", *[TelegramApiError("unreachable", "down")] * 10)
    bridge._ready.clear()
    bridge.cycle()
    assert soon in spool_files(bridge)  # still valid, and the next sweep is not due yet
    env.clock.advance(121)
    bridge.cycle()
    assert soon not in spool_files(bridge) and not bridge._ready.is_set()


def test_a_valid_file_that_a_queued_input_owns_survives_the_sweeps_and_is_never_recreated_after_expiry(env):
    bridge = ready(env)
    secret_request(env, seconds=600)
    row = owner_says(env, "marketing", "SPOOLED_QUEUED_VALUE")
    assert row["sensitive"] == 1 and row["spool_id"]
    path = bridge.spool.directory / (row["spool_id"] + ".spool")
    assert path.exists()
    bridge.sweep_spool()
    bridge.sweep_spool(startup=True)
    assert path.exists() and "SPOOLED_QUEUED_VALUE" in path.read_text()
    env.clock.advance(601)
    bridge.sweep_spool()
    assert not path.exists()
    assert bridge.spool.take(row["spool_id"], bridge.now()) is None
    assert "SPOOLED_QUEUED_VALUE" not in every_stored_text(env)


def test_a_fresh_orphan_is_kept_by_the_periodic_sweep_while_an_ingestion_may_be_in_flight(env):
    bridge = ready(env)
    fresh = spool_file(bridge, "SPOOLED_INFLIGHT", store.plus(bridge.now(), 600))
    bridge.sweep_spool()
    assert fresh in spool_files(bridge)
    bridge.sweep_spool(startup=True)
    assert fresh not in spool_files(bridge)
