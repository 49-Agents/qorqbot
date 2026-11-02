"""Inbound owner messages and reactions.

Only the exact owner ``user.id`` at a configured destination is accepted. A destination is a chat and, in forum mode, one topic:
a message is routed by its own validated topic metadata and never by its chat alone, so General, an unbound topic and a message
with malformed topic fields are not a fallback to some other configured topic. A reaction names no topic, so it is resolved
through the bridge's own stored post. Bots, service messages, anonymous senders, other users, channel posts, aggregate reaction
counts and edits are ignored with a stored receipt. Forwarded text is content, never authorization. Text is literal: terminal
control sequences are refused, never sanitized into something else.
"""

from __future__ import annotations

import logging

from qorqut_operations.contracts import DomainError, new_id
from . import store
from .bridge import FAIL_CLOSED
from .config import AGENT_CHATS
from .inbox_port import MAX_BODY, deliver_owner_message
from .outbox import enqueue_notice

LOGGER = logging.getLogger(__name__)
MAX_OWNER_CHARS = 16000
LABELS = {"ordubashy": "Ordubashy", "signoffs": "Signoffs", "urgent": "Urgent Notifications"}
SECRET_LIFETIME_SECONDS = 600
EXCERPT = 300
MAX_UPDATE_ATTEMPTS = 3


def clean_text(text):
    """Return ``(text, error)``. Errors: too_long, control_characters, empty. CRLF becomes LF; tab and newline pass."""
    if len(text) > MAX_OWNER_CHARS:
        return None, "too_long"
    text = text.replace("\r\n", "\n")
    for char in text:
        code = ord(char)
        if char in "\n\t":
            continue
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            return None, "control_characters"
    if not text.strip():
        return None, "empty"
    return text, None


def attachment_marker(kinds):
    return "[Telegram attachment not delivered: " + ", ".join(kinds) + "]"


REFUSALS = {
    "too_long": "That message is longer than 16000 characters, so it was not delivered. Send a shorter one.",
    "control_characters": "That message contains terminal control characters, so it was not delivered. Send plain text.",
    "empty": "That message had no text to deliver.",
    "expired": "That secret request has expired or was already answered, so nothing was delivered. Ask the agent to request it again.",
    "request_ended": "That secret request has expired or was already answered, so nothing was delivered. Ask the agent to request it again.",
    "cross_topic_reply": "That reply points at a message from another topic, so it was not delivered. Reply inside this topic.",
}


def destination_of(config, item):
    """The binding an owner message was sent to, from its own validated topic metadata; None for General, an unbound topic, a
    topic that is not configured as one, malformed topic fields or an unconfigured chat."""
    if item.get("topic_invalid"):
        return None
    return config.destination(item.get("chat_id"), item.get("message_thread_id"))


class Inbound:
    def __init__(self, bridge):
        self.bridge = bridge

    # ----- ingestion (before the batch and its cursor commit) ---------------------------------------------

    def prepare(self, items):
        """Decide what may be stored before the batch commits: no secret body, and no body of unrouted traffic.

        Text is kept only for owner messages the bridge will actually route (an agent topic or chat, or a reply in Signoffs, from the
        exact owner sent after activation and after the current binding generation took effect, in the exact configured destination,
        and not replying to a known message of another destination). Every other update loses its text here: what is not routed
        is never retained, which includes General, unknown topics and malformed topic metadata. Owner
        text that the bridge would refuse (too long, control characters, empty) keeps only the refusal. Text sent while a
        secret request was open, or in reply to a secret prompt at any time, is spooled instead of stored, or discarded when
        it can no longer be delivered.

        The spool file is fsynced here, before the caller commits the queue pointer and advances the cursor. A crash in
        between leaves an orphan the startup sweep removes; Telegram redelivers the update.
        """
        bridge = self.bridge
        config = bridge.config
        now = bridge.now()
        pending = []
        answered = {}  # secret request id -> when its first deliverable answer of this batch was sent
        with bridge.read() as uow:
            # Every stored update freezes the identity it was fetched under. A refusal here stores nothing and leaves the
            # cursor where it was, so Telegram redelivers the batch once the binding verifies again.
            binding = bridge.check_binding(uow)
            state = store.get_state(uow)
            if state is None:
                raise DomainError("operation_unavailable", "The Telegram bridge has not been activated")
            cutoff = store.cutoff(state)
            for item in items:
                item["binding"] = binding
                if item.get("kind") != "message":
                    continue
                chat = destination_of(config, item)
                routed = (chat is not None and (chat.name in AGENT_CHATS or chat.name == "signoffs")
                          and item.get("chat_type") == chat.chat_type and item.get("actor_id") == config.owner_telegram_user_id
                          and not (item.get("from_is_bot") or item.get("anonymous") or item.get("service"))
                          and item.get("sent_at") is not None and item["sent_at"] >= cutoff)
                if not routed:
                    item.pop("text", None)
                    continue
                if self.crosses_destination(uow, chat, item):
                    # A known parent at another destination supplies no context or authority, and this is settled before any
                    # secret body could be spooled or stored.
                    item.pop("text", None)
                    continue
                if not isinstance(item.get("text"), str):
                    continue
                request = self.governing_secret(uow, chat, item, now, answered) if chat.name in AGENT_CHATS else None
                if request is not None:
                    pending.append((item, request))
                    prompt, deliverable = request
                    # The first text that can still be delivered answers the request, so a later text of this very batch is
                    # ordinary: the closure is only committed when the answer is routed, after every item was classified.
                    if deliverable and prompt["id"] not in answered and clean_text(item["text"])[1] is None:
                        answered[prompt["id"]] = item["sent_at"]
                    continue
                _, error = clean_text(item["text"]) if item["text"] else (None, None)
                if error is not None:
                    item["refused"] = error
                    item.pop("text")
        for item, (request, active) in pending:
            text = item.pop("text")
            item["sensitive"] = True
            item["request_message_id"] = request["id"]
            cleaned, error = clean_text(text) if active else (None, "expired")
            if error is None and item.get("attachments"):
                cleaned = f"{cleaned}\n{attachment_marker(item['attachments'])}"
            if error is not None:
                item["refused"] = error
                continue
            expiry = min(store.parse(request["request_expires_at"]), store.parse(now) + store.timedelta(seconds=SECRET_LIFETIME_SECONDS))
            item["spool_id"] = bridge.spool.put(cleaned, store.iso(expiry))
            item["spool_expires_at"] = store.iso(expiry)
        return items

    @staticmethod
    def crosses_destination(uow, chat, item):
        """Whether the message replies to a message the bridge knows at a different destination (topic, chat or logical name)."""
        reply = (item.get("reply") or {}).get("message_id")
        parent = store.message_by_provider(uow, chat.chat_id, reply) if reply else None
        return parent is not None and not store.in_destination(parent, chat)

    def governing_secret(self, uow, chat, item, now, answered=None):
        """The secret prompt that makes this owner text sensitive, as ``(prompt_row, still_deliverable)``, or None.

        Judged by when the owner sent the text: an explicit reply to a secret prompt is sensitive whenever it is read, and
        so is any text sent while such a request was open. Whether it can still be delivered is a separate question about
        the request now: an expired, answered or cancelled request keeps the text sensitive and never delivers it. ``answered``
        maps the requests that an earlier text of the same fetched batch answers (not committed until it is routed) to that
        text's send time: a text sent after it is ordinary, and one in the same second stays inside, as after a commit.
        """
        reply = (item.get("reply") or {}).get("message_id")
        prompt = None
        if reply:
            found = store.message_by_provider(uow, chat.chat_id, reply)
            if store.in_destination(found, chat) and found["input_mode"] == "secret":
                prompt = found
        if prompt is None:
            prompt = store.secret_at(uow, chat, item["sent_at"])
            if prompt is not None and item["sent_at"] > (answered or {}).get(prompt["id"], item["sent_at"]):
                return None  # sent after an earlier text of the same batch answered the request
        if prompt is None:
            return None
        return prompt, prompt["request_state"] == "pending" and prompt["request_expires_at"] > now

    # ----- processing ------------------------------------------------------------------------------------

    def process_pending(self):
        """Handle stored unfinished updates in order. Returns True if any were processed."""
        bridge = self.bridge
        worked = False
        while True:
            with bridge.read() as uow:
                state = store.get_state(uow)
                rows = store.pending_updates(uow, state["bot_id"], 50) if state else []
            if not rows:
                return worked
            for row in rows:
                self.process(row)
                worked = True

    def process(self, row):
        bridge = self.bridge
        item = row["normalized"]
        try:
            with bridge.write("inbound") as uow:
                current = uow.repository("telegram_updates").get((row["bot_id"], row["update_id"]))
                if current is None or current["state"] != "received":
                    return
                now = bridge.now()
                state, reason, result = self._route(uow, item, now)
                store.finish_update(uow, row["bot_id"], row["update_id"], state, reason=reason, result=result, now=now)
        except DomainError as exc:
            if exc.code in ("maintenance", "busy") or exc.code in FAIL_CLOSED:
                raise
            LOGGER.warning("Telegram update %s failed: %s", row["update_id"], exc.code)
            self._record_failure(row, exc.code)
            return
        bridge.wake()

    def _record_failure(self, row, code):
        bridge = self.bridge
        with bridge.write("inbound.failure") as uow:
            current = uow.repository("telegram_updates").get((row["bot_id"], row["update_id"]))
            if current is None or current["state"] != "received":
                return
            attempts = current["attempts"] + 1
            uow.repository("telegram_updates").update(
                (row["bot_id"], row["update_id"]),
                {"attempts": attempts, "reason": code, **({"state": "failed"} if attempts >= MAX_UPDATE_ATTEMPTS else {})},
                expected_revision=current["revision"])

    def _route(self, uow, item, now):
        config = self.bridge.config
        if item["kind"] == "ignored":
            return "ignored", item.get("reason", "ignored"), None
        # A stored update keeps the identity it was fetched under. After any owner, installation, chat or agent rebinding it
        # is discarded here; it is never upgraded to the new generation by being routed late.
        if item.get("binding") != self.bridge.check_binding(uow):
            return "ignored", "binding_changed", None
        if item["kind"] == "message_reaction":
            # A reaction carries no topic. It is resolved through the stored post it names, below; here only the chat is known.
            bindings = config.chats_in(item.get("chat_id"))
            chat = bindings[0] if bindings else None
            if chat is None:
                return "ignored", "unknown_chat", None
        else:
            if item.get("topic_invalid"):
                return "ignored", "invalid_topic", None
            chat = destination_of(config, item)
            if chat is None:
                return "ignored", ("unknown_topic" if config.chats_in(item.get("chat_id")) else "unknown_chat"), None
        if item.get("chat_type") != chat.chat_type:
            return "ignored", "chat_type_mismatch", None
        if item.get("anonymous"):
            return "ignored", "anonymous_actor", None
        if item.get("from_is_bot"):
            return "ignored", "bot_actor", None
        if item.get("actor_id") != config.owner_telegram_user_id:
            return "ignored", "not_owner", None
        if item["kind"] == "message" and item.get("service"):
            return "ignored", "service_message", None
        state = store.get_state(uow)
        if item.get("sent_at") is None or item["sent_at"] < state["activation_at"]:
            return "ignored", "before_activation", None
        if item["sent_at"] < state["binding_effective_at"]:
            return "ignored", "before_binding_effective", None
        if item["kind"] == "message_reaction":
            handler = getattr(self.bridge, "signoff_bridge", None)
            # Only a chat that holds the Signoffs destination can carry a decision reaction (a forum group does, as one topic of it).
            if handler is None or not any(binding.name == "signoffs" for binding in bindings):
                return "ignored", "no_reaction_route", None
            return handler.route_reaction(uow, item, now)
        if self.crosses_destination(uow, chat, item):
            self._notice(uow, chat, item["update_id"], "cross_topic_reply")
            return "ignored", "cross_topic_reply", None
        if chat.name in AGENT_CHATS:
            return self._owner_message(uow, chat, item, now)
        if chat.name == "signoffs":
            handler = getattr(self.bridge, "signoff_bridge", None)
            if handler is None:
                return "ignored", "no_message_route", None
            return handler.route_reply(uow, chat, item, now)
        return "ignored", "no_inbound_route", None

    # ----- owner text to an agent chat -------------------------------------------------------------------

    def _notice(self, uow, chat, update_id, code, text=None):
        enqueue_notice(self.bridge, uow, chat.name, text or REFUSALS[code], f"inbound:{update_id}:{code}")

    def _excerpt(self, row, uow):
        if row is None:
            return None
        if row["body_redacted"] or row["body"] is None:
            return "(redacted)"
        # A captionless file or photo reads as File: or Photo: and its name; nothing synthetic is ever sent to Telegram.
        text = " ".join(store.display_text(row["body"], store.attachment_of(uow, row["id"])).split())
        return text[:EXCERPT] + ("…" if len(text) > EXCERPT else "")

    def _owner_message(self, uow, chat, item, now):
        bridge = self.bridge
        agent_id = chat.agent_id
        update_id = item["update_id"]
        sensitive = bool(item.get("sensitive"))
        reply_row = (store.message_by_provider(uow, chat.chat_id, item["reply"]["message_id"]) if item.get("reply") else None)
        if not store.in_destination(reply_row, chat):
            reply_row = None
        request = None
        if sensitive:
            request = store.message(uow, item["request_message_id"])
        elif reply_row is not None and reply_row["request_key"]:
            request = reply_row
        else:
            request = store.pending_request(uow, chat)
        refusal = item.get("refused")
        spool_id = item.get("spool_id")
        if sensitive and refusal is None and (request is None or request["request_state"] != "pending"):
            refusal = "request_ended"
        text = None
        if not sensitive:
            raw = item.get("text") or ""
            attachments = item.get("attachments") or []
            if item.get("refused"):
                refusal = item["refused"]
            elif raw:
                text, refusal = clean_text(raw)
            elif not attachments:
                refusal = "empty"
            if refusal is None and attachments:
                text = (text + "\n" if text else "") + attachment_marker(attachments)
        if refusal is None and item.get("attachments"):
            # A secret caption gets the same single notice; it names no content, only the unsupported kind of message.
            self._notice(uow, chat, update_id, "attachments",
                         "Attachments are not supported: only the text was passed on. Files, images and voice are never downloaded. "
                         "Please send the details as text, or use the dashboard upload flow for files.")
        profile = uow.repository("agent_profiles", business_id=None).get(agent_id)
        if refusal is None:
            try:
                bridge.check_agent_chat(uow, chat)
            except DomainError:
                refusal = "agent_unavailable"
        message = store.insert_message(
            uow, direction="inbound", chat=chat, purpose="owner", body=text if refusal is None else f"[not delivered: {refusal}]",
            now=now, occurred_at=item["sent_at"],
            agent_id=agent_id,
            reply_to_id=request["id"] if sensitive and request else reply_row["id"] if reply_row else None,
            reply_to_telegram_id=(item.get("reply") or {}).get("message_id"), update_id=update_id,
            telegram_message_id=item["message_id"], sensitive=sensitive)
        if refusal is not None:
            if spool_id:
                bridge.spool.delete(spool_id)
            if sensitive:
                store.enqueue_cleanup(uow, message["id"], "secret_refused", now=now)
            self._notice(uow, chat, update_id, refusal, REFUSALS.get(refusal) or
                         f"The {LABELS[chat.name]} agent is not active, so your message was not delivered.")
            return "done", "refused_" + refusal, {"delivered": False, "reason": refusal}
        if request is not None and request["request_state"] == "pending" and (
                sensitive or request["id"] == (reply_row or {}).get("id")):
            store.close_request(uow, request, "answered", item["sent_at"])
        correlation = new_id()
        body = self._body(item, text, reply_row, request, sensitive, message, uow)
        expires_at = item.get("spool_expires_at")
        binding = item["binding"]
        canonical, event_id = deliver_owner_message(
            uow, owner_id=binding["owner"], agent_id=agent_id, body=body, created_at=now, message_id=correlation,
            summary="Secret input from the owner (redacted)" if sensitive else None)
        uow.repository("telegram_owner_inputs").insert({
            "id": new_id(), "purpose": "owner_message", "correlation_id": canonical, "message_id": message["id"],
            "agent_id": agent_id, "inbox_event_id": str(event_id), "sensitive": 1 if sensitive else 0,
            "owner_principal_id": binding["owner"], "config_generation": binding["generation"],
            "binding_fingerprint": binding["fingerprint"], "target_fingerprint": store.target_fingerprint(profile),
            "spool_id": spool_id, "expires_at": expires_at, "state": "queued", "created_at": now, "updated_at": now})
        return "done", None, {"delivered": "queued", "correlation_id": canonical}

    def _body(self, item, text, reply_row, request, sensitive, message, uow):
        lines = [f"[Telegram message {message['id']}]"]
        if item.get("reply"):
            if reply_row is not None:
                who = "your earlier message" if reply_row["direction"] == "outbound" else "an earlier owner message"
                lines.append(f'[In reply to {who} {reply_row["id"]}: "{self._excerpt(reply_row, uow)}"]')
            else:
                lines.append("[In reply to a Telegram message that is not in the bridge history]")
        if request is not None:
            lines.append(f'[Request {request["request_key"]} ({request["input_mode"]}): {request["request_reason"]}]')
        if sensitive:
            lines.append("[Secret input: the value is not stored here. It is queued for direct delivery to the agent's terminal "
                         "and is discarded if it cannot be delivered before it expires.]")
            return "\n".join(lines)
        # Telegram caps a message at 4096 characters, far below the canonical limit; if a header ever did not fit, the message
        # keeps its identifying first line before it would lose all context, and the text itself is never cut.
        for header in ("\n".join(lines), lines[0]):
            combined = header + "\n" + text
            if len(combined) <= MAX_BODY:
                return combined
        return text
