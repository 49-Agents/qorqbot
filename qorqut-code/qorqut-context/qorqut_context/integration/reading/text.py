"""Bounded Markdown for agent and owner bootstrap, derived from the same pinned graph DTO ``context_home`` returns.

``home_text`` renders one page of ``SharedContextReader.home``: the published revision, the totals, node and
relationship references (``node:ID`` / ``edge:ID``) with their summaries, and an explicit statement for the states that
are not a graph (``not_initialized``, ``unavailable``) or an empty one. When the graph has a personal context node, the
text also carries that record's facts from the same page, within their own bound, so every agent starts with what the
owner already said about themselves and does not ask for it again. The published graph is shared by
every registered agent, so the text never depends on who asks or on a business; the business argument exists only to
satisfy the host's text-provider call shape and is ignored. Rendering never raises: a failed read is reported as
``unavailable`` text, never as an empty graph."""

from __future__ import annotations

from qorqut_operations.contracts import DomainError

from .reader import SharedContextReader

NODES = 12
RELATIONSHIPS = 3
ATTRIBUTE_NAMES = 6
ATTRIBUTE_MEMBERS = 4
ATTRIBUTE_CHARS = 80
SUMMARY_CHARS = 240
MAX_CHARACTERS = 6000
PERSONAL_CHARACTERS = 3000
PERSONAL_MEMBERS = 8
PERSONAL_MEMBER_CHARS = 220
PERSONAL_HIDDEN = frozenset({"basic_entity_type"})
TRUNCATED = "\n[truncated; read the rest with context_home, context_find, context_object and context_expand]"
READS = ("Read more with context_home (follow page.next_cursor), context_find, context_object(ref) and "
         "context_expand(ref); every response names the published revision it was read at. Notes are separate records, "
         "not graph nodes: find them with context_find(kind=note) and open note:ID with context_object.")


def _clip(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _attributes(view):
    shown = []
    for name, members in view["attributes"].items():
        if members == [view["summary"]]:
            continue
        shown.append(f"{name}: " + ", ".join(_clip(member, ATTRIBUTE_CHARS) for member in members[:ATTRIBUTE_MEMBERS])
                     + (", …" if len(members) > ATTRIBUTE_MEMBERS else ""))
        if len(shown) == ATTRIBUTE_NAMES:
            break
    omitted = len(view["attributes"]) > len(shown) and len(shown) == ATTRIBUTE_NAMES
    return (" [" + "; ".join(shown) + ("; …" if omitted else "") + "]") if shown else ""


def _node_lines(node):
    count = node["relationship_count"]
    lines = [f"- {node['ref']} — {_clip(node['summary'], SUMMARY_CHARS)}{_attributes(node)}"
             f" · {count} relationship{'' if count == 1 else 's'}"]
    for edge in node["relationships"][:RELATIONSHIPS]:
        other = edge["to"] if edge["direction"] != "in" else edge["from"]
        arrow = {"out": "→", "in": "←", "self": "↺"}[edge["direction"]]
        lines.append(f"  - {edge['ref']} {arrow} {other['ref']} ({_clip(other['summary'] or '', 80)}): "
                     f"{_clip(edge['summary'], SUMMARY_CHARS)}")
    if count > min(len(node["relationships"]), RELATIONSHIPS):
        lines.append(f"  - … {count - min(len(node['relationships']), RELATIONSHIPS)} more; open {node['ref']} for all")
    return lines


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


def render_home(dto, *, characters=MAX_CHARACTERS) -> str:
    """The bounded text of one ``context_home`` DTO (or of a not-ready status DTO); it opens with a plain title line so
    it nests under a host section heading. The owner's record takes its own bound, on top of ``characters``."""
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
    shown = [node for node in dto["nodes"][:NODES] if node["ref"] != personal_ref]
    lines = [head, summary,
             "One published graph shared by every registered agent; drafts are never shown. "
             f"Showing {len(shown)} of {totals['nodes']} nodes, most connected first."]
    for node in shown:
        # The owner's record is rendered once, above; the index does not repeat it or its relationships.
        visible = [edge for edge in node["relationships"]
                   if personal_ref not in (edge["from"]["ref"], edge["to"]["ref"])]
        lines.extend(_node_lines({**node, "relationships": visible}))
    lines.append(READS)
    text = "\n".join(lines)
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
        try:
            dto = self.reader.home(limit=NODES)
        except (DomainError, OSError):
            dto = {"status": "unavailable", "revision": None, "reason": {"code": "read_failed", "message": ""}}
        return render_home(dto, characters=self.characters)
