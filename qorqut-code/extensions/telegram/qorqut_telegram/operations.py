"""Native Telegram tools. Closed schemas, no chat, token, agent or path fields, and the same policy/idempotency path as every
other operation.

``telegram_send``, ``telegram_send_file`` (``media_operations``), ``telegram_history`` and ``telegram_delivery_get`` serve only the
exact configured active Global Ordubashy agent, bound to its own chat; no other agent has a Telegram channel. ``telegram_notify`` serves trusted registered
workers and the owner and always targets Urgent Notifications. ``telegram_status`` and ``telegram_delivery_resolve`` are
owner-only HTTP routes and never agent tools. Grants are rechecked when the effect is applied and again right before it is
dispatched.
"""

from __future__ import annotations

from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.application.plans import PlanBuilder, schema
from . import notify as urgent
from . import store
from .bridge import TelegramBridge
from .config import AGENT_CHATS
from .media_operations import TelegramFiles
from .outbox import enqueue_text

UUID = {"type": "string", "format": "uuid"}
SECRET_MAX_SECONDS = 600
TEXT_DEFAULT_SECONDS = 3600
HISTORY_LIMIT = 10
COMMON = dict(supports_signoff=False, scope_kinds=frozenset({"global"}), global_capability="read_global_context",
              resolve_namespace=lambda ctx, args: "global")
SEND_SCHEMA = schema({
    "text": {"type": "string", "minLength": 1, "maxLength": store.MAX_UNICODE_CHARS,
             "description": "Plain text, at most 4000 characters. No formatting is interpreted."},
    "purpose": {"enum": ["reply", "blocking_input", "update"]},
    "reply_to": {**UUID, "description": "Optional local message_id from telegram_history, in your own chat."},
    "request_key": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$", "maxLength": 80},
    "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    "input_mode": {"enum": ["text", "secret", "screen_action"]},
    "expires_in_seconds": {"type": "integer", "minimum": 60, "maximum": 3600},
}, ("text", "purpose"))
SEND_DESCRIPTION = (
    "Send a plain-text message to the owner in your own Telegram chat; you cannot choose the chat. purpose=reply answers the "
    "owner, update reports progress, blocking_input asks for something you cannot continue without and requires request_key, "
    "reason and input_mode (text, secret or screen_action; secret requests expire within 10 minutes, and no secret is ever "
    "returned to you in history). Reusing a pending request_key returns the existing request. The result is the local "
    "message_id and its truthful delivery state (queued at return; read telegram_delivery_get for sent or uncertain). A "
    "Telegram message is scoped owner input, not blanket authorization; use Signoffs for approvals and durable questions.")


REQUEST_GUIDANCE = {
    "text": "Reply to this message with your answer.",
    "secret": "Reply to this message with the requested code. It is kept only briefly and typed to the agent; Telegram chats are "
              "not end-to-end encrypted.",
    "screen_action": 'Do the action on the screen, then reply "done" to this message.',
}


def render_request(text, request, expires_at):
    """What the owner sees for a blocking request: the agent's words, why it is needed, when it expires and how to answer."""
    expiry = store.parse(expires_at).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join([text.strip(), "", f"Why it is needed: {request['reason'].strip()}", f"Expires: {expiry}",
                      REQUEST_GUIDANCE[request["input_mode"]]])


def _kind(row):
    return "owner" if row["direction"] == "inbound" else "you"


class TelegramOperations:
    def __init__(self, runtime):
        self.runtime = runtime
        self.app = runtime.application

    @property
    def bridge(self) -> TelegramBridge:
        return self.runtime.extensions["telegram"].bridge

    def register(self):
        registry = self.runtime.registry
        owner = dict(COMMON, owner_only=True, exposure=frozenset({"dashboard"}))
        registry.register(OperationDescriptor("telegram_send", SEND_SCHEMA, **COMMON, exposure=frozenset({"mcp"}),
            description=SEND_DESCRIPTION, prepare=self.prepare_send, apply=self.apply_send))
        registry.register(OperationDescriptor("telegram_history", schema({"limit": {"type": "integer", "minimum": 1, "maximum": HISTORY_LIMIT}}),
            **COMMON, exposure=frozenset({"mcp"}), effect_kind="read",
            description="Read the last messages (default and maximum 10, oldest first) exchanged with the owner in your own Telegram chat since "
                        "activation. Secret replies are redacted. A file or photo shows its attachment (filename, send_as, size, sha256); "
                        "one without a caption shows File: or Photo: and its filename as text. "
                        "Signoffs and other chats are never included.",
            read=self.history))
        registry.register(OperationDescriptor("telegram_delivery_get", schema({"message_id": UUID, "outbox_id": UUID}), **COMMON,
            exposure=frozenset({"mcp", "dashboard"}), effect_kind="read",
            description="Read delivery receipts: one message by its local message_id, or your ten latest outbound messages. States are "
                        "queued, dispatching, uncertain (send may or may not have happened; never assume either), sent, failed and cancelled. "
                        "A file or photo also shows its attachment (filename, send_as, size, sha256). "
                        "The owner view also lists every delivery of a message (its send and any edits) and accepts an outbox_id.",
            read=self.delivery_get))
        registry.register(OperationDescriptor("telegram_notify", schema(urgent.INPUT_SCHEMA, urgent.REQUIRED), **COMMON,
            exposure=frozenset({"dashboard", "worker"}),
            description="Post or update one actionable notification in Urgent Notifications (what happened, impact, needed action). "
                        "Repeating an identical active event is suppressed; a change or state=resolved updates the known post.",
            prepare=self.prepare_notify, apply=self.apply_notify))
        registry.register(OperationDescriptor("telegram_status", schema(), **owner, effect_kind="read",
            description="Owner status of the Telegram bridge: health, cursor, outbox, uncertain sends, cleanup failures and queued inputs.",
            read=self.status))
        registry.register(OperationDescriptor("telegram_delivery_resolve", schema({
            "outbox_id": UUID, "action": {"enum": ["mark_delivered", "retry"]},
            "provider_message_id": {"type": "integer", "minimum": 1},
            "expected_revision": {"type": "integer", "minimum": 1, "description": "The receipt_revision you inspected."}},
            ("outbox_id", "action", "expected_revision")), **owner,
            description="Resolve one uncertain Telegram send or edit, named by its exact outbox_id from status or telegram_delivery_get. "
                        "mark_delivered records the exact Telegram message ID the owner saw (for an edit, the ID of the message that was edited); "
                        "retry queues it again under its saved target and binding and can duplicate a send, and an edit that a newer update "
                        "of its event already replaced is closed instead of sent. expected_revision is the receipt_revision from "
                        "telegram_delivery_get or status; a changed receipt is refused. Never changes a Signoff decision.",
            prepare=self.prepare_resolve, apply=self.apply_resolve))
        self.bridge.admit_hooks["agent"] = self.admit_agent
        self.bridge.admit_hooks["notify"] = self.admit_notify
        # The file and photo send registers its own descriptor and the agent_file admission and loader hooks.
        self.files = TelegramFiles(self).register(COMMON)
        self.app.telegram_operations = self
        return self

    # ----- eligibility ---------------------------------------------------------------------------------

    def agent_chat(self, ctx, uow):
        """The caller's own chat, or an error. Delegated identities and other roles or agents are refused."""
        ctx = self.app.current_context(ctx, uow)
        if ctx.principal_kind != "agent" or ctx.delegation_assignment_id is not None:
            raise DomainError("forbidden", "Telegram tools are limited to the configured Global agents themselves")
        config = self.bridge.config
        if not config.enabled:
            raise DomainError("operation_unavailable", "The Telegram bridge is not enabled")
        chat = config.agent_chat(ctx.principal_id)
        if chat is None or chat.name not in AGENT_CHATS:
            raise DomainError("forbidden", "This agent has no Telegram chat")
        # Reads and sends alike stop at an owner or installation mismatch until the operator reconfigures.
        self.bridge.check_binding(uow)
        try:
            self.bridge.check_agent_chat(uow, chat)
        except DomainError as exc:
            raise DomainError("forbidden", exc.message) from None
        return ctx, chat

    # ----- telegram_send -------------------------------------------------------------------------------

    def validate_send(self, ctx, args, uow):
        ctx, chat = self.agent_chat(ctx, uow)
        text = args["text"]
        if not text.strip():
            raise DomainError("validation_error", "A Telegram message needs text")
        if len(text) > store.MAX_UNICODE_CHARS or store.utf16_units(text) > store.MAX_UTF16_UNITS:
            raise DomainError("validation_error", "Telegram text exceeds 4000 characters or 4096 UTF-16 units")
        request = None
        present = {name for name in ("request_key", "reason", "input_mode", "expires_in_seconds") if name in args}
        if args["purpose"] == "blocking_input":
            missing = [name for name in ("request_key", "reason", "input_mode") if name not in args]
            if missing:
                raise DomainError("validation_error", "blocking_input requires " + ", ".join(missing))
            secret = args["input_mode"] == "secret"
            lifetime = args.get("expires_in_seconds", SECRET_MAX_SECONDS if secret else TEXT_DEFAULT_SECONDS)
            if secret and lifetime > SECRET_MAX_SECONDS:
                raise DomainError("validation_error", "A secret request expires within 10 minutes")
            request = {"request_key": args["request_key"], "input_mode": args["input_mode"], "reason": args["reason"],
                       "lifetime": lifetime}
            longest = render_request(text, request, "2000-01-01T00:00:00.000000Z")
            if len(longest) > store.MAX_UNICODE_CHARS or store.utf16_units(longest) > store.MAX_UTF16_UNITS:
                raise DomainError("validation_error", "The request with its reason and instructions exceeds 4000 characters; shorten it")
        elif present:
            raise DomainError("validation_error", "Request fields are only valid with purpose blocking_input")
        reply = None
        if "reply_to" in args:
            reply = store.message(uow, args["reply_to"])
            if not store.in_destination(reply, chat):
                raise DomainError("not_found", "The reply target is unavailable in your chat")
        return ctx, chat, request, reply

    def prepare_send(self, ctx, args, options, uow):
        ctx, *_ = self.validate_send(ctx, args, uow)
        return PlanBuilder(ctx, "telegram_send", args, "Send Telegram message", clock=self.bridge.clock,
                           namespace="global", affected_scope={"kind": "global"}).build()

    def apply_send(self, ctx, plan, uow):
        args = plain(plan.arguments)
        ctx, chat, request, reply = self.validate_send(ctx, args, uow)
        bridge, now = self.bridge, self.bridge.now()
        reused = False
        if request is not None:
            self.expire_stale(uow, chat, now)
            existing = store.pending_request(uow, chat, request["request_key"])
            if existing is not None:
                if (existing["body"], existing["request_reason"], existing["input_mode"]) != (
                        render_request(args["text"], request, existing["request_expires_at"]), request["reason"], request["input_mode"]):
                    raise DomainError("revision_conflict", "A pending request with this key differs; use a new request_key or wait for it to end")
                return self.view(uow, existing, reused=True)
            if request["input_mode"] == "secret":
                other = uow.connection.execute(
                    "SELECT 1 FROM telegram_messages WHERE chat_name=? AND chat_id=? AND message_thread_id IS ? "
                    "AND request_state='pending' AND input_mode='secret'",
                    (chat.name, chat.chat_id, chat.message_thread_id)).fetchone()
                if other:
                    raise DomainError("revision_conflict", "One secret request may be outstanding in a chat")
            request = {**request, "expires_at": store.plus(now, request["lifetime"])}
        text = render_request(args["text"], request, request["expires_at"]) if request is not None else args["text"]
        message, outbox, _ = enqueue_text(bridge, uow, chat, text=text, purpose=args["purpose"], source_kind="agent",
                                          source_key=store.new_id(), agent_id=ctx.principal_id, reply_to=reply, request=request)
        bridge.wake()
        return self.view(uow, message, reused=reused)

    @staticmethod
    def expire_stale(uow, chat, now):
        """Close requests whose time ran out before the periodic sweep saw them, so a new one is never blocked by a dead one."""
        for row in [dict(r) for r in uow.connection.execute(
                "SELECT * FROM telegram_messages WHERE chat_name=? AND chat_id=? AND message_thread_id IS ? "
                "AND request_state='pending' AND request_expires_at<=?", (chat.name, chat.chat_id, chat.message_thread_id, now))]:
            store.close_request(uow, row, "expired", row["request_expires_at"])
            if row["input_mode"] == "secret":
                store.enqueue_cleanup(uow, row["id"], "secret_request_expired", now=now)

    # ----- reads ---------------------------------------------------------------------------------------

    def view(self, uow, message, *, reused=False, owner=False):
        outbox = store.outbox(uow, message["outbox_id"]) if message["outbox_id"] else None
        result = {"message_id": message["id"], "purpose": message["purpose"], "from": _kind(message),
                  "state": outbox["state"] if outbox else ("received" if message["direction"] == "inbound" else "queued"),
                  "created_at": message["created_at"]}
        if outbox is not None:
            result.update(attempts=outbox["attempts"], error_code=outbox["error_code"], sent_at=outbox["sent_at"])
        attachment = store.attachment_of(uow, message["id"]) if message["direction"] == "outbound" else None
        if attachment is not None:
            result["attachment"] = attachment
            if message["reply_to_id"]:
                result["reply_to"] = message["reply_to_id"]
        if message["request_key"]:
            result["request"] = {"request_key": message["request_key"], "input_mode": message["input_mode"],
                                 "state": message["request_state"], "expires_at": message["request_expires_at"]}
        if reused:
            result["reused"] = True
        if owner:
            result.update(chat=message["chat_name"], message_thread_id=message["message_thread_id"], agent_id=message["agent_id"],
                          outbox_id=message["outbox_id"],
                          receipt_revision=(outbox or {}).get("revision"),
                          provider_message_id=(outbox or {}).get("provider_message_id") or message["telegram_message_id"],
                          error_message=(outbox or {}).get("error_message"),
                          deliveries=[{"outbox_id": row["id"], "kind": row["kind"], "state": row["state"],
                                       "receipt_revision": row["revision"], "event_sequence": row["event_sequence"],
                                       "error_code": row["error_code"], "provider_message_id": row["provider_message_id"],
                                       "created_at": row["created_at"]} for row in uow.connection.execute(
                              "SELECT * FROM telegram_outbox WHERE message_id=? ORDER BY created_at, rowid", (message["id"],))])
        return result

    def history(self, ctx, args, options, uow):
        ctx, chat = self.agent_chat(ctx, uow)
        state = store.get_state(uow)
        activation = state["activation_at"] if state else self.bridge.config.activation_at
        rows = store.history(uow, chat, ctx.principal_id, activation_at=activation, limit=args.get("limit", HISTORY_LIMIT))
        items = []
        for row in rows:
            attachment = store.attachment_view(row) if row["sha256"] else None
            item = {"message_id": row["id"], "from": _kind(row), "purpose": row["purpose"], "at": row["occurred_at"],
                    "text": store.REDACTED_BODY if row["body_redacted"] or row["body"] is None else store.display_text(row["body"], attachment),
                    "reply_to": row["reply_to_id"]}
            if attachment is not None:
                item["attachment"] = attachment
            if row["direction"] == "outbound":
                item["delivery_state"] = row.get("delivery_state")
            if row["request_key"]:
                item["request"] = {"request_key": row["request_key"], "input_mode": row["input_mode"], "state": row["request_state"]}
            items.append(item)
        return {"items": items}

    def delivery_get(self, ctx, args, options, uow):
        ctx = self.app.current_context(ctx, uow)
        owner = ctx.principal_kind == "human"
        if owner:
            installation = uow.repository("installation").get("service")
            if not installation or installation["owner_principal_id"] != ctx.principal_id:
                raise DomainError("forbidden", "Only the installation owner may read every Telegram receipt")
        else:
            ctx, chat = self.agent_chat(ctx, uow)
        if "outbox_id" in args and not owner:
            raise DomainError("forbidden", "Only the installation owner may read a delivery by its outbox_id")
        if "outbox_id" in args and "message_id" in args:
            raise DomainError("validation_error", "Give a message_id or an outbox_id, not both")
        if "outbox_id" in args:
            delivery = store.outbox(uow, args["outbox_id"])
            if delivery is None or not delivery["message_id"]:
                raise DomainError("not_found", "No such Telegram message")
            args = {"message_id": delivery["message_id"]}
        if "message_id" in args:
            row = store.message(uow, args["message_id"])
            if row is None or row["direction"] != "outbound" or (not owner and row["agent_id"] != ctx.principal_id):
                raise DomainError("not_found", "No such Telegram message")
            rows = [row]
        else:
            if owner:
                found = uow.connection.execute("SELECT * FROM telegram_messages WHERE direction='outbound' "
                                               "ORDER BY created_at DESC, rowid DESC LIMIT 10").fetchall()
            else:
                found = uow.connection.execute("SELECT * FROM telegram_messages WHERE direction='outbound' AND agent_id=? "
                                               "ORDER BY created_at DESC, rowid DESC LIMIT 10", (ctx.principal_id,)).fetchall()
            rows = [dict(row) for row in found]
        return {"items": [self.view(uow, row, owner=owner) for row in rows]}

    def status(self, ctx, args, options, uow):
        return self.bridge.status()

    # ----- telegram_notify -----------------------------------------------------------------------------

    def notify_producer(self, ctx):
        if ctx.principal_kind == "agent" or ctx.delegation_assignment_id is not None:
            raise DomainError("forbidden", "Agents notify the owner through their own Telegram chat")
        return "owner" if ctx.principal_kind == "human" else f"principal:{ctx.principal_id}"

    def prepare_notify(self, ctx, args, options, uow):
        ctx = self.app.current_context(ctx, uow)
        self.notify_producer(ctx)
        if not self.bridge.config.enabled:
            raise DomainError("operation_unavailable", "The Telegram bridge is not enabled")
        clean, state = urgent.validate(args)
        urgent.render(clean, state)
        return PlanBuilder(ctx, "telegram_notify", args, "Post urgent Telegram notification", clock=self.bridge.clock,
                           namespace="global", affected_scope={"kind": "global"}).build()

    def apply_notify(self, ctx, plan, uow):
        ctx = self.app.current_context(ctx, uow)
        producer = self.notify_producer(ctx)
        outbox_producer = producer if producer.startswith("principal:") else None
        result = urgent.notify(self.bridge, uow, producer, plain(plan.arguments), gateway_producer=outbox_producer)
        return result

    # ----- owner reconciliation ------------------------------------------------------------------------

    def prepare_resolve(self, ctx, args, options, uow):
        ctx = self.app.current_context(ctx, uow)
        self.resolvable(args, uow)
        return PlanBuilder(ctx, "telegram_delivery_resolve", args, "Resolve a Telegram delivery", clock=self.bridge.clock,
                           namespace="global", affected_scope={"kind": "global"}).build()

    @staticmethod
    def resolvable(args, uow):
        """The exact uncertain delivery (a send or an edit) named by ``outbox_id`` at the receipt revision the owner inspected."""
        outbox = store.outbox(uow, args["outbox_id"])
        message = store.message(uow, outbox["message_id"]) if outbox and outbox["message_id"] else None
        if outbox is None or message is None or message["direction"] != "outbound":
            raise DomainError("not_found", "No such Telegram send")
        if outbox["revision"] != args["expected_revision"]:
            raise DomainError("revision_conflict", "The delivery receipt changed since you inspected it; read it again")
        if args["action"] == "mark_delivered":
            if outbox["state"] != "uncertain":
                raise DomainError("invalid_transition", "Only an uncertain send can be marked delivered")
            if "provider_message_id" not in args:
                raise DomainError("validation_error", "mark_delivered needs the exact Telegram message ID")
            if outbox["kind"] == "edit" and args["provider_message_id"] != message["telegram_message_id"]:
                raise DomainError("validation_error", "For an edit, give the Telegram message ID of the message that was edited")
        else:
            if outbox["state"] != "uncertain":
                raise DomainError("invalid_transition", "Only an uncertain send can be retried")
            if "provider_message_id" in args:
                raise DomainError("validation_error", "retry takes no provider message ID")
        return message, outbox

    def apply_resolve(self, ctx, plan, uow):
        ctx = self.app.current_context(ctx, uow)
        args = plain(plan.arguments)
        message, outbox = self.resolvable(args, uow)
        bridge, now = self.bridge, self.bridge.now()
        if args["action"] == "mark_delivered":
            provider = args["provider_message_id"]
            known = store.message_by_provider(uow, outbox["chat_id"], provider)
            if known is not None and known["id"] != message["id"] and outbox["kind"] != "edit":
                raise DomainError("validation_error", "That Telegram message ID already belongs to another known message")
            store.set_outbox(uow, outbox, state="sent", provider_message_id=provider, sent_at=now, error_code="owner_confirmed",
                             error_message=None, resolved_by=ctx.principal_id, resolved_at=now)
            if outbox["kind"] != "edit" and message["telegram_message_id"] is None:
                store.update_message(uow, message, telegram_message_id=provider)
            elif outbox["kind"] == "edit" and not self._replaced_by_newer_edit(uow, outbox):
                # The confirmed payload is now what the owner saw. A newer edit that already reached Telegram keeps its content.
                store.update_message(uow, message, body=outbox["payload_json"]["text"])
            # The owner's confirmation is the receipt: source-kind follow-up (for example activating a Signoff post) runs as usual.
            hook = bridge.sent_hooks.get(outbox["source_kind"])
            if hook is not None:
                hook(uow, store.outbox(uow, outbox["id"]), provider)
        else:
            # The frozen target and binding stay as saved: dispatch rechecks them and fails closed if they changed.
            store.set_outbox(uow, outbox, state="queued", attempts=0, next_attempt_at=now, error_code=None, error_message=None,
                             resolved_by=ctx.principal_id, resolved_at=now)
            bridge.wake()
        return self.view(uow, store.message(uow, message["id"]), owner=True)

    @staticmethod
    def _replaced_by_newer_edit(uow, outbox):
        """Whether a later edit of the same message was already delivered, so this older receipt must not claim the content."""
        if outbox["event_sequence"] is not None:
            found = uow.connection.execute(
                "SELECT 1 FROM telegram_outbox WHERE message_id=? AND kind='edit' AND state='sent' AND event_sequence>? AND id!=?",
                (outbox["message_id"], outbox["event_sequence"], outbox["id"])).fetchone()
        else:
            found = uow.connection.execute(
                "SELECT 1 FROM telegram_outbox WHERE message_id=? AND kind='edit' AND state='sent' AND created_at>? AND id!=?",
                (outbox["message_id"], outbox["created_at"], outbox["id"])).fetchone()
        return found is not None

    # ----- dispatch-time grant refresh -----------------------------------------------------------------

    def _still_allowed(self, uow, principal_id, operation):
        policy = self.app.policy
        context = policy.context_for(principal_id, business_id=None, review_business_id=None, uow=uow)
        return policy.resolve(context, self.runtime.registry.get(operation), (), uow).mode == "allow"

    def admit_agent(self, uow, row):
        """Right before a send: the agent is still the exact bound active Global agent and still holds the grant."""
        try:
            chat = self.bridge.config.agent_chat(row["agent_id"])
            if chat is None or chat.destination != (row["chat_id"], row["message_thread_id"]) or chat.name != row["chat_name"]:
                raise DomainError("forbidden", "The agent is no longer bound to this chat and topic")
            self.bridge.check_agent_chat(uow, chat)
            if not self._still_allowed(uow, row["agent_id"], "telegram_send"):
                raise DomainError("policy_denied", "The Telegram grant was revoked")
        except DomainError as exc:
            return ("cancelled", "grant_revoked", exc.message)
        return None

    def admit_notify(self, uow, row):
        producer = row["producer_id"] or ""
        if producer.startswith("principal:"):
            try:
                if not self._still_allowed(uow, producer.removeprefix("principal:"), "telegram_notify"):
                    raise DomainError("policy_denied", "The notification grant was revoked")
            except DomainError as exc:
                return ("cancelled", "grant_revoked", exc.message)
        return urgent.admit(uow, row)
