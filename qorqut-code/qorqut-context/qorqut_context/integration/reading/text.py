"""Bounded text for agent and owner bootstrap, derived from the same pinned graph ``context_home`` reads.

``home_text`` says what the published graph holds and how to read it, and lists none of its nodes: the published
revision, the totals, how many nodes there are of each type, and the four reads. It used to list the twelve most
connected nodes; those are hubs, so the people, companies and terms an agent needs to place were almost never among
them, and the list read as if it were what is known. When the graph has a personal context node, the text also carries
that record's facts, within their own bound, so every agent starts with what the owner already said about themselves
and does not ask for it again. It states plainly the states that are not a graph (``not_initialized``,
``unavailable``) or an empty one. The published graph is shared by every registered agent, so the text never depends
on who asks or on a business; the business argument exists only to satisfy the host's text-provider call shape and is
ignored. Rendering never raises: a failed read is reported as ``unavailable`` text, never as an empty graph."""

from __future__ import annotations

from qorqut_operations.contracts import DomainError

from .reader import SharedContextReader

TYPES = 8
TYPE_CHARS = 30
SUMMARY_CHARS = 240
MAX_CHARACTERS = 6000
PERSONAL_CHARACTERS = 3000
PERSONAL_MEMBERS = 8
PERSONAL_MEMBER_CHARS = 220
PERSONAL_HIDDEN = frozenset({"basic_entity_type"})
TRUNCATED = "\n[truncated; read the rest with context_home, context_find, context_object and context_expand]"
HOLDS = ("One published graph shared by every registered agent; drafts are never shown. It records the people, companies, "
         "projects, terms and other things that have been filed, how they relate, and the notes and sources about them.")
READS = ("None of its nodes are listed here. When a message names someone or something you cannot place, look it up "
         "before asking: context_find(query) takes a name or a question and returns the most relevant records first; "
         "context_object(ref) reads one record with its relationships; context_expand(ref) shows what a node is "
         "connected to; context_home pages every node (follow page.next_cursor). Every response names the published "
         "revision it was read at. Notes are separate records, not graph nodes: find them with context_find(kind=note) "
         "and open note:ID with context_object.")


def _clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _types(types):
    shown = [f"{_clip(item['type'], TYPE_CHARS)} {item['count']}" for item in (types or [])[:TYPES]]
    if not shown:
        return None
    return "Nodes by type: " + ", ".join(shown) + ("; other types fewer." if len(types) > TYPES else ".")


def _personal_lines(ref, record, characters=PERSONAL_CHARACTERS):
    """The owner's recorded facts, shortest entries first so that names, contacts and dates always fit; whatever is
    cut or left out is named, so a reader knows the full record holds more."""
    rule = (f"About the owner (personal record {ref}): the one store of what the owner has told us about themselves. "
            "Use it. Never ask the owner for a fact this record holds. Before you ask the owner anything about "
            f"themselves, or build a form of such questions, read context_object(ref='{ref}') fresh, fill in what it "
            "holds and ask only for what is missing. Ordubashy maintains it; other agents pass new personal facts to "
            "Ordubashy through the existing inbox, with the source and date.")
    entries, more = [], []
    for name, members in (record.get("attributes") or {}).items():
        if name in PERSONAL_HIDDEN or not members:
            continue
        cut = len(members) > PERSONAL_MEMBERS or any(
            len(" ".join(str(member).split())) > PERSONAL_MEMBER_CHARS for member in members)
        entries.append((f"- {name}: " + "; ".join(_clip(member, PERSONAL_MEMBER_CHARS)
                                                 for member in members[:PERSONAL_MEMBERS]), name, cut))
    lines, used = [rule], len(rule)
    summary = _clip(record.get("summary") or "", SUMMARY_CHARS)
    if summary:
        lines.append(summary)
        used += len(summary) + 1
    for line, name, cut in sorted(entries, key=lambda entry: (len(entry[0]), entry[1])):
        if used + len(line) + 1 > characters:
            more.append(name)
            continue
        lines.append(line)
        used += len(line) + 1
        if cut:
            more.append(name)
    if more:
        lines.append(f"The record holds more under: {', '.join(sorted(more))}. Open {ref} for all of it.")
    return lines


def render_home(dto, *, characters=MAX_CHARACTERS, types=None) -> str:
    """The bounded text for one ``context_home`` DTO (or a not-ready status DTO) and, when known, the node counts by
    type; it opens with a plain title line so it nests under a host section heading. The owner's record takes its own
    bound, on top of ``characters``."""
    state = dto.get("status")
    head = "Shared Context (published graph)"
    if state == "not_initialized":
        return (f"{head}\nNot initialized: no published Context revision exists yet, so there is nothing to read. "
                "This is not an empty graph; context reads report the same state until the first revision is published.")
    if state != "ready":
        reason = dto.get("reason") or {}
        return (f"{head}\nUnavailable ({reason.get('code') or 'unknown'}): the published Context cannot be read now. "
                "This is not an empty graph; do not conclude that nothing is known. Retry later or report it to the owner.")
    totals = dto["totals"]
    summary = (f"Revision {dto['revision']}: {totals['nodes']} nodes, {totals['edges']} relationships, "
               f"{totals['sources']} sources, {totals['events']} events.")
    if dto["empty"]:
        return (f"{head}\n{summary}\nInitialized and empty: no nodes or relationships are published yet. "
                "The same published graph is shared by every registered agent.")
    pointer = dto.get("personal_context")
    personal_ref = pointer["ref"] if pointer else None
    text = "\n".join(filter(None, (head, summary, HOLDS, _types(types), READS)))
    text = text if len(text) <= characters else text[:characters - len(TRUNCATED)].rstrip() + TRUNCATED
    if personal_ref:
        head_line, _, rest = text.partition("\n")
        text = "\n".join([head_line, *_personal_lines(personal_ref, pointer), rest])
    return text


class PublishedContextText:
    """The host's ``home_text(business_id)`` provider over one shared reader."""

    def __init__(self, reader: SharedContextReader, *, characters=MAX_CHARACTERS):
        if not isinstance(reader, SharedContextReader):
            raise TypeError("PublishedContextText requires a SharedContextReader")
        self.reader, self.characters = reader, characters

    def status(self) -> dict:
        """The reader's readiness (``ready`` / ``not_initialized`` / ``unavailable``) for callers that report it."""
        return self.reader.status()

    def home_text(self, business_id=None) -> str:
        types = None
        try:
            dto = self.reader.home(limit=1)
            if dto.get("status") == "ready" and not dto["empty"]:
                counted = self.reader.entity_types(revision=dto["revision"])
                types = counted["types"] if counted.get("status") == "ready" else None
        except (DomainError, OSError):
            dto = {"status": "unavailable", "revision": None, "reason": {"code": "read_failed", "message": ""}}
        return render_home(dto, characters=self.characters, types=types)
