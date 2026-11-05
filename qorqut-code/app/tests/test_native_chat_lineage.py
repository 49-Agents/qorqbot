"""Conversation lineage, rendered read boundaries and verified system provenance of derived Chat.

* A visible entry id and a completion id are scoped by agent, provider, native conversation and archive lineage plus the record's
  own identity, so two conversations that share a raw offset or reuse a provider item id never collide, and a resumed
  conversation keeps every id.
* A page acknowledges only the replies it rendered (``rendered_boundary``), never the agent's newest reply.
* A system marker is verified against the durable input receipts for exactly the markers a page shows, in that page's
  conversation, so provenance neither ages out nor crosses conversations.
* A late ``SessionEnd`` hook from an earlier process cannot close the resumed conversation.

Synthetic agents, panes and transcripts only."""

import json

import pytest

from bazarlyk.native_chat import captures as captures_module, reader as reader_module, store
from bazarlyk.native_chat.contracts import (InputRequest, STATE_SUBMITTED, STATE_WAITING, TYPE_BROADCAST, TYPE_NUDGE)
from bazarlyk.native_chat.projection import Lineage, marker_text
from bazarlyk.reply_notifications import replies
from native_chat_support import Native, count, rows

KEY = "chat-key-000001"
FIXED_TIME = "2026-10-11T10:00:00.000000Z"


def chat_items(native):
    return [item for item in replies(native.runtime, native.owner_context())["items"] if item.get("chat")]


def unkeyed(record):
    """A record that carries no native id of its own, so only its raw position identifies it."""
    record = json.loads(json.dumps(record))
    record.pop("uuid", None)
    record.get("message", {}).pop("id", None)
    record["timestamp"] = FIXED_TIME
    return record


def keyed(record):
    record["timestamp"] = FIXED_TIME
    return record


# ----- identity ---------------------------------------------------------------------------------------------------------

def test_two_conversations_sharing_a_raw_offset_and_a_provider_item_id_keep_separate_entries_and_replies(tmp_path):
    native = Native(tmp_path)
    native.publish()

    def converse(label):
        native.write(unkeyed(native.claude_user("question")), unkeyed(native.claude_assistant(f"raw reply {label}")),
                     keyed(native.claude_assistant(f"shared item reply {label}", message_id="msg-shared", uuid_="uuid-shared")))
        native.pass_(2)

    first = native.session_id
    converse("one")
    native.end()
    native.pass_(3)
    native.new_session()
    second = native.session_id
    converse("two")

    stored = rows(native.runtime, "SELECT * FROM native_replies ORDER BY completion_sequence")
    assert [r["completion_sequence"] for r in stored] == [1, 2, 3, 4] and len({r["id"] for r in stored}) == 4
    assert {r["conversation_id"] for r in stored} == {first, second}
    assert all(r["archive_id"] == f"claude:{r['conversation_id']}" for r in stored)
    raw = [r for r in stored if r["identity_kind"] == "raw_range"]
    assert len(raw) == 2 and raw[0]["raw_offset"] == raw[1]["raw_offset"] and raw[0]["identity_key"] == raw[1]["identity_key"]
    shared = [r for r in stored if r["identity_kind"] == "native_id"]
    assert len(shared) == 2 and shared[0]["identity_key"] == shared[1]["identity_key"] == "msg-shared"
    assert [item["sequence"] for item in chat_items(native)] == [4]            # the newest reply is the unread notification

    one = native.chat.state(native.owner, native.agent, conversation=first, provider="claude")["entries"]
    two = native.chat.state(native.owner, native.agent, conversation=second, provider="claude")["entries"]
    assert [e["text"] for e in one][-1] == "shared item reply one" and [e["text"] for e in two][-1] == "shared item reply two"
    assert len(one) == len(two) == 3 and {e["position"] for e in one} == {e["position"] for e in two}
    assert len({e["id"] for e in one} | {e["id"] for e in two}) == 6           # no entry id repeats across conversations
    assert [e["completion_sequence"] for e in one] == [None, 1, 2] and [e["completion_sequence"] for e in two] == [None, 3, 4]

    # A restart changes nothing about any identity.
    native.build()
    native.pass_(2)
    assert rows(native.runtime, "SELECT id FROM native_replies ORDER BY completion_sequence") == [{"id": r["id"]} for r in stored]
    assert native.chat.state(native.owner, native.agent, conversation=second, provider="claude")["entries"] == two


def test_resuming_a_conversation_keeps_entry_and_completion_ids_and_never_duplicates_a_reply(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.write(native.claude_user("one"), native.claude_assistant("First.", message_id="m-1", uuid_="u-1"))
    native.pass_()
    before = native.chat.state(native.owner, native.agent)["entries"]
    stored = rows(native.runtime, "SELECT id,archive_id FROM native_replies")
    native.end()
    native.pass_(3)
    native.new_session(same_conversation=True, pid=9001)
    native.pass_(3)
    assert rows(native.runtime, "SELECT generation FROM native_sessions ORDER BY generation") == [{"generation": 1}, {"generation": 2}]
    assert native.chat.state(native.owner, native.agent)["entries"] == before
    assert rows(native.runtime, "SELECT id,archive_id FROM native_replies") == stored


# ----- rendered boundary ------------------------------------------------------------------------------------------------

def conversation_of(native, count_):
    for index in range(count_):
        native.write(native.claude_user(f"question {index}"), native.claude_assistant(f"answer {index}", message_id=f"m{index}"))
    native.pass_(3)


def test_each_page_names_the_completions_it_rendered_and_a_newer_reply_stays_unread(tmp_path):
    native = Native(tmp_path)
    native.publish()
    conversation_of(native, 6)
    page = native.chat.state(native.owner, native.agent, limit=4)
    assert [e["text"] for e in page["entries"]] == ["question 4", "answer 4", "question 5", "answer 5"]
    assert [e["completion_sequence"] for e in page["entries"]] == [None, 5, None, 6]
    assert page["rendered_boundary"] == 6 and page["newest_sequence"] == 6

    older = native.chat.history(native.owner, native.agent, before=page["older"], limit=4)
    assert [e["text"] for e in older["entries"]] == ["question 2", "answer 2", "question 3", "answer 3"]
    assert [e["completion_sequence"] for e in older["entries"]] == [None, 3, None, 4] and older["rendered_boundary"] == 4
    oldest = native.chat.history(native.owner, native.agent, before=older["older"], limit=4)
    assert oldest["rendered_boundary"] == 2 and oldest["older"] is None
    single = native.chat.history(native.owner, native.agent, before=older["entries"][0]["position"], limit=1)
    assert [e["speaker"] for e in single["entries"]] == ["agent"] and single["rendered_boundary"] == 2

    # A reply finishes after the page was rendered: acknowledging what the page showed leaves it unread.
    native.write(native.claude_user("question 6"), native.claude_assistant("answer 6", message_id="m6"))
    native.pass_()
    assert native.chat.state(native.owner, native.agent, limit=4)["newest_sequence"] == 7
    assert native.chat.mark_read(native.owner, native.agent, page["rendered_boundary"])["last_read_sequence"] == 6
    assert [item["sequence"] for item in chat_items(native)] == [7]
    # Reading an older page afterwards never moves the saved boundary back.
    assert native.chat.mark_read(native.owner, native.agent, older["rendered_boundary"])["last_read_sequence"] == 6
    fresh = native.chat.state(native.owner, native.agent, limit=4)
    native.chat.mark_read(native.owner, native.agent, fresh["rendered_boundary"])
    assert fresh["rendered_boundary"] == 7 and chat_items(native) == []


def test_a_page_of_an_older_conversation_cannot_acknowledge_a_newer_conversations_reply(tmp_path):
    native = Native(tmp_path)
    native.publish()
    first = native.session_id
    conversation_of(native, 2)
    native.end()
    native.pass_(3)
    native.new_session()
    native.write(native.claude_user("new"), native.claude_assistant("New conversation reply.", message_id="n-1"))
    native.pass_(2)
    old_page = native.chat.state(native.owner, native.agent, conversation=first, provider="claude")
    assert old_page["conversation"]["conversation_id"] == first
    assert old_page["rendered_boundary"] == 2 and old_page["newest_sequence"] == 3
    native.chat.mark_read(native.owner, native.agent, old_page["rendered_boundary"])
    [item] = chat_items(native)
    assert item["sequence"] == 3 and item["text"] == "New conversation reply."
    current = native.chat.state(native.owner, native.agent)
    assert current["conversation"]["conversation_id"] == native.session_id and current["rendered_boundary"] == 3
    native.chat.mark_read(native.owner, native.agent, current["rendered_boundary"])
    assert chat_items(native) == []


def codex_native(tmp_path):
    native = Native(tmp_path, "codex")
    native.transport, native.thread_owner = "codex_queue", "owner-1"
    native.publish()
    return native


def test_a_codex_reply_named_only_by_its_turn_end_is_a_visible_entry_with_its_full_text_before_it_is_acknowledged(tmp_path):
    native = codex_native(tmp_path)
    reply = ("Summary that only the turn end names. " * 12).strip()          # longer than the 180-character preview
    native.write(native.codex_session_meta(), native.codex_event("task_started"),
                 native.codex_event("user_message", message="Summarize"),
                 native.codex_event("task_complete", last_agent_message=reply))
    native.pass_(2)
    [stored] = rows(native.runtime, "SELECT * FROM native_replies")
    assert stored["identity_kind"] == "raw_range" and stored["identity_key"] == f"{stored['raw_offset']}#end"
    assert len(stored["preview"]) == 180 and reply.startswith(stored["preview"]) and len(reply) > 180

    page = native.chat.state(native.owner, native.agent)
    assert [e["speaker"] for e in page["entries"]] == ["user", "agent"]
    answer = page["entries"][1]
    assert answer["text"] == reply                                  # the raw record's full text, never the shortened preview
    lineage = Lineage(native.agent, "codex", native.session_id, stored["archive_id"])
    assert answer["identity"] == "raw_range" and answer["position"] == stored["raw_offset"]
    assert answer["id"] == lineage.entry_id("raw_range", stored["identity_key"])
    assert stored["id"] == lineage.reply_id("raw_range", stored["identity_key"])
    assert answer["completion_id"] == stored["id"] and answer["completion_sequence"] == 1 and answer["partial"] is False
    assert page["rendered_boundary"] == 1 and page["newest_sequence"] == 1

    [item] = chat_items(native)
    assert item["text"] == stored["preview"]                        # the notification keeps the short preview
    native.chat.mark_read(native.owner, native.agent, page["rendered_boundary"])
    assert chat_items(native) == []
    native.build()                                                  # the entry and its identity survive a restart
    native.pass_(2)
    assert native.chat.state(native.owner, native.agent)["entries"] == page["entries"]
    assert count(native.runtime, "native_replies") == 1


def test_a_turn_end_that_repeats_an_assistant_message_adds_no_second_entry_even_on_a_page_that_starts_mid_turn(tmp_path, monkeypatch):
    native = codex_native(tmp_path)
    answer = "The final answer, shown once."
    native.write(native.codex_session_meta(), native.codex_event("task_started"),
                 native.codex_event("user_message", message="Question"),
                 native.codex_assistant(answer, final=True, native_id="item-final"),
                 native.codex_event("task_complete", last_agent_message=answer))
    native.pass_(2)
    [stored] = rows(native.runtime, "SELECT * FROM native_replies")
    assert (stored["identity_kind"], stored["identity_key"]) == ("native_id", "item-final")
    full = native.chat.state(native.owner, native.agent)
    assert [e["speaker"] for e in full["entries"]] == ["user", "agent"]
    assert full["entries"][1]["identity"] == "native_id" and full["entries"][1]["completion_sequence"] == 1
    assert full["rendered_boundary"] == 1

    # A bounded page that begins exactly at the turn-end record has lost the turn's state. Read alone, that record would look like a
    # lone turn-end message; the complete-stream index says the reply was the earlier final answer, so no second copy is invented.
    data = native.transcript_path().read_bytes()
    turn_end_at = len(data) - len(data.rstrip(b"\n").rsplit(b"\n", 1)[1]) - 1
    monkeypatch.setattr(reader_module, "FIRST_WINDOW", len(data) - turn_end_at + 1)
    native.chat.reader._cache.clear()
    bounded = native.chat.state(native.owner, native.agent, limit=1)
    assert bounded["entries"] == [] and bounded["rendered_boundary"] == 0 and bounded["older"] == turn_end_at
    native.chat.mark_read(native.owner, native.agent, bounded["rendered_boundary"])
    assert [item["sequence"] for item in chat_items(native)] == [1]           # the page showed no reply, so it acknowledged none
    before = native.chat.history(native.owner, native.agent, before=bounded["older"], limit=1)
    assert [e["text"] for e in before["entries"]] == [answer] and before["entries"][0]["identity"] == "native_id"
    assert before["entries"][0]["completion_sequence"] == 1 and before["rendered_boundary"] == 1
    native.chat.mark_read(native.owner, native.agent, before["rendered_boundary"])
    assert chat_items(native) == [] and count(native.runtime, "native_replies") == 1


def test_a_turn_end_reply_the_index_has_not_reached_yet_is_not_shown_and_acknowledges_nothing(tmp_path, monkeypatch):
    native = codex_native(tmp_path)
    native.write(native.codex_session_meta(), native.codex_event("task_started"),
                 native.codex_event("user_message", message="Summarize"),
                 native.codex_event("task_complete", last_agent_message="Late summary."))
    monkeypatch.setattr(captures_module, "BATCH_RECORDS", 3)
    native.pass_()                                                  # the archive holds all four records; the index read three
    early = native.chat.state(native.owner, native.agent)
    assert [e["speaker"] for e in early["entries"]] == ["user"] and early["rendered_boundary"] == 0
    assert early["newest_sequence"] == 0 and chat_items(native) == []
    native.pass_(2)
    done = native.chat.state(native.owner, native.agent)
    assert [e["speaker"] for e in done["entries"]] == ["user", "agent"] and done["entries"][1]["text"] == "Late summary."
    assert done["rendered_boundary"] == 1 and [item["sequence"] for item in chat_items(native)] == [1]


def test_a_page_that_returns_no_completed_reply_leaves_the_replys_notification_unread(tmp_path):
    native = Native(tmp_path)
    native.publish()
    conversation_of(native, 2)
    page = native.chat.state(native.owner, native.agent, limit=1)
    assert [e["text"] for e in page["entries"]] == ["answer 1"] and page["rendered_boundary"] == 2
    question = native.chat.history(native.owner, native.agent, before=page["older"], limit=1)
    assert [e["speaker"] for e in question["entries"]] == ["user"] and question["rendered_boundary"] == 0
    assert native.chat.mark_read(native.owner, native.agent, question["rendered_boundary"])["last_read_sequence"] == 0
    assert [item["sequence"] for item in chat_items(native)] == [2]
    native.chat.mark_read(native.owner, native.agent, page["rendered_boundary"])
    assert chat_items(native) == []


def test_a_page_without_a_reply_has_boundary_zero(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.write(native.claude_user("only a question"))
    native.pass_()
    page = native.chat.state(native.owner, native.agent)
    assert page["rendered_boundary"] == 0 and page["newest_sequence"] == 0 and page["entries"][0]["completion_sequence"] is None
    (tmp_path / "other").mkdir()
    empty = Native(tmp_path / "other")
    assert empty.chat.state(empty.owner, empty.agent)["rendered_boundary"] == 0


# ----- verified system provenance ---------------------------------------------------------------------------------------

def flood(native, number):
    """Later inputs of any kind, stored as the arbiter stores them, so an old nudge is far behind the recent ones."""
    with native.runtime.database.transaction() as uow:
        session = store.live_session(uow.connection, native.agent)
        for index in range(number):
            request = InputRequest(native.agent, TYPE_BROADCAST, f"flood-{index}", f"note {index}")
            store.insert_input(uow.connection, session, request, store.request_hash(request), state=STATE_SUBMITTED,
                               reason="processing_not_confirmed")


def notices(native, **options):
    state = native.chat.state(native.owner, native.agent, limit=100, **options)
    return [e for e in state["entries"] if e["speaker"] == "notice"]


def test_an_old_nudge_keeps_its_verified_provenance_after_more_than_a_hundred_later_inputs(tmp_path):
    native = Native(tmp_path)
    native.publish()
    nudge = native.arbiter.admit_nudge(native.agent, "nudge-old", "New inbox events are available.")
    assert nudge.state == STATE_SUBMITTED and nudge.marker
    native.pass_()
    flood(native, 130)
    [notice] = notices(native)
    assert notice["system"] == {"input_id": nudge.input_id, "input_type": TYPE_NUDGE, "verified": True}
    assert notice["text"] == "New inbox events are available."
    # The same page by history, and after the same conversation was resumed by a new process (a new generation).
    native.end()
    native.pass_(3)
    native.new_session(same_conversation=True, pid=9001)
    native.pass_(2)
    assert [e["system"] for e in notices(native)] == [notice["system"]]
    assert native.chat.history(native.owner, native.agent, before=10 ** 9, limit=100)["entries"][0]["system"] == notice["system"]


def test_a_marker_copied_into_another_conversation_or_never_delivered_is_not_verified_system_input(tmp_path):
    native = Native(tmp_path)
    native.publish()
    first = native.session_id
    nudge = native.arbiter.admit_nudge(native.agent, "nudge-1", "Check the inbox.")
    native.pass_()
    native.end()
    native.pass_(3)
    native.new_session()
    second = native.session_id
    # The owner (or an agent) pastes the very same marker text into a different conversation.
    native.client_user_turn(marker_text(nudge.marker, TYPE_NUDGE, "Check the inbox."))
    native.pass_(2)
    [copied] = notices(native, conversation=second, provider="claude")
    assert copied["system"] == {"input_id": None, "input_type": TYPE_NUDGE, "verified": False, "reason": "no_matching_input"}
    [original] = notices(native, conversation=first, provider="claude")
    assert original["system"]["verified"] is True and original["system"]["input_id"] == nudge.input_id

    # An input that was admitted but never typed: its marker in a transcript is somebody else's text.
    native.pane.pasted = "half-written owner text"
    waiting = native.arbiter.admit_nudge(native.agent, "nudge-2", "Another.", wait=True)
    assert waiting.state == STATE_WAITING and native.pane.typed() == []
    native.client_user_turn(marker_text(waiting.marker, TYPE_NUDGE, "Another."))
    # And a delivered marker presented under another type.
    native.client_user_turn(marker_text(nudge.marker, "broadcast", "Wrong type."))
    native.pass_(2)
    reasons = {e["text"]: e["system"]["reason"] for e in notices(native, conversation=second, provider="claude") if not e["system"]["verified"]}
    assert reasons["Another."] == "not_delivered" and reasons["Wrong type."] == "no_matching_input"
    assert rows(native.runtime, "SELECT state FROM native_inputs WHERE id=?", (waiting.input_id,)) == [{"state": STATE_WAITING}]


def test_a_marker_typed_under_a_different_type_than_the_stored_input_is_not_verified(tmp_path):
    native = Native(tmp_path)
    native.publish()
    nudge = native.arbiter.admit_nudge(native.agent, "nudge-1", "Check the inbox.")
    native.client_user_turn(marker_text(nudge.marker, "broadcast", "Relabelled."))
    native.client_user_turn(f'<qorqut-system-input id="{nudge.marker}">\nNo declared type.\n</qorqut-system-input>')
    native.pass_(2)
    by_text = {e["text"]: e["system"] for e in notices(native)}
    assert by_text["Relabelled."] == {"input_id": None, "input_type": "broadcast", "verified": False, "reason": "type_mismatch"}
    assert by_text["No declared type."]["verified"] is False and by_text["No declared type."]["reason"] == "type_mismatch"
    assert by_text["Check the inbox."]["verified"] is True


# ----- exact exit evidence ----------------------------------------------------------------------------------------------

def test_a_late_session_end_from_the_earlier_process_does_not_end_the_resumed_conversation(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.pass_()
    old = {"pid": native.pid, "started": native.started}
    native.new_session(same_conversation=True, pid=9001)           # `--resume`: the same conversation, a new process
    native.pass_(2)
    assert rows(native.runtime, "SELECT generation,ended_at IS NULL AS open FROM native_sessions ORDER BY generation") == [
        {"generation": 1, "open": 0}, {"generation": 2, "open": 1}]
    native.pane.pasted = "half-written owner text"                  # keep the Chat message waiting
    sent = native.chat.send(native.owner, native.agent, "Wait for me", KEY, native.pin())
    marker = json.loads((native.workspace / ".bazarlyk/terminal-chat.json").read_text())
    # The earlier process's SessionEnd hook arrives late and replaces the marker (same session id, the old process).
    (native.workspace / ".bazarlyk/terminal-chat.json").write_text(json.dumps({**marker, **old, "event": "SessionEnd"}))
    native.pass_(3)
    assert rows(native.runtime, "SELECT generation,ended_at IS NULL AS open FROM native_sessions ORDER BY generation") == [
        {"generation": 1, "open": 0}, {"generation": 2, "open": 1}]
    with native.runtime.database.read() as uow:
        assert store.get_input(uow.connection, sent["input"]["id"])["state"] == STATE_WAITING
    assert native.pane.typed() == [] and native.pane.pasted == "half-written owner text"
    # The live process's next hook restores the marker; the input goes out into the same (second) binding once the composer is free.
    native.publish()
    native.pane.pasted = None
    native.pass_(2)
    assert native.pane.typed() == ["Wait for me"]
    assert rows(native.runtime, "SELECT state FROM native_inputs") == [{"state": STATE_SUBMITTED}]


def test_a_session_end_from_the_bound_process_still_ends_its_binding_and_undelivers_what_waited(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.pass_()
    native.pane.pasted = "half-written owner text"
    sent = native.chat.send(native.owner, native.agent, "Never typed", KEY, native.pin())
    native.end()                                                   # the bound process's own SessionEnd (and it is gone)
    native.pass_(3)
    assert count(native.runtime, "native_sessions", "ended_at IS NOT NULL") == 1
    assert rows(native.runtime, "SELECT state,reason FROM native_inputs WHERE id=?", (sent["input"]["id"],)) == [
        {"state": "undelivered", "reason": "session_ended"}]


# ----- capture states ---------------------------------------------------------------------------------------------------

def test_capture_that_is_on_but_not_yet_published_is_not_the_same_as_capture_that_is_off(tmp_path):
    (tmp_path / "enabled").mkdir()
    (tmp_path / "disabled").mkdir()
    ready_soon = Native(tmp_path / "enabled")
    marker = {"agent_id": ready_soon.agent, "session_id": ready_soon.session_id, "runtime": "claude-code",
              "workspace": str(ready_soon.workspace), "pid": ready_soon.pid, "started": ready_soon.started, "owner": None,
              "event": "SessionStart"}
    (ready_soon.workspace / ".bazarlyk/native-binding.json").write_text(json.dumps(marker))
    state = ready_soon.chat.state(ready_soon.owner, ready_soon.agent)["session"]
    assert state["state"] == "live" and state["reason"] == "capture_not_ready" and state["can_send"] is False
    with pytest.raises(Exception) as early:
        ready_soon.chat.send(ready_soon.owner, ready_soon.agent, "Too early", KEY, ready_soon.pin())
    assert early.value.code == "capture_not_ready"
    ready_soon.publish()                                           # the client's hook publishes the transcript
    ready_soon.pass_()
    assert ready_soon.chat.state(ready_soon.owner, ready_soon.agent)["session"]["reason"] == "live"
    assert ready_soon.chat.send(ready_soon.owner, ready_soon.agent, "Now it works", KEY, ready_soon.pin())["input"]["state"] == "waiting"

    disabled = Native(tmp_path / "disabled", capture=False)
    (disabled.workspace / ".bazarlyk/native-binding.json").write_text(json.dumps({**marker, "agent_id": disabled.agent,
                                                                                  "session_id": disabled.session_id,
                                                                                  "workspace": str(disabled.workspace)}))
    off = disabled.chat.state(disabled.owner, disabled.agent)["session"]
    assert off["state"] == "live" and off["reason"] == "capture_disabled" and off["can_send"] is False
    with pytest.raises(Exception) as final:
        disabled.chat.send(disabled.owner, disabled.agent, "Never", KEY, disabled.pin())
    assert final.value.code == "capture_disabled"
    disabled.publish()                                             # a stray transcript marker never overrides the registration
    disabled.pass_()
    assert disabled.chat.state(disabled.owner, disabled.agent)["session"]["reason"] == "capture_disabled"
    assert count(disabled.runtime, "native_captures") == 0
