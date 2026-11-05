"""Successful outgoing agent messages in derived Chat.

A native conversation shows each successful ``agents_message_send`` result as a text-free notice that names the stored messages and
their distinct recipients. The archive is the only source of the receipts; the stored ``agent_messages`` rows decide which of them
are this agent's own messages to agents. Nothing is delivered, stored or acknowledged by reading them.

Synthetic agents, messages and transcripts only."""

import json
import uuid

import pytest

from bazarlyk.native_chat import store
from host_foundation_support import create_worker, historical_business_agent
from native_chat_records import claude_result, claude_results, codex_send_call, send_envelope, text_content
from native_chat_support import Native, now, rows

BASE_KEYS = {"id", "kind", "speaker", "text", "timestamp", "position", "partial", "identity", "system", "completion_sequence",
             "completion_id"}
OPERATIONS = {"icon": "brain"}
BODY = "Private body of a synthetic message"


def prepared(tmp_path, provider="claude"):
    native = Native(tmp_path, provider)
    if provider == "codex":
        native.transport, native.thread_owner = "codex_queue", "owner-1"
    native.publish()
    if provider == "codex":
        native.write(native.codex_session_meta())
    return native


def worker(native, name, **presentation):
    identity = create_worker(native.instance, name)
    if presentation:
        with native.runtime.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.presentation',json(?)) "
                                   "WHERE agent_id=?", (json.dumps(presentation), identity))
    return identity


def stored(native, recipient_id, *, sender=None, routed_to=None):
    """One original message row, as the send operation writes it: original participants plus current routing participants."""
    identity, sender = str(uuid.uuid4()), sender or native.agent
    with native.runtime.database.transaction() as uow:
        event = uow.connection.execute(
            "INSERT INTO inbox_events(business_id,source_type,source_id,actor_id,event_type,summary,created_at) VALUES(?,?,?,?,?,?,?)",
            (None, "agent_messages", identity, sender, "direct_message", "Synthetic", now())).lastrowid
        uow.connection.execute(
            "INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,thread_id,body,created_at,current_sender_id,"
            "current_recipient_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (identity, event, None, sender, recipient_id, identity, BODY, now(), sender, routed_to or recipient_id))
    return identity


def sends(native, *message_ids):
    """One native record holding one successful send result per message, in the running provider's own shape."""
    texts = [send_envelope(identity, "recipient-0001") for identity in message_ids]
    if native.provider == "claude":
        return claude_results(native.session_id, *(claude_result(text_content(text)) for text in texts),
                              uuid_=str(uuid.uuid4()), cwd=str(native.workspace))
    return codex_send_call(*texts, thread=native.session_id, item_id="call_" + uuid.uuid4().hex)


def notices(native, **options):
    return [e for e in native.chat.state(native.owner, native.agent, **options)["entries"] if e["speaker"] == "notice"]


def counters(native):
    (row,) = rows(native.runtime, "SELECT completion_sequence,work_sequence,user_sequence FROM native_agent_counters WHERE agent_id=?",
                  (native.agent,))
    return row


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_each_successful_send_is_a_text_free_notice_naming_its_distinct_recipients_in_receipt_order(tmp_path, provider):
    native = prepared(tmp_path, provider)
    atlas, scout = worker(native, "atlas", **OPERATIONS), worker(native, "scout")
    first, second, third = stored(native, atlas), stored(native, scout), stored(native, atlas)
    native.client_user_turn("Tell the team")
    native.write(sends(native, first, second, first), sends(native, third))
    native.pass_()

    state = native.chat.state(native.owner, native.agent)
    assert [e["speaker"] for e in state["entries"]] == ["user", "notice", "notice"]
    user, one, two = state["entries"]
    assert set(user) == BASE_KEYS and "agent_message" not in user                    # every other entry keeps its shape
    assert [set(e) for e in (one, two)] == [BASE_KEYS | {"agent_message"}] * 2
    assert (one["text"], one["system"], one["completion_sequence"], one["completion_id"], one["partial"]) == ("", None, None, None, None)
    scout_view = {"agent_id": scout, "display_name": "Scout", "role": {"id": "global-worker"}, "presentation": {"icon": "bot"}}   # the created default
    atlas_view = {"agent_id": atlas, "display_name": "Atlas", "role": {"id": "global-worker"}, "presentation": OPERATIONS}
    assert one["agent_message"] == {"message_ids": [first, second], "recipients": [atlas_view, scout_view]}
    assert two["agent_message"] == {"message_ids": [third], "recipients": [atlas_view]}
    assert user["position"] < one["position"] < two["position"] and len({e["id"] for e in state["entries"]}) == 3
    assert BODY not in json.dumps(state) and "recipient-0001" not in json.dumps(state)      # no body, no raw receipt


def test_only_this_agents_original_messages_to_agents_are_shown_with_the_original_recipient(tmp_path):
    native = prepared(tmp_path)
    atlas, scout = worker(native, "atlas", **OPERATIONS), worker(native, "scout")
    retired = historical_business_agent(native.instance, native.instance.business, status="inactive")
    unlisted = str(uuid.uuid4())
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("INSERT INTO principals(id,kind,display_name,active,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                               (unlisted, "agent", "Unlisted Bot", 0, now(), now()))
    to_owner, from_other = stored(native, native.owner), stored(native, atlas, sender=scout)
    rerouted, absent = stored(native, atlas, routed_to=scout), str(uuid.uuid4())
    to_retired, to_unlisted = stored(native, retired), stored(native, unlisted)

    native.client_user_turn("Tell them")
    native.write(sends(native, to_owner), sends(native, from_other), sends(native, to_owner, rerouted, absent, from_other),
                 sends(native, to_retired), sends(native, to_unlisted))
    native.pass_()

    shown = [e["agent_message"] for e in notices(native)]
    assert shown[0] == {"message_ids": [rerouted], "recipients": [
        {"agent_id": atlas, "display_name": "Atlas", "role": {"id": "global-worker"}, "presentation": OPERATIONS}]}
    # A retired profile still presents; a recipient with no profile at all falls back to its principal's display name.
    assert [(m["message_ids"], [r["agent_id"] for r in m["recipients"]]) for m in shown[1:]] == [([to_retired], [retired]),
                                                                                                 ([to_unlisted], [unlisted])]
    assert shown[1]["recipients"][0]["display_name"] == "Onbashy" and shown[1]["recipients"][0]["role"] == {"id": "business-agent"}
    assert shown[2]["recipients"] == [{"agent_id": unlisted, "display_name": "Unlisted Bot"}]
    assert len(shown) == 3                                                           # nothing else produced a notice


def test_the_stored_message_lookup_is_chunked_and_returns_only_recipient_presentation(tmp_path):
    native = prepared(tmp_path)
    atlas = worker(native, "atlas", **OPERATIONS)
    real = [stored(native, atlas) for _ in range(3)]
    candidates = [str(uuid.uuid4()) for _ in range(store.LOOKUP_CHUNK + 20)] + real + ["", real[0]]
    with native.runtime.database.read() as uow:
        found = store.sent_messages(uow.connection, native.agent, candidates)
        none = store.sent_messages(uow.connection, atlas, candidates)
    assert sorted(found) == sorted(real) and none == {}
    assert set(found[real[0]]) == {"agent_id", "display_name", "role", "presentation"}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_notice_ids_are_stable_across_pages_resume_and_restart_and_a_new_send_refreshes_the_page(tmp_path, provider):
    native = prepared(tmp_path, provider)
    atlas = worker(native, "atlas", **OPERATIONS)
    native.client_user_turn("one")
    native.write(sends(native, stored(native, atlas)))
    native.client_user_turn("two")
    native.write(sends(native, stored(native, atlas)))
    native.pass_()
    whole = native.chat.state(native.owner, native.agent)
    assert [e["speaker"] for e in whole["entries"]] == ["user", "notice", "user", "notice"]

    page = native.chat.state(native.owner, native.agent, limit=2)
    older = native.chat.history(native.owner, native.agent, before=page["older"], limit=2)
    assert older["older"] is None and older["entries"] + page["entries"] == whole["entries"]
    single = native.chat.history(native.owner, native.agent, before=whole["entries"][3]["position"], limit=1)
    assert single["entries"] == [whole["entries"][2]]

    native.end()
    native.pass_(3)
    native.new_session(same_conversation=True, pid=9001)
    native.pass_(3)
    native.build()
    native.pass_(2)
    resumed = native.chat.state(native.owner, native.agent)
    assert resumed["entries"] == whole["entries"]

    before = resumed["revision"]
    assert native.chat.events(native.owner, native.agent, before) == ([], before)
    latest_message = stored(native, atlas)
    native.write(sends(native, latest_message))
    native.pass_()
    events, latest = native.chat.events(native.owner, native.agent, before)
    assert latest > before and [e["kind"] for e in events] == ["changed"]
    after = native.chat.state(native.owner, native.agent)
    assert after["revision"] == latest and after["entries"][:4] == whole["entries"]
    assert after["entries"][4]["agent_message"]["message_ids"] == [latest_message]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_sends_are_not_replies_user_work_or_unread_and_change_no_completion_counter(tmp_path, provider):
    native = prepared(tmp_path, provider)
    atlas = worker(native, "atlas")
    native.write(sends(native, stored(native, atlas)))
    native.pass_()
    assert counters(native) == {"completion_sequence": 0, "work_sequence": 0, "user_sequence": 0}
    assert rows(native.runtime, "SELECT * FROM native_replies") == []
    page = native.chat.state(native.owner, native.agent)
    assert [e["speaker"] for e in page["entries"]] == ["notice"] and page["rendered_boundary"] == 0

    # A turn the owner started: the reply is the only completion, it is work, and the send between them changes neither.
    native.client_user_turn("Please tell Atlas")
    native.write(sends(native, stored(native, atlas)))
    if provider == "claude":
        native.write(native.claude_assistant("Told them.", message_id="msg-1"))
    else:
        native.write(native.codex_assistant("Told them.", final=True, native_id="item-1"), native.codex_event("task_complete"))
    native.pass_()
    assert counters(native) == {"completion_sequence": 1, "work_sequence": 1, "user_sequence": 0}
    (reply,) = rows(native.runtime, "SELECT origin,user_work,raw_offset FROM native_replies")
    assert (reply["origin"], reply["user_work"]) == ("user", 1)
    state = native.chat.state(native.owner, native.agent)
    assert [e["speaker"] for e in state["entries"]] == ["notice", "user", "notice", "agent"]
    assert [e["completion_sequence"] for e in state["entries"]] == [None, None, None, 1] and state["rendered_boundary"] == 1


def test_a_page_with_only_unresolvable_candidates_shows_nothing_and_keeps_its_cursor_semantics(tmp_path):
    native = prepared(tmp_path)
    native.write(sends(native, str(uuid.uuid4())), sends(native, stored(native, native.owner)))
    native.pass_()
    page = native.chat.state(native.owner, native.agent)
    assert page["entries"] == [] and page["older"] is None and page["rendered_boundary"] == 0
