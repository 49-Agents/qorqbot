"""Derived Chat: the facade the dashboard uses (send, state, history, events) and its owner-only HTTP surface.

Synthetic agents, panes and transcripts only; opening or reading Chat never touches a terminal."""

import asyncio
import json

import pytest
from starlette.requests import Request

from qorqut_operations.contracts import DomainError

from bazarlyk.native_chat import http as chat_http, store
from bazarlyk.native_chat.contracts import InputRequest, STATE_SUBMITTED, TYPE_BROADCAST, TYPE_DIRECT
from native_chat_support import Native, count, rows

KEY = "chat-key-000001"


def prepared(tmp_path, provider="claude"):
    native = Native(tmp_path, provider)
    if provider == "codex":
        native.transport, native.thread_owner = "codex_queue", "owner-1"
    native.publish()
    if provider == "codex":
        native.write(native.codex_session_meta())
    return native


# ----- send -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_chat_message_is_admitted_at_once_then_typed_into_the_same_conversation_and_echoed_from_the_transcript(tmp_path, provider):
    native = prepared(tmp_path, provider)
    sent = native.chat.send(native.owner, native.agent, "Please summarize", KEY, native.pin())
    assert sent["created"] is True and sent["input"]["state"] == "waiting" and sent["input"]["input_type"] == "chat" and sent["input"]["origin"] == "owner"
    assert sent["binding"]["conversation_id"] == native.session_id and sent["binding"]["generation"] == 1
    assert native.pane.typed() == [] and native.queued == []               # the request never touches the terminal
    native.pass_()
    if provider == "claude":
        assert native.pane.typed() == ["Please summarize"]
    else:
        assert native.queued[0][0][-1] == "Please summarize"
        native.client_user_turn("Please summarize")                         # the Codex client's own echo
    native.pass_()
    state = native.chat.state(native.owner, native.agent)
    assert [i["state"] for i in state["inputs"]] == ["submitted"] and state["inputs"][0]["id"] == sent["input"]["id"]
    users = [e for e in state["entries"] if e["speaker"] == "user"]
    assert [e["text"] for e in users] == ["Please summarize"]
    # Once the transcript holds the message, no second copy of its text remains in the application's records.
    assert state["inputs"][0]["body"] is None
    assert set(state["inputs"][0]) == {"id", "kind", "sequence", "origin", "input_type", "state", "reason", "body", "session_id",
                                       "binding", "idempotency_key", "evidence", "created_at", "settled_at", "transport_state"}
    assert state["inputs"][0]["binding"] == sent["binding"] and state["inputs"][0]["idempotency_key"] == KEY


def test_a_repeated_key_returns_the_original_input_even_after_the_session_ended(tmp_path):
    native = prepared(tmp_path)
    pin = native.pin()
    first = native.chat.send(native.owner, native.agent, "Once only", KEY, pin)
    native.pass_()
    native.end()
    native.pass_(2)
    again = native.chat.send(native.owner, native.agent, "Once only", KEY, pin)
    assert again["created"] is False and again["input"]["id"] == first["input"]["id"] and again["input"]["state"] == "submitted"
    assert again["binding"]["binding_id"] == pin and native.pane.typed() == ["Once only"]
    with pytest.raises(DomainError) as conflict:
        native.chat.send(native.owner, native.agent, "Something else", KEY, pin)
    assert conflict.value.code == "idempotency_conflict"


def test_offline_chat_rejects_and_stores_nothing(tmp_path):
    native = prepared(tmp_path)
    pin = native.pin()
    native.end()
    with pytest.raises(DomainError) as refused:
        native.chat.send(native.owner, native.agent, "Anyone there?", KEY, pin)
    assert refused.value.code == "terminal_offline" and count(native.runtime, "native_inputs") == 0


def test_chat_text_is_validated_before_anything_is_admitted(tmp_path):
    native = prepared(tmp_path)
    pin = native.pin()
    for text, key, binding in (("", KEY, pin), ("   ", KEY, pin), ("ok", "short", pin), ("bell\x07", KEY, pin),
                               ("x" * 20000, KEY, pin), ("ok", KEY, ""), ("ok", KEY, None)):
        with pytest.raises(DomainError):
            native.chat.send(native.owner, native.agent, text, key, binding)
    assert count(native.runtime, "native_inputs") == 0


def test_the_owners_composer_text_is_preserved_and_the_message_waits_in_order(tmp_path):
    native = prepared(tmp_path)
    native.pane.pasted = "unsent owner draft"
    one = native.chat.send(native.owner, native.agent, "First", "chat-key-000001", native.pin())
    two = native.chat.send(native.owner, native.agent, "Second", "chat-key-000002", native.pin())
    native.pass_(3)
    assert native.pane.typed() == [] and native.pane.pasted == "unsent owner draft"
    assert [i["state"] for i in native.chat.state(native.owner, native.agent)["inputs"]] == ["waiting", "waiting"]
    native.pane.pasted = None
    native.pass_()
    assert native.pane.typed() == ["First"]                                   # one head per pass, in order
    native.pass_()
    assert native.pane.typed() == ["First", "Second"]
    assert one["input"]["sequence"] < two["input"]["sequence"]


# ----- state and DTO ----------------------------------------------------------------------------------------------------

def test_the_state_dto_has_the_documented_shape(tmp_path):
    native = prepared(tmp_path)
    native.write(native.claude_user("hi"), native.claude_assistant("Hello.", message_id="m1"))
    native.pass_()
    state = native.chat.state(native.owner, native.agent)
    assert set(state) == {"agent_id", "name", "session", "revision", "cursor", "conversation", "conversations", "entries", "older",
                          "inputs", "last_read_sequence", "newest_sequence", "rendered_boundary"}
    assert set(state["session"]) == {"state", "reason", "can_send", "working", "provider", "conversation_id", "transport", "binding",
                                     "detail"}
    assert state["session"]["state"] == "live" and state["session"]["can_send"] is True
    assert state["session"]["binding"] == {"binding_id": native.pin(), "agent_id": native.agent, "provider": "claude",
                                           "conversation_id": native.session_id, "generation": 1, "transport": "terminal"}
    assert state["session"]["provider"] == "claude" and state["session"]["conversation_id"] == native.session_id
    assert set(state["entries"][0]) == {"id", "kind", "speaker", "text", "timestamp", "position", "partial", "identity", "system",
                                        "completion_sequence", "completion_id"}
    assert state["rendered_boundary"] == 1 and state["entries"][1]["completion_sequence"] == 1
    assert [e["speaker"] for e in state["entries"]] == ["user", "agent"] and "actions" not in state
    assert state["conversation"]["conversation_id"] == native.session_id and state["conversation"]["error"] is None


def test_working_comes_from_the_hook_activity_of_the_exact_session_only(tmp_path):
    native = prepared(tmp_path)
    assert native.chat.state(native.owner, native.agent)["session"]["working"] is False
    activity = native.workspace / ".bazarlyk/activity"
    activity.mkdir(parents=True, exist_ok=True)
    record = {"agent_id": native.agent, "session_id": native.session_id, "runtime": "claude-code",
              "owner": f"{native.pid}:{native.started}", "state": "running"}
    (activity / f"{native.session_id}.json").write_text(json.dumps(record))
    assert native.chat.state(native.owner, native.agent)["session"]["working"] is True
    (activity / f"{native.session_id}.json").write_text(json.dumps({**record, "session_id": "other"}))
    assert native.chat.state(native.owner, native.agent)["session"]["working"] is False


def test_the_event_stream_position_is_the_durable_revision(tmp_path):
    native = prepared(tmp_path)
    start = native.chat.state(native.owner, native.agent)["revision"]
    assert native.chat.events(native.owner, native.agent, start) == ([], start)
    native.write(native.claude_user("hi"), native.claude_assistant("Hello.", message_id="m1"))
    native.pass_()
    events, latest = native.chat.events(native.owner, native.agent, start)
    assert latest > start and [e["kind"] for e in events] == ["changed"] and events[0]["data"]["newest_sequence"] == 1
    assert native.chat.events(native.owner, native.agent, latest) == ([], latest)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_hook_only_working_changes_wake_chat_without_polling_or_new_messages(tmp_path, provider):
    native = prepared(tmp_path, provider)
    native.pass_(2)
    activity = native.workspace / ".bazarlyk/activity"
    activity.mkdir(parents=True, exist_ok=True)
    path = activity / f"{native.session_id}.json"
    record = {"agent_id": native.agent, "session_id": native.session_id, "runtime": native.runtime_name,
              "owner": native.thread_owner or f"{native.pid}:{native.started}"}
    previous = native.chat.state(native.owner, native.agent)["cursor"]
    for state in ("running", "idle"):
        path.write_text(json.dumps({**record, "state": state}))
        native.pass_()
        events, latest = native.chat.events(native.owner, native.agent, previous)
        assert latest > previous and [e["kind"] for e in events] == ["changed"]
        page = native.chat.state(native.owner, native.agent)
        assert page["session"]["working"] is (state == "running")
        assert page["inputs"] == [] and page["entries"] == []
        native.pass_()
        assert native.chat.events(native.owner, native.agent, latest) == ([], latest)
        previous = latest


# ----- HTTP -------------------------------------------------------------------------------------------------------------

def call(native, method, tail, *, query="", body=None, headers=(), principal=None, agent=None):
    raw = json.dumps(body).encode() if body is not None else b""

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {"type": "http", "method": method, "path": "/", "query_string": query.encode(), "client": ("127.0.0.1", 1),
             "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode()), *headers]}
    parts = ["root", "agents", agent or native.agent, "chat", *tail]
    assert chat_http.matches(parts)
    result = asyncio.run(chat_http.route(native.runtime, Request(scope, receive), principal or native.owner, parts, "req-1"))
    return json.loads(result.body)


def test_the_http_surface_reads_sends_and_acknowledges_for_the_owner_only(tmp_path):
    native = prepared(tmp_path)
    native.write(native.claude_user("hi"), native.claude_assistant("Hello.", message_id="m1"))
    native.pass_()
    state = call(native, "GET", [], query="limit=10")
    assert state["status"] == "ok" and state["data"]["entries"][-1]["text"] == "Hello."
    sent = call(native, "POST", ["messages"], body={"text": "Over HTTP", "idempotency_key": KEY, "expected_binding": native.pin()})
    assert sent["data"]["input"]["state"] == "waiting"
    read = call(native, "POST", ["read"], body={"sequence": 1})
    assert read["data"] == {"last_read_sequence": 1, "newest_sequence": 1}
    with pytest.raises(DomainError):                                          # an agent bearer credential is refused outright
        call(native, "GET", [], headers=[(b"authorization", b"Bearer token")])
    with pytest.raises(DomainError):
        call(native, "GET", [], principal=native.agent)
    with pytest.raises(DomainError):
        call(native, "GET", [], agent="not-a-uuid")
    with pytest.raises(DomainError):
        call(native, "POST", ["messages"], body={"text": "x", "idempotency_key": KEY, "expected_binding": native.pin(), "extra": 1})
    with pytest.raises(DomainError) as unpinned:                              # no unpinned compatibility route remains
        call(native, "POST", ["messages"], body={"text": "x", "idempotency_key": "chat-key-000002"})
    assert unpinned.value.code == "validation_error" and "expected_binding" in unpinned.value.field_errors
    with pytest.raises(DomainError):
        call(native, "GET", ["stop"])
    with pytest.raises(DomainError):
        call(native, "GET", ["history"])                                      # `before` is required


def test_the_retired_runner_endpoints_do_not_exist(tmp_path):
    native = prepared(tmp_path)
    for tail in (["stop"], ["actions", "x"], ["runtime"], ["runtime", "enable"], ["runner"]):
        for method, body in (("GET", None), ("POST", {})):
            with pytest.raises(DomainError) as missing:
                call(native, method, tail, body=body)
            assert missing.value.code in {"not_found", "validation_error"}
    assert rows(native.runtime, "SELECT COUNT(*) AS n FROM native_inputs") == [{"n": 0}]


def test_the_retired_agent_browser_is_removed_while_current_chat_still_reads(tmp_path):
    from starlette.testclient import TestClient
    from bazarlyk.service.web import create_app

    native = prepared(tmp_path)
    native.write(native.claude_user("hi"), native.claude_assistant("Hello.", message_id="m1"))
    native.pass_()
    dashboard = tmp_path / "dashboard"
    for folder in ("js", "css"):
        (dashboard / folder).mkdir(parents=True)
    app = create_app(native.runtime, origins=["http://testserver"], dashboard_root=dashboard, manage_lifecycle=False)
    with TestClient(app) as client:
        for suffix in ("", f"/{native.agent}", f"/{native.agent}/conversation?provider=claude&conversation={native.session_id}"):
            assert client.get("/api/root/agent-history" + suffix, headers={"Origin": "http://testserver"}).status_code == 404
        current = client.get(f"/api/root/agents/{native.agent}/chat", headers={"Origin": "http://testserver"})
        assert current.status_code == 200
        assert current.json()["data"]["entries"][-1]["text"] == "Hello."


# ----- the binding and selection contract -------------------------------------------------------------------------------

def current_pairs(state):
    return [(c["provider"], c["conversation_id"]) for c in state["conversations"] if c["current"]]


def test_selecting_an_archive_changes_neither_the_current_conversation_nor_the_live_binding(tmp_path):
    native = prepared(tmp_path)
    first = native.session_id
    native.write(native.claude_user("one"), native.claude_assistant("First.", message_id="m1"))
    native.pass_(2)
    native.end()
    native.pass_(3)
    native.new_session()
    second = native.session_id
    native.write(native.claude_user("two"), native.claude_assistant("Second.", message_id="m2"))
    native.pass_(2)
    default = native.chat.state(native.owner, native.agent)
    assert default["conversation"]["conversation_id"] == second and current_pairs(default) == [("claude", second)]
    archive = native.chat.state(native.owner, native.agent, conversation=first, provider="claude")
    assert archive["conversation"]["conversation_id"] == first and [e["text"] for e in archive["entries"]][-1] == "First."
    assert current_pairs(archive) == [("claude", second)]                       # asking for an archive does not make it current
    assert archive["session"] == default["session"] and archive["session"]["binding"]["conversation_id"] == second
    assert archive["session"]["binding"]["generation"] == 2 and archive["session"]["can_send"] is True


def test_the_same_native_id_under_two_providers_never_aliases_and_a_half_or_unknown_selection_is_refused(tmp_path):
    native = prepared(tmp_path)
    first = native.session_id
    native.write(native.claude_user("one"), native.claude_assistant("First.", message_id="m1"))
    native.pass_(2)
    with native.runtime.database.transaction() as uow:                         # a Codex capture that reuses the Claude id
        store.ensure_capture(uow.connection, {"agent_id": native.agent, "provider": "codex", "conversation_id": first,
                                              "transcript_path": "/synthetic/rollout.jsonl", "id": None})
    claude = native.chat.state(native.owner, native.agent, conversation=first, provider="claude")
    codex = native.chat.state(native.owner, native.agent, conversation=first, provider="codex")
    assert claude["conversation"]["provider"] == "claude" and [e["text"] for e in claude["entries"]][-1] == "First."
    assert codex["conversation"]["provider"] == "codex" and codex["entries"] == []
    assert sorted((c["provider"], c["conversation_id"]) for c in claude["conversations"]) == [("claude", first), ("codex", first)]
    assert current_pairs(claude) == current_pairs(codex) == [("claude", first)]
    for options, code in (({"conversation": first}, "validation_error"), ({"provider": "claude"}, "validation_error"),
                          ({"conversation": first, "provider": "gemini"}, "validation_error"),
                          ({"conversation": "no-such-conversation", "provider": "claude"}, "not_found")):
        with pytest.raises(DomainError) as refused:
            native.chat.state(native.owner, native.agent, **options)
        assert refused.value.code == code
        with pytest.raises(DomainError) as refused_history:
            native.chat.history(native.owner, native.agent, before=10 ** 9, **options)
        assert refused_history.value.code == code
    over_http = call(native, "GET", [], query=f"conversation={first}&provider=codex")["data"]
    assert over_http["conversation"]["provider"] == "codex" and current_pairs(over_http) == [("claude", first)]
    for query, code in ((f"conversation={first}", "validation_error"), ("provider=claude", "validation_error"),
                        (f"conversation={first}&provider=gemini", "validation_error"),
                        ("conversation=no-such-conversation&provider=codex", "not_found")):
        for tail in ([], ["history"]):
            with pytest.raises(DomainError) as refused_over_http:
                call(native, "GET", tail, query=query + ("&before=100" if tail else ""))
            assert refused_over_http.value.code == code


def test_a_state_read_registers_no_session_and_offers_send_only_for_an_observed_open_binding(tmp_path):
    native = prepared(tmp_path)
    view = native.chat.state(native.owner, native.agent)["session"]
    assert view["state"] == "live" and view["can_send"] is False and view["reason"] == "binding_not_ready" and view["binding"] is None
    assert "connecting" in view["detail"] and count(native.runtime, "native_sessions") == 0
    assert count(native.runtime, "native_captures") == 0 and count(native.runtime, "native_inputs") == 0
    native.pass_()                                                            # the normal backend observer records it
    view = native.chat.state(native.owner, native.agent)["session"]
    assert view["can_send"] is True and view["reason"] == "live" and view["binding"]["generation"] == 1
    assert view["binding"] == {"binding_id": native.pin(), "agent_id": native.agent, "provider": "claude",
                               "conversation_id": native.session_id, "generation": 1, "transport": "terminal"}
    native.new_session(same_conversation=True, pid=9001)                      # `--resume`: a new process holds the conversation
    view = native.chat.state(native.owner, native.agent)["session"]
    assert view["can_send"] is False and view["reason"] == "binding_not_ready" and view["binding"] is None
    assert count(native.runtime, "native_sessions") == 1                      # reading still created nothing
    native.pass_(2)
    view = native.chat.state(native.owner, native.agent)["session"]
    assert view["can_send"] is True and view["binding"]["generation"] == 2 and count(native.runtime, "native_sessions") == 2


def test_every_input_keeps_its_own_binding_after_the_session_ends_and_only_owner_chat_shows_its_key(tmp_path):
    native = prepared(tmp_path)
    pin = native.pin()
    sent = native.chat.send(native.owner, native.agent, "Please summarize", KEY, pin)
    native.arbiter.admit_nudge(native.agent, "nudge-private-source-key", "Look at your inbox.")
    native.arbiter.submit(InputRequest(native.agent, TYPE_DIRECT, "relay-private-source-key", "From a teammate."))
    native.pass_(2)
    native.end()
    native.pass_(3)
    native.new_session()
    native.pass_(2)
    state = native.chat.state(native.owner, native.agent)                     # a fresh read after the session was replaced
    inputs = {item["input_type"]: item for item in state["inputs"]}
    assert set(inputs) == {"chat", "inbox_nudge", "direct_message"}
    assert all(item["binding"] == sent["binding"] and item["binding"]["binding_id"] == pin for item in state["inputs"])
    assert state["session"]["binding"]["generation"] == 2 and state["session"]["binding"]["binding_id"] != pin
    assert inputs["chat"]["idempotency_key"] == KEY and inputs["chat"]["id"] == sent["input"]["id"]
    assert inputs["inbox_nudge"]["idempotency_key"] is None and inputs["direct_message"]["idempotency_key"] is None
    assert "private-source-key" not in json.dumps(state)                       # no other producer's source key is exposed
    assert [item["sequence"] for item in state["inputs"]] == sorted((item["sequence"] for item in state["inputs"]), reverse=True)


def test_state_inputs_are_the_latest_thirty_in_descending_sequence(tmp_path):
    native = prepared(tmp_path)
    native.pass_()
    with native.runtime.database.transaction() as uow:
        session = store.live_session(uow.connection, native.agent)
        for index in range(35):
            request = InputRequest(native.agent, TYPE_BROADCAST, f"flood-{index}", f"note {index}")
            store.insert_input(uow.connection, session, request, store.request_hash(request), state=STATE_SUBMITTED,
                               reason="processing_not_confirmed")
    sequences = [item["sequence"] for item in native.chat.state(native.owner, native.agent)["inputs"]]
    assert sequences == list(range(35, 5, -1))


def test_a_stale_pin_stores_and_types_nothing_and_an_exact_retry_keeps_its_original_receipt(tmp_path):
    native = prepared(tmp_path)
    old = native.pin()
    first = native.chat.send(native.owner, native.agent, "Pinned text", KEY, old)
    native.pass_()
    assert native.pane.typed() == ["Pinned text"]
    native.end()
    native.pass_(3)
    native.new_session()
    new = native.pin()
    assert new != old
    stored = count(native.runtime, "native_inputs")
    for pin in (old, "no-such-binding-id"):                                   # a NEW request for a session that is not the live one
        with pytest.raises(DomainError) as stale:
            native.chat.send(native.owner, native.agent, "Fresh text", "chat-key-000002", pin)
        assert stale.value.code == "terminal_changed"
    native.pass_(2)
    assert count(native.runtime, "native_inputs") == stored and native.pane.typed() == []
    again = native.chat.send(native.owner, native.agent, "Pinned text", KEY, old)         # the ambiguous retry, its pin unchanged
    assert again["created"] is False and again["input"]["id"] == first["input"]["id"]
    assert again["binding"]["binding_id"] == old and again["input"]["binding"]["generation"] == 1
    with pytest.raises(DomainError) as moved:                                 # the same key under another pin is another request
        native.chat.send(native.owner, native.agent, "Pinned text", KEY, new)
    assert moved.value.code == "idempotency_conflict"
    native.end()
    native.pass_(3)
    offline = native.chat.send(native.owner, native.agent, "Pinned text", KEY, old)       # and still after the session is gone
    assert offline["created"] is False and offline["input"]["id"] == first["input"]["id"]
    assert count(native.runtime, "native_inputs") == stored
