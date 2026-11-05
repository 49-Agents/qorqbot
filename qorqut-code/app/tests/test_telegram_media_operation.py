"""``telegram_send_file`` over the real composition: authority, atomic queueing, sealed replay, dispatch, reconciliation, lifecycle.

One composed host serves the module (real storage, policy, document store, file sources, the registered Ordubashy and a second
Global agent, the real ``TelegramBridge`` and dispatcher); only the Bot API is a recording fake. Each case starts from a clean
delivery queue. The component suites keep their own parameter coverage; these are the end-to-end scenarios where authority, a
transaction and a file meet.
"""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain

from bazarlyk.composition import locations
from bazarlyk.telegram import store
from bazarlyk.telegram.api import TelegramApiError
from bazarlyk.telegram.config import TelegramConfig
from host_foundation_support import historical_business_agent, owner_global_context, set_agent_overrides
from telegram_media_operation_support import (
    OPERATION, PDF, TOPIC, CHAT_ID, accepted_inbound, agent_context, build_host, configure, counts, digest, dispatch, heal, invoke,
    media_files, messages, outbox, payloads, png, rebind, register_document, send_arguments, sql, uncertain, workspace_source,
    write_file, write_sql)
from telegram_media_source_support import set_archived, set_field


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    return build_host(tmp_path_factory.mktemp("media-operation"))


@pytest.fixture
def host(shared):
    """The shared host with every open delivery closed, a clean fake provider and the real clock."""
    heal(shared)
    with shared.bridge.write("test.reset") as uow:
        for row in [dict(r) for r in uow.connection.execute("SELECT * FROM telegram_outbox WHERE state IN ('queued','dispatching','uncertain')")]:
            store.set_outbox(uow, store.outbox(uow, row["id"]), state="failed", error_code="test_reset")
    shared.api.uploads.clear(); shared.api.texts.clear(); shared.api.script.clear()
    shared.bridge._stop.clear()
    shared.bridge.clock = lambda: datetime.now(timezone.utc)
    shared.bridge.media.monotonic = __import__("time").monotonic
    yield shared


def data_of(result):
    assert result.status == "ok", result.error
    return plain(result.data)


def failed(result, code=None):
    assert result.status == "error", result.data
    if code is not None:
        assert result.error["code"] == code, result.error
    return result.error


def prepare_and_seal(host, args, key):
    """The engine's prepare and external phases, run as the registered Ordubashy: the snapshot is sealed, nothing is queued."""
    app, descriptor = host.runtime.application, host.runtime.registry.get(OPERATION)
    ctx = agent_context(host, host.ordubashy)
    with host.database.read() as uow:
        ctx = app.current_context(ctx, uow)
        plan = descriptor.prepare(ctx, app.registry.bind_arguments(OPERATION, args), InvocationOptions(idempotency_key=key), uow)
    descriptor.external(ctx, plan)
    return plan


def last(rows):
    return rows[-1]


@contextmanager
def trigger_off(host, name):
    """A schema trigger dropped for one statement-level change a test must make (it is restored from its own stored SQL)."""
    definition = sql(host, "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", name)[0]["sql"]
    write_sql(host, f"DROP TRIGGER {name}")
    try:
        yield
    finally:
        write_sql(host, definition)


def set_provider_id(host, message_id, provider_id):
    with trigger_off(host, "telegram_messages_identity"):
        write_sql(host, "UPDATE telegram_messages SET telegram_message_id=? WHERE id=?", provider_id, message_id)


def untouched(host, before):
    assert counts(host) == before


# ----- the end-to-end operation ----------------------------------------------------------------------------------


def test_a_workspace_file_is_queued_atomically_uploaded_once_and_replays(host):
    write_file(host, "exports/report.pdf")
    before = counts(host)
    args = send_arguments(workspace_source("exports/report.pdf"), purpose="reply", caption="Weekly report")
    data = data_of(invoke(host, args, key="flow-1"))
    assert data["state"] == "queued" and data["purpose"] == "reply" and data["from"] == "you"
    assert data["attachment"] == {"filename": "report.pdf", "send_as": "document", "size": len(PDF), "sha256": digest(PDF)}
    assert set(data) == {"message_id", "purpose", "from", "state", "created_at", "attempts", "error_code", "sent_at", "attachment"}
    assert {name: counts(host)[name] - before[name] for name in before} == {
        "telegram_messages": 1, "telegram_outbox": 1, "telegram_file_payloads": 1}
    message, delivery, payload = last(messages(host)), last(outbox(host)), last(payloads(host))
    assert (message["body"], message["purpose"], message["direction"], message["agent_id"]) == (
        "Weekly report", "reply", "outbound", host.ordubashy)
    assert message["outbox_id"] == delivery["id"] and message["id"] == data["message_id"]
    assert (delivery["source_kind"], delivery["kind"], delivery["state"], delivery["agent_id"]) == (
        "agent_file", "document", "queued", host.ordubashy)
    assert json.loads(delivery["payload_json"]) == {"version": 1, "payload_id": payload["id"]}
    assert (payload["state"], payload["message_id"], payload["outbox_id"], payload["principal_id"]) == (
        "attached", message["id"], delivery["id"], host.ordubashy)
    assert (payload["filename"], payload["send_as"], payload["size_bytes"], payload["sha256"]) == (
        "report.pdf", "document", len(PDF), digest(PDF))
    manifest = json.loads(payload["source_binding_json"])
    assert set(manifest) == {"version", "source", "destination", "caption", "reply"} and manifest["reply"] is None
    assert manifest["caption"] == "Weekly report" and manifest["source"]["type"] == "workspace"
    assert manifest["destination"] == {"chat_name": "ordubashy", "chat_id": CHAT_ID, "message_thread_id": TOPIC,
                                       "agent_id": host.ordubashy, "config_generation": delivery["config_generation"],
                                       "binding_fingerprint": delivery["binding_fingerprint"]}
    assert dispatch(host) is True
    [upload] = host.api.uploads
    assert (upload.mode, upload.filename, upload.size, upload.caption, upload.content_type, upload.thread, upload.reply_to,
            upload.chat_id) == ("document", "report.pdf", len(PDF), "Weekly report", "application/pdf", TOPIC, None, CHAT_ID)
    assert upload.data == PDF
    sent, sent_message = last(outbox(host)), last(messages(host))
    assert sent["state"] == "sent" and sent["provider_message_id"] == sent_message["telegram_message_id"]
    # The same key again is the original receipt: no new copy, message, delivery or upload.
    now = counts(host)
    assert data_of(invoke(host, args, key="flow-1"))["message_id"] == data["message_id"]
    assert counts(host) == now and len(host.api.uploads) == 1
    receipt = data_of(invoke(host, {"message_id": data["message_id"]}, name="telegram_delivery_get"))["items"][0]
    assert receipt["state"] == "sent" and receipt["attachment"] == data["attachment"]


def test_a_photo_is_sent_as_a_photo_with_the_validated_type(host):
    image = png()
    write_file(host, "downloads/chart.png", image)
    data = data_of(invoke(host, send_arguments(workspace_source("downloads/chart.png"), image, send_as="photo", purpose="update")))
    delivery, payload = last(outbox(host)), last(payloads(host))
    assert delivery["kind"] == "photo" and payload["send_as"] == "photo" and data["attachment"]["send_as"] == "photo"
    assert json.loads(payload["metadata_json"]) == {"send_as": "photo", "filename": "chart.png", "size": len(image),
                                                    "mime": "image/png", "format": "PNG", "width": 8, "height": 6}
    assert "'photo'" in sql(host, "SELECT sql FROM sqlite_master WHERE name='telegram_outbox'")[0]["sql"]
    assert dispatch(host) is True
    [upload] = host.api.uploads
    assert (upload.mode, upload.filename, upload.content_type, upload.caption, upload.data) == (
        "photo", "chart.png", "image/png", None, image)


@pytest.mark.parametrize("data, send_as, code", [
    pytest.param(b"not an image at all", "photo", "media_invalid", id="not-an-image"),   # never converted or sent as a document
    pytest.param(PDF, "document", "hash_mismatch", id="wrong-digest"),                   # the digest is of other bytes
])
def test_a_refused_file_leaves_no_message_delivery_or_snapshot_and_spends_its_key(host, data, send_as, code):
    write_file(host, "exports/refused.bin", data)
    arguments = send_arguments(workspace_source("exports/refused.bin"), data, send_as=send_as)
    if code == "hash_mismatch":
        arguments["expected_sha256"] = digest(b"other bytes")
    before, files = counts(host), media_files(host)
    key = f"refused-{code}"
    failed(invoke(host, arguments, key=key), code)
    after = counts(host)
    assert (after["telegram_messages"], after["telegram_outbox"]) == (before["telegram_messages"], before["telegram_outbox"])
    assert media_files(host) == files                          # the failed copy removed itself
    failed(invoke(host, arguments, key=key), "staging_failed")      # the key is spent, not retried
    assert host.api.uploads == []


@pytest.mark.parametrize("arguments", [
    {"caption": "bad\x00caption"}, {"caption": "x" * 1025}, {"expected_sha256": "A" * 64}, {"extra": "field"},
    {"source": {"type": "workspace", "path": "exports/../secret.pdf"}}, {"source": {"type": "workspace", "path": "other/a.pdf"}},
    {"source": {"type": "workspace", "path": "/etc/passwd"}}, {"reply_to": "not-a-uuid"}, {"purpose": "blocking_input"},
    {"send_as": "video"}])
def test_invalid_arguments_are_refused_before_any_side_effect(host, arguments):
    write_file(host, "exports/valid.pdf")
    before, files = counts(host), media_files(host)
    failed(invoke(host, {**send_arguments(workspace_source("exports/valid.pdf")), **arguments}))
    untouched(host, before)
    assert media_files(host) == files


def test_an_empty_caption_is_stored_verbatim_and_never_sent_as_a_label(host):
    write_file(host, "exports/plain.pdf")
    data = data_of(invoke(host, send_arguments(workspace_source("exports/plain.pdf"))))
    assert last(messages(host))["body"] == ""
    history = data_of(invoke(host, {}, name="telegram_history"))["items"][-1]
    assert history["message_id"] == data["message_id"] and history["text"] == "File: plain.pdf"
    assert history["attachment"] == data["attachment"]
    assert dispatch(host) is True
    assert host.api.uploads[0].caption is None            # the display label is never a provider caption
    assert last(messages(host))["body"] == ""             # and the stored message still holds the real (empty) caption


# ----- registered documents ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["asset", "data_upload", "agent_note", "content", "context_note"])
def test_a_registered_business_document_of_a_supported_kind_is_sent(host, kind):
    document = register_document(host, kind=kind, path=f"{kind}-{new_id()[:8]}.txt", data=b"body of " + kind.encode(), business_id=host.acme)
    arguments = send_arguments({"type": "document", "document_id": document.id, "business_id": host.acme}, document.data)
    assert data_of(invoke(host, arguments))["attachment"]["filename"] == document.path
    assert dispatch(host) is True and host.api.uploads[0].data == document.data


def test_a_global_note_is_sent_and_unsupported_or_unreadable_documents_are_refused(host):
    note = register_document(host, kind="agent_note", path=f"global-{new_id()[:8]}.txt", data=b"global note", business_id=None)
    assert data_of(invoke(host, send_arguments({"type": "document", "document_id": note.id, "business_id": None}, note.data)))
    journal = register_document(host, kind="journal", path=f"journal-{new_id()[:8]}.txt", data=b"private", business_id=host.acme)
    asset = register_document(host, kind="asset", path=f"asset-{new_id()[:8]}.txt", data=b"asset", business_id=host.acme)
    before = counts(host)
    failed(invoke(host, send_arguments({"type": "document", "document_id": journal.id, "business_id": host.acme}, journal.data)))
    failed(invoke(host, send_arguments({"type": "document", "document_id": asset.id, "business_id": None}, asset.data)),
           "not_found")                                               # the document's own business is not Global
    set_agent_overrides(host.instance, host.ordubashy, {"assets_get": "deny"}, [host.acme, host.beta])
    try:
        failed(invoke(host, send_arguments({"type": "document", "document_id": asset.id, "business_id": host.acme}, asset.data)),
               "policy_denied")                                       # the canonical reader's own policy decides
    finally:
        set_agent_overrides(host.instance, host.ordubashy, {}, [host.acme, host.beta])
    untouched(host, before)


# ----- replies -------------------------------------------------------------------------------------------------


def test_a_reply_to_an_own_topic_message_carries_its_provider_id(host):
    write_file(host, "exports/answer.pdf")
    inbound = accepted_inbound(host, provider_id=9101)
    data = data_of(invoke(host, send_arguments(workspace_source("exports/answer.pdf"), purpose="reply", reply_to=inbound["id"])))
    assert data["reply_to"] == inbound["id"]
    message = last(messages(host))
    assert (message["reply_to_id"], message["reply_to_telegram_id"]) == (inbound["id"], 9101)
    assert json.loads(last(payloads(host))["source_binding_json"])["reply"] == {"message_id": inbound["id"], "provider_message_id": 9101}
    assert dispatch(host) is True and host.api.uploads[0].reply_to == 9101


def test_an_unavailable_reply_target_is_refused_before_anything_is_queued(host):
    write_file(host, "exports/answer.pdf")
    with host.bridge.write("test.foreign") as uow:
        chat = host.bridge.config.chat("ordubashy")
        foreign = store.insert_message(uow, direction="inbound", chat=chat, purpose="owner", body="not mine", now=host.bridge.now(),
                                       agent_id=host.other, telegram_message_id=9201)
        elsewhere = store.insert_message(uow, direction="inbound", chat=host.bridge.config.chat("signoffs"), purpose="owner",
                                         body="another topic", now=host.bridge.now(), agent_id=host.ordubashy, telegram_message_id=9202)
        unsent = store.insert_message(uow, direction="outbound", chat=chat, purpose="update", body="not yet sent",
                                      now=host.bridge.now(), agent_id=host.ordubashy)
    before, files = counts(host), media_files(host)
    for target, code in ((foreign["id"], "not_found"), (elsewhere["id"], "not_found"), (new_id(), "not_found"),
                         (unsent["id"], "validation_error")):
        failed(invoke(host, send_arguments(workspace_source("exports/answer.pdf"), purpose="reply", reply_to=target)), code)
    untouched(host, before)
    assert media_files(host) == files


def test_a_reply_binding_that_changed_after_sealing_or_queueing_is_never_retargeted(host):
    write_file(host, "exports/rebound.pdf")
    inbound = accepted_inbound(host, provider_id=9301)
    arguments = send_arguments(workspace_source("exports/rebound.pdf"), purpose="reply", reply_to=inbound["id"])
    prepare_and_seal(host, arguments, "reply-seal")
    set_provider_id(host, inbound["id"], 9399)
    before = counts(host)
    failed(invoke(host, arguments, key="reply-seal"), "reply_changed")        # sealed, then the local binding moved: no apply
    untouched(host, before)
    set_provider_id(host, inbound["id"], 9301)
    data_of(invoke(host, arguments, key="reply-seal"))                       # restored: the very same request now completes
    set_provider_id(host, inbound["id"], 9398)
    dispatch(host)                                                           # moved again after queueing: admission refuses
    delivery = last(outbox(host))
    assert (delivery["state"], delivery["error_code"]) == ("failed", "reply_changed") and host.api.uploads == []


def test_a_message_body_that_no_longer_matches_the_frozen_caption_is_not_uploaded(host):
    write_file(host, "exports/caption.pdf")
    data_of(invoke(host, send_arguments(workspace_source("exports/caption.pdf"), caption="original")))
    write_sql(host, "UPDATE telegram_messages SET body='tampered' WHERE id=?", last(messages(host))["id"])
    dispatch(host)
    assert (last(outbox(host))["state"], last(outbox(host))["error_code"]) == ("failed", "payload_mismatch")
    assert host.api.uploads == []


# ----- identity: only the exact configured Ordubashy ---------------------------------------------------------


def test_no_other_caller_can_send_a_file(host):
    write_file(host, "exports/mine.pdf")
    arguments = send_arguments(workspace_source("exports/mine.pdf"))
    before = counts(host)
    # Another registered Global agent, even with an explicit grant: the role is necessary and it has no Telegram chat.
    set_agent_overrides(host.instance, host.other, {OPERATION: "allow"}, [host.acme, host.beta])
    try:
        failed(invoke(host, arguments, agent=host.other))
    finally:
        set_agent_overrides(host.instance, host.other, {}, [host.acme, host.beta])
    failed(invoke(host, arguments, ctx=host.instance.human()))                        # the owner is not an agent
    failed(invoke(host, arguments, ctx=owner_global_context(host.instance)))
    untouched(host, before)
    # A delegated identity of the real Ordubashy is refused by the operation's own gate.
    with host.database.read() as uow:
        delegated = replace(agent_context(host, host.ordubashy), delegation_assignment_id=new_id(), delegation_binding_id=new_id(),
                            delegation_generation=1)
        with pytest.raises(DomainError) as refused:
            host.runtime.telegram_operations.agent_chat(delegated, uow)
    assert refused.value.code == "forbidden"
    business_agent = historical_business_agent(host.instance, host.acme)
    with pytest.raises(DomainError):
        failed(invoke(host, arguments, agent=business_agent))                      # refused even before a context exists
    untouched(host, before)


def test_the_exact_bound_active_ordubashy_is_required_at_every_phase(host):
    write_file(host, "exports/bound.pdf")
    arguments = send_arguments(workspace_source("exports/bound.pdf"))
    before = counts(host)
    # The chat is bound to a different agent: the real Ordubashy has no Telegram chat any more.
    original = host.bridge.config
    rebind(host, chats=tuple(replace(chat, agent_id=host.other) if chat.name == "ordubashy" else chat for chat in original.chats))
    try:
        failed(invoke(host, arguments), "forbidden")
        failed(invoke(host, arguments, agent=host.other))
    finally:
        rebind(host, chats=original.chats)
    untouched(host, before)
    # An inactive Ordubashy cannot send, and a queued file is cancelled instead of being sent.
    data_of(invoke(host, arguments, key="bound-queued"))
    context = agent_context(host, host.ordubashy)
    write_sql(host, "UPDATE agent_profiles SET status='inactive' WHERE agent_id=?", host.ordubashy)
    try:
        with pytest.raises(DomainError) as inactive:
            agent_context(host, host.ordubashy)
        assert inactive.value.code == "inactive_agent"
        failed(invoke(host, arguments, key="bound-other", ctx=context))         # a context built earlier is not a licence
        assert dispatch(host) is False                                         # cancelled at admission, nothing claimed
    finally:
        write_sql(host, "UPDATE agent_profiles SET status='active' WHERE agent_id=?", host.ordubashy)
    delivery = last(outbox(host))
    assert (delivery["state"], delivery["error_code"]) == ("cancelled", "grant_revoked") and host.api.uploads == []


def test_a_revoked_grant_prevents_the_call_and_cancels_a_queued_file(host):
    write_file(host, "exports/granted.pdf")
    arguments = send_arguments(workspace_source("exports/granted.pdf"))
    data_of(invoke(host, arguments, key="grant-queued"))
    set_agent_overrides(host.instance, host.ordubashy, {OPERATION: "deny"}, [host.acme, host.beta])
    try:
        failed(invoke(host, arguments, key="grant-new"), "policy_denied")
        failed(invoke(host, arguments, key="grant-queued"), "policy_denied")  # a saved receipt never outlives the grant
        assert dispatch(host) is False
    finally:
        set_agent_overrides(host.instance, host.ordubashy, {}, [host.acme, host.beta])
    delivery = last(outbox(host))
    assert (delivery["state"], delivery["error_code"]) == ("cancelled", "grant_revoked") and host.api.uploads == []
    assert data_of(invoke(host, arguments, key="grant-queued"))["state"] == "queued"      # restored: the receipt is honoured again


def test_telegram_that_is_disabled_or_unready_refuses_without_touching_the_media_store(host):
    write_file(host, "exports/off.pdf")
    arguments = send_arguments(workspace_source("exports/off.pdf"))
    before, files = counts(host), media_files(host)
    enabled = host.bridge.config
    host.bridge.config = TelegramConfig()
    try:
        failed(invoke(host, arguments), "operation_unavailable")
    finally:
        host.bridge.config = enabled
    untouched(host, before)
    assert media_files(host) == files


# ----- sealed replay -----------------------------------------------------------------------------------------


def test_a_sealed_request_survives_source_edit_and_deletion_and_keeps_its_name(host):
    source = write_file(host, "exports/volatile.pdf")
    arguments = send_arguments(workspace_source("exports/volatile.pdf"), caption="sealed")
    prepare_and_seal(host, arguments, "sealed-1")
    source.write_bytes(b"edited after sealing")
    source.unlink()
    data = data_of(invoke(host, arguments, key="sealed-1"))                    # the source is gone; the sealed bytes are used
    assert data["attachment"]["filename"] == "volatile.pdf" and data["attachment"]["sha256"] == digest(PDF)
    assert dispatch(host) is True
    assert (host.api.uploads[0].data, host.api.uploads[0].filename) == (PDF, "volatile.pdf")
    # Different arguments under the spent key conflict before anything is copied or queued.
    before = counts(host)
    failed(invoke(host, {**arguments, "caption": "changed"}, key="sealed-1"), "idempotency_conflict")
    failed(invoke(host, {**arguments, "purpose": "reply"}, key="sealed-1"), "idempotency_conflict")
    untouched(host, before)


def test_a_document_keeps_its_sealed_bytes_and_name_when_its_file_name_and_path_change(host):
    document = register_document(host, kind="agent_note", path=f"before-{new_id()[:8]}.txt", data=b"sealed note", business_id=None)
    arguments = send_arguments({"type": "document", "document_id": document.id, "business_id": None}, document.data)
    prepare_and_seal(host, arguments, "doc-sealed")
    set_field(host, document, "path", f"after-{new_id()[:8]}.txt")
    set_field(host, document, "metadata_json", json.dumps({"filename": "declared-later.txt"}))
    document.file.write_bytes(b"completely different content now")
    data = data_of(invoke(host, arguments, key="doc-sealed"))
    assert data["attachment"]["filename"] == document.path and data["attachment"]["sha256"] == digest(b"sealed note")
    assert dispatch(host) is True and host.api.uploads[0].data == b"sealed note"


def test_a_failure_while_applying_rolls_back_the_message_the_delivery_and_the_attachment(host, monkeypatch):
    source = write_file(host, "exports/atomic.pdf")
    arguments = send_arguments(workspace_source("exports/atomic.pdf"))
    before = counts(host)

    def attach_fails(*args, **kwargs):
        raise DomainError("payload_mismatch", "injected failure while attaching")

    monkeypatch.setattr(host.bridge.media, "attach", attach_fails)
    failed(invoke(host, arguments, key="atomic-1"), "payload_mismatch")
    after = counts(host)
    assert (after["telegram_messages"], after["telegram_outbox"]) == (before["telegram_messages"], before["telegram_outbox"])
    assert after["telegram_file_payloads"] == before["telegram_file_payloads"] + 1
    assert last(payloads(host))["state"] == "ready" and last(payloads(host))["message_id"] is None
    monkeypatch.undo()
    source.unlink()                                                            # the retry needs only the sealed snapshot
    assert data_of(invoke(host, arguments, key="atomic-1"))["state"] == "queued"
    assert counts(host)["telegram_file_payloads"] == after["telegram_file_payloads"]
    assert last(payloads(host))["state"] == "attached"


def test_an_in_progress_copy_is_reported_busy_and_then_completes_once(host, monkeypatch):
    write_file(host, "exports/slow.pdf")
    arguments = send_arguments(workspace_source("exports/slow.pdf"))
    entered, release, results = threading.Event(), threading.Event(), []
    real_open = host.files._open

    @contextmanager
    def gated(authorized):
        with real_open(authorized) as stream:
            entered.set()
            assert release.wait(30)
            yield stream

    monkeypatch.setattr(host.files, "_open", gated)
    worker = threading.Thread(target=lambda: results.append(invoke(host, arguments, key="busy-1")))
    worker.start()
    assert entered.wait(30)
    error = failed(invoke(host, arguments, key="busy-1"), "busy")
    assert error["retryable"] is True
    release.set()
    worker.join(30)
    first = data_of(results[0])
    assert data_of(invoke(host, arguments, key="busy-1"))["message_id"] == first["message_id"]
    assert len([m for m in messages(host) if m["id"] == first["message_id"]]) == 1
    assert len([p for p in payloads(host) if p["request_key"] == "busy-1"]) == 1


def test_a_replay_still_requires_the_source_authority_it_was_issued_under(host):
    document = register_document(host, kind="agent_note", path=f"replay-{new_id()[:8]}.txt", data=b"replay note", business_id=None)
    arguments = send_arguments({"type": "document", "document_id": document.id, "business_id": None}, document.data)
    data = data_of(invoke(host, arguments, key="replay-1"))
    set_archived(host, document)
    failed(invoke(host, arguments, key="replay-1"), "source_archived")        # revoked: the saved private receipt is withheld
    set_archived(host, document, False)
    assert data_of(invoke(host, arguments, key="replay-1"))["message_id"] == data["message_id"]


# ----- revocation after queueing ------------------------------------------------------------------------------


def test_an_archived_source_cancels_the_queued_file(host):
    document = register_document(host, kind="agent_note", path=f"gone-{new_id()[:8]}.txt", data=b"soon archived", business_id=host.acme)
    data_of(invoke(host, send_arguments({"type": "document", "document_id": document.id, "business_id": host.acme}, document.data)))
    set_archived(host, document)
    assert dispatch(host) is False
    delivery = last(outbox(host))
    assert (delivery["state"], delivery["error_code"]) == ("cancelled", "source_revoked") and host.api.uploads == []
    assert last(payloads(host))["state"] == "attached"                          # kept for the 24 hour terminal retention


def test_an_archived_business_or_a_remapped_workspace_cancels_the_queued_file(host, monkeypatch):
    document = register_document(host, kind="agent_note", path=f"biz-{new_id()[:8]}.txt", data=b"business note", business_id=host.beta)
    data_of(invoke(host, send_arguments({"type": "document", "document_id": document.id, "business_id": host.beta}, document.data)))
    write_sql(host, "UPDATE businesses SET archived_at='2026-01-01T00:00:00Z' WHERE id=?", host.beta)
    try:
        assert dispatch(host) is False
    finally:
        write_sql(host, "UPDATE businesses SET archived_at=NULL WHERE id=?", host.beta)
    assert last(outbox(host))["state"] == "cancelled"
    write_file(host, "exports/mapped.pdf")
    data_of(invoke(host, send_arguments(workspace_source("exports/mapped.pdf"))))
    moved = host.paths.operations / "agents" / "elsewhere"
    monkeypatch.setattr(locations, "agent_workspace", lambda paths, agent_id: moved)
    assert dispatch(host) is False
    assert (last(outbox(host))["state"], last(outbox(host))["error_code"]) == ("cancelled", "source_revoked")
    assert host.api.uploads == []


def test_a_rebound_destination_is_never_retargeted(host):
    write_file(host, "exports/topic.pdf")
    arguments = send_arguments(workspace_source("exports/topic.pdf"))
    prepare_and_seal(host, arguments, "topic-sealed")
    data_of(invoke(host, arguments, key="topic-queued"))
    original = host.bridge.config
    rebind(host, chats=tuple(replace(chat, message_thread_id=TOPIC + 5) if chat.name == "ordubashy" else chat for chat in original.chats))
    try:
        before = counts(host)
        failed(invoke(host, arguments, key="topic-sealed"), "binding_changed")   # sealed for the old topic, never moved
        untouched(host, before)
        dispatch(host)                                                           # the queued one fails closed as it always did
        assert (last(outbox(host))["state"], last(outbox(host))["error_code"]) == ("failed", "binding_changed")
        assert host.api.uploads == []
        assert data_of(invoke(host, arguments, key="topic-new"))["state"] == "queued"       # a fresh key follows the new binding
        assert json.loads(last(payloads(host))["source_binding_json"])["destination"]["message_thread_id"] == TOPIC + 5
    finally:
        rebind(host, chats=original.chats)


# ----- the sealed bytes ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_a_damaged_or_missing_snapshot_fails_before_any_request_and_is_never_rebuilt(host, damage):
    source = write_file(host, f"exports/{damage}.pdf")
    data_of(invoke(host, send_arguments(workspace_source(f"exports/{damage}.pdf"))))
    snapshot = host.state / "outbound-media" / (last(payloads(host))["storage_name"] + ".media")
    if damage == "corrupt":
        content = bytearray(snapshot.read_bytes())
        content[0] ^= 0xFF
        snapshot.write_bytes(bytes(content))
    else:
        snapshot.unlink()
    source.write_bytes(PDF)                                                      # the original is still there and intact
    assert dispatch(host) is True
    delivery = last(outbox(host))
    assert (delivery["state"], delivery["error_code"]) == ("failed", "media_unavailable") and host.api.uploads == []
    assert snapshot.exists() is (damage == "corrupt")                            # nothing was rebuilt from the source
    if damage == "corrupt":
        assert snapshot.read_bytes()[0] == PDF[0] ^ 0xFF


def test_a_loader_that_finds_authority_gone_requeues_and_the_next_admission_cancels(host):
    document = register_document(host, kind="agent_note", path=f"late-{new_id()[:8]}.txt", data=b"late revocation", business_id=host.acme)
    data_of(invoke(host, send_arguments({"type": "document", "document_id": document.id, "business_id": host.acme}, document.data)))
    real_open = host.bridge.media.open_payload

    @contextmanager
    def revoked_while_opening(payload_id):
        with real_open(payload_id) as opened:
            set_archived(host, document)                                           # lost between the claim and the upload
            yield opened

    host.bridge.media.open_payload = revoked_while_opening
    try:
        assert dispatch(host) is True
    finally:
        del host.bridge.media.open_payload
    assert host.api.uploads == [] and last(outbox(host))["state"] == "queued"
    assert dispatch(host) is False
    assert (last(outbox(host))["state"], last(outbox(host))["error_code"]) == ("cancelled", "source_revoked")


# ----- the Telegram binding at the last moment before the stream is handed over -----------------------------------


def queue_one(host, name="exports/late.pdf", **extra):
    write_file(host, name)
    data_of(invoke(host, send_arguments(workspace_source(name), **extra)))
    return last(outbox(host))


def change_after_verification(host, monkeypatch, change):
    """Run ``change`` once, at the boundary after the sealed bytes were really verified and before the stream is yielded."""
    real_open, fired = host.bridge.media.open_payload, []

    @contextmanager
    def changed(payload_id):
        with real_open(payload_id) as opened:
            if not fired:
                fired.append(True)
                change()
            yield opened

    monkeypatch.setattr(host.bridge.media, "open_payload", changed)


def disabled(host):
    host.bridge.config = replace(host.bridge.config, enabled=False)


def owner_inactive(host):
    write_sql(host, "UPDATE principals SET active=0 WHERE id=?", host.bridge.config.owner_principal_id)


def owner_roundtrip(host):
    original = host.bridge.config.owner_telegram_user_id
    rebind(host, owner_telegram_user_id=original + 1)
    rebind(host, owner_telegram_user_id=original)


def other_owner_pin(host):
    host.bridge.config = replace(host.bridge.config, owner_principal_id=new_id())


def other_installation_pin(host):
    host.bridge.config = replace(host.bridge.config, installation_id=new_id())


def other_bot_unadopted(host):
    host.bridge.config = replace(host.bridge.config, bot_id=host.bridge.config.bot_id + 1)


def other_topic_unadopted(host):
    chats = tuple(replace(chat, message_thread_id=TOPIC + 40) if chat.name == "ordubashy" else chat for chat in host.bridge.config.chats)
    host.bridge.config = replace(host.bridge.config, chats=chats)


def restore_owner(host):
    write_sql(host, "UPDATE principals SET active=1 WHERE id=?", host.bridge.config.owner_principal_id)


LATE_BINDING = [
    # (mutation, restore, error code raised to the control pass, health component that records it, resumable once restored)
    pytest.param(disabled, "config", "operation_unavailable", "worker", True, id="disabled"),
    pytest.param(owner_inactive, "owner", "binding_invalid", "startup", True, id="owner-inactive"),
    pytest.param(other_owner_pin, "config", "owner_binding_mismatch", "binding", True, id="owner-pin"),
    pytest.param(other_installation_pin, "config", "installation_binding_mismatch", "binding", True, id="installation-pin"),
    pytest.param(other_bot_unadopted, "config", "binding_changed", "binding", True, id="bot-unadopted"),
    pytest.param(other_topic_unadopted, "config", "binding_changed", "binding", True, id="topic-unadopted"),
    pytest.param(owner_roundtrip, None, "binding_changed", "binding", False, id="generation-roundtrip"),
]


@pytest.mark.parametrize("mutate, restore, code, component, resumable", LATE_BINDING)
def test_a_binding_that_moves_while_the_snapshot_is_verified_never_uploads_and_fails_closed(
        host, monkeypatch, mutate, restore, code, component, resumable):
    delivery = queue_one(host)
    original = host.bridge.config
    change_after_verification(host, monkeypatch, lambda: mutate(host))
    try:
        host.bridge.cycle()                                  # the real control pass: dispatcher step, fail-closed handling
        assert host.api.uploads == []                        # no request was made: nothing is uncertain
        queued = last(outbox(host))
        assert (queued["id"], queued["state"], queued["error_code"]) == (delivery["id"], "queued", code)
        assert queued["attempts"] == 1 and queued["provider_message_id"] is None
        assert last(messages(host))["telegram_message_id"] is None and last(payloads(host))["state"] == "attached"
        assert host.bridge.health()[component] == code
        if code != "operation_unavailable":
            assert host.bridge._ready.is_set() is False      # every effect stops until the identity verifies again
        with pytest.raises(DomainError) as stopped:
            dispatch(host)                                   # and stays stopped while the change stands
        assert stopped.value.code == "operation_unavailable" and host.api.uploads == []
    finally:
        monkeypatch.undo()
        host.bridge.config = original
        if restore == "owner":
            restore_owner(host)
    heal(host)
    assert dispatch(host) is resumable
    if resumable:
        # The same generation and fingerprint came back unchanged: the very same frozen send is made, once.
        assert [upload.data for upload in host.api.uploads] == [PDF] and last(outbox(host))["state"] == "sent"
    else:
        # A new generation was adopted and restored: the destination looks as before, but the old send never resumes.
        assert host.api.uploads == []
        closed = last(outbox(host))
        assert (closed["state"], closed["error_code"]) == ("failed", "binding_changed")
        assert host.bridge.config.destination(delivery["chat_id"], delivery["message_thread_id"]).name == "ordubashy"
        assert host.bridge.config.binding_fingerprint() == delivery["binding_fingerprint"]
        with host.database.read() as uow:
            assert store.get_state(uow)["config_generation"] == delivery["config_generation"] + 2


def test_a_late_binding_change_is_not_reported_as_an_attachment_integrity_failure(host, monkeypatch):
    delivery = queue_one(host)
    change_after_verification(host, monkeypatch, lambda: owner_roundtrip(host))
    with pytest.raises(DomainError) as moved:
        dispatch(host)
    assert moved.value.code == "binding_changed"             # the bridge's own refusal, not media_unavailable
    monkeypatch.undo()
    heal(host)
    assert dispatch(host) is False
    closed = last(outbox(host))
    assert (closed["id"], closed["state"], closed["error_code"]) == (delivery["id"], "failed", "binding_changed")
    assert closed["error_code"] != "media_unavailable" and host.api.uploads == []


def test_stable_authority_at_the_final_check_sends_exactly_once_through_the_control_pass(host, monkeypatch):
    delivery = queue_one(host)
    seen = []
    change_after_verification(host, monkeypatch, lambda: seen.append("verified"))
    host.bridge.cycle()
    assert seen == ["verified"] and host.bridge._ready.is_set() and "binding" not in host.bridge.health()
    [upload] = host.api.uploads
    assert upload.data == PDF
    sent = last(outbox(host))
    assert (sent["id"], sent["state"], sent["attempts"]) == (delivery["id"], "sent", 1)
    host.bridge.cycle()
    assert len(host.api.uploads) == 1


# ----- uncertainty and reconciliation ----------------------------------------------------------------------


def owner_resolve(host, outbox_id, action, **extra):
    owner = owner_global_context(host.instance)
    with host.database.read() as uow:
        revision = uow.repository("telegram_outbox").get(outbox_id)["revision"]
    return invoke(host, {"outbox_id": outbox_id, "action": action, "expected_revision": revision, **extra},
                  name="telegram_delivery_resolve", ctx=owner)


def test_an_uncertain_upload_is_never_resent_automatically_and_the_owner_reconciles_the_saved_bytes(host):
    write_file(host, "exports/uncertain.pdf")
    inbound = accepted_inbound(host, provider_id=9501)
    data_of(invoke(host, send_arguments(workspace_source("exports/uncertain.pdf"), purpose="reply", reply_to=inbound["id"],
                                        caption="maybe sent")))
    host.api.script.append(uncertain())
    assert dispatch(host) is True
    delivery = last(outbox(host))
    assert (delivery["state"], delivery["error_code"]) == ("uncertain", "provider_uncertain")
    assert dispatch(host) is False and len(host.api.uploads) == 1                  # no automatic resend
    status = host.bridge.status()
    entry = next(item for item in status["uncertain_sends"] if item["outbox_id"] == delivery["id"])
    assert entry["attachment"] == {"filename": "uncertain.pdf", "send_as": "document", "size": len(PDF), "sha256": digest(PDF)}
    assert status["media"]["payloads"]["attached"] >= 1 and last(payloads(host))["error_code"] is None    # this snapshot is intact
    # Retention never removes the bytes of an uncertain delivery, however old.
    host.bridge.clock = lambda: datetime.now(timezone.utc) + timedelta(days=3)
    host.bridge.media.sweep()
    assert (host.state / "outbound-media" / (last(payloads(host))["storage_name"] + ".media")).exists()
    host.bridge.clock = lambda: datetime.now(timezone.utc)
    resolved = data_of(owner_resolve(host, delivery["id"], "retry"))
    assert resolved["state"] == "queued" and resolved["attachment"]["sha256"] == digest(PDF)
    assert dispatch(host) is True and len(host.api.uploads) == 2
    first, second = host.api.uploads
    assert (second.data, second.filename, second.caption, second.reply_to, second.thread) == (
        first.data, first.filename, first.caption, first.reply_to, first.thread)
    assert last(outbox(host))["state"] == "sent"
    description = host.runtime.registry.get("telegram_delivery_resolve").description
    assert "can duplicate a send" in description


def test_the_owner_can_mark_an_uncertain_file_delivered_without_a_second_upload(host):
    write_file(host, "exports/confirmed.pdf")
    data_of(invoke(host, send_arguments(workspace_source("exports/confirmed.pdf"))))
    host.api.script.append(uncertain())
    dispatch(host)
    delivery = last(outbox(host))
    resolved = data_of(owner_resolve(host, delivery["id"], "mark_delivered", provider_message_id=7777))
    assert resolved["state"] == "sent" and resolved["provider_message_id"] == 7777
    assert dispatch(host) is False and len(host.api.uploads) == 1
    assert last(messages(host))["telegram_message_id"] == 7777


def test_a_provider_refusal_is_final_and_a_failed_file_is_not_retried(host):
    write_file(host, "exports/refused.pdf")
    data_of(invoke(host, send_arguments(workspace_source("exports/refused.pdf"))))
    host.api.script.append(TelegramApiError("refused", "Bad Request: wrong file", error_code=400))
    assert dispatch(host) is True
    delivery = last(outbox(host))
    assert (delivery["state"], delivery["error_code"]) == ("failed", "refused_400")
    assert dispatch(host) is False and len(host.api.uploads) == 1


# ----- history, excerpts and privacy ------------------------------------------------------------------------


def test_a_reply_to_a_captionless_file_reads_with_its_label_and_nothing_private_is_projected(host):
    write_file(host, "exports/label.pdf")
    data = data_of(invoke(host, send_arguments(workspace_source("exports/label.pdf"))))
    assert dispatch(host) is True
    sent = last(messages(host))
    with host.database.read() as uow:
        body = host.bridge.inbound._body({"reply": {"message_id": sent["telegram_message_id"]}}, "thanks", dict(sent), None, False,
                                         {"id": "inbound-1"}, uow)
    assert f'[In reply to your earlier message {data["message_id"]}: "File: label.pdf"]' in body
    captioned = data_of(invoke(host, send_arguments(workspace_source("exports/label.pdf"), caption="  the words  "), key="cap-label"))
    assert dispatch(host) is True
    with host.database.read() as uow:
        text = host.bridge.inbound._excerpt(dict(last(messages(host))), uow)
    assert text == "the words" and captioned["attachment"]["filename"] == "label.pdf"
    projected = json.dumps([
        plain(invoke(host, {}, name="telegram_history").data), plain(invoke(host, {}, name="telegram_delivery_get").data),
        plain(invoke(host, {"message_id": data["message_id"]}, name="telegram_delivery_get").data), host.bridge.status(), data,
        plain(invoke(host, {"outbox_id": last(outbox(host))["id"]}, name="telegram_delivery_get",
                     ctx=owner_global_context(host.instance)).data)], default=str)
    secrets = [str(host.workspace), str(host.paths.operations), str(host.state), "workspace_root", "source_binding", "storage_name",
               "outbound-media", ".media", *[row["id"] for row in payloads(host)], *[row["storage_name"] for row in payloads(host)]]
    assert [item for item in secrets if item in projected] == []
    tools = json.dumps([plain(invoke(host, {}, name="telegram_history").data), data], default=str)
    assert "binding_fingerprint" not in tools and "config_generation" not in tools
    for row in payloads(host):
        assert "attachment" not in row and json.loads(row["source_binding_json"])["version"] == 1       # private, and only here


# ----- lifecycle -----------------------------------------------------------------------------------------------


def test_media_upkeep_runs_with_telegram_disabled_and_status_still_answers(host):
    write_file(host, "exports/orphan.pdf")
    prepare_and_seal(host, send_arguments(workspace_source("exports/orphan.pdf")), "lifecycle-1")     # sealed, never queued
    orphan = next(row for row in payloads(host) if row["request_key"] == "lifecycle-1")
    snapshot = host.state / "outbound-media" / (orphan["storage_name"] + ".media")
    assert snapshot.exists() and orphan["state"] == "ready"
    write_file(host, "exports/waiting.pdf")
    data_of(invoke(host, send_arguments(workspace_source("exports/waiting.pdf")), key="lifecycle-2"))      # queued, not yet sent
    waiting = next(row for row in payloads(host) if row["request_key"] == "lifecycle-2")
    kept = host.state / "outbound-media" / (waiting["storage_name"] + ".media")
    host.bridge.clock = lambda: datetime.now(timezone.utc) + timedelta(days=2)
    enabled = host.bridge.config
    host.bridge._ready.clear()
    try:
        host.bridge.start()                      # no configuration file: the integration is disabled and nothing is verified
        assert host.bridge.config.enabled is False and host.bridge._ready.is_set() is False
        assert not snapshot.exists() and host.bridge.health().get("media") is None
        row = next(row for row in payloads(host) if row["request_key"] == "lifecycle-1")
        assert row["state"] == "deleted" and row["cleanup_reason"] == "expired"
        status = host.bridge.status()
        assert status["enabled"] is False and status["media"]["payloads"]["deleted"] >= 1
        assert kept.exists() and next(row for row in payloads(host) if row["id"] == waiting["id"])["state"] == "attached"
        assert host.bridge._threads["media"].is_alive()        # a queued file still needs upkeep, so the watcher runs
    finally:
        host.bridge.stop()
        host.bridge.config = enabled
        host.bridge._ready.set()
        host.bridge.clock = lambda: datetime.now(timezone.utc)


def test_a_media_upkeep_failure_is_visible_and_does_not_stop_text_delivery(host):
    text = invoke(host, {"text": "still working", "purpose": "update"}, name="telegram_send")
    assert text.status == "ok", text.error

    def broken():
        raise OSError("disk trouble")

    host.bridge.media.sweep, host.bridge.media.recover = broken, broken
    host.bridge._media_swept, host.bridge._media_recovered = None, False
    try:
        host.bridge._housekeeping()
        assert host.bridge.health()["media"] == "storage_error" and host.bridge.status()["health"]["media"] == "storage_error"
        host.bridge.cycle()                      # the whole control pass: the queued text is delivered despite the media trouble
        assert [item.text for item in host.api.texts] == ["still working"]
    finally:
        del host.bridge.media.sweep, host.bridge.media.recover
    host.bridge._media_swept = None
    host.bridge._housekeeping()
    assert "media" not in host.bridge.health()


def test_retention_keeps_open_deliveries_and_frees_terminal_ones_after_a_day(host):
    write_file(host, "exports/retained.pdf")
    data_of(invoke(host, send_arguments(workspace_source("exports/retained.pdf"))))
    dispatch(host)
    payload = last(payloads(host))
    snapshot = host.state / "outbound-media" / (payload["storage_name"] + ".media")
    host.bridge.media.sweep()
    assert snapshot.exists()                                                       # sent just now: still within its 24 hours
    host.bridge.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=25)
    result = host.bridge.media.sweep()
    assert result["delivered"] >= 1 and not snapshot.exists()
    assert next(row for row in payloads(host) if row["id"] == payload["id"])["state"] == "deleted"
    history = data_of(invoke(host, {}, name="telegram_history"))["items"]
    assert any(item.get("attachment", {}).get("sha256") == digest(PDF) for item in history)       # the facts outlive the bytes


def test_a_new_process_over_the_same_store_sends_the_queued_file_and_honours_the_receipt(tmp_path):
    from bazarlyk.service.runtime import build_runtime
    from telegram_media_operation_support import FakeApi

    first = build_host(tmp_path)
    write_file(first, "exports/restart.pdf")
    arguments = send_arguments(workspace_source("exports/restart.pdf"), caption="across a restart")
    queued = data_of(invoke(first, arguments, key="restart-1"))
    first.runtime.release_instance()                                             # the old process is gone
    runtime = build_runtime(first.instance.workspace, paths=first.paths)         # a new one reopens the same database and state
    runtime.own_instance()
    runtime.freeze_registry()
    second = SimpleNamespace(**{**vars(first), "runtime": runtime, "database": runtime.database, "bridge": runtime.telegram,
                                "files": runtime.telegram_operations.files, "api": FakeApi()})
    second.bridge.state_dir_override = first.state
    configure(second)
    second.bridge.sweep_media()                                                  # the startup recovery and sweep
    assert second.bridge.health().get("media") is None
    assert dispatch(second) is True
    [upload] = second.api.uploads
    assert (upload.data, upload.filename, upload.caption) == (PDF, "restart.pdf", "across a restart")
    assert last(outbox(second))["state"] == "sent"
    before = counts(second)
    again = invoke(second, arguments, key="restart-1")
    assert data_of(again)["message_id"] == queued["message_id"]            # the saved receipt, as first issued
    assert counts(second) == before and len(second.api.uploads) == 1
