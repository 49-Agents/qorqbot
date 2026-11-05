"""On-disk records: the fixed-size receipt, the latest pointer and the lineage header.

A receipt record is exactly ``RECORD_SIZE`` bytes: one canonical JSON line, one
``sha256=<digest of that line>`` line, then space padding and a final newline. The constant
size makes the per-capture metadata cost independent of how much the transcript contains; the
digest of the *whole* record is what the next receipt (and the pointer) cite, so the chain is
tamper-evident from the pointer back to the first capture.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from ._types import (
    CaptureReceipt, FinalCaptureEvidence, PROVIDERS, REASONS, decode_component, encode_component,
)

RECORD_SIZE = 4096
RECEIPT_FORMAT = "qorqut.native-archive.receipt/1"
POINTER_FORMAT = "qorqut.native-archive.pointer/1"
HEADER_FORMAT = "qorqut.native-archive.lineage/1"
SMALL_LIMIT = 4096
HEX = re.compile(r"[0-9a-f]{64}")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_RECEIPT_ID = re.compile(r"(claude|codex):([A-Za-z0-9_%-]{1,384}):([1-9][0-9]{0,11})")

_INT_FIELDS = ("generation", "source_device", "source_inode", "source_size", "source_size_end", "length",
               "appended", "records", "malformed", "record_boundary", "partial_tail_bytes", "index_entries")
_STR_FIELDS = ("agent_id", "provider", "conversation_id", "workspace", "source_path", "observed_at")
_KEYS = frozenset(_INT_FIELDS + _STR_FIELDS + (
    "format", "archive_id", "sequence", "predecessor_sha256", "prefix_sha256", "complete",
    "incomplete_reasons", "final", "index_sha256"))


class FormatError(ValueError):
    """A stored record is not a well-formed instance of its format."""


def canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def timestamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def receipt_id(archive_id: str, sequence: int) -> str:
    return f"{archive_id}:{sequence}"


def parse_receipt_id(value: object) -> tuple[str, str, int]:
    """(provider, encoded conversation, sequence) of a receipt ID, strictly."""
    match = _RECEIPT_ID.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise FormatError("not a receipt ID")
    provider, encoded, sequence = match.group(1), match.group(2), int(match.group(3))
    try:
        decode_component(encoded)
    except ValueError:
        raise FormatError("not a receipt ID") from None
    return provider, encoded, sequence


def receipt_name(sequence: int) -> str:
    return f"{sequence:012d}.rcpt"


def _record(body: bytes) -> bytes:
    check = b"sha256=" + hashlib.sha256(body).hexdigest().encode("ascii") + b"\n"
    used = len(body) + 1 + len(check)
    if used >= RECORD_SIZE:
        raise FormatError("receipt exceeds its fixed record size")
    return body + b"\n" + check + b" " * (RECORD_SIZE - used - 1) + b"\n"


def _open_record(data: bytes) -> dict:
    if len(data) != RECORD_SIZE:
        raise FormatError("receipt record has the wrong size")
    parts = data.split(b"\n", 2)
    if len(parts) != 3:
        raise FormatError("receipt record has no checksum line")
    body, check, padding = parts
    if check != b"sha256=" + hashlib.sha256(body).hexdigest().encode("ascii"):
        raise FormatError("receipt record checksum mismatch")
    if padding != b" " * (len(padding) - 1) + b"\n":
        raise FormatError("receipt record padding is not canonical")
    try:
        value = json.loads(body.decode("utf-8"))
    except ValueError as error:
        raise FormatError("receipt record is not JSON") from error
    if not isinstance(value, dict) or canonical(value) != body:
        raise FormatError("receipt record is not canonical JSON")
    return value


def record_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_receipt(fields: dict) -> bytes:
    return _record(canonical(fields))


def decode_receipt(data: bytes, *, archive_id: str, sequence: int) -> CaptureReceipt:
    """Parse and strictly validate one stored receipt record cited as ``archive_id`` number ``sequence``."""
    fields = _open_record(data)
    if set(fields) != _KEYS or fields["format"] != RECEIPT_FORMAT:
        raise FormatError("receipt record has unexpected fields")
    if fields["archive_id"] != archive_id or fields["sequence"] != sequence:
        raise FormatError("receipt record does not match the place it was found")
    for name in _INT_FIELDS + ("sequence",):
        if type(fields[name]) is not int or fields[name] < 0:
            raise FormatError(f"receipt field {name} is invalid")
    for name in _STR_FIELDS:
        if not isinstance(fields[name], str) or not fields[name]:
            raise FormatError(f"receipt field {name} is invalid")
    predecessor = fields["predecessor_sha256"]
    if (sequence == 1) != (predecessor is None) or (predecessor is not None and not HEX.fullmatch(str(predecessor))):
        raise FormatError("receipt predecessor is invalid")
    if not HEX.fullmatch(str(fields["prefix_sha256"])) or not HEX.fullmatch(str(fields["index_sha256"])):
        raise FormatError("receipt digest is invalid")
    reasons = fields["incomplete_reasons"]
    if (not isinstance(reasons, list) or list(dict.fromkeys(reasons)) != reasons
            or any(reason not in REASONS for reason in reasons)
            or not isinstance(fields["complete"], bool) or fields["complete"] == bool(reasons)):
        raise FormatError("receipt completeness is inconsistent")
    if fields["provider"] not in PROVIDERS or fields["generation"] < 1:
        raise FormatError("receipt identity is invalid")
    if archive_id != f"{fields['provider']}:{encode_component(fields['conversation_id'])}":
        raise FormatError("receipt archive ID does not match its identity")
    if fields["partial_tail_bytes"] != fields["length"] - fields["record_boundary"] or fields["record_boundary"] > fields["length"]:
        raise FormatError("receipt tail cursor is inconsistent")
    final = fields["final"]
    final_evidence = None
    if final is not None:
        if not isinstance(final, dict) or set(final) != {"process_token", "exited_at"} or not fields["complete"]:
            raise FormatError("receipt final evidence is invalid")
        try:
            final_evidence = FinalCaptureEvidence(
                fields["agent_id"], fields["provider"], fields["conversation_id"], fields["generation"],
                final["process_token"], datetime.fromisoformat(final["exited_at"]))
        except (ValueError, TypeError) as error:
            raise FormatError("receipt final evidence is invalid") from error
    return CaptureReceipt(
        receipt_id=receipt_id(archive_id, sequence), archive_id=archive_id, sequence=sequence,
        predecessor_id=receipt_id(archive_id, sequence - 1) if sequence > 1 else None,
        predecessor_sha256=predecessor, record_sha256=record_digest(data),
        agent_id=fields["agent_id"], provider=fields["provider"], conversation_id=fields["conversation_id"],
        generation=fields["generation"], workspace=fields["workspace"], source_path=fields["source_path"],
        source_device=fields["source_device"], source_inode=fields["source_inode"],
        observed_at=fields["observed_at"], source_size=fields["source_size"],
        source_size_end=fields["source_size_end"], length=fields["length"], prefix_sha256=fields["prefix_sha256"],
        appended=fields["appended"], complete=fields["complete"], incomplete_reasons=tuple(reasons),
        final=final_evidence, records=fields["records"], malformed=fields["malformed"],
        record_boundary=fields["record_boundary"], partial_tail_bytes=fields["partial_tail_bytes"],
        index_entries=fields["index_entries"], index_sha256=fields["index_sha256"])


def encode_pointer(archive_id: str, sequence: int, digest: str) -> bytes:
    return canonical({"format": POINTER_FORMAT, "archive_id": archive_id, "sequence": sequence,
                      "receipt_sha256": digest}) + b"\n"


def decode_pointer(data: bytes, archive_id: str) -> tuple[int, str]:
    try:
        value = json.loads(data.decode("utf-8"))
    except ValueError as error:
        raise FormatError("latest pointer is not JSON") from error
    if (not isinstance(value, dict) or set(value) != {"format", "archive_id", "sequence", "receipt_sha256"}
            or value["format"] != POINTER_FORMAT or value["archive_id"] != archive_id
            or type(value["sequence"]) is not int or value["sequence"] < 1
            or not HEX.fullmatch(str(value["receipt_sha256"])) or canonical(value) + b"\n" != data):
        raise FormatError("latest pointer is invalid")
    return value["sequence"], value["receipt_sha256"]


def encode_header(agent_id: str, provider: str, conversation_id: str) -> bytes:
    return canonical({"format": HEADER_FORMAT, "agent_id": agent_id, "provider": provider,
                      "conversation_id": conversation_id,
                      "archive_id": f"{provider}:{encode_component(conversation_id)}"}) + b"\n"


def decode_header(data: bytes, archive_id: str) -> dict:
    try:
        value = json.loads(data.decode("utf-8"))
    except ValueError as error:
        raise FormatError("lineage header is not JSON") from error
    if (not isinstance(value, dict) or set(value) != {"format", "agent_id", "provider", "conversation_id", "archive_id"}
            or value["format"] != HEADER_FORMAT or value["archive_id"] != archive_id
            or canonical(value) + b"\n" != data):
        raise FormatError("lineage header is invalid")
    if archive_id != f"{value['provider']}:{encode_component(str(value['conversation_id']))}":
        raise FormatError("lineage header does not match its location")
    return value
