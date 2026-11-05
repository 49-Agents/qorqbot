"""Bridge lifecycle, verification, the single poller, cursor durability, the outbox and cleanup (injected adapters only)."""
from datetime import timedelta
import json
import threading
import time

import pytest

from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.storage.quiescence import BoundedWriter
from bazarlyk.telegram import store
from bazarlyk.telegram.api import TelegramApiError
from bazarlyk.telegram.outbox import enqueue_text
from telegram_support import BOT_ID, CHAT_IDS, OWNER_TG, Env, START


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.close()


def queue(env, text="hi", chat="founder", source_key=None, purpose="reply", **kwargs):
    bridge = env.bridge
    with bridge.write("test") as uow:
        return enqueue_text(bridge, uow, bridge.config.chat(chat), text=text, purpose=purpose, source_kind="agent",
                            source_key=source_key or new_id(), agent_id=env.agents.get(chat), **kwargs)


def outbox(env, outbox_id):
    return env.one("SELECT * FROM telegram_outbox WHERE id=?", outbox_id)


# ----- readiness ------------------------------------------------------------------------------------------------


def test_ready_verifies_the_bot_webhook_and_every_chat_then_pins_state(env):
    bridge = env.ready()
    assert bridge.health() == {} and bridge.status()["state"] == "running"
    calls = env.api.calls
    assert calls.count("get_chat") == 6 and calls[:2] == ["get_me", "get_webhook_info"]
    assert calls.count("get_chat_member") == 12  # the bot's rights and the owner's membership in each chat
    state = env.one("SELECT * FROM telegram_state")
    assert (state["bot_id"], state["owner_telegram_user_id"], state["config_generation"], state["poll_offset"]) == (BOT_ID, OWNER_TG, 1, 0)


@pytest.mark.parametrize("arrange,component", [
    (lambda e: setattr(e.api, "bot", {"id": 5, "is_bot": True}), "verification"),
    (lambda e: setattr(e.api, "bot", {"id": BOT_ID, "is_bot": False}), "verification"),
    (lambda e: setattr(e.api, "webhook_url", "https://example.invalid/hook"), "verification"),
    (lambda e: e.api.chat_overrides.update({CHAT_IDS["urgent"]: {"type": "channel"}}), "verification"),
    (lambda e: e.api.chat_overrides.update({CHAT_IDS["signoffs"]: {"username": "public_chat"}}), "verification"),
    (lambda e: e.api.chat_overrides.update({CHAT_IDS["signoffs"]: {"active_usernames": ["x"]}}), "verification"),
    (lambda e: e.api.member_overrides.update({(CHAT_IDS["founder"], BOT_ID): {"status": "member"}}), "verification"),
    (lambda e: e.api.member_overrides.update({(CHAT_IDS["founder"], BOT_ID): {"status": "administrator", "can_delete_messages": False}}), "verification"),
    (lambda e: e.api.member_overrides.update({(CHAT_IDS["founder"], OWNER_TG): {"status": "left"}}), "verification"),
    (lambda e: e.api.member_overrides.update({(CHAT_IDS["founder"], OWNER_TG): {"status": "kicked"}}), "verification"),
    (lambda e: e.api.script("get_me", TelegramApiError("unreachable", "down")), "verification"),
])
def test_startup_refuses_a_wrong_bot_a_webhook_public_chats_missing_rights_or_a_missing_owner(env, arrange, component):
    arrange(env)
    bridge = env.bridge
    bridge.load_config()
    assert bridge._ensure_ready() is False
    assert component in bridge.health() and not bridge._ready.is_set()
    assert bridge.status()["state"] in ("stopped", "degraded")


def channels(env, *, bot):
    """Urgent Notifications as a channel, with the bot's rights there set by ``bot``."""
    def mutate(data):
        data["chats"]["urgent"]["type"] = "channel"
    env.write_config(mutate=mutate)
    env.api.chat_overrides[CHAT_IDS["urgent"]] = {"type": "channel"}
    env.api.member_overrides[(CHAT_IDS["urgent"], BOT_ID)] = bot


def test_channels_need_the_right_to_post_not_to_delete(env):
    channels(env, bot={"status": "administrator", "can_post_messages": True, "can_delete_messages": False})
    bridge = env.ready()
    assert bridge.health() == {} and bridge.config.chat("urgent").chat_type == "channel"


@pytest.mark.parametrize("bot", [
    {"status": "administrator", "can_post_messages": False, "can_delete_messages": True},
    {"status": "administrator", "can_delete_messages": True},
    {"status": "member"},
])
def test_a_channel_without_the_right_to_post_refuses_startup(env, bot):
    channels(env, bot=bot)
    bridge = env.bridge
    bridge.load_config()
    assert bridge._ensure_ready() is False and "verification" in bridge.health()


def test_a_receipt_that_cannot_be_committed_is_kept_and_recorded_later_and_the_send_is_never_repeated(env):
    bridge = env.ready()
    message, row, _ = queue(env, "hello")
    real_write, paused = bridge.write, {"on": True}

    def write(operation):
        if operation == "dispatch.receipt" and paused["on"]:
            raise DomainError("maintenance", "Writes are paused")
        return real_write(operation)

    bridge.write = write
    bridge.dispatcher.run()
    assert len(env.api.sent) == 1 and outbox(env, row["id"])["state"] == "dispatching"
    bridge.dispatcher.run()
    assert len(env.api.sent) == 1 and outbox(env, row["id"])["state"] == "dispatching"
    paused["on"] = False
    assert bridge.dispatcher.run() is True
    stored = outbox(env, row["id"])
    assert stored["state"] == "sent" and stored["provider_message_id"] == env.api.sent[0]["message_id"] and len(env.api.sent) == 1
    assert env.one("SELECT telegram_message_id FROM telegram_messages WHERE id=?", message["id"])["telegram_message_id"] == stored["provider_message_id"]


def test_one_failing_control_step_does_not_stall_the_others(env):
    bridge = env.ready()
    queue(env, "still goes out")

    def broken():
        raise DomainError("storage_error", "A step failed")

    bridge.steps.insert(0, broken)
    assert bridge.cycle() == 5
    assert bridge.health().get("worker") == "storage_error" and [m["text"] for m in env.api.sent] == ["still goes out"]
    bridge.steps.remove(broken)
    bridge.cycle()
    assert "worker" not in bridge.health()


def test_a_webhook_is_never_installed_or_removed_and_recovery_follows_the_owners_manual_removal(env):
    env.api.webhook_url = "https://example.invalid/hook"
    bridge = env.bridge
    bridge.load_config()
    assert bridge._ensure_ready() is False and bridge.health() == {"verification": "webhook_present"}
    assert not hasattr(env.api, "delete_webhook") and not hasattr(env.api, "set_webhook")
    env.api.webhook_url = ""
    assert bridge._ensure_ready() is True and bridge.health() == {}


def test_local_binding_checks_require_an_active_owner_and_the_exact_global_role(env):
    for mutate, name in [
        (lambda u: u.connection.execute("UPDATE agent_profiles SET status='inactive' WHERE name='founder'"), "founder"),
        (lambda u: u.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.role.id','business-agent') WHERE name='marketing'"), "marketing"),
        (lambda u: u.connection.execute("UPDATE principals SET active=0 WHERE display_name='Ordubashy'"), "ordubashy"),
    ]:
        environment = Env(env.tmp / name, poll_timeout=0.05)
        try:
            with environment.database.transaction() as uow:
                mutate(uow)
            environment.bridge.load_config()
            assert environment.bridge._ensure_ready() is False
            assert environment.bridge.health() == {"startup": "binding_invalid"}
        finally:
            environment.close()
    inactive_owner = Env(env.tmp / "owner")
    try:
        with inactive_owner.database.transaction() as uow:
            uow.connection.execute("UPDATE principals SET active=0 WHERE kind='human'")
        inactive_owner.bridge.load_config()
        assert inactive_owner.bridge._ensure_ready() is False and inactive_owner.bridge.health() == {"startup": "binding_invalid"}
    finally:
        inactive_owner.close()


def test_a_global_agent_must_not_be_a_business_agent(env):
    with env.database.transaction() as uow:
        business = uow.connection.execute("SELECT id FROM businesses LIMIT 1").fetchone()[0]
        uow.connection.execute("PRAGMA foreign_keys=OFF")
        uow.connection.execute("UPDATE agent_profiles SET home_business_id=?, namespace=? WHERE name='founder'", (business, business))
    env.bridge.load_config()
    assert env.bridge._ensure_ready() is False and env.bridge.health() == {"startup": "binding_invalid"}


def test_only_one_poller_per_installation_and_bot(env):
    first = env.ready()
    second = env.make_bridge()
    second.load_config()
    assert second._ensure_ready() is False and second.health() == {"startup": "lock_held"}
    first.lock.release()
    first.lock = None
    assert second._ensure_ready() is True


def test_a_binding_change_starts_a_new_generation_and_keeps_the_cursor_and_cutoff(env):
    bridge = env.ready()
    env.api.push(env.message("founder", "one"))
    bridge.poll_once()
    offset = env.one("SELECT poll_offset FROM telegram_state")["poll_offset"]
    assert offset > 100
    first_cutoff = env.one("SELECT activation_at FROM telegram_state")["activation_at"]
    def change(data):
        data["chats"]["urgent"]["chat_id"] = -2006
        data["activation_at"] = "2030-01-01T00:00:00Z"  # a later cutoff in the file never overrides the pinned one
    env.write_config(mutate=change)
    env.api.chat_ids["urgent"] = -2006
    bridge.lock.release(); bridge.lock = None
    restarted = env.make_bridge()
    restarted.load_config()
    assert restarted._ensure_ready() is True
    state = env.one("SELECT * FROM telegram_state")
    assert state["config_generation"] == 2 and state["poll_offset"] == offset and state["activation_at"] == first_cutoff


def test_a_different_bot_fails_closed_without_touching_the_saved_cursor(env):
    bridge = env.ready()
    env.api.push(env.message("founder", "kept"))
    bridge.poll_once()
    offset = env.one("SELECT poll_offset FROM telegram_state")["poll_offset"]
    bridge.lock.release(); bridge.lock = None
    other_bot = BOT_ID + 1
    env.token_file.write_text(f"{other_bot}:" + "B" * 35 + "\n")
    env.write_config(mutate=lambda data: data.update(expected_bot_id=other_bot))
    env.api.bot = {"id": other_bot, "is_bot": True}
    other = env.make_bridge()
    other.load_config()
    assert other._ensure_ready() is False and other.health() == {"startup": "binding_conflict"}
    state = env.one("SELECT bot_id, poll_offset FROM telegram_state")
    assert (state["bot_id"], state["poll_offset"]) == (BOT_ID, offset)


def test_a_changed_or_disabled_config_pauses_effects_until_it_is_verified_again(env):
    bridge = env.ready()
    queue(env)
    env.write_config(enabled=False)
    with pytest.raises(DomainError, match="not ready"):
        bridge.current_config()
    assert bridge.cycle() == 30 and bridge.health() == {"config": "disabled"}
    assert env.api.sent == [] and outbox(env, env.one("SELECT id FROM telegram_outbox")["id"])["state"] == "queued"
    env.write_config(enabled=True)
    assert bridge.cycle() < 1 and len(env.api.sent) == 1


def test_a_broken_config_file_degrades_the_bridge_and_never_raises_from_start(env):
    env.config_path.write_text("{ nope")
    bridge = env.bridge
    bridge.start()
    assert bridge.health() == {"config": "Telegram configuration is not valid JSON"} and not bridge._threads
    assert bridge.status()["state"] == "disabled"


def test_a_disabled_bridge_starts_no_threads_reads_no_token_and_reports_safely(tmp_path):
    e = Env(tmp_path, enabled=False)
    try:
        e.token_file.unlink()
        bridge = e.bridge
        bridge.start()
        assert not bridge._threads and bridge.api is None and bridge.status()["state"] == "disabled"
        assert e.api.calls == [] and e.rows("SELECT * FROM telegram_state") == []
        bridge.stop(timeout=1)
    finally:
        e.close()


# ----- poller, cursor and duplicates ----------------------------------------------------------------------------


def test_the_cursor_advances_only_with_a_committed_batch(env):
    bridge = env.ready()
    env.api.push(env.message("founder", "a", update_id=210))
    env.api.push(env.message("founder", "b", update_id=211))
    bridge.poll_once()
    assert env.one("SELECT poll_offset FROM telegram_state")["poll_offset"] == 212
    bridge.poll_once()
    assert env.api.polled_offsets[-2:] == [0, 212]
    assert [r["update_id"] for r in env.rows("SELECT update_id FROM telegram_updates ORDER BY 1")] == [210, 211]


def test_a_crash_before_the_batch_commits_leaves_the_cursor_and_redelivery_has_one_effect(env, monkeypatch):
    bridge = env.ready()
    env.api.push(env.message("founder", "only once", update_id=300))
    real = store.store_batch
    monkeypatch.setattr(store, "store_batch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash before commit")))
    with pytest.raises(RuntimeError):
        bridge.poll_once()
    assert env.one("SELECT poll_offset FROM telegram_state")["poll_offset"] == 0
    assert env.rows("SELECT * FROM telegram_updates") == [] and env.rows("SELECT * FROM agent_messages") == []
    monkeypatch.setattr(store, "store_batch", real)
    bridge.poll_once()
    bridge.poll_once()
    assert env.one("SELECT count(*) n FROM agent_messages")["n"] == 1
    assert env.one("SELECT poll_offset FROM telegram_state")["poll_offset"] == 301


def test_stored_unfinished_updates_are_processed_first_after_a_crash_between_commit_and_processing(env, monkeypatch):
    bridge = env.ready()
    env.api.push(env.message("founder", "survives", update_id=310))
    real = bridge.inbound.process_pending
    calls = []
    def crash_after_commit():
        calls.append(1)
        if len(calls) == 2:  # the first pass (before polling) has nothing stored; the second follows the batch commit
            raise RuntimeError("crash after commit")
        return real()
    monkeypatch.setattr(bridge.inbound, "process_pending", crash_after_commit)
    with pytest.raises(RuntimeError):
        bridge.poll_once()
    assert env.one("SELECT state FROM telegram_updates")["state"] == "received"
    assert env.one("SELECT poll_offset FROM telegram_state")["poll_offset"] == 311  # committed with the batch
    env.api.updates.clear()  # Telegram will not redeliver: the cursor already passed it
    bridge.lock.release(); bridge.lock = None
    restarted = env.make_bridge()
    env.ready(restarted)
    restarted.poll_once()
    assert env.one("SELECT state FROM telegram_updates")["state"] == "done"
    assert env.one("SELECT count(*) n FROM agent_messages")["n"] == 1
    restarted.poll_once()
    assert env.one("SELECT count(*) n FROM agent_messages")["n"] == 1


def test_a_duplicate_update_is_stored_and_applied_once(env):
    bridge = env.ready()
    update = env.message("founder", "dup", update_id=320)
    env.api.push(update)
    env.api.push(dict(update))
    bridge.poll_once()
    assert env.one("SELECT count(*) n FROM telegram_updates")["n"] == 1
    assert env.one("SELECT count(*) n FROM telegram_messages WHERE direction='inbound'")["n"] == 1
    assert env.one("SELECT count(*) n FROM agent_messages")["n"] == 1


def test_a_failing_route_rolls_back_its_effects_and_marks_the_update_failed_after_bounded_attempts(env, monkeypatch):
    bridge = env.ready()
    env.api.push(env.message("founder", "poison", update_id=330))
    monkeypatch.setattr(bridge.inbound, "_owner_message", lambda *a: (_ for _ in ()).throw(DomainError("validation_error", "boom")))
    for _ in range(4):
        bridge.poll_once()
    row = env.one("SELECT * FROM telegram_updates")
    assert (row["state"], row["attempts"]) == ("failed", 3) and row["reason"] == "validation_error"
    assert env.rows("SELECT * FROM telegram_messages") == [] and env.rows("SELECT * FROM agent_messages") == []


@pytest.mark.parametrize("build,reason", [
    (lambda e: e.message("founder", "x", sender=OWNER_TG + 5), "not_owner"),
    (lambda e: e.message("founder", "x", is_bot=True), "bot_actor"),
    (lambda e: e.message("founder", "x", sender_chat=True), "anonymous_actor"),
    (lambda e: e.message("founder", None, extra={"new_chat_title": "x"}), "service_message"),
    (lambda e: e.message("urgent", "x"), "no_inbound_route"),
    (lambda e: e.message("founder", "x", at=START - timedelta(days=1)), "before_activation"),
    (lambda e: {"update_id": e.next_update_id(), "edited_message": {"message_id": 1}}, "edited_message"),
    (lambda e: {"update_id": e.next_update_id(), "channel_post": {"message_id": 1}}, "channel_post"),
    (lambda e: {"update_id": e.next_update_id(), "message_reaction_count": {}}, "message_reaction_count"),
    (lambda e: {"update_id": e.next_update_id(), "callback_query": {}}, "callback_query"),
    (lambda e: {"update_id": e.next_update_id(), "message": {"chat": {"id": -999, "type": "supergroup"}, "message_id": 1,
                "date": 1, "from": {"id": OWNER_TG}}}, "unknown_chat"),
    (lambda e: {"update_id": e.next_update_id(), "message": "garbage"}, "malformed"),
    (lambda e: e.reaction("founder", 5, ["👍"]), "no_reaction_route"),
    (lambda e: e.reaction("signoffs", 5, ["👍"], sender=OWNER_TG + 9), "not_owner"),
    (lambda e: e.reaction("signoffs", 5, ["👍"], actor_chat=True), "anonymous_actor"),
])
def test_unauthorized_or_unsupported_updates_become_ignored_receipts_with_no_effect(env, build, reason):
    bridge = env.ready()
    env.api.push(build(env))
    bridge.poll_once()
    row = env.one("SELECT state, reason FROM telegram_updates")
    assert (row["state"], row["reason"]) == ("ignored", reason)
    assert env.rows("SELECT * FROM telegram_messages") == [] and env.rows("SELECT * FROM agent_messages") == []
    assert env.rows("SELECT * FROM telegram_outbox") == []


def test_an_update_without_an_id_is_dropped_and_the_wrong_chat_type_is_ignored(env):
    bridge = env.ready()
    env.api.push({"message": {"chat": {"id": 1, "type": "private"}}})  # no update_id: nothing to track, dropped
    private = env.message("founder", "x")
    private["message"]["chat"]["type"] = "private"
    env.api.push(private)
    bridge.poll_once()
    rows = env.rows("SELECT reason FROM telegram_updates")
    assert [r["reason"] for r in rows] == ["chat_type_mismatch"]


def test_updates_before_activation_are_ignored_and_the_pinned_cutoff_survives_a_restart(env):
    bridge = env.ready()
    old = env.message("founder", "before", at=START - timedelta(hours=3))
    fresh = env.message("founder", "after", at=START)
    env.api.push(old); env.api.push(fresh)
    bridge.poll_once()
    assert {r["reason"] for r in env.rows("SELECT reason FROM telegram_updates WHERE state='ignored'")} == {"before_activation"}
    assert env.one("SELECT count(*) n FROM agent_messages")["n"] == 1
    bridge.lock.release(); bridge.lock = None
    env.write_config(activation="2027-01-01T00:00:00Z")
    restarted = env.make_bridge()
    env.ready(restarted)
    env.api.push(env.message("founder", "still accepted", at=START))
    restarted.poll_once()
    assert env.one("SELECT count(*) n FROM agent_messages")["n"] == 2


def test_an_extended_outage_is_reported_never_guessed_away(env):
    bridge = env.ready()
    bridge.poll_once()
    env.clock.advance(hours=25)
    bridge.poll_once()
    outage = env.one("SELECT outage_json FROM telegram_state")["outage_json"]
    assert json.loads(outage)["possible_message_loss"] is True
    assert bridge.status()["outage"]["possible_message_loss"] is True


def test_poll_failures_degrade_only_the_poller_and_follow_the_provider_hints(env):
    bridge = env.ready()
    env.api.script("get_updates", TelegramApiError("rate_limited", "Too Many Requests", error_code=429, retry_after=7.0))
    assert bridge.poll_once() == 7.0 and bridge.health() == {"poller": "api_rate_limited"}
    env.api.script("get_updates", TelegramApiError("refused", "Conflict: terminated by other getUpdates request", error_code=409))
    assert bridge.poll_once() == 30.0 and bridge.health() == {"poller": "poller_conflict"}
    assert "poller_conflict" in env.one("SELECT last_error_json FROM telegram_state")["last_error_json"]
    env.api.script("get_updates", TelegramApiError("refused", "Unauthorized", error_code=401))
    assert bridge.poll_once() == 30.0 and bridge.health() == {"poller": "refused_401"}
    env.api.script("get_updates", TelegramApiError("timeout", "slow"))
    assert 2.0 <= bridge.poll_once() <= 5.0 and bridge.health() == {"poller": "api_timeout"}
    assert bridge.poll_once() == 0.0 and bridge.health() == {}
    assert bridge.status()["state"] == "running"


# ----- outbox ---------------------------------------------------------------------------------------------------


def test_a_queued_message_is_sent_and_its_receipt_binds_the_provider_id(env):
    bridge = env.ready()
    message, first, created = queue(env, "hello there")
    assert created and first["state"] == "queued" and message["telegram_message_id"] is None
    assert bridge.dispatcher.dispatch_one() is True and bridge.dispatcher.dispatch_one() is False
    sent = env.api.sent
    assert len(sent) == 1 and sent[0]["text"] == "hello there" and sent[0]["chat_id"] == CHAT_IDS["founder"]
    row = outbox(env, first["id"])
    assert (row["state"], row["attempts"], row["provider_message_id"]) == ("sent", 1, sent[0]["message_id"])
    assert env.one("SELECT telegram_message_id FROM telegram_messages WHERE id=?", message["id"])["telegram_message_id"] == sent[0]["message_id"]


def test_the_same_source_enqueues_once_and_replies_resolve_the_provider_message(env):
    bridge = env.ready()
    parent, _, _ = queue(env, "first", source_key="s1")
    bridge.dispatcher.run()
    again, _, created = queue(env, "first", source_key="s1")
    assert created is False and again["id"] == parent["id"]
    with bridge.write("t") as uow:
        row = store.message(uow, parent["id"])
    _, second, _ = queue(env, "second", reply_to=row)
    bridge.dispatcher.run()
    assert [m["text"] for m in env.api.sent] == ["first", "second"]
    assert env.api.sent[1]["reply_to"] == env.api.sent[0]["message_id"]


def test_text_limits_are_enforced_before_anything_is_stored(env):
    env.ready()
    for text in ("", "   ", "x" * 4001, "😀" * 2500):  # 2500 characters but 5000 UTF-16 units
        with pytest.raises(DomainError):
            queue(env, text)
    assert queue(env, "y" * 4000)[2] and queue(env, "😀" * 2048)[2]
    assert env.one("SELECT count(*) n FROM telegram_outbox")["n"] == 2


def test_rate_limiting_obeys_retry_after_and_never_sends_early(env):
    bridge = env.ready()
    _, row, _ = queue(env, "later")
    env.api.script("send_message", TelegramApiError("rate_limited", "Too Many Requests: retry after 30", error_code=429, retry_after=30.0))
    bridge.dispatcher.dispatch_one()
    queued = outbox(env, row["id"])
    assert queued["state"] == "queued" and queued["error_code"] == "rate_limited" and env.api.sent == []
    assert store.parse(queued["next_attempt_at"]) - env.clock.now >= timedelta(seconds=30)
    env.clock.advance(20)
    assert bridge.dispatcher.dispatch_one() is False and env.api.sent == []
    env.clock.advance(20)
    assert bridge.dispatcher.dispatch_one() is True and len(env.api.sent) == 1
    assert outbox(env, row["id"])["state"] == "sent"


def test_pre_send_failures_back_off_with_bounded_growth_and_finally_fail_explicitly(env):
    bridge = env.ready()
    bridge.timings["max_attempts"] = 4
    _, row, _ = queue(env, "flaky")
    env.api.script("send_message", *[TelegramApiError("unreachable", "ConnectionRefusedError")] * 4)
    delays = []
    for _ in range(4):
        env.clock.advance(3600)
        bridge.dispatcher.dispatch_one()
        current = outbox(env, row["id"])
        if current["state"] == "queued":
            delays.append((store.parse(current["next_attempt_at"]) - env.clock.now).total_seconds())
    assert delays == sorted(delays) and all(0 < d <= 900 for d in delays)
    final = outbox(env, row["id"])
    assert final["state"] == "failed" and final["error_code"] == "unreachable_exhausted" and env.api.sent == []


def test_a_provider_refusal_fails_permanently_and_is_never_retried(env):
    bridge = env.ready()
    _, row, _ = queue(env, "nope")
    env.api.script("send_message", TelegramApiError("refused", "Forbidden: bot was kicked from the supergroup chat", error_code=403))
    bridge.dispatcher.dispatch_one()
    env.clock.advance(hours=5)
    assert bridge.dispatcher.dispatch_one() is False
    failed = outbox(env, row["id"])
    assert (failed["state"], failed["error_code"], failed["attempts"]) == ("failed", "refused_403", 1)


@pytest.mark.parametrize("kind", ["timeout", "uncertain"])
def test_a_timeout_or_unknown_response_after_a_possible_send_is_uncertain_and_never_resent(env, kind):
    bridge = env.ready()
    _, row, _ = queue(env, "maybe")
    env.api.script("send_message", TelegramApiError(kind, "read timed out"))
    bridge.dispatcher.dispatch_one()
    for _ in range(5):
        env.clock.advance(hours=1)
        bridge.dispatcher.run()
    assert env.api.calls.count("send_message") == 1 and env.api.sent == []
    state = outbox(env, row["id"])
    assert state["state"] == "uncertain" and state["error_code"] == "provider_" + kind
    assert [entry["outbox_id"] for entry in bridge.status()["uncertain_sends"]] == [row["id"]]


def test_a_crash_after_the_intent_commits_is_uncertain_after_restart_and_is_not_resent(env):
    bridge = env.ready()
    _, row, _ = queue(env, "in flight")
    class Crash(BaseException):
        pass
    env.api.script("send_message", Crash())
    with pytest.raises(Crash):
        bridge.dispatcher.dispatch_one()
    assert outbox(env, row["id"])["state"] == "dispatching"
    bridge.lock.release(); bridge.lock = None
    restarted = env.make_bridge()
    env.ready(restarted)
    assert outbox(env, row["id"])["state"] == "uncertain" and outbox(env, row["id"])["error_code"] == "interrupted"
    env.clock.advance(hours=3)
    restarted.dispatcher.run()
    assert env.api.sent == [] and env.api.calls.count("send_message") == 1


def test_a_receipt_that_cannot_commit_leaves_the_intent_for_uncertain_recovery(env, monkeypatch):
    bridge = env.ready()
    _, row, _ = queue(env, "sent but unrecorded")
    real = bridge.write
    def guarded(operation):
        if operation == "dispatch.receipt":
            raise DomainError("maintenance", "closed", retryable=True)
        return real(operation)
    monkeypatch.setattr(bridge, "write", guarded)
    assert bridge.dispatcher.dispatch_one() is True
    assert len(env.api.sent) == 1 and outbox(env, row["id"])["state"] == "dispatching"
    # The receipt is kept for a retry; only losing it (a crash before writes reopen) leaves the row for recovery.
    assert len(bridge.dispatcher._unrecorded) == 1
    bridge.dispatcher._unrecorded.clear()
    monkeypatch.setattr(bridge, "write", real)
    bridge.recover()
    assert outbox(env, row["id"])["state"] == "uncertain"


def test_a_queued_effect_fails_closed_when_the_binding_changed_after_it_was_queued(env):
    bridge = env.ready()
    _, row, _ = queue(env, "queued before")
    bridge.lock.release(); bridge.lock = None
    def change(data):
        data["chats"]["urgent"]["chat_id"] = -2006
    env.write_config(mutate=change)
    env.api.chat_ids["urgent"] = -2006
    restarted = env.make_bridge()
    env.ready(restarted)
    restarted.dispatcher.run()
    failed = outbox(env, row["id"])
    assert (failed["state"], failed["error_code"]) == ("failed", "binding_changed") and env.api.sent == []


def test_a_revoked_producer_credential_blocks_its_queued_sends(env):
    bridge = env.ready()
    with bridge.write("t") as uow:
        store.enqueue(uow, bridge.config.chat("urgent"), 1, bridge.config.binding_fingerprint(), source_kind="notify", source_key="k",
                      payload={"text": "alert"}, now=bridge.now(), producer_id="monitor")
    bridge.dispatcher.run()
    row = env.one("SELECT * FROM telegram_outbox")
    assert (row["state"], row["error_code"]) == ("cancelled", "producer_revoked") and env.api.sent == []


def test_a_request_that_ended_before_dispatch_is_cancelled_and_expiry_marks_and_cleans_secret_prompts(env):
    bridge = env.ready()
    request = {"request_key": "sms", "input_mode": "secret", "reason": "Need the code", "expires_at": store.plus(bridge.now(), 60)}
    message, row, _ = queue(env, "Send me the SMS code", chat="marketing", purpose="blocking_input", request=request)
    env.clock.advance(61)
    bridge.dispatcher.run()
    assert outbox(env, row["id"])["state"] == "cancelled" and env.api.sent == []
    assert env.one("SELECT request_state FROM telegram_messages WHERE id=?", message["id"])["request_state"] == "expired"
    assert env.one("SELECT count(*) n FROM telegram_cleanup_jobs")["n"] == 1

    text_request = {"request_key": "note", "input_mode": "text", "reason": "Which one?", "expires_at": store.plus(bridge.now(), 60)}
    sent_message, sent_row, _ = queue(env, "Which one?", chat="ordubashy", purpose="blocking_input", request=text_request)
    bridge.dispatcher.run()
    env.clock.advance(120)
    bridge.dispatcher.run()
    assert env.one("SELECT request_state FROM telegram_messages WHERE id=?", sent_message["id"])["request_state"] == "expired"
    assert env.one("SELECT state FROM telegram_outbox WHERE id=?", sent_row["id"])["state"] == "sent"
    assert env.one("SELECT count(*) n FROM telegram_cleanup_jobs")["n"] == 1  # non-secret requests are not deleted


def test_an_edit_updates_the_known_message_and_a_missing_target_fails_explicitly(env):
    bridge = env.ready()
    message, row, _ = queue(env, "status: firing")
    bridge.dispatcher.run()
    with bridge.write("t") as uow:
        target = store.message(uow, message["id"])
        store.enqueue(uow, bridge.config.chat("founder"), 1, bridge.config.binding_fingerprint(), kind="edit", source_kind="agent",
                      source_key="edit-1", payload={"text": "status: resolved", "target_message_id": target["id"]}, now=bridge.now(),
                      message_id=target["id"])
        store.enqueue(uow, bridge.config.chat("founder"), 1, bridge.config.binding_fingerprint(), kind="edit", source_kind="agent",
                      source_key="edit-2", payload={"text": "x", "target_message_id": new_id()}, now=bridge.now())
    bridge.dispatcher.run()
    rows = env.rows("SELECT source_key, state, error_code FROM telegram_outbox WHERE kind='edit' ORDER BY source_key")
    assert rows == [{"source_key": "edit-1", "state": "sent", "error_code": None},
                    {"source_key": "edit-2", "state": "failed", "error_code": "target_unknown"}]
    assert env.api.edited == [{"chat_id": CHAT_IDS["founder"], "message_id": env.api.sent[0]["message_id"], "text": "status: resolved"}]
    assert env.one("SELECT body FROM telegram_messages WHERE id=?", message["id"])["body"] == "status: resolved"


# ----- cleanup --------------------------------------------------------------------------------------------------


def sent_message(env, text="delete me", chat="signoffs"):
    bridge = env.bridge
    message, row, _ = queue(env, text, chat=chat)
    bridge.dispatcher.run()
    return env.one("SELECT * FROM telegram_messages WHERE id=?", message["id"])


def enqueue_cleanup(env, message_id, reason="test"):
    with env.bridge.write("t") as uow:
        return store.enqueue_cleanup(uow, message_id, reason, now=env.bridge.now())


def test_cleanup_deletes_only_known_messages_once_and_treats_an_already_gone_message_as_cleaned(env):
    bridge = env.ready()
    known = sent_message(env)
    with pytest.raises(DomainError, match="known"):
        enqueue_cleanup(env, new_id())
    enqueue_cleanup(env, known["id"])
    assert enqueue_cleanup(env, known["id"])["message_id"] == known["id"]
    bridge.cleanup.run()
    assert env.api.deleted == [(CHAT_IDS["signoffs"], known["telegram_message_id"])]
    assert env.one("SELECT state FROM telegram_cleanup_jobs")["state"] == "deleted"
    gone = sent_message(env, "second")
    enqueue_cleanup(env, gone["id"])
    env.api.script("delete_message", TelegramApiError("refused", "Bad Request: message to delete not found", error_code=400))
    bridge.cleanup.run()
    assert env.one("SELECT state,error_code FROM telegram_cleanup_jobs WHERE message_id=?", gone["id"]) == {"state": "deleted", "error_code": "already_gone"}


def test_cleanup_refuses_after_48_hours_and_reports_permission_failures_explicitly(env):
    bridge = env.ready()
    old = sent_message(env, "old")
    enqueue_cleanup(env, old["id"])
    env.clock.advance(hours=49)
    bridge.cleanup.run()
    job = env.one("SELECT * FROM telegram_cleanup_jobs")
    assert (job["state"], job["error_code"]) == ("failed", "too_old_48h") and env.api.deleted == []
    failure = bridge.status()["cleanup_failures"][0]
    assert failure["error_code"] == "too_old_48h" and failure["message_id"] == old["id"]
    assert failure["telegram_message_id"] == old["telegram_message_id"] and "chat_id" not in failure
    assert failure["link"] is None and "by hand" in failure["instructions"] and failure["chat"]
    fresh = sent_message(env, "fresh")
    enqueue_cleanup(env, fresh["id"])
    env.api.script("delete_message", TelegramApiError("refused", "Forbidden: not enough rights to delete", error_code=403))
    bridge.cleanup.run()
    assert env.one("SELECT error_code FROM telegram_cleanup_jobs WHERE message_id=?", fresh["id"])["error_code"] == "delete_refused_403"


def test_a_failed_deletion_in_a_private_supergroup_carries_its_message_link_and_manual_instructions(env):
    from types import SimpleNamespace
    from bazarlyk.telegram.bridge import TelegramBridge
    config = SimpleNamespace(destination=lambda chat_id, thread=None: (
        SimpleNamespace(name="urgent") if (chat_id, thread) == (-1001234567890, None) else None))
    row = {"message_id": "m1", "error_code": "too_old_48h", "reason": "signoff_retired", "chat_name": "urgent",
           "chat_id": -1001234567890, "message_thread_id": None, "telegram_message_id": 77}
    entry = TelegramBridge._manual_cleanup(row, config)
    assert entry["link"] == "https://t.me/c/1234567890/77" and entry["chat"] == "urgent" and entry["still_configured"] is True
    assert entry["link"] in entry["instructions"] and "urgent chat" in entry["instructions"]
    unknown = TelegramBridge._manual_cleanup({**row, "chat_id": -5, "telegram_message_id": None}, config)
    assert unknown["link"] is None and unknown["chat"] == "urgent" and unknown["still_configured"] is False
    assert "by hand" in unknown["instructions"]


def test_transient_cleanup_failures_retry_with_backoff_then_fail_and_unsent_messages_wait_or_complete(env):
    bridge = env.ready()
    bridge.timings["cleanup_attempts"] = 2
    message = sent_message(env)
    enqueue_cleanup(env, message["id"])
    env.api.script("delete_message", TelegramApiError("unreachable", "down"), TelegramApiError("timeout", "slow"))
    bridge.cleanup.run()
    assert env.one("SELECT state, error_code FROM telegram_cleanup_jobs") == {"state": "queued", "error_code": "unreachable"}
    env.clock.advance(3600)
    bridge.cleanup.run()
    assert env.one("SELECT state, error_code FROM telegram_cleanup_jobs") == {"state": "failed", "error_code": "timeout_exhausted"}
    pending, pending_row, _ = queue(env, "not sent yet", chat="urgent")
    enqueue_cleanup(env, pending["id"])
    bridge.cleanup.run()
    assert env.one("SELECT state FROM telegram_cleanup_jobs WHERE message_id=?", pending["id"])["state"] == "queued"
    with env.database.transaction() as uow:
        uow.connection.execute("UPDATE telegram_outbox SET state='cancelled' WHERE id=?", (pending_row["id"],))
    env.clock.advance(120)
    bridge.cleanup.run()
    assert env.one("SELECT state, error_code FROM telegram_cleanup_jobs WHERE message_id=?", pending["id"]) == {"state": "deleted", "error_code": "never_sent"}


# ----- threads, shutdown and quiescence -------------------------------------------------------------------------


def wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_threads_verify_poll_dispatch_and_stop_within_a_bound_even_during_a_blocked_long_poll(env):
    bridge = env.bridge
    env.api.poll_gate = threading.Event()
    bridge.start()
    assert wait_for(lambda: bridge._ready.is_set())
    env.api.push(env.message("founder", "over threads"))
    queue(env, "outbound over threads")
    env.api.poll_gate.set()
    assert wait_for(lambda: env.rows("SELECT * FROM agent_messages") and env.api.sent)
    env.api.poll_gate = threading.Event()  # block the next long poll
    assert wait_for(lambda: len(env.api.polled_offsets) >= 2)
    started = time.monotonic()
    bridge.stop(timeout=5)
    assert time.monotonic() - started < 4 and not bridge._threads and bridge.lock is None
    bridge.start()
    assert wait_for(lambda: bridge._ready.is_set())
    bridge.stop(timeout=5)


def test_the_writer_joins_recovery_quiescence_through_the_bounded_writer_contract(env):
    bridge = env.bridge
    bridge.start()
    assert wait_for(lambda: bridge._ready.is_set())
    writer = BoundedWriter(bridge, "telegram")
    proof = writer.drain(timeout=5)
    assert proof.state == "drained" and bridge._thread is None
    queue(env, "held during recovery")
    time.sleep(0.2)
    assert env.api.sent == []  # nothing is dispatched while drained
    writer.resume()
    assert wait_for(lambda: env.api.sent)
    bridge.stop(timeout=5)


def test_a_worker_bug_degrades_the_bridge_and_never_escapes_the_thread(env, monkeypatch):
    bridge = env.bridge
    monkeypatch.setattr(bridge, "steps", [lambda: (_ for _ in ()).throw(RuntimeError("bug"))])
    bridge.start()
    assert wait_for(lambda: bridge.health().get("worker") == "internal_error")
    assert bridge._thread is not None
    bridge.stop(timeout=5)


def test_status_never_contains_bodies_tokens_or_identifiers(env):
    bridge = env.ready()
    queue(env, "a very private body")
    env.api.push(env.message("founder", "another private body"))
    bridge.poll_once()
    text = json.dumps(bridge.status())
    for secret in ("private body", "AAAA", str(env.tmp), *[str(v) for v in CHAT_IDS.values()], *env.agents.values()):
        assert secret not in text


def test_a_later_activation_time_in_the_file_is_not_a_binding_change(env):
    bridge = env.ready()
    _, row, _ = queue(env, "queued before")
    generation = env.one("SELECT config_generation FROM telegram_state")["config_generation"]
    bridge.lock.release(); bridge.lock = None
    env.write_config(activation="2031-01-01T00:00:00Z")
    restarted = env.make_bridge()
    env.ready(restarted)
    assert env.one("SELECT config_generation FROM telegram_state")["config_generation"] == generation
    restarted.dispatcher.run()
    assert outbox(env, row["id"])["state"] == "sent"


def test_building_the_runtime_is_inert_and_does_not_read_the_token(tmp_path, monkeypatch):
    from bazarlyk.telegram.bridge import TelegramBridge
    from types import SimpleNamespace
    monkeypatch.delenv("QORQUT_TELEGRAM_CONFIG_FILE", raising=False)
    bridge = TelegramBridge(SimpleNamespace(database=None, application=None), environ={})
    assert bridge.api is None and not bridge._threads and bridge.config.enabled is False
    bridge.start()
    assert not bridge._threads and bridge.status()["state"] == "disabled"
    bridge.stop(timeout=1)
