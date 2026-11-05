"""The add-only question output of an automatic filing.

Questions are never Context data: they are not notes, graph attributes, foundation evidence, event or handoff
payloads. They are staged in runtime material while an event is pending and become a deliverable outbox payload
only after the event publishes. The external collection that stores and manages them is not part of this engine.
"""
from __future__ import annotations

from .schema import ContextError, encode, identifier, text

KINDS = ("event", "node", "edge", "note")


def reference(item) -> dict:
    """One ``{kind: event|node|edge|note, id}`` reference to Context records, validated for shape only."""
    if not isinstance(item, dict) or set(item) != {"kind", "id"} or item["kind"] not in KINDS:
        raise ContextError("A question reference requires exactly kind event/node/edge/note and id")
    return {"kind": item["kind"], "id": identifier(item["id"])}


def question(body, related=None) -> dict:
    """One question ``{text, related}``; ``related`` may be empty, and a repeated reference is kept once."""
    body = text(body, "question text")
    try:
        body.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ContextError("question text must be valid UTF-8") from error
    if related is None:
        related = []
    if not isinstance(related, list):
        raise ContextError("question related must be a list of references")
    result, seen = [], set()
    for item in related:
        entry = reference(item)
        if (entry["kind"], entry["id"]) not in seen:
            seen.add((entry["kind"], entry["id"]))
            result.append(entry)
    return {"text": body, "related": result}


def entries(values, limit=None) -> list:
    """A validated question list; its canonical serialization must fit the source byte ceiling."""
    if not isinstance(values, list):
        raise ContextError("Question output must be a list")
    result = []
    for item in values:
        if not isinstance(item, dict) or set(item) != {"text", "related"}:
            raise ContextError("A question requires exactly text and related")
        result.append(question(item["text"], item["related"]))
    if limit is not None and len(encode(result)) > limit:
        raise ContextError("Question output exceeds the configured byte limit")
    return result
