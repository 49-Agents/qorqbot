"""JSONL framing and provider identity checks for raw transcript bytes.

Identity rules are the v2.2 donor's (``app/qorqut_app/native/transcripts/_providers.py`` at
``qorqut-v2.2/qorqut-code`` commit df7daa7fddf387a7e91212f5ed831b32c0735f96): Claude session /
workspace evidence with sidechain records never establishing the binding, and Codex session
metadata that must precede its records. Unlike the donor they run incrementally over a bounded
stream and keep no state except one sticky flag, and nothing here projects Chat text or
correlates inputs: provider bytes are framed, parsed when valid and otherwise only described.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import struct

from ._types import INDEX_KINDS

MAX_RECORD_BYTES = 32 * 1024 * 1024
ENTRY = struct.Struct("<QQII8x")
ENTRY_SIZE = ENTRY.size
_KIND_CODES = {name: code for code, name in INDEX_KINDS.items()}


class IdentityError(ValueError):
    """Valid provider records that contradict the bound session or workspace."""


@dataclass(frozen=True)
class Frame:
    """One complete line of the stream: ``kind`` is ``json``, ``blank``, ``malformed`` or ``oversized``."""

    offset: int
    length: int
    kind: str
    value: dict | None


def pack_entry(frame: Frame) -> bytes:
    return ENTRY.pack(frame.offset, frame.length, _KIND_CODES[frame.kind], 0)


def unpack_entries(data: bytes) -> list[tuple[int, int, str]]:
    entries = []
    for start in range(0, len(data), ENTRY_SIZE):
        offset, length, code, _ = ENTRY.unpack_from(data, start)
        entries.append((offset, length, INDEX_KINDS[code]))
    return entries


def _classify(line: bytes) -> tuple[str, dict | None]:
    if not line.strip():
        return "blank", None
    try:
        record = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):
        return "malformed", None
    if isinstance(record, dict) and isinstance(record.get("type"), str):
        return "json", record
    return "malformed", None


class FrameScanner:
    """Splits a byte stream into newline-terminated records with bounded memory.

    ``start`` must be a record boundary. A line longer than ``max_record`` is reported as
    ``oversized`` without being parsed or buffered. After the last chunk, :meth:`pending`
    describes the unterminated final line (retained bytes, not a record).
    """

    def __init__(self, start: int, *, max_record: int | None = None):
        self._cap = MAX_RECORD_BYTES if max_record is None else max_record
        self._line_start = start
        self._length = 0
        self._buffer = bytearray()
        self._over = False

    def feed(self, data: bytes) -> list[Frame]:
        frames: list[Frame] = []
        index, size = 0, len(data)
        while index < size:
            end = data.find(b"\n", index)
            if end < 0:
                self._extend(data, index, size)
                break
            if not self._length and end - index < self._cap:
                kind, value = _classify(data[index:end])
                length = end - index
            else:
                self._extend(data, index, end)
                kind, value = ("oversized", None) if self._over else _classify(bytes(self._buffer))
                length = self._length
            frames.append(Frame(self._line_start, length + 1, kind, value))
            self._line_start += length + 1
            self._length = 0
            self._buffer.clear()
            self._over = False
            index = end + 1
        return frames

    def _extend(self, data: bytes, start: int, end: int) -> None:
        count = end - start
        if count <= 0:
            return
        if not self._over:
            if len(self._buffer) + count > self._cap:
                self._over = True
                self._buffer.clear()
            else:
                self._buffer += memoryview(data)[start:end]
        self._length += count

    def pending(self) -> tuple[int, int]:
        """Offset and length of the unterminated final line (length 0 when the stream ends on a boundary)."""
        return self._line_start, self._length


class IdentityEvidence:
    """Incremental session/workspace evidence for one binding; ``bound`` is sticky."""

    def __init__(self, provider: str, conversation_id: str, workspace: str, *, bound: bool = False):
        self._provider, self._conversation, self._workspace = provider, conversation_id, workspace
        self.bound = bound

    def check(self, record: dict) -> None:
        if self._provider == "claude":
            self._claude(record)
        else:
            self._codex(record)

    def _claude(self, record: dict) -> None:
        if "sessionId" in record and record["sessionId"] != self._conversation:
            raise IdentityError("Transcript contains a different Claude session")
        if record.get("sessionId") == self._conversation:
            if record.get("cwd") == self._workspace and not record.get("isSidechain"):
                self.bound = True
        if record["type"] in {"user", "assistant"} and record.get("sessionId") != self._conversation:
            raise IdentityError("Claude message lacks the bound session identity")

    def _codex(self, record: dict) -> None:
        payload = record.get("payload")
        if record["type"] == "session_meta":
            if (not isinstance(payload, dict) or payload.get("id") != self._conversation
                    or payload.get("cwd") != self._workspace):
                raise IdentityError("Codex metadata does not match the bound session/workspace")
            self.bound = True
        elif record["type"] in {"response_item", "event_msg", "turn_context"} and not self.bound:
            raise IdentityError("Codex record precedes bound session metadata")
