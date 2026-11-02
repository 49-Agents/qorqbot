"""The read-only Context queue summary of one namespace, for the existing process views.

It counts the Operations Context queue's own rows and nothing else: exact unfinished input states of this namespace, the
published filings whose terminal receipt is not applied yet, and the single shared fact that something ahead of every later input
is blocked. It never counts generic job activity (a deferred Context job is not a model that is running), never fabricates a
pending completion for a graph or Action publication that has no filing, and starts, initializes and writes nothing.
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError

TABLES = ("context_inputs", "context_filings", "context_completions")
STATES = ("queued", "processing", "blocked", "discard_requested")


def context_queue_summary(connection, business_id) -> dict:
    """``{queued, processing, blocked, discard_requested, publication_pending_completion, completion_conflict, queue_blocked,
    blocked_input_id}`` for the business namespace, or for the Global namespace when ``business_id`` is None.

    The first four counts are the exact unfinished input states of this namespace. ``publication_pending_completion`` counts this
    namespace's published filings whose receipt is pending (or not yet recorded), and ``completion_conflict`` those whose
    receipt is a conflict; an applied receipt counts for neither. ``queue_blocked`` is the shared queue's own fact, across every
    namespace, and ``blocked_input_id`` names the blocked input only when it belongs to this namespace. A database without the
    queue and filing storage is an explicit error, never an empty summary."""
    present = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (%s)" % ",".join("?" for _ in TABLES), TABLES)}
    missing = [name for name in TABLES if name not in present]
    if missing:
        raise DomainError("operation_unavailable", "The Context queue is not composed on this installation: missing "
                          + ", ".join(missing))
    counts = {state: 0 for state in STATES}
    for row in connection.execute("SELECT state, COUNT(*) AS n FROM context_inputs WHERE business_id IS ? AND state IN (%s) GROUP BY state"
                                  % ",".join("?" for _ in STATES), (business_id, *STATES)):
        counts[row["state"]] = row["n"]
    completions = {"pending": 0, "conflict": 0}
    for row in connection.execute(
            "SELECT COALESCE(c.state, 'pending') AS state, COUNT(*) AS n FROM context_filings f "
            "JOIN context_inputs i ON i.id = f.input_id LEFT JOIN context_completions c ON c.filing_id = f.id "
            "WHERE i.business_id IS ? AND i.state = 'published' AND COALESCE(c.state, 'pending') IN ('pending', 'conflict') "
            "GROUP BY COALESCE(c.state, 'pending')", (business_id,)):
        completions[row["state"]] = row["n"]
    blocked = connection.execute("SELECT id, business_id FROM context_inputs WHERE state='blocked'").fetchone()
    return {**counts, "publication_pending_completion": completions["pending"], "completion_conflict": completions["conflict"],
            "queue_blocked": blocked is not None,
            "blocked_input_id": blocked["id"] if blocked is not None and blocked["business_id"] == business_id else None}
