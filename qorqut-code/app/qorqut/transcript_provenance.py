"""Provider-authored transcript metadata shared by every owner-input consumer.

Compaction is context maintenance, even when a provider transports its summary
with the user role. Never infer this from prose: an owner can paste a summary or
ask about compaction, and that is still their input.
"""


def is_compaction(record):
    """Recognize Claude summaries/boundaries and Codex compaction records."""
    if not isinstance(record, dict):
        return False
    if record.get("isCompactSummary"):
        return True
    kind = record.get("type")
    if kind in {"summary", "compacted"}:
        return True
    if kind == "system" and record.get("subtype") == "compact_boundary":
        return True
    payload = record.get("payload")
    if kind != "event_msg" or not isinstance(payload, dict):
        return False
    if payload.get("isCompactSummary") or payload.get("type") == "context_compacted":
        return True
    item = payload.get("item")
    return (payload.get("type") in {"item_started", "item_completed"} and isinstance(item, dict)
            and (item.get("type") == "ContextCompaction" or bool(item.get("isCompactSummary"))))
