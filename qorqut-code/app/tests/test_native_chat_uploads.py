"""Real attachment bytes in disposable agent workspaces; no model or live data."""
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.requests import Request

from qorqut_operations.contracts import DomainError
from bazarlyk.native_chat import uploads
from bazarlyk.native_chat import http as chat_http
from test_native_chat_chat import prepared
from native_chat_support import Native


def target(tmp_path):
    root = tmp_path / "operations"
    workspace = root / "agents" / "ordubashy"
    workspace.mkdir(parents=True)
    return root, {"agent_id": "agent-1", "workspace": str(workspace)}


def test_copy_is_binary_exact_and_retries_reuse_it_without_overwriting_a_namesake(tmp_path):
    root, profile = target(tmp_path)
    body = b"\x00\xffhello\n"
    first = uploads.save(root, profile, "owner", "binding", "upload-key-1", "Design brief.pdf", body)
    assert Path(first["path"]).read_bytes() == body
    assert Path(first["path"]).is_relative_to(Path(profile["workspace"]) / "downloads")
    assert uploads.save(root, profile, "owner", "binding", "upload-key-1", "Design brief.pdf", body) == first
    second = uploads.save(root, profile, "owner", "binding", "upload-key-2", "Design brief.pdf", b"other")
    assert second["path"] != first["path"] and Path(first["path"]).read_bytes() == body
    query = parse_qs(urlsplit(first["url"]).query)
    assert (root / query["path"][0]).read_bytes() == body
    assert str(Path(first["path"])) in first["reference"]
    with pytest.raises(DomainError) as conflict:
        uploads.save(root, profile, "owner", "binding", "upload-key-1", "Design brief.pdf", b"changed")
    assert conflict.value.code == "idempotency_conflict"


def test_downloads_symlink_cannot_redirect_a_copy(tmp_path):
    root, profile = target(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (Path(profile["workspace"]) / "downloads").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError):
        uploads.save(root, profile, "owner", "binding", "upload-key-1", "brief.txt", b"hi")
    assert list(outside.iterdir()) == []


def test_bad_names_and_oversize_uploads_are_refused_before_writing(tmp_path, monkeypatch):
    root, profile = target(tmp_path)
    for name in ("../secret", "a/b", "a\\b", "", "a\nfile"):
        with pytest.raises(DomainError):
            uploads.save(root, profile, "owner", "binding", "upload-key-1", name, b"hi")
    monkeypatch.setattr(uploads, "MAX_BYTES", 1)
    with pytest.raises(DomainError) as too_big:
        uploads.save(root, profile, "owner", "binding", "upload-key-1", "brief.txt", b"hi")
    assert too_big.value.code == "payload_too_large"
    assert not (Path(profile["workspace"]) / "downloads").exists()


def test_raw_http_upload_accepts_binary_bytes_and_returns_a_viewer_reference(tmp_path):
    native = prepared(tmp_path)
    native.pass_()
    body = b"\x00\xffraw binary"

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": "/", "query_string": b"",
        "headers": [(b"content-type", b"application/octet-stream"), (b"content-length", str(len(body)).encode()),
                    (b"x-file-name", b"draft%20brief.pdf"), (b"idempotency-key", b"http-upload-1"),
                    (b"x-chat-binding", native.pin().encode())]}, receive)
    response = asyncio.run(chat_http.route(native.runtime, request, native.owner,
                                          ["root", "agents", native.agent, "chat", "attachments"], "test"))
    result = json.loads(response.body)
    assert result["status"] == "ok" and result["data"]["name"] == "draft brief.pdf"
    assert Path(result["data"]["path"]).read_bytes() == body
    assert result["data"]["url"].startswith("/files/view?path=")


def test_thread_upload_uses_configured_ordubashy_offline_and_retries_once(tmp_path):
    native = Native(tmp_path, role="ordubashy")
    native.alive = False
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'", (native.agent,))
    assert native.chat.thread_upload_target(native.owner) == native.agent
    body = b"\x89PNG\r\n\x1a\nclipboard image"

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    def upload():
        request = Request({"type": "http", "method": "POST", "path": "/", "query_string": b"",
            "headers": [(b"x-file-name", b"image.png"), (b"idempotency-key", b"thread-paste-1")]}, receive)
        return json.loads(asyncio.run(chat_http.route(native.runtime, request, native.owner,
            ["root", "thread-attachments"], "test")).body)["data"]

    assert chat_http.matches(["root", "thread-attachments"])
    first = upload()
    assert upload() == first
    assert Path(first["path"]).is_relative_to(native.workspace / "downloads")
    assert Path(first["path"]).read_bytes() == body
    assert native.pane.typed() == [] and native.queued == []
    with pytest.raises(DomainError) as forbidden:
        native.chat.thread_upload_target(native.agent)
    assert forbidden.value.code == "forbidden"
    with native.runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE installation SET management_coordinator_id=NULL WHERE id='service'")
    with pytest.raises(DomainError) as changed:
        native.chat.upload_thread(native.owner, native.agent, "thread-paste-2", "image.png", body)
    assert changed.value.code == "revision_conflict"


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_chat_upload_saves_first_then_delivers_only_its_reference_to_the_exact_conversation(tmp_path, provider):
    native = prepared(tmp_path, provider)
    native.pass_()
    pin = native.pin()
    with pytest.raises(DomainError) as wrong:
        native.chat.upload(native.owner, native.agent, "upload-key-0", "another-binding", "brief.txt", b"private")
    assert wrong.value.code == "terminal_changed"
    assert not (native.workspace / "downloads").exists()
    uploaded = native.chat.upload(native.owner, native.agent, "upload-key-1", pin, "brief.txt", b"private bytes")
    assert Path(uploaded["path"]).read_bytes() == b"private bytes"
    assert native.pane.typed() == [] and native.queued == []
    message = "Please review this.\n\n" + uploaded["reference"]
    native.chat.send(native.owner, native.agent, message, "message-key-1", pin)
    native.pass_()
    if provider == "codex":
        assert native.queued[0][0][-1] == message
        native.client_user_turn(message)
    else:
        assert native.pane.typed() == [message]
    native.pass_()
    assert native.chat.state(native.owner, native.agent)["entries"][0]["text"] == message
    assert "private bytes" not in message
