"""Composed-team integration check that no single stream owned: a Chat upload and the forwarded owner message.

Everything is disposable: the synthetic native chat worker of the owner-message-forwarding suite, with no provider, model,
network, live path or native agent."""
from pathlib import Path

import pytest

from test_owner_message_forwarding_workers import chat_notices, chat_worker


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_chat_upload_alone_leaves_no_notice_and_a_message_carrying_its_reference_is_forwarded_once(tmp_path, provider):
    """The newer Chat upload stays the one attachment path: the file is saved first (no owner message, so no notice), and the
    accepted message that carries the existing reference is forwarded to Ordubashy as its ordinary complete body, exactly once."""
    native, ordubashy = chat_worker(tmp_path, provider)
    pin = native.pin()
    uploaded = native.chat.upload(native.owner, native.agent, "upload-key-1", pin, "brief.txt", b"private bytes")
    assert Path(uploaded["path"]).read_bytes() == b"private bytes"
    assert native.pane.typed() == [] and native.queued == []
    assert chat_notices(native, ordubashy) == []                                   # saving a file is not an owner message
    message = "Please review this.\n\n" + uploaded["reference"]
    native.chat.send(native.owner, native.agent, message, "message-key-1", pin)
    [notice] = chat_notices(native, ordubashy)
    assert notice["body"].endswith("\nOwner message:\n" + message) and uploaded["reference"] in notice["body"]
    assert "private bytes" not in notice["body"]                                  # the reference is forwarded, never the bytes
    native.chat.send(native.owner, native.agent, message, "message-key-1", pin)    # the exact retry adds nothing
    native.pass_()
    assert [row["id"] for row in chat_notices(native, ordubashy)] == [notice["id"]]
