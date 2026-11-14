"""Derive Chat entries and reply completions from archived provider records.

The transcript archive owns the bytes; this module only reads parsed records and states what each one means for Chat. It
holds no database, no process and no archive handle. The same ``Projector`` serves two callers: the backend indexer (strictly
forward from a saved cursor, storing completions) and the Chat reader (a bounded window, storing nothing).

Provenance rules, applied to every record:

* A provider records every input as a user turn and gives it no application origin, so a native ``user`` entry is never
  labelled owner or system on its own. The one exception is an application marker: a system input is typed with a leading
  ``<qorqut-system-input id="…">`` tag whose id the application stored, so the entry that starts with it carries that id
  (``marker``). The reader looks the id up; an unknown id is shown as an unverified notice, never as a system claim.
* Identity is the provider's own native id when the record has one; otherwise the record's raw start offset in the lineage
  payload (offsets are stable because the payload is append-only) plus a discriminator. A visible entry id and a completion id
  are further scoped by the ``Lineage`` they belong to (agent, provider, native conversation and the immutable archive lineage),
  because a native id or a raw offset is only unique inside one conversation. Never a capture id, a timestamp, a generation, a
  page position or a hash of text.
* A successful outgoing agent message is derived from the provider's own tool result for ``agents_message_send`` (Codex: a
  completed ``qorqut`` MCP tool call; Claude: a ``tool_result`` JSON envelope). It is a notice entry that carries only the
  message IDs the result named; the reader checks them against the stored messages. It is never a user input, a reply, a turn
  origin or user work, and it changes no projector state.
* A successful ``request_user_input`` result is the archived source of an agent presentation: an ordinary agent message with
  identity-only references below it (``Entry.references``). It is derived only from the provider's own tool result for that
  tool (Codex: a completed ``qorqut`` MCP tool call; Claude: a ``tool_result`` JSON envelope), never from assistant prose, quoted
  JSON, a failed result or user text. It is never a completion, a user input or a turn origin, and changes no projector state.
* A reply is complete only at the provider's own end-of-turn evidence. Narration before a tool call, tool-only records, owner
  echoes and system notices are never completions.
* Every completion is a visible assistant entry of its own record, so a client can show the reply it acknowledges. The one
  completion with no assistant record of its own is a Codex turn that names its last message only at the turn end
  (``last_agent_message``): that record yields a ``turn_end`` entry carrying the full text. It is a fallback for a *complete* stream
  (nothing earlier in the turn was an assistant message); a window that starts mid-turn cannot know that, so the reader shows such an
  entry only when the durable index completed that exact turn-end record.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Iterator

from qorqut.transcript_provenance import is_compaction

from . import references as user_input

SPEAKER_USER = "user"
SPEAKER_AGENT = "agent"
SPEAKER_NOTICE = "notice"
# Who started the turn a reply completed. ``user`` is a plain native user entry (the owner, in Chat or typed in the terminal),
# ``nudge`` a turn started by a proven ``inbox_nudge`` input, ``system`` any other proven system input, ``unknown`` a turn whose
# start this run did not see. Whether the turn included real user work is a separate fact (``Completion.user_work``): a turn
# that a nudge started and the owner then steered is still work, and a turn only system inputs started or steered is not.
ORIGIN_USER = "user"
ORIGIN_NUDGE = "nudge"
ORIGIN_SYSTEM = "system"
ORIGIN_UNKNOWN = "unknown"
NUDGE_TYPE = "inbox_nudge"
# The application's own system input types (the other one is the bootstrap recovery wake).
RECOVERY_TYPE = "bootstrap_recovery"
SYSTEM_TYPES = (NUDGE_TYPE, RECOVERY_TYPE)
# What proves that a leading marker was typed by the application: ``(marker id, declared type)`` to the stored input type, or
# ``None``. The capture/index boundary supplies it from the exact registered conversation's input receipts; the parser alone only
# says what the text looks like, and text that merely looks like a marker is whatever its author typed.
MarkerResolver = Callable[[str, str | None], str | None]
KIND_NATIVE = "native_id"
KIND_RAW = "raw_range"

MARKER_TAG = "qorqut-system-input"
_MARKER = re.compile(r'<' + MARKER_TAG + r'\s+id="([A-Za-z0-9_-]{8,120})"(?:\s+type="([a-z_]{1,40})")?\s*>')
_LEADING_SPACE = " \t\n\r\x0b\x0c"
_OPENING_TAG = re.compile(r"<[A-Za-z].*>", re.S)
_COMMAND_PREFIXES = ("<local-command-", "<command-name>", "<system-reminder>")
# Claude Code records a slash command as ``<command-name>/x</command-name> … <command-args>a b</command-args>``. Chat shows it
# as the command the owner typed (``/x a b``), so a command sent from Chat finds its own entry instead of waiting forever.
_COMMAND_RECORD = re.compile(r"<command-name>\s*(/?[^<\s]{1,200})\s*</command-name>.*?(?:<command-args>(.*?)</command-args>|$)", re.S)
MAX_ENTRY_CHARS = 200_000
SEND_SERVER = "qorqut"
SEND_TOOL = "agents_message_send"
INPUT_TOOL = "request_user_input"
MAX_INPUTS = 8               # presentations one record can show
MAX_RECEIPTS = 20            # successful sends one record can show
MAX_RECEIPT_CHARS = 100_000  # a full-detail receipt echoes the message body; anything larger is not a receipt
COMPLETING_STOPS = frozenset({"end_turn", "stop_sequence"})
CODEX_TURN_END = frozenset({"task_complete", "turn_complete"})
CODEX_TURN_START = frozenset({"task_started", "turn_started"})
CODEX_TURN_ABORT = frozenset({"turn_aborted"})
PROVENANCE_VERSION = 1


def marker_text(marker: str, input_type: str, body: str) -> str:
    """The text a system input is typed as: the application marker first, then the body."""
    return f'<{MARKER_TAG} id="{marker}" type="{input_type}">\n{body}\n</{MARKER_TAG}>'


def leading_marker(text: Any) -> tuple[str, str | None] | None:
    """The application marker (id, declared type) a typed input starts with, or ``None``. Only a leading tag counts: a quoted or
    embedded one is plain text. The marker alone proves nothing; the application checks the id against what it stored."""
    match = _MARKER.match(text.lstrip(_LEADING_SPACE)) if isinstance(text, str) else None
    return (match.group(1), match.group(2)) if match else None


def unwrap_marker(text: str) -> str:
    """The body of a marker-wrapped system input, without the application tags."""
    stripped = text.strip()
    match = _MARKER.match(stripped)
    if not match:
        return text
    inner = stripped[match.end():]
    closing = f"</{MARKER_TAG}>"
    if inner.rstrip().endswith(closing):
        inner = inner.rstrip()[:-len(closing)]
    return inner.strip("\n")


def internal_notification(body: Any) -> bool:
    """A message whose text starts with an opening tag is an internal notice for either speaker; the archive keeps it."""
    return isinstance(body, str) and _OPENING_TAG.match(body.lstrip(_LEADING_SPACE)) is not None


def unwrap_claude_paste(text):
    # Claude's expanded paste representation is provider chrome around the full input.
    # Accept only an entire single wrapper with matching IDs (decimal or hexadecimal), never an embedded quotation.
    match = re.fullmatch(r'\s*<pasted_content id="([0-9a-fA-F]+)">\n(.*?)\n</pasted_content id="\1">\s*',
                             text, re.S)
    if match:
        return match.group(2)
    return text


def text_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content if isinstance(block, dict)
                     and block.get("type") in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str))


def _send_receipt(text: Any) -> str | None:
    """The message ID of one successful send, or ``None``. Only a text that is itself the JSON success envelope counts, never
    quoted or embedded JSON: ``status`` ok and ``data.message`` carrying the send receipt fields."""
    if not isinstance(text, str) or len(text) > MAX_RECEIPT_CHARS or '"recipient_id"' not in text:
        return None  # the cheap tests first: nearly every tool result is some other output
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):  # not JSON, or nested so deeply that no tool could have written it
        return None
    data = value.get("data") if isinstance(value, dict) and value.get("status") == "ok" else None
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict) or type(message.get("event_id")) is not int:
        return None
    complete = all(isinstance(message.get(name), str) and message[name] for name in ("id", "thread_id", "created_at", "recipient_id"))
    return message["id"] if complete else None


def _result_texts(content: Any) -> list[str]:
    """The text blocks of one tool result's content: a JSON string, or blocks each holding its own JSON envelope."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [block["text"] for block in content
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)]
    return []


def _result_receipts(content: Any) -> list[str]:
    """The send receipts in one tool result's content."""
    return [found for found in map(_send_receipt, _result_texts(content)) if found]


def _user_input(text: Any) -> dict | None:
    """The presentation of one successful ``request_user_input`` result, or ``None``. Only a text that is itself the JSON success
    envelope counts (``status`` ok and ``data`` holding exactly ``user_input``), never quoted or embedded JSON."""
    if not isinstance(text, str) or len(text) > MAX_RECEIPT_CHARS or '"user_input"' not in text:
        return None  # the cheap tests first: nearly every tool result is some other output
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    data = value.get("data") if isinstance(value, dict) and value.get("status") == "ok" else None
    if not isinstance(data, dict) or set(data) != {"user_input"}:
        return None
    return user_input.presentation(data["user_input"])


def _result_inputs(content: Any) -> list[dict]:
    return [found for found in map(_user_input, _result_texts(content)) if found]


def _unique_receipts(found: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(found))[:MAX_RECEIPTS]


def _claude_results(content: Any, found: Callable[[Any], list]) -> list:
    """What ``found`` reads from the successful ``tool_result`` blocks of one Claude user record, in block order."""
    if not isinstance(content, list):
        return []
    return [item for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
            and not block.get("is_error") and not block.get("isError")
            for item in found(block.get("content"))]


def _claude_receipts(content: Any) -> tuple[str, ...]:
    """The unique successful send receipts of one Claude user record's ``tool_result`` blocks, in block order."""
    return _unique_receipts(_claude_results(content, _result_receipts))


def _claude_inputs(content: Any) -> tuple[dict, ...]:
    """The presentations of one Claude user record's successful ``request_user_input`` results, in block order."""
    return tuple(_claude_results(content, _result_inputs)[:MAX_INPUTS])


def _codex_result(item: dict, tool: str) -> Any:
    """The result content of one completed Codex ``McpToolCall`` item of ``tool``; a failed call or MCP error has none."""
    result = item.get("result")
    if (item.get("server") not in {SEND_SERVER, "bazarlyk"} or item.get("tool") != tool or item.get("status") != "completed"
            or item.get("error") or not isinstance(result, dict) or result.get("isError")):
        return None
    return result.get("content")


def _codex_receipts(item: dict) -> tuple[str, ...]:
    """The unique successful send receipts of one completed Codex ``McpToolCall`` item."""
    return _unique_receipts(_result_receipts(_codex_result(item, SEND_TOOL)))


def _codex_inputs(item: dict) -> tuple[dict, ...]:
    """The presentations of one completed Codex ``request_user_input`` call."""
    return tuple(_result_inputs(_codex_result(item, INPUT_TOOL))[:MAX_INPUTS])


@dataclass(frozen=True)
class Lineage:
    """The immutable identity scope of one native conversation's records.

    Two conversations of the same agent can hold the same raw offset or even reuse a provider item id, and a resumed
    conversation keeps its lineage; so every durable and visible identity is the digest of this scope plus the record's own
    identity (native id or raw discriminator). The scope has no session generation, receipt or page: none of those is stable.
    """

    agent_id: str
    provider: str
    conversation_id: str
    archive_id: str

    def identity(self, domain: str, kind: str, key: str) -> str:
        return hashlib.sha256(json.dumps([domain, self.agent_id, self.provider, self.conversation_id, self.archive_id, kind, key],
                                         ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def entry_id(self, kind: str, key: str) -> str:
        return self.identity("native-entry", kind, key)

    def reply_id(self, kind: str, key: str) -> str:
        return self.identity("native-reply", kind, key)

    def turn_id(self, kind: str, key: str) -> str:
        return self.identity("native-owner-turn", kind, key)


@dataclass(frozen=True)
class Entry:
    """One visible conversation entry derived from one archived record."""

    identity_kind: str
    key: str
    speaker: str
    text: str
    offset: int
    length: int
    timestamp: str | None
    partial: bool | None = None
    marker: str | None = None
    marker_type: str | None = None
    turn_end: bool = False
    receipts: tuple[str, ...] = ()  # message IDs a successful ``agents_message_send`` result named (a notice without text)
    # A ``request_user_input`` presentation: ``text`` is its message and these are its identity-only references, each
    # ``(type, id, business_id)`` in order. ``None`` for every other entry; an empty tuple is a message with no references.
    references: tuple[user_input.Key, ...] | None = None
    command: bool = False  # a slash command the user typed (``/compact``): shown as a user entry, but it asks the agent nothing

    def public_id(self, lineage: Lineage) -> str:
        return lineage.entry_id(self.identity_kind, self.key)


@dataclass(frozen=True)
class Completion:
    """A logical reply reached its provider-declared end. ``origin`` is who started the turn; ``user_work`` is whether any
    genuine, non-notice native user input took part in it (the first input, or steering that came after a notice)."""

    identity_kind: str
    key: str
    offset: int
    length: int
    timestamp: str | None
    preview: str
    origin: str = ORIGIN_UNKNOWN
    user_work: bool = False


@dataclass
class ProjectorState:
    """What a forward run must remember between receipts: the Codex turn in progress, who started the open turn and whether any
    genuine user input has taken part in it. Serialised as small JSON."""

    turn_open: bool = False
    turn_done: bool = False
    candidate: dict | None = None
    origin: str | None = None
    work: bool = False  # a genuine, non-notice native user input took part in the open turn; a later notice cannot clear it
    bound: bool = False  # Codex: this rollout's ``session_meta`` named this conversation (so later records are its own)

    def dump(self) -> dict:
        return {"turn_open": self.turn_open, "turn_done": self.turn_done, "candidate": self.candidate, "origin": self.origin,
                "work": self.work, "bound": self.bound, "provenance_version": PROVENANCE_VERSION}

    def end_turn(self) -> tuple[str, bool]:
        """The origin and work of the turn that just ended, with both reset for the next one."""
        ended = (self.origin or ORIGIN_UNKNOWN, self.work)
        self.origin, self.work = None, False
        return ended

    @classmethod
    def load(cls, value: Any) -> "ProjectorState":
        if not isinstance(value, dict):
            return cls()
        candidate = value.get("candidate")
        origin = value.get("origin")
        return cls(bool(value.get("turn_open")), bool(value.get("turn_done")), candidate if isinstance(candidate, dict) else None,
                   origin if origin in {ORIGIN_USER, ORIGIN_NUDGE, ORIGIN_SYSTEM} else None, bool(value.get("work")),
                   bool(value.get("bound")))


@dataclass
class Result:
    """What one record means. A record whose tool results hold several presentations yields the first as ``entry`` and the rest as
    ``extra``, all at the record's offset in block order."""

    entry: Entry | None = None
    completion: Completion | None = None
    hidden: bool = False
    extra: tuple[Entry, ...] = ()

    @property
    def entries(self) -> tuple[Entry, ...]:
        return ((self.entry,) if self.entry is not None else ()) + self.extra


def _timestamp(record: dict) -> str | None:
    value = record.get("timestamp")
    return value if isinstance(value, str) else None


def _entry_text(text: str) -> str:
    return text if len(text) <= MAX_ENTRY_CHARS else text[:MAX_ENTRY_CHARS]


def _user_entry(identity_kind, key, text, record, offset, length, *, partial=None):
    marker = leading_marker(text)
    if marker:
        return Entry(identity_kind, key, SPEAKER_NOTICE, _entry_text(text), offset, length, _timestamp(record),
                     marker=marker[0], marker_type=marker[1])
    return Entry(identity_kind, key, SPEAKER_USER, _entry_text(text), offset, length, _timestamp(record), partial)


class Projector:
    """Feed archived records in order; read ``Result``s back. ``conversation_id`` binds Claude's ``sessionId`` and Codex's
    ``session_meta`` id, so a record copied in from another conversation is not attributed to this one.

    ``resolver`` decides which leading markers were really typed by the application. Without one nothing is proven: a leading
    marker still displays as a notice, but it never changes who started a turn or whether the turn included user work."""

    def __init__(self, provider: str, conversation_id: str, state: ProjectorState | None = None,
                 resolver: MarkerResolver | None = None):
        self.provider, self.conversation_id, self.resolver = provider, conversation_id, resolver
        self.state = state or ProjectorState()
        self.bound = provider != "codex" or self.state.bound or self.state.turn_open or self.state.candidate is not None

    def feed(self, record) -> Result:
        value = record.value if record.kind == "json" else None
        if not isinstance(value, dict):
            return Result()
        if is_compaction(value):
            # Shared by Chat, delivery reconciliation, reply/activity counters
            # and Overseer evidence. A summary is never owner input, including
            # when reading an old archive or starting a window at the summary.
            # Preserve genuine work already in progress across compaction.
            return Result(hidden=True)
        return self._claude(value, record) if self.provider == "claude" else self._codex(value, record)

    def feed_all(self, records: Iterable) -> Iterator[Result]:
        for record in records:
            result = self.feed(record)
            if result.entry or result.completion or result.extra:
                yield result

    # ----- Claude Code -----------------------------------------------------------------------------------------------

    def _claude(self, value: dict, record) -> Result:
        if value.get("sessionId") != self.conversation_id or value.get("isSidechain") or value.get("isMeta"):
            return Result()
        kind = value.get("type")
        native = value.get("uuid") if isinstance(value.get("uuid"), str) and value.get("uuid") else None
        identity = (KIND_NATIVE, native) if native else (KIND_RAW, f"{record.offset}#0")
        if kind == "attachment":
            attachment = value.get("attachment")
            if (isinstance(attachment, dict) and attachment.get("type") == "queued_command"
                    and isinstance(attachment.get("origin"), dict) and attachment["origin"].get("kind") == "human"
                    and attachment.get("commandMode") == "prompt" and not attachment.get("isMeta")):
                text = unwrap_claude_paste(text_blocks(attachment.get("prompt")))
                return self._user(identity, text, value, record)
            return Result()
        message = value.get("message")
        if kind not in {"user", "assistant"} or not isinstance(message, dict):
            return Result()
        text = text_blocks(message.get("content"))
        if kind == "user":
            result = self._user(identity, unwrap_claude_paste(text), value, record)
            if result.entry is not None:
                return result  # visible user text in the record always wins over a receipt
            # A tool result carries no user text, or only hidden harness text (which changes no state).
            notice = self._record_entries(identity, _claude_receipts(message.get("content")),
                                          _claude_inputs(message.get("content")), value, record)
            return notice if notice.entries else result
        final = message.get("stop_reason") in COMPLETING_STOPS
        if not text.strip():
            if final:
                self.state.end_turn()  # the turn ended without a visible reply; the next one starts fresh
            return Result()
        hidden = internal_notification(text)
        entry = Entry(identity[0], identity[1], SPEAKER_AGENT, _entry_text(text), record.offset, record.length, _timestamp(value),
                      partial=not final)
        if hidden:
            if final:
                self.state.end_turn()
            return Result(hidden=True)
        completion = None
        if final:
            reply = message.get("id") if isinstance(message.get("id"), str) and message.get("id") else None
            key = (KIND_NATIVE, reply) if reply else identity
            origin, work = self.state.end_turn()
            completion = Completion(key[0], key[1], record.offset, record.length, _timestamp(value), text.strip(), origin, work)
        return Result(entry=entry, completion=completion)

    def _user(self, identity, text, value, record) -> Result:
        if not text.strip():
            return Result()
        stripped = text.lstrip(_LEADING_SPACE)
        if stripped.startswith("<command-name>"):
            command = _COMMAND_RECORD.match(stripped)
            if command is None:
                return Result(hidden=True)
            name = command.group(1) if command.group(1).startswith("/") else "/" + command.group(1)
            typed = (name + " " + (command.group(2) or "").strip()).strip()
            # Shown, but not a turn: commands such as /compact or /clear get no assistant reply to end one.
            return Result(entry=replace(_user_entry(identity[0], identity[1], typed, value, record.offset, record.length),
                                        command=True))
        if stripped.startswith(_COMMAND_PREFIXES):
            return Result(hidden=True)
        entry = _user_entry(identity[0], identity[1], text, value, record.offset, record.length)
        if entry.speaker == SPEAKER_USER and internal_notification(text):
            return Result(hidden=True)
        system = self._proven_type(entry)
        if system is None:
            # Genuine user input: a plain entry, or text that only looks like a marker (nothing the application stored). It is
            # work whenever it arrives in the turn, and a notice that comes after it cannot take that back.
            self.state.work = True
        if self.state.origin is None:
            # The first input of a turn names who started it; a later one (steering) does not change that.
            self.state.origin = ORIGIN_USER if system is None else ORIGIN_NUDGE if system == NUDGE_TYPE else ORIGIN_SYSTEM
        return Result(entry=entry)

    @staticmethod
    def _record_entries(identity, receipts, inputs, value, record) -> Result:
        """The entries of one tool-result record, in block order: a successful outgoing message as a text-free notice, then each
        ``request_user_input`` presentation as an agent message of its own. None of them touches turn state: not an input, a reply,
        the turn's origin or user work."""
        entries = []
        if receipts:
            entries.append(Entry(identity[0], identity[1], SPEAKER_NOTICE, "", record.offset, record.length, _timestamp(value),
                                 receipts=receipts))
        for index, shown in enumerate(inputs):
            # One identity per presentation, scoped by its record's own: stable across windows, pages and re-reads.
            entries.append(Entry(identity[0], f"{identity[1]}#input{index}", SPEAKER_AGENT, _entry_text(shown["message"]),
                                 record.offset, record.length, _timestamp(value), partial=False,
                                 references=tuple(map(user_input.key, shown["references"]))))
        return Result(entry=entries[0], extra=tuple(entries[1:])) if entries else Result()

    def _proven_type(self, entry: Entry) -> str | None:
        """The application's input type this entry is proven to be, or ``None`` for anything a person (or a program) typed."""
        if entry.speaker != SPEAKER_NOTICE or entry.marker is None or self.resolver is None:
            return None
        return self.resolver(entry.marker, entry.marker_type)

    # ----- Codex -----------------------------------------------------------------------------------------------------

    def _codex(self, value: dict, record) -> Result:
        payload = value.get("payload")
        if not isinstance(payload, dict):
            return Result()
        kind, inner = value.get("type"), payload.get("type")
        if kind == "session_meta":
            self.bound = self.state.bound = payload.get("id") == self.conversation_id
            return Result()
        if not self.bound:
            return Result()
        state = self.state
        if kind == "event_msg":
            if inner in CODEX_TURN_START:
                if not state.turn_open:
                    state.end_turn()
                state.turn_open, state.turn_done, state.candidate = True, False, None
                return Result()
            if inner == "user_message":
                state.turn_open, state.turn_done, state.candidate = True, False, None
                return self._user((KIND_RAW, f"{record.offset}#0"), payload.get("message") or "", value, record)
            if inner == "item_completed":
                item = payload.get("item")
                if isinstance(item, dict) and item.get("type") == "UserMessage":
                    state.turn_open, state.turn_done, state.candidate = True, False, None
                    native = item.get("id") if isinstance(item.get("id"), str) and item.get("id") else None
                    identity = (KIND_NATIVE, native) if native else (KIND_RAW, f"{record.offset}#0")
                    return self._user(identity, text_blocks(item.get("content")), value, record)
                thread = payload.get("thread_id")
                if isinstance(item, dict) and item.get("type") == "McpToolCall" and thread in (None, self.conversation_id):
                    native = item.get("id") if isinstance(item.get("id"), str) and item.get("id") else None
                    identity = (KIND_NATIVE, native) if native else (KIND_RAW, f"{record.offset}#0")
                    return self._record_entries(identity, _codex_receipts(item), _codex_inputs(item), value, record)
                return Result()
            if inner in CODEX_TURN_ABORT:
                state.turn_open, state.turn_done, state.candidate = False, False, None
                state.end_turn()
                return Result()
            if inner in CODEX_TURN_END:
                return self._codex_turn_end(payload, value, record)
            return Result()
        if kind == "response_item" and inner == "message" and payload.get("role") == "assistant":
            if payload.get("channel") == "analysis":
                return Result()
            text = text_blocks(payload.get("content"))
            if not text.strip() or internal_notification(text):
                return Result(hidden=bool(text.strip()))
            native = payload.get("id") if isinstance(payload.get("id"), str) and payload.get("id") else None
            identity = (KIND_NATIVE, native) if native else (KIND_RAW, f"{record.offset}#0")
            final = payload.get("phase") == "final_answer" or payload.get("channel") == "final"
            entry = Entry(identity[0], identity[1], SPEAKER_AGENT, _entry_text(text), record.offset, record.length,
                          _timestamp(value), partial=False if final else None)
            completion = None
            if final and not state.turn_done:
                state.turn_done, state.candidate = True, None
                completion = Completion(identity[0], identity[1], record.offset, record.length, _timestamp(value), text.strip(),
                                        state.origin or ORIGIN_UNKNOWN, state.work)
            elif not final and not state.turn_done:
                state.candidate = {"kind": identity[0], "key": identity[1], "offset": record.offset, "length": record.length,
                                   "timestamp": _timestamp(value), "preview": text.strip()[:240]}
            return Result(entry=entry, completion=completion)
        return Result()

    def _codex_turn_end(self, payload: dict, value: dict, record) -> Result:
        state = self.state
        candidate, state.candidate = state.candidate, None
        done, state.turn_open, state.turn_done = state.turn_done, False, False
        origin, work = state.end_turn()
        if done:
            return Result()
        if candidate:
            return Result(completion=Completion(candidate["kind"], candidate["key"], candidate["offset"], candidate["length"],
                                                candidate.get("timestamp"), candidate.get("preview", ""), origin, work))
        last = payload.get("last_agent_message")
        if isinstance(last, str) and last.strip() and not internal_notification(last):
            key = f"{record.offset}#end"
            entry = Entry(KIND_RAW, key, SPEAKER_AGENT, _entry_text(last), record.offset, record.length, _timestamp(value),
                          partial=False, turn_end=True)
            return Result(entry=entry, completion=Completion(KIND_RAW, key, record.offset, record.length, _timestamp(value),
                                                             last.strip(), origin, work))
        return Result()
