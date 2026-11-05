"""The established Telegram flows beside the attachment path, on the real composed host: nothing may regress.

A canonical Signoff question and a blocking-input prompt with the owner's reply each run through the real
bridge (its Signoff step, polling and inbound acceptance, owner-input queue and dispatcher) with only
the Bot API and the native terminal's arbiter replaced by recording fakes. Every flow runs alone, beside a queued attachment
upload, and while media upkeep is failing: the attachment machinery may add work but must never change or stop these.
"""

from datetime import datetime, timezone
import json

import pytest

from qorqut_operations.contracts import new_id

from bazarlyk.telegram import store
from telegram_media_operation_support import (
    CHAT_ID, SIGNOFFS, TOPIC, PDF, attach_arbiter, ask_owner, build_host, heal, invoke, messages, outbox,
    poll, pump, push_owner_message, send_arguments, sql, workspace_source, write_file)


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    return build_host(tmp_path_factory.mktemp("media-flows"))


@pytest.fixture
def host(shared):
    heal(shared)
    with shared.bridge.write("test.reset") as uow:
        for row in [dict(r) for r in uow.connection.execute("SELECT * FROM telegram_outbox WHERE state IN ('queued','dispatching','uncertain')")]:
            store.set_outbox(uow, store.outbox(uow, row["id"]), state="failed", error_code="test_reset")
    for collection in (shared.api.uploads, shared.api.texts, shared.api.documents, shared.api.deleted, shared.api.script):
        collection.clear()
    shared.bridge.clock = lambda: datetime.now(timezone.utc)
    yield shared
    heal(shared)


@pytest.fixture(params=["alone", "pending_attachment", "failing_media"])
def beside(request, host, monkeypatch):
    """Arrange the media machinery beside the flow under test; the returned check proves it neither broke nor blocked anything."""
    mode = request.param
    if mode == "pending_attachment":
        write_file(host, "exports/beside.pdf")
        sent = invoke(host, send_arguments(workspace_source("exports/beside.pdf"), caption="beside"), name="telegram_send_file")
        assert sent.status == "ok", sent.error

    if mode == "failing_media":
        def broken():
            raise OSError("disk trouble")

        monkeypatch.setattr(host.bridge.media, "sweep", broken)
        monkeypatch.setattr(host.bridge.media, "recover", broken)
        host.bridge._media_swept, host.bridge._media_recovered = None, False

    def check():
        if mode == "pending_attachment":
            [upload] = host.api.uploads                                 # the file went out too, exactly once, to its own topic
            assert (upload.thread, upload.data, upload.caption) == (TOPIC, PDF, "beside")
            assert [row["state"] for row in outbox(host) if row["source_kind"] == "agent_file"][-1] == "sent"
        else:
            assert host.api.uploads == []
        # Only a failing media sweep may be visible, and it degrades nothing else.
        assert host.bridge.health() == ({"media": "storage_error"} if mode == "failing_media" else {})
        assert host.bridge._ready.is_set()

    return check


def topic_texts(host, thread):
    return [item for item in host.api.texts if item.thread == thread and item.chat_id == CHAT_ID]


def test_a_canonical_signoff_question_is_posted_to_its_topic_and_stays_pending(host, beside):
    signoff = ask_owner(host, f"direction-{new_id()[:8]}", "Which direction should marketing take?")
    pump(host)
    [post] = [item for item in topic_texts(host, SIGNOFFS) if "Which direction should marketing take?" in item.text]
    assert post.text.startswith("Question: Which direction should marketing take?")
    assert "requested by" in post.text and "Reply to this message with your answer." in post.text
    assert not [item for item in topic_texts(host, TOPIC) if "Which direction should marketing take?" in item.text]
    row = sql(host, "SELECT * FROM signoffs WHERE id=?", signoff["id"])[0]
    assert (row["state"], row["category"], row["superseded_by_id"]) == ("pending", "input_request", None)      # a post decides nothing
    tracked = sql(host, "SELECT * FROM telegram_signoff_posts WHERE signoff_id=?", signoff["id"])
    assert [(item["state"], item["input_form"]) for item in tracked] == [("active", "input")]
    sent = [item for item in outbox(host) if item["source_kind"] == "signoff" and item["id"] == tracked[0]["outbox_id"]]
    assert [(item["state"], item["chat_name"]) for item in sent] == [("sent", "signoffs")]
    pump(host)
    assert len([item for item in topic_texts(host, SIGNOFFS) if "Which direction should marketing take?" in item.text]) == 1
    beside()


def test_the_owner_reply_to_a_signoff_post_is_recorded_through_the_canonical_answer_and_typed_once(host, beside, monkeypatch):
    arbiter = attach_arbiter(host, monkeypatch)
    signoff = ask_owner(host, f"reply-{new_id()[:8]}", "Pick the launch week")
    pump(host)
    provider_id = sql(host, "SELECT m.telegram_message_id FROM telegram_signoff_posts p JOIN telegram_messages m ON m.id=p.message_id "
                            "WHERE p.signoff_id=?", signoff["id"])[0]["telegram_message_id"]
    # Another sender in the same topic is not the owner: nothing is recorded or decided.
    push_owner_message(host, "I am not the owner", thread=SIGNOFFS, reply_to=provider_id, sender=999)
    poll(host)
    pump(host)
    assert sql(host, "SELECT state FROM signoffs WHERE id=?", signoff["id"])[0]["state"] == "pending" and arbiter.typed == []
    push_owner_message(host, "Launch in the second week of November.", thread=SIGNOFFS, reply_to=provider_id)
    poll(host)
    pump(host)
    notes = sql(host, "SELECT body FROM signoff_notes WHERE signoff_id=?", signoff["id"])
    assert [note["body"] for note in notes] == ["Launch in the second week of November."]
    assert sql(host, "SELECT state FROM signoffs WHERE id=?", signoff["id"])[0]["state"] != "pending"      # the canonical answer decided
    [typed] = arbiter.typed
    assert "Pick the launch week" in typed.text and "Launch in the second week of November." in typed.text and typed.sensitive is False
    assert [row["state"] for row in sql(host, "SELECT state FROM telegram_owner_inputs WHERE signoff_id=?", signoff["id"])] == ["submitted"]
    pump(host)
    assert len(arbiter.typed) == 1
    beside()


def test_a_blocking_input_prompt_and_the_owner_reply_reach_the_agent_through_the_owner_input_queue(host, beside, monkeypatch):
    arbiter = attach_arbiter(host, monkeypatch)
    key = f"database-{new_id()[:8]}"
    sent = invoke(host, {"text": "Which database should I use for the pilot?", "purpose": "blocking_input", "request_key": key,
                         "reason": "I cannot continue without the choice", "input_mode": "text"}, name="telegram_send")
    assert sent.status == "ok", sent.error
    pump(host)
    [prompt] = [item for item in topic_texts(host, TOPIC) if "Which database should I use for the pilot?" in item.text]
    assert "I cannot continue without the choice" in prompt.text
    prompt_row = sql(host, "SELECT * FROM telegram_messages WHERE id=?", sent.data["message_id"])[0]
    assert (prompt_row["request_state"], prompt_row["input_mode"], prompt_row["telegram_message_id"] is not None) == ("pending", "text", True)
    push_owner_message(host, "Use SQLite for the pilot.", thread=TOPIC, reply_to=prompt_row["telegram_message_id"])
    poll(host)
    pump(host)
    inbox = sql(host, "SELECT body FROM agent_messages WHERE body LIKE ?", f"%[Request {key} (text)%Use SQLite for the pilot.%")
    assert len(inbox) == 1                                                                # the canonical copy in the agent's inbox
    answered = sql(host, "SELECT request_state FROM telegram_messages WHERE id=?", sent.data["message_id"])[0]["request_state"]
    assert answered == "answered"
    [typed] = arbiter.typed
    assert typed.agent_id == host.ordubashy and typed.sensitive is False
    assert "Qorqut owner message via Telegram" in typed.text and "Use SQLite for the pilot." in typed.text
    delivery = sql(host, "SELECT state, sensitive, spool_id FROM telegram_owner_inputs ORDER BY created_at DESC, rowid DESC LIMIT 1")[0]
    assert (delivery["state"], delivery["sensitive"], delivery["spool_id"]) == ("submitted", 0, None)       # nonsecret: never spooled
    pump(host)
    assert len(arbiter.typed) == 1
    beside()
