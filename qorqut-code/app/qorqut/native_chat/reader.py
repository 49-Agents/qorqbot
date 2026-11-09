"""Read a bounded window of one conversation from the archive and derive its Chat entries. Nothing here stores anything.

The archive's payload is one append-only file per lineage, so a byte offset is a stable position and a newline always ends a
record (a newline inside a JSON string is escaped). A window therefore starts at the first newline inside it and reads forward
to the committed record boundary; if it holds fewer entries than asked, the window grows backwards. Cost is bounded by
``MAX_WINDOW`` per request and the archive's own prefix verification.
"""

from __future__ import annotations

from collections import OrderedDict
import threading

from qorqut.native_archive import CaptureError, MAX_READ_BYTES

from .contracts import STATE_SUBMITTED, STATE_SUBMITTING, STATE_UNCERTAIN
from .projection import Entry, Lineage, Projector, SPEAKER_AGENT, SPEAKER_NOTICE, unwrap_marker
from .references import unavailable

FIRST_WINDOW = 1 << 20
MAX_WINDOW = 32 << 20
PAGE_LIMIT = 100


# An input counts as delivered system input only once its claim was made and it was not definitely refused: waiting, failed and
# undelivered inputs were never typed, so a notice carrying their marker is somebody else's text.
DELIVERED_STATES = frozenset({STATE_SUBMITTING, STATE_SUBMITTED, STATE_UNCERTAIN})


class Window:
    """A page of projected entries read from the archive and the cursor for the page before it. The entries are a projection of
    raw records, not yet what an owner sees: ``visible`` applies the durable completions."""

    def __init__(self, entries: list[Entry], older: int | None, boundary: int):
        self.entries, self.older, self.boundary = entries, older, boundary


class ConversationReader:
    """Window reads with a small in-process cache, so a poll with no growth costs no archive pass.

    A receipt id names only the provider, conversation and sequence, so it repeats across agents and lineages. The cache key is
    the receipt's full identity and content (agent, archive, receipt id, record hash), the projection asked for (provider and
    conversation) and the window (end, limit); a projection that is not the receipt's own is refused before any lookup."""

    def __init__(self, *, cache_size: int = 64):
        self._cache: OrderedDict = OrderedDict()
        self._size = cache_size
        self._guard = threading.Lock()

    def window(self, archive, receipt, provider: str, conversation_id: str, *, before: int | None = None,
               limit: int = 50) -> Window:
        if (provider, conversation_id) != (receipt.provider, receipt.conversation_id):
            raise CaptureError("receipt_mismatch", "The receipt names another provider or conversation")
        limit = max(1, min(int(limit), PAGE_LIMIT))
        end = receipt.record_boundary if before is None else min(before, receipt.record_boundary)
        key = (receipt.agent_id, receipt.archive_id, receipt.receipt_id, receipt.record_sha256, provider, conversation_id,
               end, limit)
        with self._guard:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
        found = self._read(archive, receipt, provider, conversation_id, end, limit)
        with self._guard:
            self._cache[key] = found
            while len(self._cache) > self._size:
                self._cache.popitem(last=False)
        return found

    def _read(self, archive, receipt, provider, conversation_id, end, limit) -> Window:
        size, begin, entries = FIRST_WINDOW, 0, []
        while True:
            start = max(0, end - size)
            begin = 0 if start == 0 else self._align(archive, receipt, start, end)
            entries = [] if begin is None else self._project(archive, receipt, provider, conversation_id, begin, end)
            if start == 0 or len(entries) >= limit or size >= MAX_WINDOW:
                break
            size *= 4
        first = max(0, len(entries) - limit)
        # One record can yield several entries (a result with several presentations); a page never splits a record, because the
        # cursor for the page before it is the record's offset.
        while 0 < first < len(entries) and entries[first - 1].offset == entries[first].offset:
            first -= 1
        page = entries[first:]
        # More history exists when the window held more entries than the page, or does not reach the start of the payload.
        more = first > 0 or (begin is not None and begin > 0)
        return Window(page, page[0].offset if page and more else None, receipt.record_boundary)

    @staticmethod
    def _align(archive, receipt, start: int, end: int) -> int | None:
        """The first record boundary at or after ``start``: the byte after the first newline in the window."""
        position = start
        while position < end:
            chunk = archive.read_raw(receipt, offset=position, limit=min(MAX_READ_BYTES, 1 << 20, end - position))
            index = chunk.data.find(b"\n")
            if index >= 0:
                return position + index + 1
            position += len(chunk.data)
            if not chunk.data:
                break
        return None

    @staticmethod
    def _project(archive, receipt, provider, conversation_id, begin, end) -> list[Entry]:
        projector = Projector(provider, conversation_id)
        # The lineage's identity was verified when the archive bound it; a window may begin mid-conversation.
        projector.bound = True
        entries = []
        with archive.iter_records(receipt, offset=begin) as records:
            for record in records:
                if record.kind == "partial" or record.end > end:
                    break
                entries.extend(projector.feed(record).entries)
        return entries


def proven_type(row: dict | None, declared: str | None) -> str | None:
    """The application input type a leading marker is proven to be: the stored type of the system input admitted for this
    conversation when it matches the declared one and the input reached delivery. Anything else proves nothing."""
    return row["input_type"] if row is not None and declared == row["input_type"] and row["state"] in DELIVERED_STATES else None


def provenance(entry: Entry, row: dict | None) -> dict:
    """What the application can say about a notice that starts with a system marker. Verified only when the marker belongs to a
    system input admitted for this very conversation, the typed type matches the stored one, and the input reached delivery.
    Equal text alone never verifies anything."""
    unverified = {"input_id": None, "input_type": entry.marker_type, "verified": False}
    if row is None:
        return {**unverified, "reason": "no_matching_input"}
    if entry.marker_type != row["input_type"]:
        return {**unverified, "reason": "type_mismatch"}
    if row["state"] not in DELIVERED_STATES:
        return {**unverified, "reason": "not_delivered"}
    return {"input_id": row["id"], "input_type": row["input_type"], "verified": True}


def bound_completion(entry: Entry, replies: dict) -> dict | None:
    """The durable completion bound to exactly this entry's record in this conversation, or ``None``. ``replies`` maps a record
    position to the completion indexed there. The record's length must agree, and a turn-end entry is bound only by the fallback
    identity the complete-stream index gave it."""
    row = replies.get(entry.offset)
    if row is None or row["raw_length"] != entry.length:
        return None
    if entry.turn_end and (row["identity_kind"], row["identity_key"]) != (entry.identity_kind, entry.key):
        return None
    return row


def visible(entries: list[Entry], replies: dict, sends: dict | None = None) -> list[tuple[Entry, dict | None]]:
    """The entries an owner sees, each with the completion bound to it. A turn-end entry stands in for a reply only when the
    complete-stream index completed that exact record: a window that starts mid-turn cannot tell a lone turn-end message from a
    repeat of an assistant message that was already shown (or lies on an older page), so without the durable completion it is
    dropped rather than shown twice or under an identity the index never gave it. An outgoing-message candidate is shown only
    while ``sends`` (the stored messages this agent originally sent to agents) still holds one of its receipts."""
    shown, sends = [], sends or {}
    for entry in entries:
        row = bound_completion(entry, replies)
        if entry.turn_end and row is None:
            continue
        if entry.receipts and not any(identity in sends for identity in entry.receipts):
            continue
        shown.append((entry, row))
    return shown


def agent_message(entry: Entry, sends: dict) -> dict:
    """The successful outgoing messages of one entry: the receipts still stored as this agent's messages to agents, in receipt
    order, and their distinct recipients in the order first seen. No body, raw receipt or profile is carried."""
    found = [identity for identity in entry.receipts if identity in sends]
    recipients = {sends[identity]["agent_id"]: sends[identity] for identity in found}
    return {"message_ids": found, "recipients": list(recipients.values())}


def entry_view(entry: Entry, lineage: Lineage, inputs: dict, completion: dict | None, sends: dict | None = None,
               references: dict | None = None) -> dict:
    """One entry as the owner sees it. ``inputs`` maps the marker ids this page shows to their stored system inputs,
    ``completion`` is the durable completion bound to this entry's record (``bound_completion``) and ``sends`` the stored
    messages the page's outgoing-message notices name and ``references`` the owner's resolved view of the references the page's
    presentations name (``resolve_references``); an entry that completed a reply carries that reply's durable completion sequence
    and id, an outgoing-message notice carries its ``agent_message`` and a presentation its ``references``."""
    view = {"id": entry.public_id(lineage), "kind": "message", "speaker": entry.speaker, "text": entry.text,
            "timestamp": entry.timestamp, "position": entry.offset, "partial": entry.partial,
            "identity": entry.identity_kind, "system": None, "completion_sequence": None, "completion_id": None}
    if entry.receipts:
        view["agent_message"] = agent_message(entry, sends or {})
    elif entry.references is not None:
        # A presentation is an ordinary agent message, never a completion: nothing here binds it to a reply.
        view["references"] = [(references or {}).get(ref) or unavailable(*ref) for ref in entry.references]
    elif entry.speaker == SPEAKER_NOTICE:
        view["text"] = unwrap_marker(entry.text)
        view["system"] = provenance(entry, inputs.get(entry.marker))
    elif completion is not None and entry.speaker == SPEAKER_AGENT:
        view["completion_sequence"], view["completion_id"] = completion["completion_sequence"], completion["id"]
    return view


__all__ = ["CaptureError", "ConversationReader", "Window", "bound_completion", "entry_view", "visible"]
