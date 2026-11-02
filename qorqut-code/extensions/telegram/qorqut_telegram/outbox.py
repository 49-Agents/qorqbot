"""Durable outbox dispatch and known-message cleanup.

Intent is committed before every external effect and the receipt after it. A failure after the request may have been
applied is *uncertain* and is never resent by the bridge; only known pre-send failures and provider refusals that state
a retry time are retried, with bounded exponential backoff. Nothing here claims exactly-once delivery.
"""

from __future__ import annotations

from contextlib import ExitStack
import logging
import re

from qorqut_operations.contracts import DomainError, new_id
from . import store
from .api import TelegramApiError, validate_upload

LOGGER = logging.getLogger(__name__)
DELETE_LIMIT_SECONDS = 48 * 3600
BATCH = 20
PAYLOAD_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
MEDIA_KINDS = ("document", "photo")


class AuthorityMoved(Exception):
    """An attachment loader found, after the claim and before any request, that the authority the send was queued under is gone.

    Nothing was sent. A dispatching send cannot be cancelled, so it goes back to ``queued`` (after the bridge's backoff when
    ``retry`` says the cause is transient) and the next admission, which judges the same authority, closes it.

    ``fail`` is the bridge's own refusal when the cause is the Telegram binding or owner pin (disabled, a moved installation or
    owner, a new generation or fingerprint): after the requeue it is raised again exactly as the dispatcher's earlier identity
    check raises it, so the control pass fails closed instead of treating the change as an unusable attachment."""

    def __init__(self, code, message="", *, retry=False, fail=None):
        super().__init__(message or code)
        self.code, self.retry, self.fail = code, retry, fail


def enqueue_text(bridge, uow, chat, *, text, purpose, source_kind, source_key, part="main", agent_id=None,
                 reply_to=None, request=None, producer_id=None, sensitive=False, next_attempt_at=None, event=None):
    """Store an outbound message and its outbox entry once per (source_kind, source_key, part).

    Returns ``(message_row, outbox_row, created)``. A repeated call returns the first rows unchanged.
    """
    existing = store.outbox_by_source(uow, source_kind, source_key, part)
    if existing is not None:
        return store.message(uow, existing["message_id"]), existing, False
    if not isinstance(text, str) or not text.strip():
        raise DomainError("validation_error", "A Telegram message needs text")
    if len(text) > store.MAX_UNICODE_CHARS or store.utf16_units(text) > store.MAX_UTF16_UNITS:
        raise DomainError("validation_error", "Telegram text exceeds 4000 characters or 4096 UTF-16 units")
    now = bridge.now()
    config = bridge.config
    # No message is queued under an identity that no longer matches the pinned installation and owner.
    bridge.check_binding(uow)
    row = store.insert_message(uow, direction="outbound", chat=chat, purpose=purpose, body=text, now=now, agent_id=agent_id,
                               reply_to_id=reply_to["id"] if reply_to else None,
                               reply_to_telegram_id=reply_to["telegram_message_id"] if reply_to else None,
                               request=request, sensitive=sensitive)
    state = store.get_state(uow)
    generation = state["config_generation"] if state else 1
    outbox, created = store.enqueue(uow, chat, generation, config.binding_fingerprint(), source_kind=source_kind,
                                    source_key=source_key, part=part, payload={"text": text}, now=now,
                                    message_id=row["id"], agent_id=agent_id, producer_id=producer_id,
                                    next_attempt_at=next_attempt_at, event=event)
    return store.message(uow, row["id"]), outbox, created


def enqueue_file(bridge, uow, chat, *, caption, purpose, send_as, payload_id, agent_id, reply_to=None):
    """Store an outbound attachment message and its ``agent_file`` outbox entry once per payload.

    The message body is the caption exactly as accepted (an empty string for a captionless file); the closed outbox envelope
    names the sealed snapshot and nothing else. Returns ``(message_row, outbox_row, created)``; a repeated call returns the first
    rows unchanged. The caller attaches the payload in the same write unit."""
    existing = store.outbox_by_source(uow, store.AGENT_FILE_SOURCE, payload_id)
    if existing is not None:
        return store.message(uow, existing["message_id"]), existing, False
    now = bridge.now()
    bridge.check_binding(uow)
    row = store.insert_message(uow, direction="outbound", chat=chat, purpose=purpose, body=caption, now=now, agent_id=agent_id,
                               reply_to_id=reply_to["id"] if reply_to else None,
                               reply_to_telegram_id=reply_to["telegram_message_id"] if reply_to else None)
    state = store.get_state(uow)
    outbox, created = store.enqueue(uow, chat, state["config_generation"] if state else 1, bridge.config.binding_fingerprint(),
                                    kind=send_as, source_kind=store.AGENT_FILE_SOURCE, source_key=payload_id,
                                    payload={"version": 1, "payload_id": payload_id}, now=now, message_id=row["id"],
                                    agent_id=agent_id)
    return store.message(uow, row["id"]), outbox, created


def enqueue_edit(bridge, uow, chat, target, text, *, source_kind, source_key, producer_id=None, generation=None, event=None):
    """Queue an update of one known, already-sent bridge message. Returns the outbox row (an existing one unchanged)."""
    existing = store.outbox_by_source(uow, source_kind, source_key, "main")
    if existing is not None:
        return existing
    if not isinstance(text, str) or not text.strip() or len(text) > store.MAX_UNICODE_CHARS or store.utf16_units(text) > store.MAX_UTF16_UNITS:
        raise DomainError("validation_error", "A Telegram message needs 1-4000 characters and at most 4096 UTF-16 units")
    bridge.check_binding(uow)
    if not store.in_destination(target, chat):
        # An edit addresses only the destination its message was sent to, which is the one currently configured for that name.
        raise DomainError("binding_changed", "The message to update is not at the currently configured destination")
    if generation is None:
        generation = (store.get_state(uow) or {}).get("config_generation", 1)
    row, _ = store.enqueue(uow, chat, generation, bridge.config.binding_fingerprint(), kind="edit", source_kind=source_kind,
                           source_key=source_key, payload={"text": text, "target_message_id": target["id"]}, now=bridge.now(),
                           message_id=target["id"], agent_id=target["agent_id"], producer_id=producer_id, event=event)
    return row


def enqueue_notice(bridge, uow, chat_name, text, source_key):
    """A bridge-owned status line in a configured chat (for example a conservative-queueing or refusal notice)."""
    chat = bridge.config.chat(chat_name)
    return enqueue_text(bridge, uow, chat, text=text[:store.MAX_UNICODE_CHARS], purpose="notice",
                        source_kind="notice", source_key=source_key)


class Dispatcher:
    def __init__(self, bridge):
        self.bridge = bridge
        self._unrecorded = []

    def run(self):
        # A provider receipt that could not be committed (maintenance, busy) is recorded first; it is never sent again.
        worked = self.flush()
        # Expired input requests are closed next so that one that timed out while queued is never sent.
        worked = self.expire_requests() or worked
        # Everything that is not an agent attachment is considered first, however many uploads are older; then at most one
        # upload, so a backlog of large files never starves the other queues and one pass stays within one upload's deadline.
        for _ in range(BATCH):
            # While the bridge is stopping, the send in flight finishes and records its outcome; no next one starts.
            if self._stopping() or not self.dispatch_one(uploads=False):
                break
            worked = True
        if not self._stopping() and self.dispatch_one(uploads=True):
            worked = True
        return worked

    def _stopping(self):
        stop = getattr(self.bridge, "_stop", None)
        return stop is not None and stop.is_set()

    # ----- claim -> send -> receipt -----------------------------------------------------------------------

    def _finish(self, uow, row, state, code, message=None, **extra):
        finished = store.set_outbox(uow, row, state=state, error_code=code, error_message=(message or "")[:500] or None, **extra)
        if state in ("failed", "cancelled") and row["message_id"]:
            # A blocking-input prompt that never reached the owner is cancelled: it must not keep treating the chat as awaiting
            # a secret, and its request key is free for a new request.
            prompt = store.message(uow, row["message_id"])
            if prompt is not None and prompt["request_state"] == "pending":
                store.close_request(uow, prompt, "cancelled", self.bridge.now())
        return finished

    def _admit(self, uow, row, config, now, binding):
        """Recheck authority immediately before the effect. Returns a send description or None if the row was closed.

        ``binding`` is the identity verified in this same transaction. A row keeps the generation and binding it was queued
        under: one from before any owner, bot, chat or agent rebinding (even one later restored) is closed, never sent.
        """
        if (row["config_generation"], row["binding_fingerprint"]) != (binding["generation"], binding["fingerprint"]):
            self._finish(uow, row, "failed", "binding_changed", "The bot, owner or chat/agent binding changed after this was queued")
            return None
        current = config.destination(row["chat_id"], row["message_thread_id"])
        if current is None or current.name != row["chat_name"]:
            self._finish(uow, row, "failed", "destination_changed", "The saved Telegram destination is no longer the configured one")
            return None
        if row["producer_id"] and not row["producer_id"].startswith("principal:"):
            producer = config.producer(row["producer_id"])
            if not config.gateway.enabled or producer is None:
                self._finish(uow, row, "cancelled", "producer_revoked", "The producer credential is disabled or revoked")
                return None
        hook = self.bridge.admit_hooks.get(row["source_kind"])
        if hook is not None:
            verdict = hook(uow, row)
            if verdict is not None:
                state, code, message = verdict
                self._finish(uow, row, state, code, message)
                return None
        message = store.message(uow, row["message_id"]) if row["message_id"] else None
        if message and message["purpose"] == "blocking_input" and (
                message["request_state"] != "pending" or (message["request_expires_at"] or "9") <= now):
            self._finish(uow, row, "cancelled", "request_ended", "The input request ended before it was sent")
            return None
        description = {"kind": row["kind"], "chat_id": row["chat_id"], "message_thread_id": row["message_thread_id"],
                       "chat_name": row["chat_name"], "payload": row["payload_json"], "id": row["id"],
                       "source_kind": row["source_kind"], "binding": binding}
        if message and message["reply_to_id"]:
            parent = store.message(uow, message["reply_to_id"])
            if parent is not None and not store.in_destination(parent, current):
                self._finish(uow, row, "failed", "reply_destination_mismatch", "The message replied to is at another destination")
                return None
            description["reply_to"] = parent["telegram_message_id"] if parent else None
        if row["kind"] == "edit":
            target = store.message(uow, row["payload_json"]["target_message_id"])
            if target is None or target["telegram_message_id"] is None:
                self._finish(uow, row, "failed", "target_unknown", "The message to update has no known Telegram message")
                return None
            if not store.in_destination(target, current):
                # Never reach for General, another topic or an old chat: the target must be at the exact saved destination.
                self._finish(uow, row, "failed", "target_destination_mismatch", "The message to update is at another destination")
                return None
            description["provider_id"] = target["telegram_message_id"]
        return description

    def dispatch_one(self, *, uploads=None):
        """Claim and send the oldest due entry. ``uploads`` False skips agent attachment uploads, True takes only those."""
        bridge = self.bridge
        config = bridge.current_config()
        now = bridge.now()
        description = None
        if uploads:
            # The upload step runs on every pass: look first so that an idle pass takes no write unit for it.
            with bridge.read() as uow:
                if not store.due_outbox(uow, now, limit=1, uploads=True):
                    return False
        with bridge.write("dispatch.claim") as uow:
            binding = bridge.check_binding(uow)
            for row in store.due_outbox(uow, now, limit=BATCH, uploads=uploads):
                description = self._admit(uow, row, config, now, binding)
                if description is None:
                    continue
                store.set_outbox(uow, row, state="dispatching", attempts=row["attempts"] + 1, dispatch_started_at=now,
                                 next_attempt_at=None)
                break
        if description is None:
            return False
        # The claim committed and the send runs with no transaction open: look at the identity once more first. If it moved,
        # nothing was sent and the row goes back to queued, where the next admission closes or dispatches it.
        try:
            with bridge.read() as uow:
                bridge.verify_frozen(uow, description["binding"])
        except DomainError as exc:
            self._requeue(description["id"], exc.code)
            raise
        chat = bridge.config.destination(description["chat_id"], description["message_thread_id"])
        if chat is None or chat.name != description["chat_name"]:
            # The destination moved after the claim and nothing was sent: it is closed, never redirected to another topic.
            self._abandon(description["id"], "destination_changed", "The saved Telegram destination is no longer the configured one")
            return True
        # A crash after this point leaves the intent ``dispatching``: uncertain on restart.
        try:
            result = self._send(description)
        except AuthorityMoved as moved:
            # The attachment loader found its authority gone before any request: nothing was sent, so it is queued again for
            # the next admission to close (or, for a transient cause, after the usual backoff).
            self._requeue(description["id"], moved.code, retry=moved.retry)
            if moved.fail is not None:
                raise moved.fail from None
        except TelegramApiError as exc:
            self._failure(description["id"], exc)
        else:
            self._success(description, result)
        return True

    def _requeue(self, outbox_id, code, *, retry=False):
        bridge = self.bridge
        with bridge.write("dispatch.requeue") as uow:
            row = store.outbox(uow, outbox_id)
            if row is not None and row["state"] == "dispatching":
                delay = store.plus(bridge.now(), bridge.backoff(row["attempts"])) if retry else None
                store.set_outbox(uow, row, state="queued", error_code=code, next_attempt_at=delay)

    def _abandon(self, outbox_id, code, message):
        """Close a claimed send that was never attempted (nothing reached Telegram) without retrying it."""
        bridge = self.bridge
        with bridge.write("dispatch.abandon") as uow:
            row = store.outbox(uow, outbox_id)
            if row is not None and row["state"] == "dispatching":
                self._finish(uow, row, "failed", code, message)

    def _send(self, description):
        api = self.bridge.api
        payload = description["payload"]
        # A topic is always named explicitly so a missing thread can never land in General; a whole-chat send is unchanged.
        topic = {"message_thread_id": description["message_thread_id"]} if description["message_thread_id"] is not None else {}
        kind = description["kind"]
        if description["source_kind"] == store.AGENT_FILE_SOURCE:
            # An attachment is a document or a photo and nothing else: any other kind is never reinterpreted as an upload.
            if kind not in MEDIA_KINDS:
                raise self._unsupported("An agent attachment must be a document or a photo")
            return self._send_agent_file(description)
        if kind == "text":
            return api.send_message(description["chat_id"], payload["text"], description.get("reply_to"), **topic)
        if kind == "edit":
            try:
                api.edit_message_text(description["chat_id"], description["provider_id"], payload["text"])
            except TelegramApiError as exc:
                if exc.kind == "refused" and "not modified" in exc.description.lower():
                    return {"message_id": description["provider_id"]}
                raise
            return {"message_id": description["provider_id"]}
        raise self._unsupported("This outbox kind has no sender for this source")

    @staticmethod
    def _unsupported(message):
        return TelegramApiError("refused", message, error_code=400, local_code="media_unsupported")

    def _send_agent_file(self, description):
        """Upload one sealed attachment. Anything wrong is found, and refused as a pre-send failure, before a request exists.

        The loader verifies the sealed bytes and yields ``(filename, stream, size, caption, content_type)`` while its context is
        open; the exact chat, topic and reply target are the ones frozen when the entry was queued.
        """
        bridge = self.bridge
        envelope = description["payload"]
        payload_id = envelope.get("payload_id") if isinstance(envelope, dict) else None
        if (not isinstance(envelope, dict) or set(envelope) != {"version", "payload_id"} or type(envelope["version"]) is not int
                or envelope["version"] != 1 or not isinstance(payload_id, str) or PAYLOAD_ID.fullmatch(payload_id) is None):
            raise self._unsupported("The queued attachment is not a version 1 payload reference")
        loader = bridge.media_loaders.get(store.AGENT_FILE_SOURCE)
        if loader is None:
            raise self._unsupported("No attachment loader is registered")
        mode = description["kind"]
        stack = ExitStack()
        try:
            try:
                media = stack.enter_context(loader(envelope))
            except (DomainError, OSError) as exc:
                # The sealed file or its record is unusable. Nothing was sent, and it is never rebuilt from the source. Only a
                # stable code is kept: a path or message from the loader never reaches the stored receipt.
                reason = f"The sealed attachment is unavailable ({exc.code})" if isinstance(exc, DomainError) else (
                    "The sealed attachment could not be read")
                raise TelegramApiError("refused", reason, error_code=400, local_code="media_unavailable") from None
            if not isinstance(media, tuple) or len(media) != 5:
                raise TelegramApiError("refused", "The attachment loader returned an unexpected shape", error_code=400,
                                       local_code="media_invalid")
            filename, stream, size, caption, content_type = media
            caption, content_type = validate_upload(mode, filename, size, caption, content_type)
            send = bridge.api.send_photo_stream if mode == "photo" else bridge.api.send_document_stream
            return send(description["chat_id"], filename, stream, size, caption, message_thread_id=description["message_thread_id"],
                        reply_to_message_id=description.get("reply_to"), content_type=content_type)
        finally:
            try:
                stack.close()
            except Exception:  # noqa: BLE001 - the provider's answer, not a failure to close the file, decides the outcome
                LOGGER.exception("Closing a Telegram attachment source failed")

    @staticmethod
    def _confirms_destination(description, result):
        """Whether a successful topic send names the destination it was meant for.

        The returned Message must carry the intended chat and topic. One that is missing either, or names another, may have
        landed in General or elsewhere: its outcome is uncertain and is never a reason to send again. A whole-chat (legacy)
        send keeps its existing contract and is not checked. An edit returns no new message and is not checked either.
        """
        thread = description["message_thread_id"]
        if thread is None or description["kind"] == "edit":
            return True
        chat = result.get("chat") if isinstance(result, dict) else None
        return (isinstance(chat, dict) and chat.get("id") == description["chat_id"] and result.get("message_thread_id") == thread
                and result.get("is_topic_message", True) is True)

    def _success(self, description, result):
        bridge = self.bridge
        provider_id = result.get("message_id") if isinstance(result, dict) else None
        if type(provider_id) is not int or provider_id <= 0:
            self._failure(description["id"], TelegramApiError("uncertain", "The provider returned no message identifier"))
            return
        if not self._confirms_destination(description, result):
            self._failure(description["id"], TelegramApiError(
                "uncertain", "The provider's response did not confirm the intended chat and topic"))
            return
        receipt = (description, provider_id, bridge.now())
        try:
            self._record(*receipt)
        except DomainError as exc:
            if exc.code not in ("maintenance", "busy"):
                raise
            # The send happened. Keep its receipt and commit it as soon as writes are admitted again; only a crash before
            # that leaves the row dispatching, which recovery turns into uncertain.
            self._unrecorded.append(receipt)

    def flush(self):
        worked = False
        for receipt in list(self._unrecorded):
            try:
                self._record(*receipt)
            except DomainError as exc:
                if exc.code not in ("maintenance", "busy"):
                    raise
                break
            self._unrecorded.remove(receipt)
            worked = True
        return worked

    def _record(self, description, provider_id, now):
        bridge = self.bridge
        with bridge.write("dispatch.receipt") as uow:
            row = store.outbox(uow, description["id"])
            if row is None or row["state"] != "dispatching":
                return
            row = store.set_outbox(uow, row, state="sent", provider_message_id=provider_id, sent_at=now,
                                   error_code=None, error_message=None)
            if row["message_id"] and description["kind"] != "edit":
                message = store.message(uow, row["message_id"])
                store.update_message(uow, message, telegram_message_id=provider_id)
            elif row["message_id"]:
                message = store.message(uow, row["message_id"])
                store.update_message(uow, message, body=description["payload"]["text"])
            hook = bridge.sent_hooks.get(row["source_kind"])
            if hook is not None:
                hook(uow, row, provider_id)

    def _failure(self, outbox_id, exc):
        bridge = self.bridge
        now = bridge.now()
        with bridge.write("dispatch.failure") as uow:
            row = store.outbox(uow, outbox_id)
            if row is None or row["state"] != "dispatching":
                return
            if exc.kind in ("timeout", "uncertain"):
                self._finish(uow, row, "uncertain", "provider_" + exc.kind, exc.description)
            elif exc.kind == "refused":
                code = exc.local_code or ("target_missing" if exc.message_missing else f"refused_{exc.error_code or 'unknown'}")
                self._finish(uow, row, "failed", code, exc.description)
            else:
                exhausted = row["attempts"] >= bridge.timings["max_attempts"]
                if exhausted:
                    self._finish(uow, row, "failed", exc.kind + "_exhausted", exc.description)
                    return
                delay = exc.retry_after + bridge.rng.random() if exc.kind == "rate_limited" else bridge.backoff(row["attempts"])
                store.set_outbox(uow, row, state="queued", next_attempt_at=store.plus(now, delay), error_code=exc.kind,
                                 error_message=exc.description[:500])

    # ----- blocking-input expiry -------------------------------------------------------------------------

    def expire_requests(self):
        bridge = self.bridge
        now = bridge.now()
        worked = False
        with bridge.write("requests.expire") as uow:
            rows = [dict(r) for r in uow.connection.execute(
                "SELECT * FROM telegram_messages WHERE request_state='pending' AND direction='outbound' AND request_expires_at<=?",
                (now,))]
            for row in rows:
                store.close_request(uow, row, "expired", row["request_expires_at"])
                worked = True
                outbox = store.outbox(uow, row["outbox_id"]) if row["outbox_id"] else None
                if outbox and outbox["state"] == "queued":
                    self._finish(uow, outbox, "cancelled", "request_ended", "The input request expired before it was sent")
                if row["input_mode"] == "secret":
                    store.enqueue_cleanup(uow, row["id"], "secret_request_expired", now=now)
        return worked


class Cleanup:
    """Delete only messages the bridge knows: never a whole chat, never an unknown message."""

    def __init__(self, bridge):
        self.bridge = bridge

    def run(self):
        worked = False
        for _ in range(BATCH):
            if not self.one():
                break
            worked = True
        return worked

    def _due(self, uow, now):
        return [dict(r) for r in uow.connection.execute(
            "SELECT * FROM telegram_cleanup_jobs WHERE state='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
            "AND (not_before IS NULL OR not_before<=?) ORDER BY created_at, rowid LIMIT ?", (now, now, BATCH))]

    def _age_anchor(self, uow, message):
        if message["direction"] == "inbound":
            return message["occurred_at"]
        outbox = store.outbox(uow, message["outbox_id"]) if message["outbox_id"] else None
        return (outbox or {}).get("sent_at") or message["occurred_at"]

    def one(self):
        bridge = self.bridge
        config = bridge.current_config()
        now = bridge.now()
        target = None
        with bridge.write("cleanup.claim") as uow:
            bridge.check_binding(uow)
            repository = uow.repository("telegram_cleanup_jobs")
            for job in self._due(uow, now):
                message = store.message(uow, job["message_id"])
                if message["telegram_message_id"] is None:
                    outbox = store.outbox(uow, message["outbox_id"]) if message["outbox_id"] else None
                    if outbox is None or outbox["state"] in ("failed", "cancelled"):
                        repository.update(job["id"], {"state": "deleted", "completed_at": now, "error_code": "never_sent"},
                                          expected_revision=job["revision"])
                    else:
                        repository.update(job["id"], {"next_attempt_at": store.plus(now, 30)}, expected_revision=job["revision"])
                    continue
                # Only a message at the exact destination it was saved for, still configured under its own name, is deleted: another
                # topic of the same group, a former binding and an unconfigured chat are all reported for manual cleanup instead.
                destination = config.destination(message["chat_id"], message["message_thread_id"])
                if destination is None or destination.name != message["chat_name"]:
                    repository.update(job["id"], {"state": "failed", "error_code": "unknown_chat"}, expected_revision=job["revision"])
                    continue
                if store.parse(now) - store.parse(self._age_anchor(uow, message)) >= store.timedelta(seconds=DELETE_LIMIT_SECONDS):
                    repository.update(job["id"], {"state": "failed", "error_code": "too_old_48h",
                        "error_message": "Telegram allows deleting a message only within 48 hours; delete it manually"},
                        expected_revision=job["revision"])
                    continue
                repository.update(job["id"], {"state": "deleting", "attempts": job["attempts"] + 1}, expected_revision=job["revision"])
                target = (job["id"], message["chat_id"], message["telegram_message_id"])
                break
        if target is None:
            return False
        job_id, chat_id, provider_id = target
        outcome = ("deleted", None)
        try:
            bridge.api.delete_message(chat_id, provider_id)
        except TelegramApiError as exc:
            if exc.message_missing:
                outcome = ("deleted", "already_gone")
            elif exc.too_old:
                outcome = ("failed", "too_old_48h")
            elif exc.kind == "refused":
                outcome = ("failed", f"delete_refused_{exc.error_code or 'unknown'}")
            else:
                outcome = ("retry", exc.kind)
        self._record(job_id, outcome, bridge)
        return True

    def _record(self, job_id, outcome, bridge):
        now = bridge.now()
        state, code = outcome
        with bridge.write("cleanup.receipt") as uow:
            repository = uow.repository("telegram_cleanup_jobs")
            job = repository.get(job_id)
            if job is None or job["state"] != "deleting":
                return
            if state == "deleted":
                repository.update(job_id, {"state": "deleted", "completed_at": now, "error_code": code}, expected_revision=job["revision"])
            elif state == "failed":
                repository.update(job_id, {"state": "failed", "error_code": code}, expected_revision=job["revision"])
            elif job["attempts"] >= bridge.timings["cleanup_attempts"]:
                repository.update(job_id, {"state": "failed", "error_code": code + "_exhausted"}, expected_revision=job["revision"])
            else:
                repository.update(job_id, {"state": "queued", "error_code": code,
                                           "next_attempt_at": store.plus(now, bridge.backoff(job["attempts"]))},
                                  expected_revision=job["revision"])
