"""Retirement fence for the old procedural learning route (Overseer section 5).

The factual Context route keeps working exactly as before: extraction may still retain ``bucket='context'`` items and
the owner/agent may still review and file them through the existing Context authority. New procedural learning deltas
(``bucket='delta'``), their decisions, edits, work transitions, supersession/reconciliation writes and standalone
engine imports are refused in this target. Old rows and reads stay available, and a bounded read-only inventory of
pending legacy deltas is provided for the owner's later migration decision. There is no runtime toggle, no automatic
conversion, no import and no deletion.
"""
from qorqut_operations.contracts import DomainError

PROCEDURAL_BUCKET = "delta"
RETIRED_MESSAGE = (
    "The procedural learning-delta route is retired in this target; new proposals, decisions, edits, work transitions, "
    "reconciliations and engine imports must use Overseer. Retained old records stay readable and the read-only legacy "
    "pending inventory lists what an owner migration decision would need.")


def is_procedural(item):
    return isinstance(item, dict) and item.get("bucket") == PROCEDURAL_BUCKET


def refuse():
    raise DomainError("forbidden", RETIRED_MESSAGE)


def require_context(item, what="learning candidate"):
    """Refuse one stored or proposed procedural candidate; context items pass through unchanged."""
    if not is_procedural(item):
        return item
    refuse()


def context_only_items(items):
    """The factual subset of one producer batch; every procedural item is dropped before it can be emitted."""
    return [item for item in items if not is_procedural(item)]


def guard_batch(items, evidence, lookup):
    """Refuse a mixed producer batch atomically before any record is prepared.

    ``lookup`` resolves one candidate id to its stored candidate JSON (None when it does not exist yet).
    """
    procedural = sorted(str(item.get("id")) for item in items if is_procedural(item))
    for line in evidence:
        if is_procedural(lookup(line.get("of"))):
            procedural.append("relation:" + str(line.get("of")))
        if is_procedural(lookup(line.get("into"))):
            procedural.append("relation:" + str(line.get("into")))
    if procedural:
        raise DomainError("forbidden", RETIRED_MESSAGE + " Refused: " + ", ".join(procedural[:20]))


def guard_candidate(row, what="learning candidate"):
    require_context((row or {}).get("candidate_json"), what)
