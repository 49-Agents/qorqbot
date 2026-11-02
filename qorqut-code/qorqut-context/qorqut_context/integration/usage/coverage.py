"""What the usage ledger has and has not been told about: an inventory of Context events, separate from the calls.

The calls and aggregates of a read follow the selected call-start dates. Coverage does not: it counts every Context event a
queue attempt reserved (for the selected input, or for the installation), left-joined to the collector's last recorded scan,
so an event that has no imported call at all is still counted, as ``unscanned`` until a scan exists. ``ready_events_without_calls``
counts reserved pairs whose last scan reached EOF but that have no imported call: ``ready`` still means EOF and nothing more, so
such an event stays visible as missing evidence (an empty scan proves only that no call receipt was observed, never zero
execution, usage or cost). ``last_checked_at`` is the newest recorded scan, which says nothing about when any particular event
was last checked. Nothing here proves that usage or cost totals are final.
"""

from __future__ import annotations

BASIS = "all_reserved_events_for_selected_input_or_installation"

_INVENTORY = """
SELECT COUNT(*) AS reserved,
       COALESCE(SUM(s.status = 'ready'), 0) AS ready,
       COALESCE(SUM(s.status = 'partial'), 0) AS partial,
       COALESCE(SUM(s.status = 'unavailable'), 0) AS unavailable,
       COALESCE(SUM(s.status IS NULL), 0) AS unscanned,
       COALESCE(SUM(CASE WHEN s.status = 'ready' AND NOT EXISTS (
           SELECT 1 FROM context_usage_calls c WHERE c.input_id = p.input_id AND c.context_event_id = p.context_event_id)
           THEN 1 ELSE 0 END), 0) AS ready_without_calls,
       MAX(s.checked_at) AS last_checked_at
FROM (SELECT DISTINCT input_id, context_event_id FROM context_input_attempts
      WHERE context_event_id IS NOT NULL {scope}) p
LEFT JOIN context_usage_sync s ON s.input_id = p.input_id AND s.context_event_id = p.context_event_id"""

_UNATTEMPTED = """
SELECT COUNT(*) FROM context_inputs i
WHERE {scope} NOT EXISTS (SELECT 1 FROM context_input_attempts a WHERE a.input_id = i.id AND a.context_event_id IS NOT NULL)"""


def collection_coverage(connection, input_id: str | None = None) -> dict:
    """The collection inventory for one input, or for the installation when ``input_id`` is ``None``.

    ``ready_events_without_calls`` counts each reserved pair once (several queue attempts on one event are one pair) whose last scan
    is ``ready`` while the ledger holds no call for it. ``unattempted_inputs`` counts inputs no queue attempt of which reserved a Context event (never started, or stopped before
    a Context event existed): nothing can be collected for them yet, and they are not part of ``reserved_events``."""
    arguments = () if input_id is None else (input_id,)
    row = connection.execute(_INVENTORY.format(scope="" if input_id is None else "AND input_id = ?"), arguments).fetchone()
    unattempted = connection.execute(_UNATTEMPTED.format(scope="" if input_id is None else "i.id = ? AND"), arguments).fetchone()[0]
    return {"basis": BASIS, "input_id": input_id, "reserved_events": row["reserved"], "ready_events": row["ready"],
            "partial_events": row["partial"], "unavailable_events": row["unavailable"], "unscanned_events": row["unscanned"],
            "ready_events_without_calls": row["ready_without_calls"], "unattempted_inputs": unattempted,
            "last_checked_at": row["last_checked_at"]}


def incomplete(facts: dict) -> bool:
    """Whether the recorded scans leave any reserved event not read to its end (``ready``) or read to its end with no call."""
    return bool(facts["partial_events"] or facts["unavailable_events"] or facts["unscanned_events"]
                or facts["ready_events_without_calls"])
