"""Backend capture, reply indexing, completion/read state and the owner notification projection.

The backend runs with Chat closed: nothing here opens a Chat view before a reply exists. Synthetic transcripts are written
into a temporary tree; the real ``bazarlyk.native_archive`` captures them."""

import json
import threading

import pytest

from qorqut_operations.contracts import DomainError

from bazarlyk import attention_queue
from bazarlyk.native_archive import CaptureBinding, TranscriptArchive
from bazarlyk.native_chat import store
from bazarlyk.native_chat.projection import Lineage
from bazarlyk.reply_notifications import replies
from host_foundation_support import historical_business_agent
from native_chat_support import Native, count, rows


def feed(native):
    return replies(native.runtime, native.owner_context())["items"]


def chat_items(native):
    return [item for item in feed(native) if item.get("chat")]


# ----- closed-Chat capture -> eligible reply -> read -> restart ---------------------------------------------------------

@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_reply_finished_with_chat_closed_is_unread_then_read_and_stays_read_after_restart(tmp_path, provider):
    native = Native(tmp_path, provider)
    native.publish()
    if provider == "claude":
        native.write(native.claude_user("Summarize the plan"),
                     native.claude_assistant("Let me look.", stop="tool_use", tool=True),   # narration before a tool: not a reply
                     native.claude_assistant("The plan has three steps.", message_id="msg-final"))
    else:
        native.write(native.codex_session_meta(), native.codex_event("task_started"),
                     native.codex_event("user_message", message="Summarize the plan"),
                     native.codex_assistant("Let me look."),                             # not final: held as a candidate
                     native.codex_assistant("The plan has three steps.", final=True, native_id="item-final"),
                     native.codex_event("task_complete"))
    native.pass_(2)
    [item] = chat_items(native)
    assert item["id"].startswith("gchat:") and item["unread"] and item["sequence"] == 1 and item["nav_only"] is False
    assert item["text"] == "The plan has three steps." and item["agent_id"] == native.agent
    assert count(native.runtime, "native_replies") == 1            # the tool narration / non-final item is no completion

    # Nothing opened Chat; the first state read shows the same facts, and reading through the boundary clears the indication.
    state = native.chat.state(native.owner, native.agent)
    assert state["newest_sequence"] == 1 and state["last_read_sequence"] == 0
    assert native.chat.mark_read(native.owner, native.agent, 1) == {"last_read_sequence": 1, "newest_sequence": 1}
    assert chat_items(native) == []

    # A new service instance over the same database neither re-announces the reply nor forgets the boundary.
    native.build()
    native.pass_(2)
    assert chat_items(native) == [] and count(native.runtime, "native_replies") == 1
    assert native.chat.state(native.owner, native.agent)["last_read_sequence"] == 1


def test_the_attention_card_and_chat_read_state_are_independent(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.write(native.claude_user("hi"), native.claude_assistant("Ready."))
    native.pass_()
    [item] = chat_items(native)
    context = native.owner_context()
    assert [card["id"] for card in attention_queue.queue(native.runtime, context)["items"]] == [item["id"]]
    assert attention_queue.queue(native.runtime, context, {"viewed": [item["id"]]})["items"] == []
    assert [i["id"] for i in chat_items(native)] == [item["id"]]            # acknowledged in attention, still unread in Chat
    native.chat.mark_read(native.owner, native.agent, 1)
    assert chat_items(native) == [] and attention_queue.queue(native.runtime, context)["items"] == []


def test_a_reply_that_arrives_while_the_owner_reads_an_older_one_stays_unread(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.write(native.claude_user("one"), native.claude_assistant("First reply."))
    native.pass_()
    assert native.chat.state(native.owner, native.agent)["newest_sequence"] == 1
    native.write(native.claude_user("two"), native.claude_assistant("Second reply."))
    native.pass_()
    native.chat.mark_read(native.owner, native.agent, 1)                    # the UI had rendered through reply 1 only
    [item] = chat_items(native)
    assert item["sequence"] == 2 and item["text"] == "Second reply."
    assert native.chat.mark_read(native.owner, native.agent, 1)["last_read_sequence"] == 1   # never moves backwards
    native.chat.mark_read(native.owner, native.agent, 2)
    assert chat_items(native) == []


def test_a_read_boundary_must_name_an_existing_completion(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.write(native.claude_user("hi"), native.claude_assistant("Hello."))
    native.pass_()
    for bad in (7, -1, True, "1", None):
        with pytest.raises(DomainError):
            native.chat.mark_read(native.owner, native.agent, bad)
    assert native.chat.mark_read(native.owner, native.agent, 0)["last_read_sequence"] == 0


def test_only_the_installation_owner_reads_or_acknowledges_chat(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.write(native.claude_user("hi"), native.claude_assistant("Hello."))
    native.pass_()
    pin = native.pin()
    for call in (lambda who: native.chat.state(who, native.agent), lambda who: native.chat.mark_read(who, native.agent, 1),
                 lambda who: native.chat.send(who, native.agent, "hello there", "key-long-enough", pin)):
        for who in (native.agent, "not-a-principal"):
            with pytest.raises(DomainError):
                call(who)
    assert count(native.runtime, "native_reads") == 0 and count(native.runtime, "native_inputs") == 0


def test_a_historical_business_home_identity_is_never_a_chat_target_and_adds_no_item(tmp_path):
    # Businesses have no resident agents: a business-home identity (historical, here still unmigrated) is never a Chat target
    # of the owner, captures nothing and adds no Chat item or attention card. The registered global keeps its own Chat.
    native = Native(tmp_path)
    historical = historical_business_agent(native.instance, native.instance.business)
    with native.runtime.database.read() as uow:
        assert store.active_profile(uow.connection, historical, native.runtime.paths) is None
    with pytest.raises(DomainError):
        native.chat.state(native.owner, historical)
    native.publish()
    native.write(native.claude_user("hi"), native.claude_assistant("Global reply."))
    native.pass_()
    assert [item["agent_id"] for item in chat_items(native)] == [native.agent]
    assert native.chat.state(native.owner, native.agent)["entries"][-1]["text"] == "Global reply."


# ----- stable identity ---------------------------------------------------------------------------------------------

def test_resuming_the_same_conversation_in_a_new_process_keeps_every_identity(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.write(native.claude_user("one"), native.claude_assistant("First.", message_id="m-1", uuid_="u-1"))
    native.pass_()
    first = rows(native.runtime, "SELECT id,completion_sequence FROM native_replies")
    first_receipt = rows(native.runtime, "SELECT receipt_sequence FROM native_captures")[0]["receipt_sequence"]
    entries = native.chat.state(native.owner, native.agent)["entries"]

    native.end()
    native.pass_(2)
    assert rows(native.runtime, "SELECT generation,end_reason IS NOT NULL AS ended FROM native_sessions") == [{"generation": 1, "ended": 1}]
    native.new_session(same_conversation=True, pid=9001)            # `--resume`: the same conversation, a new process
    native.write(native.claude_user("two"), native.claude_assistant("Second.", message_id="m-2", uuid_="u-2"))
    native.pass_(2)

    assert rows(native.runtime, "SELECT generation,conversation_id FROM native_sessions ORDER BY generation") == [
        {"generation": 1, "conversation_id": native.session_id}, {"generation": 2, "conversation_id": native.session_id}]
    assert rows(native.runtime, "SELECT conversation_id FROM native_captures") == [{"conversation_id": native.session_id}]
    replies_now = rows(native.runtime, "SELECT id,completion_sequence FROM native_replies ORDER BY completion_sequence")
    assert replies_now[0] == first[0] and [r["completion_sequence"] for r in replies_now] == [1, 2]
    assert rows(native.runtime, "SELECT receipt_sequence FROM native_captures")[0]["receipt_sequence"] > first_receipt
    after = native.chat.state(native.owner, native.agent)["entries"]
    assert [e["id"] for e in after[:len(entries)]] == [e["id"] for e in entries]      # earlier entries keep their identity
    assert len({e["id"] for e in after}) == len(after)


def test_one_logical_reply_is_one_completion_however_often_it_is_indexed(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.write(native.claude_user("hi"), native.claude_assistant("Only once.", message_id="m-1"))
    native.pass_()
    [stored] = rows(native.runtime, "SELECT * FROM native_replies")
    with native.runtime.database.transaction() as uow:
        again = store.insert_reply(uow.connection, lineage=Lineage(native.agent, "claude", native.session_id, stored["archive_id"]),
                                   kind="native_id", key="m-1", session_id=None, receipt_id=stored["receipt_id"], offset=0, length=5,
                                   completed_at="2026-10-11T00:00:00Z", preview="Only once.")
    assert again is None and rows(native.runtime, "SELECT id,completion_sequence FROM native_replies") == [
        {"id": stored["id"], "completion_sequence": 1}]
    assert rows(native.runtime, "SELECT completion_sequence FROM native_agent_counters") == [{"completion_sequence": 1}]
    native.pass_(2)
    assert count(native.runtime, "native_replies") == 1


def test_split_assistant_records_of_one_message_make_one_completion(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.write(native.claude_user("hi"),
                 native.claude_assistant("Part one.", message_id="msg-x", uuid_="a-1"),
                 native.claude_assistant("Part two.", message_id="msg-x", uuid_="a-2"))
    native.pass_()
    assert count(native.runtime, "native_replies") == 1


def test_internal_notices_and_sidechains_are_never_replies(tmp_path):
    native = Native(tmp_path)
    native.publish()
    side = native.claude_assistant("Subagent detail.")
    side["isSidechain"] = True
    native.write(native.claude_user("hi"), side,
                 native.claude_assistant("<task-notification>background work finished</task-notification>"))
    native.pass_()
    assert count(native.runtime, "native_replies") == 0 and chat_items(native) == []


# ----- backend lifecycle ------------------------------------------------------------------------------------------

def test_the_backend_drains_in_bounded_time_and_keeps_a_surviving_thread(tmp_path):
    native = Native(tmp_path)
    native.publish()
    gate = threading.Event()
    native.backend.run_once = lambda: gate.wait(10)
    native.backend.interval = 0.01
    native.backend.start()
    with pytest.raises(TimeoutError):
        native.backend.stop_observer(0.05)
    assert native.backend._thread is not None and native.backend._thread.is_alive()
    gate.set()
    native.backend.stop_observer(5)
    assert native.backend._thread is None


def test_switching_between_agents_and_reading_state_launch_nothing(tmp_path):
    native = Native(tmp_path)
    other = Native.__new__(Native)
    native.publish()
    native.write(native.claude_user("hi"), native.claude_assistant("Hello."))
    native.pass_()
    calls = list(native.pane.calls)
    for _ in range(3):
        native.chat.state(native.owner, native.agent)
        native.chat.history(native.owner, native.agent, before=1)
        native.chat.events(native.owner, native.agent, 0)
    assert native.pane.calls == calls and other is not None            # reads never touch a terminal
    native.end()
    state = native.chat.state(native.owner, native.agent)
    assert state["session"]["state"] == "offline" and state["session"]["can_send"] is False
    assert native.pane.calls == calls                                    # an ended session starts nothing either


# ----- archive reuse: growth and integrity -------------------------------------------------------------------------

def test_the_archive_grows_append_only_and_final_capture_closes_the_session(tmp_path):
    native = Native(tmp_path)
    native.publish()
    sizes = []
    for index in range(3):
        native.write(native.claude_user(f"q{index}"), native.claude_assistant(f"a{index}", message_id=f"m{index}"))
        native.pass_()
        sizes.append(rows(native.runtime, "SELECT captured_length,receipt_sequence FROM native_captures")[0])
    assert [s["captured_length"] for s in sizes] == sorted(s["captured_length"] for s in sizes)
    assert len({s["captured_length"] for s in sizes}) == 3 and [s["receipt_sequence"] for s in sizes] == [1, 2, 3]
    native.end()
    native.pass_(4)
    assert rows(native.runtime, "SELECT finalized,complete FROM native_captures") == [{"finalized": 1, "complete": 1}]
    assert rows(native.runtime, "SELECT final_captured_at IS NOT NULL AS done FROM native_sessions") == [{"done": 1}]
    payload = next((native.workspace / "transcripts").rglob("payload.raw"))
    assert payload.read_bytes() == native.transcript_path().read_bytes()


def test_a_damaged_archive_is_reported_and_never_served_or_replaced(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.write(native.claude_user("hi"), native.claude_assistant("Hello.", message_id="m1"))
    native.pass_()
    payload = next((native.workspace / "transcripts").rglob("payload.raw"))
    damaged = bytearray(payload.read_bytes())
    damaged[10] ^= 0x01
    payload.write_bytes(bytes(damaged))
    native.chat.reader._cache.clear()
    state = native.chat.state(native.owner, native.agent)
    assert state["entries"] == [] and state["conversation"]["error"] == "archive_integrity"
    native.write(native.claude_user("again"), native.claude_assistant("More.", message_id="m2"))
    native.pass_(2)                                                       # the backend keeps running and does not overwrite it
    assert payload.read_bytes() == bytes(damaged)                        # nothing appended to, or rewritten over, the damage
    assert count(native.runtime, "native_replies") == 1
    assert rows(native.runtime, "SELECT error_code FROM native_captures") == [{"error_code": "archive_integrity"}]


def test_chat_pages_the_archive_without_reading_the_whole_conversation_back_each_time(tmp_path):
    native = Native(tmp_path)
    native.publish()
    for index in range(30):
        native.write(native.claude_user(f"question {index}"), native.claude_assistant(f"answer {index}", message_id=f"m{index}"))
    native.pass_(3)
    page = native.chat.state(native.owner, native.agent, limit=10)
    assert [e["text"] for e in page["entries"]][-1] == "answer 29" and len(page["entries"]) == 10 and page["older"] is not None
    older = native.chat.history(native.owner, native.agent, before=page["older"], limit=10)
    assert len(older["entries"]) == 10
    assert older["entries"][-1]["position"] < page["entries"][0]["position"]
    seen = {e["id"] for e in page["entries"]} | {e["id"] for e in older["entries"]}
    assert len(seen) == 20


def test_a_second_conversation_is_kept_beside_the_first(tmp_path):
    native = Native(tmp_path)
    native.publish()
    first = native.session_id
    native.write(native.claude_user("old"), native.claude_assistant("Old reply.", message_id="old-1"))
    native.pass_()
    native.end()
    native.pass_(3)
    native.new_session()
    native.write(native.claude_user("new"), native.claude_assistant("New reply.", message_id="new-1"))
    native.pass_(2)
    state = native.chat.state(native.owner, native.agent)
    assert state["conversation"]["conversation_id"] == native.session_id and state["entries"][-1]["text"] == "New reply."
    assert {c["conversation_id"] for c in state["conversations"]} == {first, native.session_id}
    old = native.chat.state(native.owner, native.agent, conversation=first, provider="claude")
    assert old["entries"][-1]["text"] == "Old reply."
    assert [r["completion_sequence"] for r in rows(native.runtime, "SELECT completion_sequence FROM native_replies ORDER BY 1")] == [1, 2]


def test_the_archive_library_is_the_only_transcript_writer(tmp_path):
    native = Native(tmp_path)
    native.publish()
    native.write(native.claude_user("hi"), native.claude_assistant("Hello.", message_id="m1"))
    native.pass_()
    archive = TranscriptArchive(str(native.workspace / "transcripts"))
    binding = CaptureBinding(native.agent, "claude", native.session_id, 1, str(native.workspace), str(native.transcript_path()))
    receipt = archive.latest(binding)
    assert receipt is not None and receipt.record_boundary == receipt.length
    flat = [p for p in (native.workspace / "transcripts").glob("*.jsonl")]
    assert flat == []                                                       # no second, flat copy of the transcript
    json.dumps(receipt.records)
