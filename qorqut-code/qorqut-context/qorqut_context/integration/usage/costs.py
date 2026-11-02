"""Observed Context call charges for the owner's Global cost overview, read from the usage ledger.

The Context ``filing`` source of the overview is a known subtotal: only calls whose charge the library recorded count, an
unknown charge is counted and never priced or treated as free, and a started call keeps its observed charge while being
reported as unfinished. Calls stream from the operational database (one pass, constant memory); no Context runtime file is
opened and no provider is contacted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal

from qorqut_context.integration.usage.coverage import collection_coverage

_EXACT = Context(prec=80)


@dataclass(frozen=True)
class ContextCharges:
    """Per-day exact known charges (UTC call-start day) up to ``today``, plus the unknown/unfinished counts and the coverage."""

    daily: dict
    unknown_calls: int
    unfinished_calls: int
    coverage: dict


def context_charges(connection, today: date) -> ContextCharges:
    """Every recorded Context call started on or before ``today`` (UTC), summed exactly per day."""
    last = today.isoformat()
    daily: dict = {}
    for row in connection.execute(
            "SELECT substr(started_at, 1, 10) AS day, cost_usd FROM context_usage_calls "
            "WHERE cost_usd IS NOT NULL AND substr(started_at, 1, 10) <= ? ORDER BY started_at, call_id", (last,)):
        day = date.fromisoformat(row["day"])
        daily[day] = _EXACT.add(daily.get(day, Decimal(0)), Decimal(row["cost_usd"]))
    counts = connection.execute(
        "SELECT COALESCE(SUM(cost_usd IS NULL), 0) AS unknown, COALESCE(SUM(state = 'started'), 0) AS unfinished "
        "FROM context_usage_calls WHERE substr(started_at, 1, 10) <= ?", (last,)).fetchone()
    return ContextCharges(daily, counts["unknown"], counts["unfinished"], collection_coverage(connection))
