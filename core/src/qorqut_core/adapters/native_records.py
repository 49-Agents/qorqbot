"""Pure, bounded visible-text projection of the two rehearsed JSONL profiles.

This is deliberately not a transcript archive or a tool interpreter. Unsupported
semantics return a coverage gap; bodies never supply identity or timestamps.
"""

import json
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, Field

from ..contracts import Counter, Model, Timestamp, _utf8
from ..messaging.models import Body

PARSER_VERSION = "native-v1"
MAX_RECORD_BYTES = 4 * 1024 * 1024
TEXT_BYTES = 65536
PROFILES = {"claude": "2.1.287", "codex": "0.160.0"}
NativeId = Annotated[str, Field(strict=True, min_length=1, max_length=512), AfterValidator(_utf8)]


class NativeTextItem(Model):
    native_id: NativeId
    role: Literal["user", "agent"]
    text: Body
    part_index: Counter
    occurred_at: Timestamp | None
    timestamp_origin: Literal["native", "ingestion"]


class ParseOutcome(Model):
    items: tuple[NativeTextItem, ...] = ()
    disposition: Literal["messages", "ignored", "gap"]
    reason: str | None = None


def _gap(reason):
    return ParseOutcome(disposition="gap", reason=reason)


def _ignored(reason):
    return ParseOutcome(disposition="ignored", reason=reason)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous duplicate JSON key")
        result[key] = value
    return result


def _time(value):
    if not isinstance(value, str) or len(value) > 100:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        pass
    return None


def _messages(native_id, role, text, timestamp):
    if not text:
        return _ignored("empty_text")
    encoded = text.encode("utf-8")
    occurred_at, items, start = _time(timestamp), [], 0
    while start < len(encoded):
        end = min(start + TEXT_BYTES, len(encoded))
        # Move back over UTF-8 continuation bytes, preserving every byte.
        while end < len(encoded) and encoded[end] & 0xC0 == 0x80:
            end -= 1
        items.append(NativeTextItem(native_id=native_id, role=role,
            text=encoded[start:end].decode("utf-8"), part_index=len(items),
            occurred_at=occurred_at, timestamp_origin="native" if occurred_at else "ingestion"))
        start = end
    return ParseOutcome(items=tuple(items), disposition="messages")


def _blocks(content, block_type):
    if not isinstance(content, list) or any(not isinstance(block, dict)
            or block.get("type") != block_type or not isinstance(block.get("text"), str)
            for block in content):
        return None
    return "".join(block["text"] for block in content)


def _claude(record, session):
    if any(record[key] != session for key in ("sessionId", "session_id") if key in record):
        return _gap("session_mismatch")
    if record.get("isSidechain") is True or record.get("agentId") is not None:
        return _ignored("sidechain")
    kind = record.get("type")
    if kind in {"mode", "permission-mode", "atis-latch", "file-history-snapshot", "last-prompt", "cost-state"}:
        return _ignored("metadata")
    if kind == "system" and record.get("subtype") in {"stop_hook_summary", "turn_duration"}:
        return _ignored("metadata")
    if kind not in {"user", "assistant"}:
        return _gap("unsupported_record")
    if record.get("sessionId") != session or record.get("isSidechain") is not False:
        return _gap("ambiguous_root_identity")
    if record.get("version", PROFILES["claude"]) != PROFILES["claude"]:
        return _gap("unsupported_profile")
    native_id = record.get("uuid")
    if not isinstance(native_id, str) or str(UUID(native_id)) != native_id:
        return _gap("invalid_native_id")
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != kind:
        return _gap("ambiguous_message")
    if kind == "user":
        if record.get("isMeta") is True:
            return _ignored("instruction")
        if record.get("origin") != {"kind": "human"} or record.get("promptSource") != "typed":
            return _gap("unverified_human_input")
        text = message.get("content")
        if not isinstance(text, str):
            return _gap("unsupported_content")
    else:
        if message.get("stop_reason") != "end_turn":
            return _gap("unsupported_assistant_semantics")
        text = _blocks(message.get("content"), "text")
        if text is None:
            return _gap("unsupported_content")
    return _messages(native_id, "user" if kind == "user" else "agent", text, record.get("timestamp"))


def _codex(record, session):
    kind, payload = record.get("type"), record.get("payload")
    if not isinstance(payload, dict):
        return _gap("invalid_payload")
    if any(payload[key] != session for key in ("thread_id", "session_id") if key in payload):
        return _gap("session_mismatch")
    if kind == "session_meta":
        if payload.get("id") != session or payload.get("source") != "cli":
            return _gap("session_mismatch")
        if payload.get("cli_version") != PROFILES["codex"]:
            return _gap("unsupported_profile")
        return _ignored("metadata")
    if kind == "response_item" and payload.get("type") == "message":
        return _ignored("response_echo_or_instruction")
    if kind in {"world_state", "turn_context", "token_usage_record"}:
        return _ignored("metadata")
    if kind != "event_msg":
        return _gap("unsupported_record")
    if payload.get("type") in {"task_started", "task_complete", "token_count", "thread_settings_applied"}:
        return _ignored("metadata_or_summary")
    if payload.get("type") != "item_completed":
        return _gap("unsupported_event")
    if payload.get("thread_id") != session:
        return _gap("ambiguous_root_identity")
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") not in {"UserMessage", "AgentMessage"}:
        return _gap("unsupported_item")
    native_id = item.get("id")
    if not isinstance(native_id, str) or not native_id or len(native_id) > 512:
        return _gap("invalid_native_id")
    user = item["type"] == "UserMessage"
    text = _blocks(item.get("content"), "text" if user else "Text")
    if text is None:
        return _gap("unsupported_content")
    return _messages(native_id, "user" if user else "agent", text, record.get("timestamp"))


def parse_native_record(provider, cli_version, native_session_id, raw) -> ParseOutcome:
    """Parse one complete record; missing time stays None for caller ingestion.

    The framer owns newline completion. Direct callers may omit the trailing LF.
    A gap has no inferred text, including mixed text/tool or attachment layouts.
    """
    if provider not in PROFILES or cli_version != PROFILES[provider]:
        return _gap("unsupported_profile")
    if not isinstance(raw, bytes):
        return _gap("invalid_record")
    if len(raw) > MAX_RECORD_BYTES:
        return _gap("record_too_large")
    try:
        if str(UUID(native_session_id)) != native_session_id:
            return _gap("invalid_session_id")
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
        if not isinstance(record, dict):
            return _gap("invalid_record")
        return (_claude if provider == "claude" else _codex)(record, native_session_id)
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        return _gap("invalid_record")
