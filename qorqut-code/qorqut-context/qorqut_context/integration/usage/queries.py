"""Read-only views of the usage ledger: stable pages of recorded calls with SQL-backed aggregates and coverage facts.

These are internal helpers for the later owner-only costs adapter, which enforces who may see them and shapes the existing
views. They return private facts (native thread and turn IDs, bounded error text) and must never be handed to ordinary shared
graph readers. They read only the operational database: no provider, Context filesystem or subprocess.

The aggregates are recorded subtotals over exactly the filtered calls, with their coverage, not a claim that an input's or the
installation's whole cost is known: a missing sync record or an empty ``ready`` scan never proves zero calls or zero cost, and
an unknown cost or unobserved usage is counted as unknown, never as zero. Money is exact ``Decimal`` arithmetic over the stored
canonical decimal text; the subtotal is ``None`` when no filtered call reports a cost.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal
import json
import re
from typing import Any

from qorqut_operations.contracts import DomainError, canonical_hash
from qorqut_context.integration.usage.contract import decimal_text, derived_flags
from qorqut_context.integration.usage.ledger import ScanRecord
from qorqut_context.integration.usage.schema import TOKEN_FIELDS

MAX_PAGE = 100
DEFAULT_PAGE = 50
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SUM = Context(prec=80)


@dataclass(frozen=True)
class UsageCall:
    """One recorded call with the sync fact of its input and Context event (``None`` before any scan was recorded)."""

    call_id: str
    business_id: str | None
    input_id: str
    context_event_id: str
    attempt_id: str
    library_attempt_sequence: int
    stage: str
    state: str
    started_at: str
    finished_at: str | None
    configured: dict
    observed: dict
    usage: dict
    evidence: dict
    bootstrap_call_id: str | None
    error: dict | None
    recorded_at: str
    updated_at: str
    sync: ScanRecord | None

    def as_dict(self) -> dict:
        usage = {**self.usage, "cost_usd": None if self.usage["cost_usd"] is None else decimal_text(self.usage["cost_usd"])}
        return {"call_id": self.call_id, "business_id": self.business_id, "input_id": self.input_id,
                "context_event_id": self.context_event_id, "attempt_id": self.attempt_id,
                "library_attempt_sequence": self.library_attempt_sequence, "stage": self.stage, "state": self.state,
                "started_at": self.started_at, "finished_at": self.finished_at, "configured": dict(self.configured),
                "observed": dict(self.observed), "usage": usage, "evidence": {**self.evidence,
                "limitations": list(self.evidence["limitations"])}, "bootstrap_call_id": self.bootstrap_call_id,
                "error": self.error, "recorded_at": self.recorded_at, "updated_at": self.updated_at,
                "sync": None if self.sync is None else self.sync.as_dict()}


@dataclass(frozen=True)
class UsageAggregates:
    """Counts and the known USD subtotal over every call matching the filters (not only the returned page).

    ``events`` is the number of distinct input and Context event pairs those calls belong to and ``events_not_ready`` how many
    of them have no ``ready`` scan record (none, ``partial`` or ``unavailable``): a coverage fact, never a proof of totals."""

    calls: int
    unfinished_calls: int
    unknown_cost_calls: int
    calls_without_token_usage: int
    known_cost_usd: Decimal | None
    events: int
    events_not_ready: int

    def as_dict(self) -> dict:
        return {"calls": self.calls, "unfinished_calls": self.unfinished_calls, "unknown_cost_calls": self.unknown_cost_calls,
                "calls_without_token_usage": self.calls_without_token_usage,
                "known_cost_usd": None if self.known_cost_usd is None else decimal_text(self.known_cost_usd),
                "events": self.events, "events_not_ready": self.events_not_ready}


@dataclass(frozen=True)
class UsagePage:
    calls: tuple[UsageCall, ...]
    next_cursor: str | None
    aggregates: UsageAggregates

    def as_dict(self) -> dict:
        return {"calls": [call.as_dict() for call in self.calls], "next_cursor": self.next_cursor,
                "aggregates": self.aggregates.as_dict()}


def _invalid(message: str, field: str | None = None):
    raise DomainError("validation_error", message, field_errors={field: message} if field else None)


def _day(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or DATE.fullmatch(value) is None:
        _invalid("must be a UTC date YYYY-MM-DD", field)
    try:
        date.fromisoformat(value)
    except ValueError:
        _invalid("must be a real UTC date YYYY-MM-DD", field)
    return value


def _binding(input_id, start_date, end_date, limit) -> str:
    return canonical_hash({"input_id": input_id, "start_date": start_date, "end_date": end_date, "limit": limit})


def _after(cursor: Any, binding: str):
    if cursor is None:
        return None
    problem = DomainError("validation_error", "Invalid or differently scoped usage cursor")
    try:
        decoded = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
        after = decoded["after"]
        if (decoded["v"] != 1 or decoded["binding"] != binding or not isinstance(after, list) or len(after) != 2
                or not all(isinstance(item, str) for item in after)
                or _cursor(binding, {"started_at": after[0], "call_id": after[1]}) != cursor):       # only a token this module issued
            raise ValueError
    except (AttributeError, ValueError, TypeError, KeyError, UnicodeError) as error:
        raise problem from error
    return tuple(after)


def _cursor(binding: str, last) -> str:
    payload = json.dumps({"v": 1, "binding": binding, "after": [last["started_at"], last["call_id"]]}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii")


def _filters(input_id, start_date, end_date):
    clauses, parameters = [], []
    if input_id is not None:
        clauses.append("c.input_id = ?")
        parameters.append(input_id)
    if start_date is not None:
        clauses.append("c.started_at >= ?")
        parameters.append(f"{start_date}T00:00:00.000000Z")
    if end_date is not None:
        clauses.append("c.started_at <= ?")
        parameters.append(f"{end_date}T23:59:59.999999Z")
    return clauses, parameters


def _scan(row, prefix: str = "") -> ScanRecord:
    code = row[prefix + "error_code"]
    return ScanRecord(row[prefix + "input_id"], row[prefix + "context_event_id"], row[prefix + "business_id"],
                      row[prefix + "status"], None if code is None else {"code": code, "message": row[prefix + "error_message"]},
                      row[prefix + "checked_at"])


def _call(row) -> UsageCall:
    counters = tuple(row[name] for name in TOKEN_FIELDS)
    cost = None if row["cost_usd"] is None else Decimal(row["cost_usd"])
    flags = derived_flags(state=row["state"], counters=counters, cost_usd=cost, ignored_updates=row["ignored_updates"])
    sync = None if row["sync_status"] is None else ScanRecord(
        row["input_id"], row["context_event_id"], row["business_id"], row["sync_status"],
        None if row["sync_error_code"] is None else {"code": row["sync_error_code"], "message": row["sync_error_message"]},
        row["sync_checked_at"])
    return UsageCall(
        call_id=row["call_id"], business_id=row["business_id"], input_id=row["input_id"], context_event_id=row["context_event_id"],
        attempt_id=row["attempt_id"], library_attempt_sequence=row["library_attempt_sequence"], stage=row["stage"],
        state=row["state"], started_at=row["started_at"], finished_at=row["finished_at"],
        configured={"model": row["configured_model"], "effort": row["configured_effort"],
                    "service_tier": row["configured_service_tier"]},
        observed={"provider": row["observed_provider"], "model": row["observed_model"], "thread_id": row["native_thread_id"],
                  "turn_id": row["native_turn_id"]},
        usage={**dict(zip(TOKEN_FIELDS, counters)), "cost_usd": cost},
        evidence={"usage_source": row["usage_source"], "usage_scope": row["usage_scope"], "usage_updates": row["usage_updates"],
                  "ignored_updates": row["ignored_updates"], **flags},
        bootstrap_call_id=row["bootstrap_call_id"],
        error=None if row["error_type"] is None else {"type": row["error_type"], "message": row["error_message"]},
        recorded_at=row["recorded_at"], updated_at=row["updated_at"], sync=sync)


def aggregates(uow, *, input_id: str | None = None, start_date: str | None = None, end_date: str | None = None) -> UsageAggregates:
    """The recorded subtotals and coverage facts of every call matching the filters."""
    start_date, end_date = _day(start_date, "start_date"), _day(end_date, "end_date")
    if input_id is not None and not isinstance(input_id, str):
        _invalid("must be a queue input ID", "input_id")
    clauses, parameters = _filters(input_id, start_date, end_date)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    unobserved = " AND ".join(f"c.{name} IS NULL" for name in TOKEN_FIELDS)
    totals = uow.connection.execute(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(c.state = 'started'), 0) AS unfinished, "
        f"COALESCE(SUM(c.cost_usd IS NULL), 0) AS unknown_cost, COALESCE(SUM({unobserved}), 0) AS unobserved "
        f"FROM context_usage_calls c{where}", parameters).fetchone()
    coverage = uow.connection.execute(
        "SELECT COUNT(*) AS events, COALESCE(SUM(s.status IS NOT 'ready'), 0) AS not_ready FROM "
        f"(SELECT DISTINCT c.input_id, c.context_event_id FROM context_usage_calls c{where}) e "
        "LEFT JOIN context_usage_sync s ON s.input_id = e.input_id AND s.context_event_id = e.context_event_id",
        parameters).fetchone()
    known = [Decimal(row[0]) for row in uow.connection.execute(
        f"SELECT c.cost_usd FROM context_usage_calls c{where}{' AND' if where else ' WHERE'} c.cost_usd IS NOT NULL", parameters)]
    subtotal = None
    if known:
        subtotal = Decimal(0)
        for amount in known:
            subtotal = _SUM.add(subtotal, amount)
    return UsageAggregates(totals["calls"], totals["unfinished"], totals["unknown_cost"], totals["unobserved"], subtotal,
                           coverage["events"], coverage["not_ready"])


def list_calls(uow, *, input_id: str | None = None, start_date: str | None = None, end_date: str | None = None,
               limit: int = DEFAULT_PAGE, cursor: str | None = None) -> UsagePage:
    """One page of recorded calls ordered by start time and call ID, with the aggregates of the whole filtered set.

    ``start_date`` and ``end_date`` are inclusive UTC dates (``YYYY-MM-DD``) of the call start time. ``limit`` is 1 to 100.
    ``next_cursor`` is an opaque token bound to the filters and the limit; it is ``None`` after the last page. A page is a
    snapshot of what is recorded: calls imported meanwhile may appear on later pages, none is ever repeated."""
    start_date, end_date = _day(start_date, "start_date"), _day(end_date, "end_date")
    if input_id is not None and not isinstance(input_id, str):
        _invalid("must be a queue input ID", "input_id")
    if start_date is not None and end_date is not None and start_date > end_date:
        _invalid("start_date must not be after end_date", "start_date")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        _invalid(f"limit must be an integer from 1 to {MAX_PAGE}", "limit")
    binding = _binding(input_id, start_date, end_date, limit)
    after = _after(cursor, binding)
    clauses, parameters = _filters(input_id, start_date, end_date)
    if after is not None:
        clauses.append("(c.started_at, c.call_id) > (?, ?)")
        parameters.extend(after)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = uow.connection.execute(
        "SELECT c.*, s.status AS sync_status, s.error_code AS sync_error_code, s.error_message AS sync_error_message, "
        "s.checked_at AS sync_checked_at FROM context_usage_calls c LEFT JOIN context_usage_sync s "
        f"ON s.input_id = c.input_id AND s.context_event_id = c.context_event_id{where} "
        "ORDER BY c.started_at, c.call_id LIMIT ?", [*parameters, limit + 1]).fetchall()
    selected = rows[:limit]
    return UsagePage(tuple(_call(row) for row in selected), _cursor(binding, selected[-1]) if len(rows) > limit else None,
                     aggregates(uow, input_id=input_id, start_date=start_date, end_date=end_date))


def sync_records(uow, *, input_id: str) -> tuple[ScanRecord, ...]:
    """Every sync record of one input, by Context event ID. An input can have a record and no call, so a scan record is
    the only fact about that event; it still proves nothing about zero calls or cost."""
    if not isinstance(input_id, str):
        _invalid("must be a queue input ID", "input_id")
    return tuple(_scan(row) for row in uow.connection.execute(
        "SELECT * FROM context_usage_sync WHERE input_id=? ORDER BY context_event_id", (input_id,)))
