"""A synthetic composed host for the ``telegram_send_file`` integration checks.

The host is the real composition (``build_instance``: assembled storage, the registered Ordubashy and a second Global agent
created through ``agents_create`` from the approved role archive, real policy, document store and file sources) with the real
``TelegramBridge`` of its runtime. Only the provider boundary is synthetic: a recording fake of the Bot API replaces the network,
and nothing is started (no poller, thread, token, model or service). Every path lives under the test's ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import io
import time
from types import SimpleNamespace

from qorqut_operations.contracts import InvocationOptions, new_id, utc_now

from bazarlyk.composition import locations
from bazarlyk.telegram import store
from bazarlyk.telegram.api import TelegramApiError
from bazarlyk.telegram.config import ChatBinding, TelegramConfig
from telegram_media_source_support import agent_context, build_host as build_source_host, register_document  # noqa: F401

CHAT_ID = -1001234567890
TOPIC = 11
SIGNOFFS, URGENT = TOPIC + 1, TOPIC + 2
OWNER_TELEGRAM_ID = 4242
ACTIVATION = "2026-01-01T00:00:00.000000Z"
PDF = b"%PDF-1.7 integration-marker-91c3 " + bytes(range(256)) * 8
OPERATION = "telegram_send_file"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def png(width=8, height=6, color=(200, 30, 30)):
    """A real, tiny, static PNG built with the pinned Pillow."""
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


class FakeApi:
    """The provider boundary: records every upload it is asked for and reads the whole stream, as the real transport would.

    ``script`` is a list of outcomes consumed per upload: ``None``/absent is a success, an exception instance is raised."""

    def __init__(self):
        self.uploads, self.texts, self.documents, self.updates, self.deleted, self.script, self._next = [], [], [], [], [], [], 5000

    def _answer(self, chat_id, thread):
        self._next += 1
        return {"message_id": self._next, "chat": {"id": chat_id}, "message_thread_id": thread, "is_topic_message": True}

    def _upload(self, mode, chat_id, filename, stream, size, caption, message_thread_id, reply_to_message_id, content_type):
        data = stream.read()
        record = SimpleNamespace(mode=mode, chat_id=chat_id, filename=filename, size=size, caption=caption, data=data,
                                 sha256=digest(data), thread=message_thread_id, reply_to=reply_to_message_id,
                                 content_type=content_type)
        self.uploads.append(record)
        if self.script:
            outcome = self.script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
        return self._answer(chat_id, message_thread_id)

    def send_document_stream(self, chat_id, filename, stream, size, caption=None, message_thread_id=None,
                             reply_to_message_id=None, content_type=None):
        return self._upload("document", chat_id, filename, stream, size, caption, message_thread_id, reply_to_message_id,
                            content_type)

    def send_photo_stream(self, chat_id, filename, stream, size, caption=None, message_thread_id=None,
                          reply_to_message_id=None, content_type=None):
        return self._upload("photo", chat_id, filename, stream, size, caption, message_thread_id, reply_to_message_id,
                            content_type)

    def send_message(self, chat_id, text, reply_to=None, message_thread_id=None):
        self.texts.append(SimpleNamespace(chat_id=chat_id, text=text, reply_to=reply_to, thread=message_thread_id))
        return self._answer(chat_id, message_thread_id)

    def send_document(self, chat_id, filename, data, caption=None, message_thread_id=None, reply_to_message_id=None):
        """The whole-bytes document path of the Bot API port."""
        self.documents.append(SimpleNamespace(chat_id=chat_id, filename=filename, data=data, caption=caption, thread=message_thread_id))
        return self._answer(chat_id, message_thread_id)

    def delete_message(self, chat_id, message_id):
        """The cleanup of a retired Signoff post: recorded, never a network call."""
        self.deleted.append((chat_id, message_id))

    def get_updates(self, offset, timeout):
        return [update for update in self.updates if update["update_id"] >= offset]

    def get_me(self):  # pragma: no cover - the bridge is never started here
        raise AssertionError("the bridge must not verify in these checks")


def uncertain():
    return TelegramApiError("uncertain", "The connection dropped after the upload was sent")


def build_host(tmp_path, *, telegram=True):
    """The composed host with the Telegram bridge configured for the registered Ordubashy (unless ``telegram`` is false)."""
    host = build_source_host(tmp_path)
    host.api = FakeApi()
    host.bridge = host.runtime.telegram
    host.files = host.runtime.telegram_operations.files
    host.state = tmp_path / "telegram-state"
    host.state.mkdir(mode=0o700)
    host.state.chmod(0o700)
    host.bridge.state_dir_override = host.state
    host.workspace = locations.agent_workspace(host.paths, host.ordubashy)
    if telegram:
        configure(host)
    return host


def chat_bindings(host, agent_id=None):
    return (ChatBinding("ordubashy", CHAT_ID, "supergroup", agent_id or host.ordubashy, TOPIC),
            ChatBinding("signoffs", CHAT_ID, "supergroup", None, TOPIC + 1),
            ChatBinding("urgent", CHAT_ID, "supergroup", None, TOPIC + 2))


def configure(host, *, agent_id=None):
    """Enable the bridge for ``agent_id`` (default: the registered Ordubashy), record its pinned state and mark it ready."""
    bridge = host.bridge
    with host.database.read() as uow:
        installation = dict(uow.repository("installation").get("service"))
    bridge.config = TelegramConfig(
        enabled=True, bot_id=777000111, installation_id=installation["installation_id"],
        owner_principal_id=installation["owner_principal_id"], owner_telegram_user_id=OWNER_TELEGRAM_ID, activation_at=ACTIVATION,
        chats=chat_bindings(host, agent_id))
    with bridge.write("test.configure") as uow:
        if store.get_state(uow) is None:
            store.ensure_state(uow, bridge.config, installation, now=bridge.now())
    bridge.api = host.api
    bridge._ready.set()
    return bridge.config


def rebind(host, **changes):
    """A new configuration generation: the same bot and owner, the chat bindings changed (as an operator edit would)."""
    bridge = host.bridge
    current = bridge.config
    chats = changes.pop("chats", current.chats)
    bridge.config = TelegramConfig(**{**current.__dict__, **changes, "chats": chats})
    with host.database.read() as uow:
        installation = dict(uow.repository("installation").get("service"))
    with bridge.write("test.rebind") as uow:
        store.ensure_state(uow, bridge.config, installation, now=bridge.now())
    return bridge.config


def write_file(host, relative, data=PDF):
    """A file in the registered Ordubashy's own workspace (``relative`` below it)."""
    target = host.workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def send_arguments(source, data=PDF, *, send_as="document", purpose="update", **extra):
    return {"source": source, "expected_sha256": digest(data), "send_as": send_as, "purpose": purpose, **extra}


def workspace_source(path):
    return {"type": "workspace", "path": path}


def invoke(host, arguments, *, key=None, agent=None, name=OPERATION, ctx=None):
    key = key or new_id()
    ctx = ctx or agent_context(host, agent or host.ordubashy)
    return host.runtime.application.invoke(ctx, name, arguments, InvocationOptions(idempotency_key=key))


def sql(host, statement, *parameters):
    with host.database.read() as uow:
        return [dict(row) for row in uow.connection.execute(statement, parameters)]


def write_sql(host, statement, *parameters):
    with host.database.transaction() as uow:
        uow.connection.execute(statement, parameters)


def payloads(host):
    return sql(host, "SELECT * FROM telegram_file_payloads ORDER BY created_at, rowid")


def outbox(host):
    return sql(host, "SELECT * FROM telegram_outbox ORDER BY created_at, rowid")


def messages(host):
    return sql(host, "SELECT * FROM telegram_messages ORDER BY created_at, rowid")


def media_files(host):
    root = host.state / "outbound-media"
    return sorted(item.name for item in root.iterdir()) if root.exists() else []


def dispatch(host):
    """One upload pass of the real dispatcher against the fake provider. Returns whether a delivery was attempted."""
    return host.bridge.dispatcher.dispatch_one(uploads=True)


def counts(host):
    return {name: len(sql(host, f"SELECT 1 FROM {name}")) for name in ("telegram_messages", "telegram_outbox", "telegram_file_payloads")}


def accepted_inbound(host, text="please send the report", provider_id=9001):
    """An owner message recorded in Ordubashy's topic with a provider ID (a valid reply target)."""
    with host.bridge.write("test.inbound") as uow:
        chat = host.bridge.config.chat("ordubashy")
        return store.insert_message(uow, direction="inbound", chat=chat, purpose="owner", body=text, now=host.bridge.now(),
                                    agent_id=host.ordubashy, telegram_message_id=provider_id)


# ----- the Signoff and owner-input flows -------------------------------------------------------------


def heal(host):
    """Ready again after a case fail-closed the bridge (the readiness handshake itself needs a real provider)."""
    for component in ("binding", "startup", "verification", "service", "worker", "config", "media"):
        host.bridge.recover_component(component)
    host.bridge._ready.set()


def pump(host, limit=40):
    """Control passes until the bridge reports itself idle (the real cycle: every step, in its own order)."""
    for _ in range(limit):
        if host.bridge.cycle() >= host.bridge.tick:
            return
    raise AssertionError("the bridge never went idle")


def poll(host):
    """One polling pass over the updates pushed to the fake provider: store, then route, as the running bridge does."""
    from bazarlyk.telegram import poller

    return poller.poll_once(host.bridge)


_update_ids = iter(range(10_000, 1_000_000))


def push_owner_message(host, text, *, thread=TOPIC, reply_to=None, message_id=None, sender=OWNER_TELEGRAM_ID):
    """A Telegram text update from ``sender`` in a topic (the exact owner by default), optionally replying to a provider message."""
    update_id = next(_update_ids)
    message = {"message_id": message_id or update_id, "date": int(time.time()), "chat": {"id": CHAT_ID, "type": "supergroup"},
               "from": {"id": sender, "is_bot": False}, "message_thread_id": thread, "is_topic_message": True, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    host.api.updates.append({"update_id": update_id, "message": message})
    return update_id


class FakeArbiter:
    """The native terminal boundary: it claims an input the way the shared arbiter does (running the checkpoint that commits the
    delivery intent) and records what would have been typed. Nothing starts, types or reaches a model."""

    def __init__(self, outcome="submitted"):
        self.outcome, self.typed = outcome, []

    def submit(self, request, *, on_checkpoint):
        from bazarlyk.native_chat.contracts import STATE_SUBMITTED, STATE_UNCERTAIN

        on_checkpoint()
        self.typed.append(SimpleNamespace(agent_id=request.agent_id, source_key=request.source_key, text=request.text,
                                          sensitive=request.sensitive))
        state = STATE_SUBMITTED if self.outcome == "submitted" else STATE_UNCERTAIN
        return SimpleNamespace(state=state, reason=None if self.outcome == "submitted" else "terminal_unconfirmed",
                               evidence={"typed": True})


def attach_arbiter(host, monkeypatch, arbiter=None):
    arbiter = arbiter or FakeArbiter()
    monkeypatch.setattr(host.runtime, "native_chat", SimpleNamespace(arbiter=arbiter), raising=False)
    return arbiter


def ask_owner(host, request_key="which-direction", title="Which direction?"):
    """A canonical input-request Signoff proposed by the registered Ordubashy through ``signoffs_input_create``."""
    form = {"title": title, "explanation": "Pick the direction and tell me why.", "kind": "question", "selection_mode": "none",
            "response_policy": "owner_only"}
    result = invoke(host, {"scope": "global", "request_key": request_key, "form": form, "attention": {"notify": True, "blocking": True, "reason": "The agent cannot continue without the answer"}},
                    name="signoffs_input_create")
    assert result.status == "ok", result.error
    host.bridge.signoff_bridge.service.notifications.tick()          # the owner's delivery is made by the service's own scheduled step
    return result.data["signoff"]
