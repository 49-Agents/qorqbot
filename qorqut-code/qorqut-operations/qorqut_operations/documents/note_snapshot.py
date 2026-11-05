"""Deterministic, escaped Notes snapshots. LF separates blocks; U+2028 continues one."""
import re
from xml.sax.saxutils import escape, quoteattr

from qorqut_operations.contracts import DomainError, canonical_hash

SOFT_BREAK = "\u2028"
STATUSES = {"todo", "white", "in_progress", "done", "blocked", "cancelled"}


def snapshot(body, metadata):
    blocks = []
    for raw in body.split("\n"):
        if raw.strip() == "⸻":
            blocks.append("<divider />")
            continue
        match = re.match(r"^([ \t]*)-[ \t]?", raw)
        lead = match[1] if match else ""
        text = raw[match.end():] if match else raw
        bold = re.match("\u2063([^\u2063]*)\u2063 ?", text)
        attrs = ' bold="true"' if bold and "b" in bold[1].split(",") else ""
        if bold:
            text = text[bold.end():]
        text = (lead + text).replace(SOFT_BREAK, "\n")
        tag = "item" if match else "text"
        if match:
            status = metadata.get("statuses", {}).get(text.strip(), "todo")
            attrs += " status=" + quoteattr(status if status in STATUSES else "todo")
        blocks.append(f"<{tag}{attrs}>{escape(text)}</{tag}>")
    title = quoteattr(str(metadata.get("title") or "Untitled"))
    date = metadata.get("date")
    date_attribute = " date=" + quoteattr(str(date)) if isinstance(date, str) and date else ""
    return '<note format="1" title=' + title + date_attribute + '>\n' + "\n".join(blocks) + "\n</note>"


def snapshot_hash(body, metadata):
    return canonical_hash(snapshot(body, metadata))


def filing_snapshot(body, metadata):
    if not body.replace(SOFT_BREAK, "").strip():
        raise DomainError("validation_error", "Write something before adding this note to Context")
    result = snapshot(body, metadata)
    if len(result) > 80000:
        raise DomainError("validation_error", "This note is too large to file (80,000 characters including formatting)")
    return result
