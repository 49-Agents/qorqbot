"""Native Telegram tools on the real shared service: eligibility, scope isolation, grants, idempotency and owner-only routes."""
from dataclasses import replace
import json
from pathlib import Path

import pytest

from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id, plain
from bazarlyk.policy import WorkerProfile
from telegram_service_support import TelegramService
from telegram_support import CHAT_IDS, TOKEN


@pytest.fixture
def service(tmp_path):
    s = TelegramService(tmp_path)
    try:
        yield s
    finally:
        s.close()


def send(s, agent, text="Hello owner", purpose="update", **extra):
    return s.call(agent, "telegram_send", {"text": text, "purpose": purpose, **extra})


# ----- send: eligibility and isolation --------------------------------------------------------------------


def test_each_configured_agent_sends_only_to_its_own_chat_and_replays_are_one_effect(service):
    s = service
    key = new_id()
    first = s.ok(s.call(s.founder, "telegram_send", {"text": "From founder", "purpose": "update"}, key))
    assert s.ok(s.call(s.founder, "telegram_send", {"text": "From founder", "purpose": "update"}, key))["message_id"] == first["message_id"]
    assert first["state"] == "queued" and first["purpose"] == "update" and "chat" not in first and "agent_id" not in first
    s.ok(send(s, s.ordubashy, "From ordubashy", "reply"))
    s.ok(send(s, s.marketing, "From marketing"))
    s.dispatch()
    sent = {(m["chat_id"], m["text"]) for m in s.api.sent}
    assert sent == {(CHAT_IDS["founder"], "From founder"), (CHAT_IDS["ordubashy"], "From ordubashy"),
                    (CHAT_IDS["marketing"], "From marketing")}
    assert s.rows("SELECT count(*) n FROM telegram_outbox")[0]["n"] == 3


@pytest.mark.parametrize("field,value", [
    ("chat_id", -1001), ("agent_id", "x"), ("token", TOKEN), ("parse_mode", "HTML"), ("path", "/etc/passwd"),
    ("bot_token_file", "x"), ("disable_web_page_preview", False), ("attachments", []),
])
def test_forged_routing_or_formatting_fields_are_rejected_by_the_closed_schema(service, field, value):
    result = send(service, service.founder, **{field: value})
    assert result.status == "error" and result.error["code"] == "validation_error"
    assert service.rows("SELECT count(*) n FROM telegram_messages")[0]["n"] == 0


@pytest.mark.parametrize("args", [
    {"text": "", "purpose": "update"}, {"text": "   ", "purpose": "update"}, {"text": "x" * 4001, "purpose": "update"},
    {"text": "😀" * 2500, "purpose": "update"}, {"text": "x", "purpose": "shout"}, {"text": "x"},
    {"text": "x", "purpose": "reply", "request_key": "k"},
    {"text": "x", "purpose": "blocking_input"},
    {"text": "x", "purpose": "blocking_input", "request_key": "k", "reason": "r"},
    {"text": "x", "purpose": "blocking_input", "request_key": "k", "reason": "r", "input_mode": "carrier-pigeon"},
    {"text": "x", "purpose": "blocking_input", "request_key": "k", "reason": "r", "input_mode": "secret", "expires_in_seconds": 601},
    {"text": "x", "purpose": "blocking_input", "request_key": "k", "reason": "r", "input_mode": "text", "expires_in_seconds": 59},
    {"text": "x", "purpose": "blocking_input", "request_key": "k", "reason": "r", "input_mode": "text", "expires_in_seconds": 3601},
    {"text": "x", "purpose": "update", "reply_to": "not-a-uuid"},
])
def test_invalid_send_shapes_are_refused_before_anything_is_stored(service, args):
    result = service.call(service.founder, "telegram_send", args)
    assert result.status == "error" and result.error["code"] in ("validation_error", "invalid_request")
    assert service.rows("SELECT count(*) n FROM telegram_messages")[0]["n"] == 0


def test_only_the_exact_configured_agent_and_role_may_use_the_tools(service):
    s = service
    # A business agent: role not eligible. A second Ordubashy-role agent: right role, not the configured one.
    assert send(s, s.onbashy).status == "error"
    stranger = s.fixture.create("stranger", "ordubashy")
    for operation, args in (("telegram_send", {"text": "x", "purpose": "update"}), ("telegram_history", {}), ("telegram_delivery_get", {})):
        result = s.call(stranger, operation, args)
        assert result.status == "error" and result.error["code"] == "forbidden", (operation, result.to_dict())
    assert s.rows("SELECT count(*) n FROM telegram_messages")[0]["n"] == 0
    # Never an agent tool: owner-only and worker-only operations.
    for operation in ("telegram_status", "telegram_delivery_resolve", "telegram_notify"):
        result = s.call(s.ordubashy, operation, {} if operation == "telegram_status" else
                        {"outbox_id": new_id(), "action": "retry", "expected_revision": 1} if operation.endswith("resolve") else
                        {"event_key": "k", "title": "t", "text": "t", "reason": "r"})
        assert result.status == "error" and result.error["code"] == "forbidden"


def test_delegated_identities_cannot_borrow_the_parents_chat(service):
    s = service
    context = replace(s.context(s.founder), delegation_assignment_id=new_id(), delegation_binding_id=new_id(), delegation_generation=1)
    result = s.runtime.application.invoke(context, "telegram_send", {"text": "x", "purpose": "update"}, {"idempotency_key": new_id()})
    assert result.status == "error"
    with s.fixture.db.read() as uow, pytest.raises(DomainError, match="themselves"):
        s.runtime.telegram_operations.agent_chat(context, uow)


def test_an_explicit_deny_override_and_a_disabled_bridge_refuse_and_a_late_deny_cancels_queued_sends(service):
    s = service
    queued = s.ok(send(s, s.founder, "Queued before revocation"))
    s.fixture.update(s.founder, {"tool_policy": {"overrides": {"telegram_send": "deny"}}})
    assert send(s, s.founder, "After revocation").status == "error"
    s.dispatch()
    assert s.api.sent == []
    receipt = s.rows("SELECT state, error_code FROM telegram_outbox")[0]
    assert (receipt["state"], receipt["error_code"]) == ("cancelled", "grant_revoked")
    assert s.rows("SELECT id FROM telegram_messages")[0]["id"] == queued["message_id"]
    s.write_config(enabled=False)
    s.bridge.cycle()  # the control loop notices the changed file within one tick
    result = send(s, s.marketing)
    assert result.status == "error" and result.error["code"] == "operation_unavailable"


def test_a_deactivated_or_rebound_agent_cannot_send_and_its_queue_fails_closed(service):
    s = service
    s.ok(send(s, s.marketing, "Queued"))
    stale = s.context(s.marketing)
    s.fixture.update(s.marketing, {"status": "inactive"})
    after = s.runtime.application.invoke(stale, "telegram_send", {"text": "After deactivation", "purpose": "update"},
                                         {"idempotency_key": new_id()})
    assert after.status == "error"
    s.dispatch()
    assert s.api.sent == []
    assert s.rows("SELECT state, error_code FROM telegram_outbox")[0]["error_code"] == "grant_revoked"


# ----- blocking input -------------------------------------------------------------------------------------


def test_an_owner_mismatch_refuses_agent_tools_until_the_operator_reconfigures(service):
    s = service
    s.ok(send(s, s.founder, "queued while matched"))
    before = s.rows("SELECT count(*) n FROM telegram_outbox")[0]["n"]
    other = new_id()
    with s.fixture.db.transaction() as uow:
        uow.repository("principals").insert({"id": other, "kind": "human", "display_name": "Second owner"})
        uow.connection.execute("UPDATE installation SET owner_principal_id=?", (other,))
    for operation, args in (("telegram_send", {"text": "x", "purpose": "update"}), ("telegram_history", {}), ("telegram_delivery_get", {})):
        result = s.call(s.founder, operation, args)
        assert result.status == "error" and result.error["code"] == "owner_binding_mismatch", (operation, result.to_dict())
    assert s.rows("SELECT count(*) n FROM telegram_outbox")[0]["n"] == before
    with pytest.raises(DomainError) as refused:
        s.bridge.dispatcher.dispatch_one()
    assert refused.value.code == "owner_binding_mismatch" and s.api.sent == []


def test_blocking_input_reuses_a_pending_request_and_conflicts_when_it_materially_changes(service):
    s = service
    args = dict(text="Need the deploy target", purpose="blocking_input", request_key="deploy-target", reason="Cannot deploy", input_mode="text")
    first = s.ok(s.call(s.ordubashy, "telegram_send", args))
    assert first["request"]["state"] == "pending" and first["request"]["input_mode"] == "text"
    again = s.ok(s.call(s.ordubashy, "telegram_send", args))
    assert again["message_id"] == first["message_id"] and again["reused"] is True
    assert s.rows("SELECT count(*) n FROM telegram_outbox")[0]["n"] == 1
    changed = s.call(s.ordubashy, "telegram_send", {**args, "text": "A different question"})
    assert changed.status == "error" and changed.error["code"] == "revision_conflict"
    assert s.call(s.ordubashy, "telegram_send", {**args, "input_mode": "screen_action"}).status == "error"


def test_a_blocking_request_states_the_reason_the_expiry_and_how_to_answer(service):
    s = service
    args = dict(text="Need the deploy target", purpose="blocking_input", request_key="deploy-target", reason="Cannot deploy without it",
                input_mode="text", expires_in_seconds=900)
    first = s.ok(s.call(s.ordubashy, "telegram_send", args))
    s.dispatch()
    text = s.api.sent[0]["text"]
    assert text.startswith("Need the deploy target") and "Why it is needed: Cannot deploy without it" in text
    assert "Expires: 2026-10-01 12:15 UTC" in text and "Reply to this message with your answer." in text
    assert s.rows("SELECT body FROM telegram_messages WHERE id=?", first["message_id"])[0]["body"] == text
    # A retry with the same key is the same request even though the clock moved on.
    s.clock.advance(60)
    assert s.ok(s.call(s.ordubashy, "telegram_send", args))["message_id"] == first["message_id"]
    secret = s.ok(s.call(s.marketing, "telegram_send", dict(text="Send the SMS code", purpose="blocking_input", request_key="sms",
                                                            reason="Login blocked", input_mode="secret")))
    screen = s.ok(s.call(s.founder, "telegram_send", dict(text="Approve the consent screen", purpose="blocking_input",
                                                          request_key="consent", reason="A dialog is blocking", input_mode="screen_action")))
    s.dispatch()
    bodies = {row["id"]: row["body"] for row in s.rows("SELECT id, body FROM telegram_messages")}
    assert "not end-to-end encrypted" in bodies[secret["message_id"]] and "Expires:" in bodies[secret["message_id"]]
    assert 'reply "done"' in bodies[screen["message_id"]]
    plain_update = s.ok(s.call(s.ordubashy, "telegram_send", {"text": "All good", "purpose": "update"}))
    s.dispatch()
    assert s.api.sent[-1]["text"] == "All good"
    too_long = s.call(s.ordubashy, "telegram_send", {**args, "request_key": "big", "text": "x" * 3990})
    assert too_long.status == "error" and too_long.error["code"] == "validation_error"
    assert plain_update["message_id"]


def test_secret_requests_default_to_ten_minutes_and_only_one_may_be_outstanding_per_chat(service):
    s = service
    args = dict(text="Send the SMS code", purpose="blocking_input", request_key="sms", reason="Login blocked", input_mode="secret")
    first = s.ok(s.call(s.marketing, "telegram_send", args))
    row = s.rows("SELECT occurred_at, request_expires_at FROM telegram_messages")[0]
    from bazarlyk.telegram import store
    assert (store.parse(row["request_expires_at"]) - store.parse(row["occurred_at"])).total_seconds() == 600
    other = s.call(s.marketing, "telegram_send", {**args, "request_key": "sms-2", "text": "Another code"})
    assert other.status == "error" and other.error["code"] == "revision_conflict"
    # Another agent's chat is independent.
    assert s.call(s.founder, "telegram_send", {**args, "request_key": "sms"}).status == "ok"
    s.clock.advance(601)
    replacement = s.ok(s.call(s.marketing, "telegram_send", {**args, "request_key": "sms-3", "text": "A fresh code please"}))
    assert replacement["message_id"] != first["message_id"]
    assert s.rows("SELECT request_state FROM telegram_messages WHERE id=?", first["message_id"])[0]["request_state"] == "expired"
    assert s.rows("SELECT count(*) n FROM telegram_cleanup_jobs WHERE message_id=?", first["message_id"])[0]["n"] == 1


def test_reply_to_must_be_a_message_in_the_callers_own_chat(service):
    s = service
    mine = s.ok(send(s, s.founder, "first"))
    s.dispatch()
    ok = s.ok(send(s, s.founder, "second", "reply", reply_to=mine["message_id"]))
    assert ok["purpose"] == "reply"
    s.dispatch()
    assert s.api.sent[1]["reply_to"] == s.api.sent[0]["message_id"]
    theirs = s.ok(send(s, s.marketing, "elsewhere"))
    foreign = send(s, s.founder, "cross-chat", "reply", reply_to=theirs["message_id"])
    assert foreign.status == "error" and foreign.error["code"] == "not_found"
    assert send(s, s.founder, "unknown", "reply", reply_to=new_id()).status == "error"


# ----- history and receipts -------------------------------------------------------------------------------


def owner_message(s, chat, text, minutes, *, sensitive=False, purpose="owner"):
    from bazarlyk.telegram import store
    agent = s.agents[chat]
    with s.bridge.write("test") as uow:
        cfg = s.bridge.config.chat(chat)
        at = store.plus(s.bridge.now(), minutes * 60)
        return store.insert_message(uow, direction="inbound", chat=cfg, purpose=purpose, body=text, now=at, occurred_at=at,
                                    agent_id=agent, sensitive=sensitive)


def test_history_is_bounded_ordered_isolated_redacted_and_starts_at_activation(service):
    s = service
    from bazarlyk.telegram import store
    with s.bridge.write("test") as uow:  # a message from before activation is never shown
        cfg = s.bridge.config.chat("founder")
        early = store.plus(s.bridge.now(), -7200)
        store.insert_message(uow, direction="inbound", chat=cfg, purpose="owner", body="before activation", now=early, occurred_at=early,
                             agent_id=s.founder.id)
    for n in range(12):
        owner_message(s, "founder", f"owner {n}", n)
    owner_message(s, "founder", "the code is 123456", 13, sensitive=True)
    owner_message(s, "marketing", "marketing only", 1)
    owner_message(s, "founder", "notice", 14, purpose="notice")
    s.clock.advance(3600)
    s.ok(send(s, s.founder, "agent answer", "reply"))
    history = plain(s.ok(s.call(s.founder, "telegram_history"))["items"])
    assert len(history) == 10 and history[-1]["text"] == "agent answer" and history[-1]["from"] == "you"
    texts = [item["text"] for item in history]
    assert texts[:-1] == ["owner 4", "owner 5", "owner 6", "owner 7", "owner 8", "owner 9", "owner 10", "owner 11",
                          "[secret input redacted]"]
    assert "before activation" not in json.dumps(history) and "123456" not in json.dumps(history)
    assert "marketing only" not in json.dumps(history) and "notice" not in texts
    assert [i["from"] for i in history[:-1]] == ["owner"] * 9
    assert len(s.ok(s.call(s.founder, "telegram_history", {"limit": 3}))["items"]) == 3
    assert s.call(s.founder, "telegram_history", {"limit": 11}).status == "error"
    assert s.call(s.founder, "telegram_history", {"limit": 0}).status == "error"
    assert s.call(s.founder, "telegram_history", {"chat": "marketing"}).status == "error"
    assert {i["text"] for i in s.ok(s.call(s.marketing, "telegram_history"))["items"]} == {"marketing only"}


def test_delivery_receipts_are_owned_and_the_owner_reads_them_all(service):
    s = service
    mine = s.ok(send(s, s.founder, "mine"))
    theirs = s.ok(send(s, s.marketing, "theirs"))
    s.dispatch()
    receipt = s.ok(s.call(s.founder, "telegram_delivery_get", {"message_id": mine["message_id"]}))["items"][0]
    assert receipt["state"] == "sent" and receipt["attempts"] == 1 and receipt["sent_at"]
    assert "provider_message_id" not in receipt and "agent_id" not in receipt
    hidden = s.call(s.founder, "telegram_delivery_get", {"message_id": theirs["message_id"]})
    assert hidden.status == "error" and hidden.error["code"] == "not_found"
    assert [i["message_id"] for i in s.ok(s.call(s.founder, "telegram_delivery_get"))["items"]] == [mine["message_id"]]
    seen = s.ok(s.owner("telegram_delivery_get", {"message_id": theirs["message_id"]}))["items"][0]
    assert seen["chat"] == "marketing" and seen["provider_message_id"] == s.api.sent[1]["message_id"]
    assert len(s.ok(s.owner("telegram_delivery_get"))["items"]) == 2


def test_uncertain_and_failed_states_are_reported_truthfully(service):
    s = service
    from bazarlyk.telegram.api import TelegramApiError
    unsure = s.ok(send(s, s.founder, "maybe"))
    s.api.script("send_message", TelegramApiError("timeout", "read timed out"))
    s.dispatch()
    state = s.ok(s.call(s.founder, "telegram_delivery_get", {"message_id": unsure["message_id"]}))["items"][0]
    assert state["state"] == "uncertain" and state["error_code"] == "provider_timeout"
    s.dispatch()
    assert s.api.calls.count("send_message") == 1


def test_no_agent_response_contains_the_token_chat_identifiers_or_other_agents(service):
    s = service
    s.ok(send(s, s.founder, "one"))
    s.dispatch()
    owner_message(s, "founder", "hello", 1)
    outputs = [s.call(s.founder, "telegram_history").to_dict(), s.call(s.founder, "telegram_delivery_get").to_dict(),
               send(s, s.founder, "two").to_dict()]
    text = json.dumps(outputs)
    for hidden in (TOKEN, str(s.tmp), *[str(v) for v in CHAT_IDS.values()], s.marketing.id, s.ordubashy.id):
        assert hidden not in text


# ----- notify ---------------------------------------------------------------------------------------------


NOTE = {"event_key": "db-down", "title": "Database unreachable", "text": "Writes fail; customers cannot log in. Restore or fail over.",
        "reason": "Data loss risk"}


def test_notify_posts_once_updates_the_known_post_and_announces_one_recovery(service):
    s = service
    first = s.ok(s.owner("telegram_notify", NOTE))
    assert first["action"] == "posted"
    s.dispatch()
    assert s.api.sent[0]["chat_id"] == CHAT_IDS["urgent"] and s.api.sent[0]["text"].startswith("URGENT: Database unreachable")
    assert s.ok(s.owner("telegram_notify", NOTE))["action"] == "suppressed"
    changed = s.ok(s.owner("telegram_notify", {**NOTE, "text": "Failover in progress; ETA 10 minutes."}))
    assert changed["action"] == "updated" and changed["message_id"] == first["message_id"]
    s.dispatch()
    assert s.api.edited[-1]["message_id"] == s.api.sent[0]["message_id"] and "Failover" in s.api.edited[-1]["text"]
    resolved = s.ok(s.owner("telegram_notify", {**NOTE, "state": "resolved", "title": "Database recovered", "text": "Writes succeed."}))
    assert resolved["action"] == "updated"
    s.dispatch()
    assert s.api.edited[-1]["text"].startswith("RESOLVED: Database recovered")
    assert s.ok(s.owner("telegram_notify", {**NOTE, "state": "resolved", "title": "Database recovered", "text": "Writes succeed."}))["action"] == "suppressed"
    again = s.ok(s.owner("telegram_notify", NOTE))
    assert again["action"] == "posted" and again["message_id"] != first["message_id"]
    assert len(s.api.sent) == 1 and s.rows("SELECT count(*) n FROM telegram_outbox")[0]["n"] == 4


def test_an_identical_event_whose_post_failed_is_posted_again_but_an_uncertain_one_is_not(service):
    s = service
    from bazarlyk.telegram.api import TelegramApiError
    s.ok(s.owner("telegram_notify", NOTE))
    s.api.script("send_message", TelegramApiError("refused", "Forbidden: bot was blocked", error_code=403))
    s.dispatch()
    assert s.rows("SELECT state FROM telegram_outbox")[0]["state"] == "failed" and s.api.sent == []
    again = s.ok(s.owner("telegram_notify", NOTE))
    assert again["action"] != "suppressed"
    s.dispatch()
    assert len(s.api.sent) == 1 and s.api.sent[0]["text"].startswith("URGENT: Database unreachable")
    assert s.ok(s.owner("telegram_notify", NOTE))["action"] == "suppressed"
    other = {**NOTE, "event_key": "disk-full"}
    s.ok(s.owner("telegram_notify", other))
    s.api.script("send_message", TelegramApiError("uncertain", "reset"))
    s.dispatch()
    assert s.ok(s.owner("telegram_notify", other))["action"] == "suppressed"
    assert len(s.api.sent) == 1


def test_a_recovery_for_an_unknown_event_and_an_unsent_original_are_handled_without_duplicates(service):
    s = service
    assert s.ok(s.owner("telegram_notify", {**NOTE, "state": "resolved"}))["action"] == "ignored"
    assert s.rows("SELECT count(*) n FROM telegram_outbox")[0]["n"] == 0
    s.ok(s.owner("telegram_notify", {**NOTE, "event_key": "disk-full"}))
    second = s.ok(s.owner("telegram_notify", {**NOTE, "event_key": "disk-full", "text": "Now 99% full and growing."}))
    assert second["action"] == "updated"
    s.dispatch()
    assert len(s.api.sent) == 1 and "99%" in s.api.sent[0]["text"] and s.api.edited == []


def _edit_rows(s):
    return s.rows("SELECT id, state, error_code, event_sequence, kind, revision, payload_json FROM telegram_outbox WHERE kind='edit' "
                  "ORDER BY event_sequence")


def test_a_delayed_retry_of_an_older_urgent_edit_never_overwrites_a_newer_recovery(service):
    from bazarlyk.telegram.api import TelegramApiError
    s = service
    s.ok(s.owner("telegram_notify", NOTE))
    s.dispatch()
    s.ok(s.owner("telegram_notify", {**NOTE, "text": "older update: failing over"}))
    s.api.script("edit_message_text", TelegramApiError("unreachable", "network down"))
    s.dispatch()  # the older edit failed before it was sent and waits to retry
    assert [r["state"] for r in _edit_rows(s)] == ["queued"] and s.api.edited == []
    s.ok(s.owner("telegram_notify", {**NOTE, "state": "resolved", "title": "Database recovered", "text": "Writes succeed."}))
    s.dispatch()
    assert [e["text"][:9] for e in s.api.edited] == ["RESOLVED:"]
    s.clock.advance(3600)
    s.dispatch()
    # The older edit was withdrawn when the newer one was queued and is not sent when its retry comes due.
    assert [e["text"][:9] for e in s.api.edited] == ["RESOLVED:"]
    rows = _edit_rows(s)
    assert [(r["event_sequence"], r["state"], r["error_code"]) for r in rows] == [(2, "cancelled", "superseded"), (3, "sent", None)]
    assert [r["event_sequence"] for r in s.rows("SELECT event_sequence FROM telegram_outbox ORDER BY created_at, rowid")] == [1, 2, 3]


def test_dispatch_rechecks_the_event_sequence_so_an_owner_retry_of_an_old_edit_cannot_resend_obsolete_content(service):
    from bazarlyk.telegram.api import TelegramApiError
    s = service
    first = s.ok(s.owner("telegram_notify", NOTE))
    s.dispatch()
    s.ok(s.owner("telegram_notify", {**NOTE, "text": "older update"}))
    s.api.script("edit_message_text", TelegramApiError("timeout", "slow"))
    s.dispatch()
    old = _edit_rows(s)[0]
    assert old["state"] == "uncertain"
    s.ok(s.owner("telegram_notify", {**NOTE, "state": "resolved", "title": "Database recovered", "text": "Writes succeed."}))
    s.dispatch()
    assert [e["text"][:9] for e in s.api.edited] == ["RESOLVED:"]
    status = s.ok(s.owner("telegram_status"))["uncertain_sends"]
    assert [(u["outbox_id"], u["kind"], u["event_sequence"]) for u in status] == [(old["id"], "edit", 2)]
    retried = s.ok(s.owner("telegram_delivery_resolve", {"outbox_id": old["id"], "action": "retry", "expected_revision": old["revision"]}))
    assert {d["outbox_id"]: d["state"] for d in retried["deliveries"]}[old["id"]] == "queued"
    calls = len(s.api.edited)
    s.dispatch()
    assert len(s.api.edited) == calls
    closed = [r for r in _edit_rows(s) if r["id"] == old["id"]][0]
    assert (closed["state"], closed["error_code"]) == ("cancelled", "superseded")
    assert s.rows("SELECT body FROM telegram_messages WHERE id=?", first["message_id"])[0]["body"].startswith("RESOLVED:")


def test_two_uncertain_edits_of_one_message_are_each_resolvable_by_outbox_id_and_never_claim_newer_content(service):
    from bazarlyk.telegram.api import TelegramApiError
    s = service
    first = s.ok(s.owner("telegram_notify", NOTE))
    s.dispatch()
    provider = s.api.sent[0]["message_id"]
    s.ok(s.owner("telegram_notify", {**NOTE, "text": "second version"}))
    s.api.script("edit_message_text", TelegramApiError("timeout", "slow"), TelegramApiError("uncertain", "reset"))
    s.dispatch()
    s.ok(s.owner("telegram_notify", {**NOTE, "text": "third version"}))
    s.dispatch()
    two, three = _edit_rows(s)
    assert (two["state"], three["state"]) == ("uncertain", "uncertain") and two["id"] != three["id"]
    listed = s.ok(s.owner("telegram_status"))["uncertain_sends"]
    assert [(u["outbox_id"], u["event_sequence"], u["kind"], u["message_id"]) for u in listed] == [
        (two["id"], 2, "edit", first["message_id"]), (three["id"], 3, "edit", first["message_id"])]
    view = s.ok(s.owner("telegram_delivery_get", {"message_id": first["message_id"]}))["items"][0]
    assert [(d["outbox_id"], d["state"]) for d in view["deliveries"]] == [
        (view["outbox_id"], "sent"), (two["id"], "uncertain"), (three["id"], "uncertain")]

    def resolve_edit(row, **extra):
        return s.owner("telegram_delivery_resolve", {"outbox_id": row["id"], "action": "mark_delivered", "expected_revision": row["revision"], **extra})

    # An edit is confirmed only with the known ID of the message it edited.
    assert resolve_edit(three).status == "error"
    assert resolve_edit(three, provider_message_id=provider + 1).error["code"] == "validation_error"
    confirmed = s.ok(resolve_edit(three, provider_message_id=provider))
    assert {d["outbox_id"]: d["state"] for d in confirmed["deliveries"]}[three["id"]] == "sent"
    assert "third version" in s.rows("SELECT body FROM telegram_messages WHERE id=?", first["message_id"])[0]["body"]
    # Confirming the older edit afterwards closes its receipt and leaves the newer content in place.
    s.ok(resolve_edit(two, provider_message_id=provider))
    assert "third version" in s.rows("SELECT body FROM telegram_messages WHERE id=?", first["message_id"])[0]["body"]
    assert not s.ok(s.owner("telegram_status"))["uncertain_sends"]
    # A receipt that already moved on cannot be resolved again from the earlier inspection.
    assert resolve_edit(two, provider_message_id=provider).status == "error"
    assert s.api.calls.count("edit_message_text") == 2  # neither uncertain edit was ever sent again


def test_confirming_the_newest_uncertain_edit_records_the_confirmed_payload_and_needs_no_resend(service):
    from bazarlyk.telegram.api import TelegramApiError
    s = service
    first = s.ok(s.owner("telegram_notify", NOTE))
    s.dispatch()
    provider = s.api.sent[0]["message_id"]
    s.ok(s.owner("telegram_notify", {**NOTE, "text": "current version"}))
    s.api.script("edit_message_text", TelegramApiError("timeout", "slow"))
    s.dispatch()
    row = _edit_rows(s)[0]
    assert "current version" not in s.rows("SELECT body FROM telegram_messages WHERE id=?", first["message_id"])[0]["body"]
    s.ok(s.owner("telegram_delivery_resolve", {"outbox_id": row["id"], "action": "mark_delivered", "provider_message_id": provider,
                                                "expected_revision": row["revision"]}))
    assert "current version" in s.rows("SELECT body FROM telegram_messages WHERE id=?", first["message_id"])[0]["body"]
    assert s.api.calls.count("edit_message_text") == 1


def test_notify_validation_and_caller_restrictions(service):
    s = service
    for bad in ({**NOTE, "title": "t" * 121}, {**NOTE, "text": "x" * 3501}, {**NOTE, "link": "https://user:pw@example.com/x"},
                {**NOTE, "link": "ftp://example.com/x"}, {**NOTE, "link": "javascript:alert(1)"}, {**NOTE, "link": "https://a b"},
                {**NOTE, "state": "paused"}, {**NOTE, "event_key": "bad key!"}, {**NOTE, "chat_id": 1}, {**NOTE, "title": " "},
                {**NOTE, "text": "y" * 3500, "title": "z" * 120, "reason": "r" * 500, "link": "https://example.com/" + "a" * 470}):
        result = s.owner("telegram_notify", bad)
        assert result.status == "error" and result.error["code"] == "validation_error", bad
    assert s.owner("telegram_notify", {**NOTE, "link": "https://qorqut:1071/signoffs/1"}).status == "ok"
    assert s.rows("SELECT count(*) n FROM telegram_events")[0]["n"] == 1


def test_a_trusted_worker_may_notify_only_with_its_own_grant_and_it_is_rechecked_before_sending(service):
    s = service
    worker = Principal(new_id(), "system", "Monitor worker")
    with s.fixture.db.transaction() as uow:
        uow.repository("principals").insert({"id": worker.id, "kind": "system", "display_name": worker.display_name})
    grants = {}
    s.runtime.policy.worker_profiles = lambda principal_id, uow: (
        WorkerProfile(dict(grants), frozenset(), frozenset({"read_global_context"})) if principal_id == worker.id else None)
    context = InvocationContext(worker, source="worker", global_capabilities=frozenset({"read_global_context"}))
    invoke = lambda args: s.runtime.application.invoke(context, "telegram_notify", args, {"idempotency_key": new_id()})
    assert invoke(NOTE).status == "error"
    grants["telegram_notify"] = "allow"
    assert s.ok(invoke(NOTE))["action"] == "posted"
    grants.clear()
    s.dispatch()
    row = s.rows("SELECT state, error_code, producer_id FROM telegram_outbox")[0]
    assert (row["state"], row["error_code"]) == ("cancelled", "grant_revoked") and row["producer_id"] == f"principal:{worker.id}"
    assert s.api.sent == []


def test_a_telegram_outage_is_status_visible_and_never_creates_urgent_events_through_the_failed_channel(service):
    s = service
    from bazarlyk.telegram.api import TelegramApiError
    s.ok(s.owner("telegram_notify", NOTE))
    s.api.script("send_message", *[TelegramApiError("unreachable", "no route")] * 3)
    for _ in range(3):
        s.bridge.dispatcher.run()
        s.clock.advance(600)
    events = s.rows("SELECT event_key, state FROM telegram_events")
    outbox = s.rows("SELECT source_kind, state FROM telegram_outbox")
    assert events == [{"event_key": "db-down", "state": "firing"}]
    assert [row["source_kind"] for row in outbox] == ["notify"] and outbox[0]["state"] in ("queued", "failed")
    assert s.api.sent == [] and TOKEN not in json.dumps(plain(s.ok(s.owner("telegram_status"))))


def test_the_urgent_backlog_is_bounded_and_reports_backpressure_without_dropping_silently(service):
    s = service
    for n in range(100):
        s.ok(s.owner("telegram_notify", {**NOTE, "event_key": f"event-{n}"}))
    full = s.owner("telegram_notify", {**NOTE, "event_key": "one-too-many"})
    assert full.status == "error" and full.error["code"] == "busy" and full.error["retryable"] is True
    assert s.rows("SELECT count(*) n FROM telegram_events WHERE event_key='one-too-many'")[0]["n"] == 0
    s.dispatch()
    assert s.ok(s.owner("telegram_notify", {**NOTE, "event_key": "one-too-many"}))["action"] == "posted"


# ----- owner-only status and reconciliation ---------------------------------------------------------------


def test_status_and_resolution_are_owner_only_and_never_agent_mcp_tools(service):
    s = service
    status = plain(s.ok(s.owner("telegram_status")))
    assert status["state"] == "running" and TOKEN not in json.dumps(status) and "secrets" not in json.dumps(status)
    descriptors = {name: s.runtime.registry.get(name) for name in ("telegram_status", "telegram_delivery_resolve")}
    assert all(d.owner_only and "mcp" not in d.exposure for d in descriptors.values())
    assert s.call(s.founder, "telegram_status").status == "error"
    assert s.call(s.founder, "telegram_delivery_resolve", {"outbox_id": new_id(), "action": "retry", "expected_revision": 1}).status == "error"


def deliveries(s, message_id):
    return s.ok(s.owner("telegram_delivery_get", {"message_id": message_id}))["items"][0]["deliveries"]


def resolve(s, message_id, action, *, outbox_id=None, **extra):
    """The owner's resolve call: the exact delivery (default the message's latest) and the receipt revision they just inspected."""
    found = [d for d in deliveries(s, message_id) if outbox_id in (None, d["outbox_id"])][-1]
    return s.owner("telegram_delivery_resolve", {"outbox_id": found["outbox_id"], "action": action,
                                                 "expected_revision": found["receipt_revision"], **extra})


def test_the_owner_reconciles_an_uncertain_send_by_exact_provider_id_or_retry(service):
    s = service
    from bazarlyk.telegram.api import TelegramApiError
    a, b = s.ok(send(s, s.founder, "a")), s.ok(send(s, s.founder, "b"))
    s.api.script("send_message", TelegramApiError("uncertain", "reset"), TelegramApiError("timeout", "slow"))
    s.dispatch()
    assert [s.ok(s.owner("telegram_delivery_get", {"message_id": m["message_id"]}))["items"][0]["state"] for m in (a, b)] == ["uncertain"] * 2
    assert resolve(s, a["message_id"], "mark_delivered").status == "error"
    assert resolve(s, a["message_id"], "retry", provider_message_id=5).status == "error"
    done = s.ok(resolve(s, a["message_id"], "mark_delivered", provider_message_id=901))
    assert done["state"] == "sent" and done["provider_message_id"] == 901
    row = s.rows("SELECT resolved_by, error_code FROM telegram_outbox WHERE message_id=?", a["message_id"])[0]
    assert row["resolved_by"] == s.fixture.owner.id and row["error_code"] == "owner_confirmed"
    assert resolve(s, a["message_id"], "retry").status == "error"
    retried = s.ok(resolve(s, b["message_id"], "retry"))
    assert retried["state"] == "queued"
    s.dispatch()
    assert [m["text"] for m in s.api.sent] == ["b"]
    assert s.rows("SELECT count(*) n FROM signoffs")[0]["n"] == 0


def test_a_resolution_names_the_inspected_receipt_revision_and_a_changed_receipt_is_refused(service):
    s = service
    from bazarlyk.telegram.api import TelegramApiError
    sent = s.ok(send(s, s.founder, "unsure"))
    s.api.script("send_message", TelegramApiError("uncertain", "reset"))
    s.dispatch()
    item = s.ok(s.owner("telegram_delivery_get", {"message_id": sent["message_id"]}))["items"][0]
    assert item["state"] == "uncertain" and item["receipt_revision"] >= 1
    status = s.ok(s.owner("telegram_status"))["uncertain_sends"][0]
    assert status["message_id"] == sent["message_id"] and status["receipt_revision"] == item["receipt_revision"]
    assert status["outbox_id"] == item["outbox_id"] and status["kind"] == "text"
    outbox_id = status["outbox_id"]
    missing = s.owner("telegram_delivery_resolve", {"outbox_id": outbox_id, "action": "retry"})
    assert missing.status == "error"
    by_message = s.owner("telegram_delivery_resolve", {"message_id": sent["message_id"], "action": "retry",
                                                        "expected_revision": item["receipt_revision"]})
    assert by_message.status == "error"  # a message is not an address: only an exact outbox_id resolves a receipt
    stale = s.owner("telegram_delivery_resolve", {"outbox_id": outbox_id, "action": "retry",
                                                   "expected_revision": item["receipt_revision"] + 1})
    assert stale.status == "error" and stale.error["code"] == "revision_conflict"
    assert s.ok(s.owner("telegram_delivery_get", {"message_id": sent["message_id"]}))["items"][0]["state"] == "uncertain"
    assert s.ok(s.owner("telegram_delivery_resolve", {"outbox_id": outbox_id, "action": "retry",
                                                       "expected_revision": item["receipt_revision"]}))["state"] == "queued"
    # The receipt moved on: repeating the earlier inspection can no longer authorize another send.
    again = s.owner("telegram_delivery_resolve", {"outbox_id": outbox_id, "action": "retry",
                                                   "expected_revision": item["receipt_revision"]})
    assert again.status == "error"
    assert s.ok(s.owner("telegram_delivery_get", {"outbox_id": outbox_id}))["items"][0]["message_id"] == sent["message_id"]
    assert s.call(s.founder, "telegram_delivery_get", {"outbox_id": outbox_id}).status == "error"


def test_a_provider_id_already_known_for_another_message_is_refused(service):
    s = service
    from bazarlyk.telegram.api import TelegramApiError
    first = s.ok(send(s, s.founder, "delivered"))
    s.dispatch()
    second = s.ok(send(s, s.founder, "unsure"))
    s.api.script("send_message", TelegramApiError("timeout", "slow"))
    s.dispatch()
    clash = resolve(s, second["message_id"], "mark_delivered", provider_message_id=s.api.sent[0]["message_id"])
    assert clash.status == "error" and clash.error["code"] == "validation_error"
    assert first["message_id"] != second["message_id"]


# ----- transports -----------------------------------------------------------------------------------------


def test_owner_http_routes_and_agent_mcp_discovery(service):
    from starlette.testclient import TestClient
    from test_service_transports import ORIGIN
    s = service
    with TestClient(s.s.web) as client:
        headers = ORIGIN
        # Service startup restarts the bridge's own threads, which re-verify asynchronously.
        assert s.bridge._ready.wait(10)
        response = client.post("/api/root/operations/telegram_status", json={}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["state"] == "running"
        assert client.post("/api/root/operations/telegram_status", json={}).status_code == 403
        response = client.post("/api/root/operations/telegram_notify", json={**NOTE, "idempotency_key": new_id()}, headers=headers)
        assert response.status_code == 200 and response.json()["data"]["action"] == "posted"
        assert client.post("/api/root/operations/telegram_send", json={"text": "x", "purpose": "update", "idempotency_key": new_id()},
                           headers=headers).status_code in (403, 404, 422)
        ready = s.fixture.prepare(s.founder)
        s.s.tokens[s.founder.id] = (Path(ready["workspace"]) / ".bazarlyk/credential").read_text().strip()
        mine = s.s.initialize(client, s.founder)
        tools = client.post("/mcp", headers=mine, json={"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}}).json()["result"]["tools"]
        names = {tool["name"] for tool in tools}
        assert {"telegram_send", "telegram_history", "telegram_delivery_get"} <= names
        assert not names & {"telegram_status", "telegram_delivery_resolve", "telegram_notify"}
        result = s.s.tool(client, mine, "telegram_send", {"text": "over MCP", "purpose": "update", "idempotency_key": new_id()})
        assert not result.get("isError"), result
        assert result["decoded"]["data"]["state"] == "queued" if "data" in result["decoded"] else True


def test_one_producers_full_urgent_backlog_does_not_block_another_producer(service):
    s = service
    for n in range(100):
        s.ok(s.owner("telegram_notify", {**NOTE, "event_key": f"event-{n}"}))
    worker = Principal(new_id(), "system", "Monitor worker")
    with s.fixture.db.transaction() as uow:
        uow.repository("principals").insert({"id": worker.id, "kind": "system", "display_name": worker.display_name})
    s.runtime.policy.worker_profiles = lambda principal_id, uow: (
        WorkerProfile({"telegram_notify": "allow"}, frozenset(), frozenset({"read_global_context"})) if principal_id == worker.id else None)
    context = InvocationContext(worker, source="worker", global_capabilities=frozenset({"read_global_context"}))
    other = s.runtime.application.invoke(context, "telegram_notify", {**NOTE, "event_key": "other-producer"}, {"idempotency_key": new_id()})
    assert s.ok(other)["action"] == "posted"
    assert s.owner("telegram_notify", {**NOTE, "event_key": "owner-again"}).error["code"] == "busy"


def test_the_owner_can_retry_only_an_uncertain_send_and_a_retry_never_refreshes_its_saved_binding(service):
    s = service
    from bazarlyk.telegram.api import TelegramApiError
    refused = s.ok(send(s, s.founder, "refused by telegram"))
    s.api.script("send_message", TelegramApiError("refused", "chat not found", error_code=400))
    s.dispatch()
    assert s.rows("SELECT state FROM telegram_outbox WHERE message_id=?", refused["message_id"])[0]["state"] == "failed"
    # A failed send is sent again by its agent, not authorized again by the owner.
    assert resolve(s, refused["message_id"], "retry").status == "error"
    unsure = s.ok(send(s, s.founder, "unsure"))
    s.api.script("send_message", TelegramApiError("uncertain", "reset"))
    s.dispatch()
    with s.fixture.db.transaction() as uow:
        uow.connection.execute("UPDATE telegram_outbox SET binding_fingerprint=? WHERE message_id=?", ("0" * 64, unsure["message_id"]))
    assert s.ok(resolve(s, unsure["message_id"], "retry"))["state"] == "queued"
    s.dispatch()
    row = s.rows("SELECT state, error_code, binding_fingerprint FROM telegram_outbox WHERE message_id=?", unsure["message_id"])[0]
    assert (row["state"], row["error_code"], row["binding_fingerprint"]) == ("failed", "binding_changed", "0" * 64) and s.api.sent == []


def test_a_blocking_request_that_could_not_be_sent_is_cancelled_and_frees_its_key(service):
    s = service
    from bazarlyk.telegram.api import TelegramApiError
    args = dict(text="Send the SMS code", purpose="blocking_input", request_key="sms", reason="Login blocked", input_mode="secret")
    first = s.ok(s.call(s.marketing, "telegram_send", args))
    s.api.script("send_message", TelegramApiError("refused", "bot was blocked by the user", error_code=403))
    s.dispatch()
    assert s.rows("SELECT state FROM telegram_outbox WHERE message_id=?", first["message_id"])[0]["state"] == "failed"
    assert s.rows("SELECT request_state FROM telegram_messages WHERE id=?", first["message_id"])[0]["request_state"] == "cancelled"
    # Nothing awaits a secret any more, and the same key can be asked again as a new request.
    again = s.ok(s.call(s.marketing, "telegram_send", {**args, "text": "Send the SMS code once more"}))
    assert again["message_id"] != first["message_id"] and again["request"]["state"] == "pending"
    grant = s.rows("SELECT count(*) n FROM telegram_messages WHERE chat_name='marketing' AND request_state='pending'")[0]["n"]
    assert grant == 1
