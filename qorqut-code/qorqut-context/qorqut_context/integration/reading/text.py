"""Bounded Markdown for agent and owner bootstrap, derived from the same pinned graph DTO ``context_home`` returns.

``home_text`` renders one page of ``SharedContextReader.home`` and nothing else: the published revision, the totals,
node and relationship references (``node:ID`` / ``edge:ID``) with their summaries, and an explicit statement for the
states that are not a graph (``not_initialized``, ``unavailable``) or an empty one. The published graph is shared by
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


def render_home(dto, *, characters=MAX_CHARACTERS) -> str:
    """The bounded text of one ``context_home`` DTO (or of a not-ready status DTO); it opens with a plain title line so
    it nests under a host section heading."""
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
    personal = dto.get("personal_context")
    personal_ref = personal["ref"] if personal else None
    shown = [node for node in dto["nodes"][:NODES] if node["ref"] != personal_ref]
    lines = [head, summary,
             "One published graph shared by every registered agent; drafts are never shown. "
             f"Showing {len(shown)} of {totals['nodes']} nodes, most connected first."]
    if personal_ref:
        lines.insert(1, f"Personal context: {personal_ref}. This is the one canonical store of information about the user. "
                     f"Read context_object(ref='{personal_ref}') on demand. Ordubashy maintains it; other agents read it "
                     "and pass new personal facts to Ordubashy through the existing inbox, with the source and date.")
    for node in shown:
        # Startup is an index, not an implicit profile read (including incident relationship summaries).
        visible = [edge for edge in node["relationships"]
                   if personal_ref not in (edge["from"]["ref"], edge["to"]["ref"])]
        lines.extend(_node_lines({**node, "relationships": visible}))
    lines.append(READS)
    text = "\n".join(lines)
    return text if len(text) <= characters else text[:characters - len(TRUNCATED)].rstrip() + TRUNCATED


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
