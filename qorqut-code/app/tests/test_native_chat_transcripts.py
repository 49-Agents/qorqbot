"""Authorized transcript consumers read the native archive: the agent pane's session list and historical session view, and
the learning programs' transcript source. The archive's committed, verified records are the only transcript authority."""

import json

import pytest

from qorqut_operations.contracts import DomainError

from bazarlyk.native_chat import transcripts
from bazarlyk.service.local import LocalFiles
from native_chat_support import Native


def conversation(native, texts, *, end=True):
    native.publish()
    if native.provider == "codex":
        native.write(native.codex_session_meta())
    for index, text in enumerate(texts):
        native.write(native.claude_user(f"q{index}"), native.claude_assistant(text, message_id=f"{native.session_id}-{index}"))
    native.pass_()
    if end:
        native.end()
        native.pass_(4)
    return native.session_id


def test_sessions_are_listed_newest_first_and_read_as_complete_archived_records(tmp_path):
    native = Native(tmp_path)
    local = LocalFiles(native.runtime)
    first = conversation(native, ["old reply"])
    native.new_session()
    second = conversation(native, ["new reply", "newer reply"], end=False)
    assert local.session_names(None, native.agent) == [f"{second}.jsonl", f"{first}.jsonl"]
    events = local.session_events(None, native.agent, f"{second}.jsonl")
    assert [e["message"]["content"][0]["text"] for e in events if e["type"] == "assistant"] == ["new reply", "newer reply"]
    # An unfinished trailing record is not served; it appears once the client completes it.
    with open(native.transcript_path(), "ab") as handle:
        handle.write(b'{"type":"assistant","uuid":"x"')
    native.pass_()
    assert len(local.session_events(None, native.agent, f"{second}.jsonl")) == len(events)
    raw = local.session_bytes(None, native.agent, f"{first}.jsonl")
    assert raw.endswith(b"\n") and b"old reply" in raw


def test_codex_conversations_keep_their_codex_prefixed_name(tmp_path):
    native = Native(tmp_path, "codex")
    native.transport, native.thread_owner = "codex_queue", "owner-1"
    native.publish()
    native.write(native.codex_session_meta(), native.codex_event("task_started"), native.codex_event("user_message", message="hi"),
                 native.codex_assistant("Done.", final=True, native_id="item-1"), native.codex_event("task_complete"))
    native.pass_()
    assert LocalFiles(native.runtime).session_names(None, native.agent) == [f"codex-{native.session_id}.jsonl"]


def test_only_registered_session_names_are_served(tmp_path):
    native = Native(tmp_path)
    local = LocalFiles(native.runtime)
    conversation(native, ["reply"], end=False)
    for bad in ("../x.jsonl", "a/b.jsonl", ".hidden.jsonl", "x.txt", "", None, 7):
        with pytest.raises(DomainError) as refused:
            local.session_bytes(None, native.agent, bad)
        assert refused.value.code == "validation_error"
    with pytest.raises(DomainError) as missing:
        local.session_bytes(None, native.agent, "00000000-0000-4000-8000-000000000000.jsonl")
    assert missing.value.code == "not_found"
    with pytest.raises(DomainError):
        local.session_names(native.instance.business, native.agent)        # not that business's agent


def test_a_flat_copy_in_the_transcripts_folder_is_not_a_session(tmp_path):
    native = Native(tmp_path)
    conversation(native, ["reply"], end=False)
    (native.workspace / "transcripts" / "stray.jsonl").write_text('{"type":"user","sessionId":"stray"}\n')
    local = LocalFiles(native.runtime)
    assert "stray.jsonl" not in local.session_names(None, native.agent)
    assert [s["session_id"] for s in local.iter_transcripts(None, native.agent)] == [native.session_id]


def test_the_learning_source_has_the_documented_shape_and_excludes_the_applier(tmp_path):
    native = Native(tmp_path)
    local = LocalFiles(native.runtime)
    conversation(native, ["Always keep source quotes."], end=False)
    [source] = list(local.iter_transcripts(None))
    assert set(source) == {"session_id", "source_id", "agent_id", "agent_kind", "content", "transcript_hash"}
    assert source["session_id"] == native.session_id and source["agent_id"] == native.agent
    assert source["source_id"] == f"native-archive/{native.agent}/claude/{native.session_id}"
    assert source["content"].encode() == local.session_bytes(None, native.agent, f"{native.session_id}.jsonl")
    import hashlib
    assert source["transcript_hash"] == hashlib.sha256(source["content"].encode()).hexdigest()
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE agent_profiles SET profile_json=json_set(profile_json,'$.role.id','learning-applier') WHERE agent_id=?",
                               (native.agent,))
    assert list(local.iter_transcripts(None)) == []
    with pytest.raises(DomainError) as excluded:
        list(local.iter_transcripts(None, native.agent))
    assert excluded.value.code == "forbidden"


def test_a_conversation_over_the_bound_is_refused_not_truncated(tmp_path, monkeypatch):
    native = Native(tmp_path)
    local = LocalFiles(native.runtime)
    conversation(native, ["a long reply " * 50], end=False)
    monkeypatch.setattr(transcripts, "MAX_SESSION_BYTES", 100)
    with pytest.raises(DomainError) as large:
        local.session_bytes(None, native.agent, f"{native.session_id}.jsonl")
    assert large.value.code == "payload_too_large"


def test_a_damaged_archive_is_not_served_as_a_transcript(tmp_path):
    native = Native(tmp_path)
    local = LocalFiles(native.runtime)
    conversation(native, ["reply"], end=False)
    payload = next((native.workspace / "transcripts").rglob("payload.raw"))
    data = bytearray(payload.read_bytes())
    data[5] ^= 1
    payload.write_bytes(bytes(data))
    with pytest.raises(DomainError) as damaged:
        local.session_bytes(None, native.agent, f"{native.session_id}.jsonl")
    assert damaged.value.code == "operation_unavailable" and "archive_integrity" in damaged.value.message
    json.dumps(local.session_names(None, native.agent))
