"""``telegram_send_file``: the one operation that lets the exact configured active Global Ordubashy send a file or photo to the
owner in its own Telegram chat.

It composes the approved foundations and adds no byte, source or transport logic of its own: ``FileSources`` says whether this
agent may read the file, ``MediaPayloads`` copies, hashes, validates and seals it into a private snapshot, and the existing
dispatcher uploads that snapshot through the bounded streaming transport. This module is the authority and transaction
boundary between them.

The operation follows the engine's real phases:

``prepare`` (read unit)
    Requires the exact bound Ordubashy, validates the arguments and the reply target, and freezes the *manifest*: the source
    binding, the destination (chat, topic, agent, binding generation and fingerprint), the caption and the reply association.
    A repeated idempotency key instead loads the manifest and filename its request already froze, rechecks current authority
    against them, and never derives a replacement or opens the source.
``external`` (no transaction)
    ``MediaPayloads.stage`` reserves the request, copies the source once through the secure opener, requires the expected
    SHA-256, validates a photo and seals the snapshot. A sealed request key returns the saved snapshot without reading anything.
``apply`` (one write unit)
    Rederives authority, compares it with the frozen manifest, inserts the outbound message (body = the caption, possibly
    empty) and its ``agent_file`` outbox entry, attaches the payload, and returns the receipt that the engine saves in the same
    transaction. The three effects commit together or not at all.

The manifest is stored in the payload row's immutable ``source_binding_json`` and never appears in any public response.
``replay_validator`` rechecks the exact caller and the source before a saved receipt is returned, without reopening anything.

Dispatch uses two hooks. ``admit`` runs inside the claim transaction (database reads only) and closes a send whose authority is
gone (``cancelled``) or whose records disagree (``failed``). ``load`` is the attachment loader: it verifies the sealed bytes
through ``MediaPayloads.open_payload`` and, outside any write unit and immediately before handing the stream to the transport,
rechecks the same authority. An authority that moved in that window requeues the send (``AuthorityMoved``), where the next
admission closes it; a damaged or missing snapshot fails before any request exists and is never rebuilt.
"""

from __future__ import annotations

from qorqut_agentic.behavior.tool_help import text as instruction_text

from contextlib import contextmanager
from dataclasses import replace

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, canonical_hash, plain

from . import store
from .config import AGENT_CHATS
from .media_schema import OPERATION_NAME
from .media_sources import EXPECTED_SHA256_SCHEMA, SOURCE_SCHEMA, FileSources
from .media_validation import CAPTION_MAX_CHARS, validate_caption, validate_sha256, validate_snapshot
from .outbox import AuthorityMoved, enqueue_file

UUID = {"type": "string", "format": "uuid"}
PURPOSES = ("reply", "update")
MANIFEST_VERSION = 1
PREPARED = "telegram_file_prepared"
DESTINATION_KEYS = frozenset({"chat_name", "chat_id", "message_thread_id", "agent_id", "config_generation", "binding_fingerprint"})

ARGUMENTS = schema({
    "source": SOURCE_SCHEMA,
    "expected_sha256": EXPECTED_SHA256_SCHEMA,
    "send_as": {"enum": ["document", "photo"],
                "description": instruction_text('media-operations-document-keeps-any-file-up-to-50')},
    "purpose": {"enum": list(PURPOSES), "description": instruction_text('media-operations-reply-answers-the-owner-update-reports-progress')},
    "caption": {"type": "string", "maxLength": CAPTION_MAX_CHARS,
                "description": instruction_text('media-operations-optional-plain-text-caption-at-most-1024')},
    "reply_to": {**UUID, "description": instruction_text('media-operations-optional-local-message-id-from-telegram-history')},
}, ("source", "expected_sha256", "send_as", "purpose"))
DESCRIPTION = (
    instruction_text('media-operations-send-one-file-or-photo-to-the'))


def _damaged(message="The saved file request is damaged"):
    return DomainError("payload_mismatch", message)


def parse_manifest(value):
    """The saved manifest as a plain dict, or ``payload_mismatch`` when it is not exactly the closed shape this module writes:
    ``{version, source, destination, caption, reply}`` with ``reply`` null or ``{message_id, provider_message_id}``."""
    value = plain(value)
    if (not isinstance(value, dict) or set(value) != {"version", "source", "destination", "caption", "reply"}
            or type(value["version"]) is not int or value["version"] != MANIFEST_VERSION
            or not isinstance(value["source"], dict) or not isinstance(value["caption"], str)):
        raise _damaged()
    destination, reply = value["destination"], value["reply"]
    if not isinstance(destination, dict) or set(destination) != DESTINATION_KEYS:
        raise _damaged()
    if reply is not None and (not isinstance(reply, dict) or set(reply) != {"message_id", "provider_message_id"}
                              or not isinstance(reply["message_id"], str) or type(reply["provider_message_id"]) is not int
                              or reply["provider_message_id"] < 1):
        raise _damaged()
    return value


def _destination(row):
    """The destination a queued entry (or a chat, agent and binding) names, in the manifest's shape."""
    return {"chat_name": row["chat_name"], "chat_id": row["chat_id"], "message_thread_id": row["message_thread_id"],
            "agent_id": row["agent_id"], "config_generation": row["config_generation"],
            "binding_fingerprint": row["binding_fingerprint"]}


class TelegramFiles:
    """The ``telegram_send_file`` operation and its dispatch hooks, bound to one ``TelegramOperations``."""

    def __init__(self, operations):
        self.operations = operations
        self.runtime = operations.runtime
        self.app = operations.app
        self.sources = FileSources(self.runtime)

    @property
    def bridge(self):
        return self.operations.bridge

    @property
    def media(self):
        return self.bridge.media

    def register(self, common):
        self.runtime.registry.register(OperationDescriptor(
            OPERATION_NAME, ARGUMENTS, **common, exposure=frozenset({"mcp"}), description=DESCRIPTION,
            prepare=self.prepare, external=self.external, apply=self.apply, replay_validator=self.replay_validator))
        self.bridge.admit_hooks[store.AGENT_FILE_SOURCE] = self.admit
        self.bridge.media_loaders[store.AGENT_FILE_SOURCE] = self.load
        return self

    # ----- authority -------------------------------------------------------------------------------------

    def _caller(self, ctx, uow):
        """The exact bound active Global Ordubashy acting as itself, its chat, and the verified binding identity."""
        ctx, chat = self.operations.agent_chat(ctx, uow)
        return ctx, chat, self.bridge.check_binding(uow)

    @staticmethod
    def _current_destination(ctx, chat, binding):
        return {"chat_name": chat.name, "chat_id": chat.chat_id, "message_thread_id": chat.message_thread_id,
                "agent_id": ctx.principal_id, "config_generation": binding["generation"],
                "binding_fingerprint": binding["fingerprint"]}

    def _reply(self, ctx, chat, reply_to, uow):
        """The reply association for a new request, or a refusal: a known message of this exact agent, chat and topic with a
        positive Telegram message ID. Nothing else is borrowed."""
        if reply_to is None:
            return None
        row = store.message(uow, reply_to)
        if row is None or not store.in_destination(row, chat) or row["agent_id"] != ctx.principal_id:
            raise DomainError("not_found", "The reply target is unavailable in your chat")
        provider_id = row["telegram_message_id"]
        if type(provider_id) is not int or provider_id < 1:
            raise DomainError("validation_error", "The message you reply to has no Telegram message yet",
                              details={"field": "reply_to"})
        return {"message_id": row["id"], "provider_message_id": provider_id}

    @staticmethod
    def _reply_current(row, agent_id, destination, reply):
        """Whether the local reply row is still the frozen one: same agent, chat and topic, same positive provider ID."""
        return (row is not None and row["agent_id"] == agent_id
                and (row["chat_name"], row["chat_id"], row["message_thread_id"]) == destination
                and row["telegram_message_id"] == reply["provider_message_id"])

    def _check_frozen(self, ctx, chat, binding, manifest, uow):
        """Compare current authority with a frozen manifest and return the freshly authorized source.

        The destination and binding generation must be exactly the frozen ones (a rebound snapshot is never retargeted), the
        source must authorize to exactly the same facts without reading its file, and a frozen reply must still be the same
        local message with the same provider ID."""
        if manifest["destination"] != self._current_destination(ctx, chat, binding):
            raise DomainError("binding_changed", "The Telegram destination or binding changed after this file was frozen; "
                                                 "use a new idempotency key")
        authorized = self.sources.recheck(ctx, manifest["source"], uow)
        reply = manifest["reply"]
        if reply is not None and not self._reply_current(
                store.message(uow, reply["message_id"]), ctx.principal_id, (chat.name, chat.chat_id, chat.message_thread_id), reply):
            raise DomainError("reply_changed", "The message to reply to is no longer the one this file was prepared for")
        return authorized

    # ----- the operation ----------------------------------------------------------------------------------

    def prepare(self, ctx, args, options, uow):
        args = plain(args)
        ctx, chat, binding = self._caller(ctx, uow)
        key = options.idempotency_key
        arguments_hash = canonical_hash(args)
        caption = validate_caption(args.get("caption", ""))
        validate_sha256(args["expected_sha256"])
        saved = self.media.find(uow, ctx.principal_id, key)
        if saved is not None:
            # The request already froze its facts: they are reused, current authority is compared with them, and no
            # replacement filename or path is derived. A changed argument set under the same key conflicts here.
            if saved["arguments_hash"] != arguments_hash:
                raise DomainError("idempotency_conflict", "This idempotency key was already used for a different file request")
            manifest, filename = parse_manifest(saved["source_binding"]), saved["filename"]
            self._check_frozen(ctx, chat, binding, manifest, uow)
        else:
            authorized = self.sources.authorize(ctx, args["source"], uow)
            manifest = {"version": MANIFEST_VERSION, "source": authorized.binding,
                        "destination": self._current_destination(ctx, chat, binding), "caption": caption,
                        "reply": self._reply(ctx, chat, args.get("reply_to"), uow)}
            filename = authorized.filename
        plan = PlanBuilder(ctx, OPERATION_NAME, args, "Send Telegram file", clock=self.bridge.clock, namespace="global",
                           affected_scope={"kind": "global"}).build()
        # Internal and never returned: the key, the argument digest and the frozen facts travel to the later phases here.
        return replace(plan, preview={**plain(plan.preview), PREPARED: {
            "key": key, "arguments_hash": arguments_hash, "manifest": manifest, "filename": filename}})

    def _prepared(self, ctx, plan):
        """The internal metadata ``prepare`` bound to this plan, checked against the operation, principal and arguments."""
        prepared = plain(plan.preview).get(PREPARED) if isinstance(plain(plan.preview), dict) else None
        if (plan.operation_name != OPERATION_NAME or plan.principal_id != ctx.principal_id or not isinstance(prepared, dict)
                or set(prepared) != {"key", "arguments_hash", "manifest", "filename"} or not isinstance(prepared["key"], str)
                or prepared["arguments_hash"] != canonical_hash(plan.arguments) or not isinstance(prepared["filename"], str)):
            raise DomainError("invalid_plan", "The file request was not prepared by this operation")
        manifest = parse_manifest(prepared["manifest"])
        if manifest["source"].get("principal_id") != ctx.principal_id or manifest["destination"]["agent_id"] != ctx.principal_id:
            raise DomainError("invalid_plan", "The file request was prepared for another agent")
        return {**prepared, "manifest": manifest}

    @contextmanager
    def _open(self, authorized):
        """The authorized file as a binary stream for the snapshot store, checked for change (change time included) after the
        last byte exactly as the source layer's own copy would."""
        with self.sources.open_source(authorized) as opened:
            yield opened.stream
            opened.assert_unchanged()

    def external(self, ctx, plan):
        prepared = self._prepared(ctx, plan)
        args, manifest = plain(plan.arguments), prepared["manifest"]
        with self.bridge.read() as uow:
            ctx, chat, binding = self._caller(ctx, uow)
            authorized = self._check_frozen(ctx, chat, binding, manifest, uow)
            saved = self.media.find(uow, ctx.principal_id, prepared["key"])
        if saved is None and authorized.filename != prepared["filename"]:
            raise DomainError("source_changed", "The source no longer has the file name it was prepared with")
        self.media.stage(
            principal_id=ctx.principal_id, request_key=prepared["key"], arguments_hash=prepared["arguments_hash"],
            source_binding=manifest, expected_sha256=args["expected_sha256"], filename=prepared["filename"],
            send_as=args["send_as"], opener=lambda: self._open(authorized), validate_snapshot=validate_snapshot)

    def apply(self, ctx, plan, uow):
        prepared = self._prepared(ctx, plan)
        args = plain(plan.arguments)
        ctx, chat, binding = self._caller(ctx, uow)
        payload = self.media.find(uow, ctx.principal_id, prepared["key"])
        if payload is None or payload["state"] not in ("ready", "attached"):
            raise DomainError("payload_unavailable", "The file for this request is not sealed; use a new idempotency key")
        manifest = parse_manifest(payload["source_binding"])
        if (payload["arguments_hash"] != prepared["arguments_hash"] or manifest != prepared["manifest"]
                or payload["filename"] != prepared["filename"] or payload["send_as"] != args["send_as"]
                or payload["expected_sha256"] != args["expected_sha256"]):
            raise DomainError("idempotency_conflict", "This idempotency key was already used for a different file request")
        self._check_frozen(ctx, chat, binding, manifest, uow)
        if payload["state"] == "attached":
            # Already queued by this very request: never enqueue it twice.
            return self.operations.view(uow, store.message(uow, payload["message_id"]), reused=True)
        reply = manifest["reply"]
        message, outbox, _ = enqueue_file(
            self.bridge, uow, chat, caption=manifest["caption"], purpose=args["purpose"], send_as=args["send_as"],
            payload_id=payload["payload_id"], agent_id=ctx.principal_id,
            reply_to=store.message(uow, reply["message_id"]) if reply else None)
        self.media.attach(uow, payload["payload_id"], message_id=message["id"], outbox_id=outbox["id"])
        self.bridge.wake()
        return self.operations.view(uow, store.message(uow, message["id"]))

    def replay_validator(self, ctx, args, replay, uow):
        """A saved receipt is returned only to the exact current Ordubashy and only while the source is still readable by it.
        Nothing is reopened, recopied or requeued."""
        data = plain(replay.data)
        message_id = data.get("message_id") if isinstance(data, dict) else None
        ctx, chat, binding = self._caller(ctx, uow)
        payload = self.media.for_message(uow, message_id) if isinstance(message_id, str) else None
        if payload is None or payload["principal_id"] != ctx.principal_id:
            raise DomainError("forbidden", "This receipt does not belong to your Telegram file requests")
        self.sources.recheck(ctx, parse_manifest(payload["source_binding"])["source"], uow)

    # ----- dispatch ---------------------------------------------------------------------------------------

    @contextmanager
    def _as_agent(self, uow, agent_id):
        """The current context of one agent, resolved inside the caller's unit without leaving it the acting principal."""
        previous = getattr(uow, "actor_id", None)
        try:
            yield self.app.current_context(InvocationContext(Principal(agent_id, "agent")), uow)
        finally:
            uow.actor_id = previous

    def _record(self, uow, row):
        """The attached payload, its manifest and its message for one ``agent_file`` entry, or a ``DomainError`` naming the damage."""
        envelope = row["payload_json"]
        payload_id = envelope.get("payload_id") if isinstance(envelope, dict) else None
        if (not isinstance(envelope, dict) or set(envelope) != {"version", "payload_id"} or type(envelope["version"]) is not int
                or envelope["version"] != 1 or not isinstance(payload_id, str)):
            raise _damaged("The queued attachment is not a version 1 payload reference")
        try:
            payload = self.media.inspect(uow, payload_id)
        except DomainError:
            raise DomainError("payload_missing", "The attachment record is missing") from None
        if payload["state"] != "attached":
            raise DomainError("payload_unavailable", f"The attachment snapshot is {payload['state']}")
        if ((payload["message_id"], payload["outbox_id"], payload["principal_id"], payload["send_as"])
                != (row["message_id"], row["id"], row["agent_id"], row["kind"])):
            raise _damaged("The delivery does not match its attachment record")
        manifest = parse_manifest(payload["source_binding"])
        message = store.message(uow, row["message_id"]) if row["message_id"] else None
        reply = manifest["reply"]
        if (message is None or message["direction"] != "outbound" or message["agent_id"] != row["agent_id"]
                or (message["chat_name"], message["chat_id"], message["message_thread_id"])
                != (row["chat_name"], row["chat_id"], row["message_thread_id"])
                or message["body"] != manifest["caption"] or manifest["destination"] != _destination(row)
                or (message["reply_to_id"], message["reply_to_telegram_id"])
                != ((reply["message_id"], reply["provider_message_id"]) if reply else (None, None))):
            raise _damaged("The delivery does not match its frozen file request")
        return payload, manifest, message

    def _verdict(self, uow, row):
        """``None`` when this queued or dispatching attachment may be sent now, else ``(state, code, message)``.

        ``cancelled``  the exact Ordubashy binding, its grant or the source authority is gone;
        ``failed``     the snapshot record, message or frozen manifest no longer agree;
        ``deferred``   a transient refusal (for example a document with an unfinished application): not a loss of authority.
        Database reads only."""
        bridge = self.bridge
        try:
            chat = bridge.config.agent_chat(row["agent_id"])
            if (chat is None or chat.name not in AGENT_CHATS or chat.destination != (row["chat_id"], row["message_thread_id"])
                    or chat.name != row["chat_name"]):
                raise DomainError("forbidden", "The agent is no longer bound to this chat and topic")
            bridge.check_agent_chat(uow, chat)
            if not self.operations._still_allowed(uow, row["agent_id"], OPERATION_NAME):
                raise DomainError("policy_denied", "The Telegram file grant was revoked")
        except DomainError as exc:
            return ("cancelled", "grant_revoked", exc.message)
        try:
            _, manifest, message = self._record(uow, row)
        except DomainError as exc:
            return ("failed", exc.code, exc.message)
        try:
            with self._as_agent(uow, row["agent_id"]) as ctx:
                self.sources.recheck(ctx, manifest["source"], uow)
        except DomainError as exc:
            return ("deferred" if exc.retryable else "cancelled", "source_revoked", exc.message)
        reply = manifest["reply"]
        if reply is not None and not self._reply_current(
                store.message(uow, reply["message_id"]), row["agent_id"], (row["chat_name"], row["chat_id"], row["message_thread_id"]),
                reply):
            return ("failed", "reply_changed", "The message to reply to is no longer the one this file was prepared for")
        return None

    def admit(self, uow, row):
        """Right before a send is claimed (inside the claim transaction): the verdict that closes it, or ``None`` to send."""
        verdict = self._verdict(uow, row)
        return None if verdict is None or verdict[0] == "deferred" else verdict

    def _binding(self, uow, row):
        """The Telegram binding and owner pin, judged now against the generation and fingerprint this send was queued under.

        The bridge's own authoritative check (enabled, installation, active owner, saved state) runs first and never adopts a new
        pin; the saved generation and fingerprint must then be exactly the row's, so a binding that moved and was restored (a new
        generation with the same destination) still closes this send. Any refusal is carried out as the bridge's own error."""
        try:
            current = self.bridge.check_binding(uow)
            if (current["generation"], current["fingerprint"]) != (row["config_generation"], row["binding_fingerprint"]):
                raise DomainError("binding_changed", "The Telegram binding or owner changed after this was accepted")
        except DomainError as exc:
            raise AuthorityMoved(exc.code, exc.message, fail=exc) from None

    @staticmethod
    def _enforce(verdict):
        if verdict is None:
            return
        state, code, message = verdict
        if state == "failed":
            raise DomainError(code, message)
        raise AuthorityMoved(code, message, retry=state == "deferred")

    @contextmanager
    def load(self, envelope):
        """Yield ``(filename, stream, size, caption, content_type)`` for one claimed attachment, from its sealed snapshot only.

        The snapshot's bytes are verified by ``open_payload``; the Telegram binding and the authority are judged again just
        before the stream is handed over, in a fresh read with no write unit open. The caption is the frozen manifest's (the
        message body was required to agree) and the content type is the validated image or document type recorded at sealing."""
        payload_id = envelope["payload_id"]
        with self.bridge.read() as uow:
            try:
                payload = self.media.inspect(uow, payload_id)
            except DomainError:
                raise DomainError("payload_missing", "The attachment record is missing") from None
            row = store.outbox(uow, payload["outbox_id"]) if payload["outbox_id"] else None
            if row is None or row["state"] != "dispatching" or row["payload_json"] != envelope:
                raise _damaged("The delivery does not match its attachment record")
            self._enforce(self._verdict(uow, row))
            caption = parse_manifest(payload["source_binding"])["caption"]
        with self.media.open_payload(payload_id) as opened:
            with self.bridge.read() as uow:
                current = store.outbox(uow, row["id"])
                if current is not None and current["state"] == "dispatching":
                    self._binding(uow, current)
                    self._enforce(self._verdict(uow, current))
                else:
                    self._enforce(("failed", "payload_mismatch", "The delivery is no longer being dispatched"))
            metadata = opened.metadata
            yield (metadata["filename"], opened.stream, metadata["size"], caption,
                   (metadata.get("validated") or {}).get("mime"))


__all__ = ["ARGUMENTS", "DESCRIPTION", "TelegramFiles", "parse_manifest"]
