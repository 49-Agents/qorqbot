"""Shared fixtures for the Telegram bridge tests: a fake Bot API, a fake clock and an isolated installation."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import random
from types import SimpleNamespace

from bazarlyk.application.engine import Application
from qorqut_operations.contracts import new_id
from bazarlyk.telegram.api import BotApi, TelegramApiError
from bazarlyk.telegram.bridge import TelegramBridge
from foundation_support import HUMAN, database_at

OWNER_TG = 1001
BOT_ID = 200200200
TOKEN = f"{BOT_ID}:" + "A" * 35
CHAT_IDS = {"ordubashy": -1001, "founder": -1002, "marketing": -1003, "signoffs": -1004, "urgent": -1005}
# Forum mode: one private supergroup, one topic per chat (synthetic IDs).
GROUP_ID = -100900100
TOPIC_IDS = {"ordubashy": 11, "founder": 12, "marketing": 13, "signoffs": 14, "urgent": 15}
TOPIC_CHAT_IDS = {name: GROUP_ID for name in TOPIC_IDS}
SYNTHETIC_PINS = ("00000000-0000-4000-8000-0000000000b1", "00000000-0000-4000-8000-0000000000b2")
ROLES = {"ordubashy": "ordubashy", "founder": "founder", "marketing": "global-marketing"}
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, start=START):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds=0, **kwargs):
        self.now += timedelta(seconds=seconds, **kwargs)
        return self.now


class FakeBotApi(BotApi):
    """Implements exactly the port; it has no webhook mutation methods, so any attempt to install/remove one raises."""

    def __init__(self, chat_ids=CHAT_IDS, forum_chats=()):
        self.calls, self.sent, self.deleted, self.edited, self.documents = [], [], [], [], []
        self.forum_chats = set(forum_chats)
        # Mutates the Message a topic send returns, to simulate a provider that lands it elsewhere or answers incompletely.
        self.response_mutator = None
        self.updates, self.failures, self.next_message_id = [], {}, 500
        self.webhook_url = ""
        self.bot = {"id": BOT_ID, "is_bot": True}
        self.chat_ids = dict(chat_ids)
        self.chat_overrides = {}
        self.member_overrides = {}
        self.poll_gate = None
        self.polled_offsets = []

    def abort_polling(self):
        if self.poll_gate is not None:
            self.poll_gate.set()

    def script(self, method, *errors):
        self.failures.setdefault(method, []).extend(errors)

    def _maybe_fail(self, method):
        self.calls.append(method)
        queue = self.failures.get(method)
        if queue:
            error = queue.pop(0)
            if error is not None:
                raise error

    def push(self, update):
        self.updates.append(update)

    def get_me(self):
        self._maybe_fail("get_me")
        return self.bot

    def get_webhook_info(self):
        self._maybe_fail("get_webhook_info")
        return {"url": self.webhook_url}

    def get_chat(self, chat_id):
        self._maybe_fail("get_chat")
        name = next((n for n, c in self.chat_ids.items() if c == chat_id), None)
        info = {"id": chat_id, "type": "supergroup"}
        if chat_id in self.forum_chats:
            info["is_forum"] = True
        info.update(self.chat_overrides.get(chat_id, {}))
        if name is None:
            raise TelegramApiError("refused", "Bad Request: chat not found", error_code=400)
        return info

    def get_chat_member(self, chat_id, user_id):
        self._maybe_fail("get_chat_member")
        if (chat_id, user_id) in self.member_overrides:
            return self.member_overrides[(chat_id, user_id)]
        if user_id == BOT_ID:
            return {"status": "administrator", "can_delete_messages": True, "can_post_messages": True}
        return {"status": "member"}

    def get_updates(self, offset, timeout):
        self._maybe_fail("get_updates")
        self.polled_offsets.append(offset)
        if self.poll_gate is not None:
            self.poll_gate.wait(10)
        self.updates = [u for u in self.updates if u.get("update_id", 0) >= offset]
        return [dict(u) for u in self.updates]

    def _returned(self, chat_id, message_thread_id):
        """What the real Bot API returns: a topic send names its chat and topic; a whole-chat send keeps the minimal contract."""
        result = {"message_id": self.next_message_id}
        if message_thread_id is not None:
            result.update(chat={"id": chat_id, "type": "supergroup"}, message_thread_id=message_thread_id, is_topic_message=True)
            if self.response_mutator is not None:
                result = self.response_mutator(result)
        return result

    def send_message(self, chat_id, text, reply_to_message_id=None, message_thread_id=None):
        self._maybe_fail("send_message")
        self.next_message_id += 1
        record = {"chat_id": chat_id, "text": text, "reply_to": reply_to_message_id, "message_id": self.next_message_id}
        if message_thread_id is not None:
            record["message_thread_id"] = message_thread_id
        self.sent.append(record)
        return self._returned(chat_id, message_thread_id)

    def send_document(self, chat_id, filename, data, caption=None, message_thread_id=None):
        self._maybe_fail("send_document")
        self.next_message_id += 1
        record = {"chat_id": chat_id, "filename": filename, "data": data, "caption": caption, "message_id": self.next_message_id}
        if message_thread_id is not None:
            record["message_thread_id"] = message_thread_id
        self.documents.append(record)
        return self._returned(chat_id, message_thread_id)

    def edit_message_text(self, chat_id, message_id, text):
        self._maybe_fail("edit_message_text")
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return {"message_id": message_id}

    def delete_message(self, chat_id, message_id):
        self._maybe_fail("delete_message")
        self.deleted.append((chat_id, message_id))
        return True


def config_data(tmp, agents, *, enabled=True, activation=None, extra=None, token_file=None, pins=None, topics=False):
    """``pins`` is ``(installation_id, owner_principal_id)``: the exact synthetic identifiers the fixture's database holds.
    ``topics`` puts all six destinations in one forum supergroup, one topic each."""
    data = {
        "version": 1, "enabled": enabled, "bot_token_file": str(token_file or tmp / "secrets" / "bot-token"),
        "owner_telegram_user_id": OWNER_TG, "expected_bot_id": BOT_ID, "dashboard_origin": "http://qorqut:1071",
        "activation_at": activation or (START - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "chats": {name: {"chat_id": chat_id, "type": "supergroup", **({"message_thread_id": TOPIC_IDS[name]} if topics else {}),
                         **({"agent_id": agents[name]} if name in agents else {})}
                  for name, chat_id in (TOPIC_CHAT_IDS if topics else CHAT_IDS).items()},
    }
    data["expected_installation_id"], data["expected_owner_principal_id"] = pins or SYNTHETIC_PINS
    data.update(extra or {})
    return data


def write_private(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(text)
    os.chmod(path, mode)
    return path


class Env:
    """An isolated installation with a bridge over injected adapters. No network, no terminals, no real service."""

    topics = False  # True: the six destinations are topics of one forum group (see ``__init__`` and ``TopicWorld``)

    def __init__(self, tmp_path, *, enabled=True, extra=None, poll_timeout=0.05, rng_seed=1, topics=False):
        self.tmp = Path(tmp_path)
        self.database = database_at(self.tmp)
        self.clock = Clock()
        self.topics = topics
        self.api = FakeBotApi(TOPIC_CHAT_IDS, forum_chats={GROUP_ID}) if topics else FakeBotApi()
        self.agents = {}
        self.installation_id, self.owner_id = new_id(), HUMAN
        with self.database.transaction() as uow:
            uow.repository("installation").insert({"id": "service", "installation_id": self.installation_id, "owner_principal_id": HUMAN})
            for name, role in ROLES.items():
                identity = new_id()
                self.agents[name] = identity
                uow.repository("principals").insert({"id": identity, "kind": "agent", "display_name": name.title()})
                uow.repository("agent_profiles", business_id=None).insert({
                    "agent_id": identity, "principal_id": identity, "namespace": "ecosystem", "name": name,
                    "workspace": str(self.tmp / "agents" / name), "status": "active",
                    "profile_json": {"role": {"id": role}, "display_name": name.title()}})
        self.token_file = write_private(self.tmp / "secrets" / "bot-token", TOKEN + "\n")
        self.config_path = self.tmp / "config" / "telegram.json"
        self.write_config(enabled=enabled, extra=extra)
        application = SimpleNamespace(ensure_writes_open=Application.ensure_writes_open, writer_admission=None)
        self.runtime = SimpleNamespace(database=self.database, application=application, root=self.tmp / "root",
                                       workspace=self.tmp / "workspace")
        self.state_dir = self.tmp / "state" / "telegram"
        self.rng_seed, self.poll_timeout = rng_seed, poll_timeout
        self.bridges = []
        self.bridge = self.make_bridge()

    def write_config(self, *, enabled=True, extra=None, activation=None, mutate=None):
        data = config_data(self.tmp, self.agents, enabled=enabled, extra=extra, activation=activation, token_file=self.token_file,
                           pins=(self.installation_id, self.owner_id), topics=self.topics)
        if mutate:
            mutate(data)
        write_private(self.config_path, json.dumps(data))
        return data

    def make_bridge(self):
        bridge = TelegramBridge(self.runtime, config_path=str(self.config_path), api_factory=lambda cfg, token: self.api,
                                clock=self.clock, sleep=lambda s: None, rng=random.Random(self.rng_seed), state_dir=self.state_dir,
                                poll_timeout=self.poll_timeout, tick=0.05)
        self.bridges.append(bridge)
        return bridge

    def ready(self, bridge=None):
        bridge = bridge or self.bridge
        bridge.load_config()
        assert bridge._ensure_ready(), bridge.health()
        return bridge

    def close(self):
        for bridge in self.bridges:
            try:
                bridge.stop(timeout=5)
            except TimeoutError:
                pass
            if bridge.lock is not None:
                bridge.lock.release()
        self.database.close()

    # ----- update builders ----------------------------------------------------------------------------

    _update = 100

    def chat_id(self, name):
        return GROUP_ID if self.topics else CHAT_IDS[name]

    def next_update_id(self):
        Env._update += 1
        return Env._update

    def message(self, chat, text=None, *, sender=OWNER_TG, is_bot=False, at=None, reply_to=None, extra=None, message_id=None,
                sender_chat=False, update_id=None):
        update_id = update_id or self.next_update_id()
        when = at or self.clock.now
        message = {"message_id": message_id or update_id + 1000, "date": int(when.timestamp()),
                   "chat": {"id": self.chat_id(chat), "type": "supergroup"},
                   "from": {"id": sender, "is_bot": is_bot}}
        if self.topics:
            # What Telegram sends for a message in a topic: its thread, the topic marker and, unless it replies to something, a
            # "reply" to the topic's own creation message.
            thread = TOPIC_IDS[chat]
            message.update(message_thread_id=thread, is_topic_message=True)
            if reply_to is None:
                message["reply_to_message"] = {"message_id": thread, "forum_topic_created": {"name": chat}}
        if sender_chat:
            message["sender_chat"] = {"id": -777}
        if text is not None:
            message["text"] = text
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": reply_to}
        message.update(extra or {})
        return {"update_id": update_id, "message": message}

    def reaction(self, chat, message_id, new, *, old=(), sender=OWNER_TG, actor_chat=False, at=None, update_id=None):
        update_id = update_id or self.next_update_id()
        body = {"chat": {"id": self.chat_id(chat), "type": "supergroup"}, "message_id": message_id,
                "date": int((at or self.clock.now).timestamp()),
                "old_reaction": [{"type": "emoji", "emoji": e} for e in old],
                "new_reaction": [{"type": "emoji", "emoji": e} for e in new]}
        if actor_chat:
            body["actor_chat"] = {"id": -888}
        else:
            body["user"] = {"id": sender, "is_bot": False}
        return {"update_id": update_id, "message_reaction": body}

    # ----- database peeks --------------------------------------------------------------------------------

    def rows(self, sql, *params):
        with self.database.read() as uow:
            return [dict(row) for row in uow.connection.execute(sql, params)]

    def one(self, sql, *params):
        rows = self.rows(sql, *params)
        return rows[0] if rows else None
