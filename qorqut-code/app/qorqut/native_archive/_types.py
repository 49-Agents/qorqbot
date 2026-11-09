"""Immutable typed inputs and results of the native raw archive.

Everything here is a plain value: building one touches no file, process or database.
Identifiers keep their full registered values; an identifier that cannot be stored
exactly (too long for its fixed-size record or directory name) is rejected, never
shortened.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import re
from typing import Literal

Provider = Literal["claude", "codex"]
PROVIDERS = ("claude", "codex")

AGENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# Directory-name budget for the percent-encoded native conversation ID. A native ID is
# opaque; the original value is kept in every receipt, so the encoding only has to be
# reversible and filesystem-safe. Longer IDs are refused rather than shortened.
MAX_ENCODED_CONVERSATION = 128
MAX_PATH_BYTES = 1024
MAX_TOKEN_BYTES = 128

# Reasons a receipt is not a complete observation. A receipt without reasons captured every
# source byte of one stable observation (it does not say the native session ended).
REASON_UNSTABLE = "source_changed_during_observation"
REASON_PARTIAL_TAIL = "partial_tail"
REASONS = (REASON_PARTIAL_TAIL, REASON_UNSTABLE)

RECORD_KINDS = ("json", "blank", "malformed", "oversized", "partial")
INDEX_KINDS = {1: "malformed", 2: "oversized"}


class CaptureError(ValueError):
    """A rejected or uncertain archive operation.

    ``code`` names the condition, ``retryable`` says whether the same request can later
    succeed without anyone repairing the archive or the source, and ``latest`` is the last
    trusted receipt of the lineage when one exists. A failed capture never replaces, rewrites
    or discards a trusted prefix; the lineage stays readable up to ``latest``.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False, latest: "CaptureReceipt | None" = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.latest = latest


def text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError(f"{name} must not contain control characters")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be valid Unicode text") from None
    if size > limit:
        raise ValueError(f"{name} exceeds {limit} bytes; it is refused, never shortened")
    return value


def absolute_path(value: object, name: str) -> str:
    """An exact absolute path in normal form: no relative, ``.``, ``..`` or empty component."""
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    path = text(value, name, MAX_PATH_BYTES)
    parts = path.split("/")
    if (not path.startswith("/") or path == "/" or "\x00" in path
            or any(part in {"", ".", ".."} for part in parts[1:])):
        raise ValueError(f"{name} must be an exact absolute path without relative, empty or parent components")
    return path


def encode_component(value: str) -> str:
    """Reversible filesystem-safe form of an opaque identifier (``%XX`` for each unsafe byte)."""
    out = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        out.append(char if byte < 0x80 and (char.isalnum() or char in "_-") else f"%{byte:02X}")
    return "".join(out)


def decode_component(encoded: str) -> str:
    """Inverse of :func:`encode_component`; only the canonical encoding is accepted."""
    if not re.fullmatch(r"(?:[A-Za-z0-9_-]|%[0-9A-F]{2})+", encoded or ""):
        raise ValueError("not an encoded archive component")
    raw = bytearray()
    index = 0
    while index < len(encoded):
        if encoded[index] == "%":
            raw.append(int(encoded[index + 1:index + 3], 16))
            index += 3
        else:
            raw.append(ord(encoded[index]))
            index += 1
    value = bytes(raw).decode("utf-8")
    if encode_component(value) != encoded:
        raise ValueError("not a canonical archive component")
    return value


def _identity(agent_id: object, provider: object, conversation_id: object, generation: object) -> None:
    if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
        raise ValueError("agent_id must be a registered agent identifier")
    if provider not in PROVIDERS:
        raise ValueError("provider must be 'claude' or 'codex'")
    text(conversation_id, "conversation_id", MAX_ENCODED_CONVERSATION)
    if len(encode_component(conversation_id)) > MAX_ENCODED_CONVERSATION:
        raise ValueError(f"encoded conversation_id exceeds {MAX_ENCODED_CONVERSATION} characters; it is refused, never shortened")
    if type(generation) is not int or generation < 1:
        raise ValueError("generation must be a positive integer")


@dataclass(frozen=True)
class CaptureBinding:
    """The verified identity of one native session's transcript source.

    The caller (the verified native-session adapter) is the only authority for it; a path or
    agent name supplied by a user is not a binding. ``conversation_id`` is the provider's exact
    native conversation/session ID and survives process generations; ``generation`` is the
    current session generation and is only observed metadata.
    """

    agent_id: str
    provider: Provider
    conversation_id: str
    generation: int
    workspace: str
    source_path: str

    def __post_init__(self) -> None:
        _identity(self.agent_id, self.provider, self.conversation_id, self.generation)
        object.__setattr__(self, "workspace", absolute_path(self.workspace, "workspace"))
        object.__setattr__(self, "source_path", absolute_path(self.source_path, "source_path"))

    @property
    def archive_id(self) -> str:
        return f"{self.provider}:{encode_component(self.conversation_id)}"


@dataclass(frozen=True)
class FinalCaptureEvidence:
    """Exact process-exit evidence for one session generation, supplied after verification.

    The archive never observes process liveness; it only refuses to claim a final tail without
    this value and a stable source observation.
    """

    agent_id: str
    provider: Provider
    conversation_id: str
    generation: int
    process_token: str
    exited_at: datetime

    def __post_init__(self) -> None:
        _identity(self.agent_id, self.provider, self.conversation_id, self.generation)
        text(self.process_token, "process_token", MAX_TOKEN_BYTES)
        if not isinstance(self.exited_at, datetime) or self.exited_at.tzinfo is None:
            raise ValueError("exited_at must be a timezone-aware datetime")
        object.__setattr__(self, "exited_at", self.exited_at.astimezone(timezone.utc))


@dataclass(frozen=True)
class CaptureReceipt:
    """One immutable, bounded record of a committed archive prefix.

    A receipt does not own bytes: ``length`` and ``prefix_sha256`` describe the first ``length``
    bytes of the lineage's single append-only payload. ``complete`` is true when that prefix is
    every byte of one stable source observation; ``final`` is set only when exact exit evidence
    accompanied such an observation (the verified final tail of ``generation``).
    """

    receipt_id: str
    archive_id: str
    sequence: int
    predecessor_id: str | None
    predecessor_sha256: str | None
    record_sha256: str
    agent_id: str
    provider: str
    conversation_id: str
    generation: int
    workspace: str
    source_path: str
    source_device: int
    source_inode: int
    observed_at: str
    source_size: int
    source_size_end: int
    length: int
    prefix_sha256: str
    appended: int
    complete: bool
    incomplete_reasons: tuple[str, ...]
    final: FinalCaptureEvidence | None
    records: int
    malformed: int
    record_boundary: int
    partial_tail_bytes: int
    index_entries: int
    index_sha256: str


@dataclass(frozen=True)
class RawRange:
    """Bytes of a receipt-bounded read; ``next_offset`` is None once ``boundary`` is reached."""

    receipt_id: str
    data: bytes
    offset: int
    next_offset: int | None
    boundary: int


@dataclass(frozen=True)
class RawRecord:
    """A raw record locator inside a receipt's committed prefix.

    ``kind`` is ``json`` (a complete JSON object with a string ``type``; ``value`` is the parsed
    object), ``blank``, ``malformed`` (complete line that is not such an object),
    ``oversized`` (complete line above the parse cap) or ``partial`` (unterminated final line).
    Nothing here decides who authored a record.
    """

    offset: int
    length: int
    kind: str
    value: dict | None = None

    @property
    def end(self) -> int:
        return self.offset + self.length


@dataclass(frozen=True)
class IndexEntry:
    """Evidence for one complete record that is not an ordinary JSON object record."""

    offset: int
    length: int
    kind: str
