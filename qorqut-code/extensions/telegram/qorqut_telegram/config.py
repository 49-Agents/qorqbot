"""Private Telegram bridge configuration: strict, inert and redacted.

The selector is the environment variable ``QORQUT_TELEGRAM_CONFIG_FILE`` naming a private JSON file. Chat IDs, the
owner and bot IDs and the Ordubashy agent UUID live only there, never in source. The bot token lives in a
separate private file that this module reads only when asked to for an enabled start or an explicit validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

from bazarlyk.agents.roles import ORDUBASHY_ROLE
from qorqut_operations.contracts import DomainError, canonical_hash
from bazarlyk.private_files import read_private_file

CONFIG_ENV = "QORQUT_TELEGRAM_CONFIG_FILE"
VERSION = 1
CHAT_NAMES = ("ordubashy", "signoffs", "urgent")
# Conversation chat -> the exact Global role its bound agent must hold. A role name alone is never sufficient. Ordubashy is
# the only agent with a Telegram channel: Founder, Marketing and every worker have none, and a configuration naming one is refused.
AGENT_CHATS = {"ordubashy": ORDUBASHY_ROLE}
CHAT_TYPES = ("private", "group", "supergroup", "channel")
# A channel carries no owner messages, so only the outbound-only chats may be one.
CHANNEL_CHATS = ("urgent",)
PRODUCER_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TOKEN = re.compile(r"^([0-9]{5,15}):[A-Za-z0-9_-]{30,80}$")
TOP_KEYS = {"version", "enabled", "bot_token_file", "owner_telegram_user_id", "expected_bot_id", "expected_installation_id",
            "expected_owner_principal_id", "dashboard_origin", "chats", "activation_at", "program_gateway"}
CHAT_KEYS = {"chat_id", "type", "agent_id", "message_thread_id"}
GATEWAY_KEYS = {"enabled", "socket_path", "producers"}
PRODUCER_KEYS = {"producer_id", "secret_sha256", "peer_uid", "enabled"}


class Secret:
    """A value that never prints: repr, str and format all redact."""

    __slots__ = ("_value",)

    def __init__(self, value):
        self._value = value

    def reveal(self):
        return self._value

    def __repr__(self):
        return "<redacted>"

    __str__ = __repr__

    def __format__(self, spec):
        return "<redacted>"

    def __reduce__(self):
        raise TypeError("A secret is never serialized")


def _bad(message, **details):
    return DomainError("invalid_configuration", message, details=details or None)


def _pairs(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise _bad("Telegram configuration contains a duplicate key")
        seen[key] = value
    return seen


def _constant(value):
    raise _bad("Telegram configuration contains a non-finite number")


def _exact_int(value, name, *, positive=True):
    if type(value) is not int or (positive and value <= 0) or (not positive and value == 0):
        raise _bad(f"{name} must be an exact non-zero integer" if not positive else f"{name} must be an exact positive integer")
    return value


def timestamp_utc(value, name="activation_at"):
    if type(value) is not str:
        raise _bad(f"{name} must be an RFC 3339 timestamp with a UTC offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _bad(f"{name} must be an RFC 3339 timestamp with a UTC offset") from exc
    if parsed.tzinfo is None:
        raise _bad(f"{name} must carry a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ChatBinding:
    """One logical destination. Its identity is ``(chat_id, message_thread_id)`` plus the name and agent binding.

    ``message_thread_id`` is the numeric ID of a forum topic (greater than 1, General being unsupported) in a supergroup, and
    ``None`` for a whole-chat (legacy separate-chat) binding. A topic's title is never part of the binding.
    """

    name: str
    chat_id: int
    chat_type: str
    agent_id: str | None = None
    message_thread_id: int | None = None

    @property
    def destination(self):
        return (self.chat_id, self.message_thread_id)


@dataclass(frozen=True)
class ProducerBinding:
    producer_id: str
    secret_sha256: str
    peer_uid: int | None = None
    enabled: bool = True


@dataclass(frozen=True)
class GatewayConfig:
    enabled: bool = False
    socket_path: Path | None = None
    producers: tuple[ProducerBinding, ...] = ()


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    source: Path | None = field(default=None, compare=False)
    bot_token_file: Path | None = None
    owner_telegram_user_id: int | None = None
    bot_id: int | None = None
    installation_id: str | None = None
    owner_principal_id: str | None = None
    dashboard_origin: str | None = None
    activation_at: str | None = None
    chats: tuple[ChatBinding, ...] = ()
    gateway: GatewayConfig = GatewayConfig()

    def chat(self, name):
        for chat in self.chats:
            if chat.name == name:
                return chat
        raise DomainError("operation_unavailable", "Telegram chat is not configured")

    def destination(self, chat_id, message_thread_id=None):
        """The binding for exactly this chat and topic (``None`` for a whole chat), or None. Never a chat-ID-only guess."""
        for chat in self.chats:
            if chat.chat_id == chat_id and chat.message_thread_id == message_thread_id:
                return chat
        return None

    def chats_in(self, chat_id):
        """Every binding that shares one provider chat (the four topics of one forum group, or a single legacy chat)."""
        return tuple(chat for chat in self.chats if chat.chat_id == chat_id)

    def group_ids(self):
        """The distinct provider chats to verify, in configuration order."""
        return tuple(dict.fromkeys(chat.chat_id for chat in self.chats))

    def agent_chat(self, agent_id):
        for chat in self.chats:
            if chat.agent_id is not None and chat.agent_id == agent_id:
                return chat
        return None

    def producer(self, producer_id):
        """The enabled producer with this ID. A disabled or removed one is None, so its queued sends are cancelled."""
        return next((item for item in self.gateway.producers if item.producer_id == producer_id and item.enabled), None)

    def binding_fingerprint(self):
        """What must not change under a queued effect: bot, installation, both owners, chats and agents.

        The activation cutoff is pinned in the saved state on first start, so a later value in the file is not a binding.
        A forum topic ID is part of its entry only when present, so a configuration without topics keeps the serialization (and
        therefore the saved generation) it had before topics existed; a real topic assignment or change is a new generation.
        """
        return canonical_hash({"bot_id": self.bot_id, "owner": self.owner_telegram_user_id,
                               "installation": self.installation_id, "owner_principal": self.owner_principal_id,
                               "chats": [[c.name, c.chat_id, c.chat_type, c.agent_id]
                                         + ([c.message_thread_id] if c.message_thread_id is not None else []) for c in self.chats]})

    def redacted(self):
        """Operator-visible summary: no token path, no token, no chat or agent identifiers."""
        return {"enabled": self.enabled, "version": VERSION, "activation_at": self.activation_at,
                "configured_chats": [chat.name for chat in self.chats],
                "forum_topics": sum(1 for chat in self.chats if chat.message_thread_id is not None),
                "program_gateway": {"enabled": self.gateway.enabled, "producers": len(self.gateway.producers)},
                "binding_fingerprint": self.binding_fingerprint() if self.chats else None}


def _uuid(value, name):
    """A canonical lowercase UUID string: the installation and owner pins are exact identifiers, never inferred."""
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise _bad(f"{name} must be a canonical lowercase UUID") from exc
    return value


def _origin(value):
    if type(value) is not str:
        raise _bad("dashboard_origin must be an HTTP(S) origin")
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise _bad("dashboard_origin must be a credential-free HTTP(S) origin without a path")
    try:
        port = parts.port
    except ValueError as exc:
        raise _bad("dashboard_origin has an invalid port") from exc
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def _chat(name, value):
    if not isinstance(value, dict) or set(value) - CHAT_KEYS or not {"chat_id", "type"} <= set(value):
        raise _bad("A chat binding needs exactly chat_id, type, (for agent chats) agent_id and (for a forum topic) message_thread_id", chat=name)
    chat_id = _exact_int(value["chat_id"], "chat_id", positive=False)
    chat_type = value["type"]
    if chat_type not in CHAT_TYPES:
        raise _bad("A chat type must be private, group, supergroup or (for urgent) channel", chat=name)
    if chat_type == "channel" and name not in CHANNEL_CHATS:
        raise _bad("Only the urgent chat may be a channel", chat=name)
    agent_id = value.get("agent_id")
    if name in AGENT_CHATS:
        try:
            if type(agent_id) is not str or str(UUID(agent_id)) != agent_id:
                raise ValueError
        except ValueError as exc:
            raise _bad("An agent chat requires a canonical lowercase agent UUID", chat=name) from exc
    elif agent_id is not None:
        raise _bad("Only the Ordubashy chat binds an agent", chat=name)
    if chat_type == "private" and chat_id < 0 or chat_type != "private" and chat_id > 0:
        raise _bad("Chat ID sign does not match the chat type", chat=name)
    thread_id = None
    if "message_thread_id" in value:
        thread_id = value["message_thread_id"]
        # A topic is an exact integer greater than 1 (General, ID 1, is unsupported) in a supergroup: never a string or a bool.
        if type(thread_id) is not int or thread_id <= 1:
            raise _bad("message_thread_id must be an exact integer forum topic ID greater than 1 (General is unsupported)", chat=name)
        if chat_type != "supergroup":
            raise _bad("Only a supergroup may name a forum topic", chat=name)
    return ChatBinding(name, chat_id, chat_type, agent_id, thread_id)


def _distinct_destinations(chats):
    """One provider chat may back several bindings only as distinct forum topics; never a whole-chat binding alongside topics."""
    by_chat = {}
    for chat in chats:
        by_chat.setdefault(chat.chat_id, []).append(chat)
    for bindings in by_chat.values():
        if len(bindings) == 1:
            continue
        if any(chat.message_thread_id is None for chat in bindings):
            raise _bad("Bindings share a chat only as distinct forum topics; a whole-chat binding cannot share its chat with another")
        if len({chat.message_thread_id for chat in bindings}) != len(bindings):
            raise _bad("Every forum topic destination must be distinct")


def _gateway(value):
    if not isinstance(value, dict) or set(value) - GATEWAY_KEYS:
        raise _bad("program_gateway must be an object with enabled, socket_path and producers")
    enabled = value.get("enabled", False)
    if type(enabled) is not bool:
        raise _bad("program_gateway.enabled must be a boolean")
    socket_path = value.get("socket_path")
    if socket_path is not None:
        if type(socket_path) is not str or not os.path.isabs(socket_path) or len(socket_path.encode()) > 100:
            raise _bad("program_gateway.socket_path must be a short absolute path")
        socket_path = Path(socket_path)
    producers = []
    for item in value.get("producers", []):
        if not isinstance(item, dict) or set(item) - PRODUCER_KEYS or not {"producer_id", "secret_sha256"} <= set(item):
            raise _bad("A producer binding needs producer_id and secret_sha256 (and an optional peer_uid and enabled)")
        if type(item["producer_id"]) is not str or not PRODUCER_ID.fullmatch(item["producer_id"]):
            raise _bad("producer_id must be a stable lowercase identifier")
        if type(item["secret_sha256"]) is not str or not SHA256.fullmatch(item["secret_sha256"]):
            raise _bad("secret_sha256 must be a lowercase SHA-256 hex digest")
        peer_uid = item.get("peer_uid")
        if peer_uid is not None and (type(peer_uid) is not int or peer_uid < 0):
            raise _bad("peer_uid must be a non-negative integer")
        if type(item.get("enabled", True)) is not bool:
            raise _bad("A producer's enabled flag must be a boolean")
        producers.append(ProducerBinding(item["producer_id"], item["secret_sha256"], peer_uid, item.get("enabled", True)))
    if len({p.producer_id for p in producers}) != len(producers) or len({p.secret_sha256 for p in producers}) != len(producers):
        raise _bad("Producer IDs and credentials must be unique")
    if enabled and (socket_path is None or not producers):
        raise _bad("An enabled program gateway needs a socket_path and at least one producer")
    return GatewayConfig(enabled, socket_path, tuple(producers))


def parse_config(data, *, source=None, require_complete=False):
    """Validate decoded JSON. An enabled configuration must be complete; a disabled one may be a staged draft."""
    if not isinstance(data, dict) or set(data) - TOP_KEYS:
        raise _bad("Telegram configuration has unknown or malformed top-level fields")
    if data.get("version") != VERSION or type(data.get("version")) is not int:
        raise _bad("Telegram configuration version must be 1")
    enabled = data.get("enabled", False)
    if type(enabled) is not bool:
        raise _bad("enabled must be a boolean")
    complete = enabled or require_complete
    required = ("bot_token_file", "owner_telegram_user_id", "expected_bot_id", "expected_installation_id",
                "expected_owner_principal_id", "dashboard_origin", "chats", "activation_at")
    missing = [key for key in required if key not in data]
    if complete and missing:
        raise _bad("Telegram configuration is incomplete", missing=missing)
    token_file = data.get("bot_token_file")
    if token_file is not None and (type(token_file) is not str or not os.path.isabs(token_file)):
        raise _bad("bot_token_file must be an absolute path")
    owner = _exact_int(data["owner_telegram_user_id"], "owner_telegram_user_id") if "owner_telegram_user_id" in data else None
    bot_id = _exact_int(data["expected_bot_id"], "expected_bot_id") if "expected_bot_id" in data else None
    installation_id = _uuid(data["expected_installation_id"], "expected_installation_id") if "expected_installation_id" in data else None
    owner_principal_id = (_uuid(data["expected_owner_principal_id"], "expected_owner_principal_id")
                          if "expected_owner_principal_id" in data else None)
    origin = _origin(data["dashboard_origin"]) if "dashboard_origin" in data else None
    activation = timestamp_utc(data["activation_at"]) if "activation_at" in data else None
    chats = ()
    if "chats" in data:
        raw = data["chats"]
        if not isinstance(raw, dict) or set(raw) - set(CHAT_NAMES) or (complete and set(raw) != set(CHAT_NAMES)):
            raise _bad("chats must name exactly Ordubashy, Signoffs and Urgent Notifications; only Ordubashy has an agent chat")
        chats = tuple(_chat(name, raw[name]) for name in CHAT_NAMES if name in raw)
        _distinct_destinations(chats)
        if len({c.agent_id for c in chats if c.agent_id}) != len([c for c in chats if c.agent_id]):
            raise _bad("Every agent binding must be unique")
        for chat in chats:
            if chat.chat_type == "private" and owner is not None and chat.chat_id != owner:
                raise _bad("A private chat must be the owner's own chat", chat=chat.name)
    gateway = _gateway(data["program_gateway"]) if "program_gateway" in data else GatewayConfig()
    return TelegramConfig(enabled, Path(source) if source else None, Path(token_file) if token_file else None,
                          owner, bot_id, installation_id, owner_principal_id, origin, activation, chats, gateway)


def load_config(path=None, *, environ=None, require_complete=False):
    """Read the selected private config file. Unset means disabled and reads nothing."""
    environ = os.environ if environ is None else environ
    selected = path or environ.get(CONFIG_ENV)
    if not selected:
        return TelegramConfig()
    raw = read_private_file(selected, max_bytes=65536)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, DomainError):
            raise
        raise _bad("Telegram configuration is not valid JSON") from exc
    return parse_config(data, source=selected, require_complete=require_complete)


def read_token(config):
    """The bot token from its private file. The numeric prefix must equal the expected bot ID."""
    if config.bot_token_file is None or config.bot_id is None:
        raise _bad("The bot token file is not configured")
    text = read_private_file(config.bot_token_file, max_bytes=512).decode("utf-8", "replace").strip()
    match = TOKEN.fullmatch(text)
    if match is None or int(match.group(1)) != config.bot_id:
        raise _bad("The bot token file does not hold a token for the expected bot")
    return Secret(text)


def redacted_example():
    """A complete, syntactically valid example with placeholder identifiers. It names no real chat or agent."""
    return {
        "version": VERSION,
        "enabled": False,
        "bot_token_file": "/home/USER/.config/qorqut/telegram/bot-token",
        "owner_telegram_user_id": 111111111,
        "expected_bot_id": 222222222,
        "expected_installation_id": "00000000-0000-4000-8000-0000000000a1",
        "expected_owner_principal_id": "00000000-0000-4000-8000-0000000000a2",
        "dashboard_origin": "http://qorqut:1071",
        "activation_at": "2026-01-01T00:00:00Z",
        # One private forum supergroup with three topics: the same synthetic group ID and three different synthetic topic IDs.
        "chats": {
            "ordubashy": {"chat_id": -1001000000001, "type": "supergroup", "message_thread_id": 1001,
                          "agent_id": "00000000-0000-4000-8000-000000000001"},
            "signoffs": {"chat_id": -1001000000001, "type": "supergroup", "message_thread_id": 1002},
            "urgent": {"chat_id": -1001000000001, "type": "supergroup", "message_thread_id": 1003},
        },
        "program_gateway": {
            "enabled": False,
            "socket_path": "/home/USER/.local/state/qorqut/telegram/gateway/notify.sock",
            "producers": [{"producer_id": "example-monitor", "secret_sha256": "0" * 64}],
        },
    }
