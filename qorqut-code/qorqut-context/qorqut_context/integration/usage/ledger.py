"""Ingesting library call receipts and recording collector scans, for the backend system principal only.

These are internal calls inside the caller's transaction, not registered operations: nothing here starts a thread, a
scheduler, a provider, a subprocess or reads the Context filesystem, and no public caller can supply them. ``driver_id``
must be an active registered ``system`` principal (the Context worker). The later host collector serializes its imports and
owns bounded traversal and reconciliation; this module only applies a validated batch atomically and truthfully.

Ownership (``business_id``, NULL for Global) is read from the stored queue input and never accepted from a caller. A receipt's
Context event must be one a queue attempt of that input reserved; the library attempt sequence it carries is its own and is
never related to the queue attempt number.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from qorqut_operations.contracts import DomainError, utc_now
from qorqut_context.integration.usage.contract import ERROR_MESSAGE_LIMIT, EVENT_ID, CallReceipt, parse_batch
from qorqut_context.integration.usage.schema import SYNC_STATUSES, TOKEN_FIELDS

INSERTED, UPDATED, UNCHANGED, STALE = "inserted", "updated", "unchanged", "stale"
SCAN_CODE_LIMIT = 64
_LOOKUP_CHUNK = 200
_COLUMNS = ("call_id", "business_id", "input_id", "context_event_id", "attempt_id", "library_attempt_sequence", "stage", "state",
            "started_at", "finished_at", "configured_model", "configured_effort", "configured_service_tier", "observed_provider",
            "observed_model", "native_thread_id", "native_turn_id", *TOKEN_FIELDS, "cost_usd", "usage_source", "usage_scope",
            "usage_updates", "ignored_updates", "bootstrap_call_id", "error_type", "error_message", "recorded_at", "updated_at")


@dataclass(frozen=True)
class CallOutcome:
    call_id: str
    outcome: str


@dataclass(frozen=True)
class IngestResult:
    """What one batch did: counts, and the outcome of every receipt in batch order."""

    inserted: int
    updated: int
    unchanged: int
    stale: int
    calls: tuple[CallOutcome, ...]

    def as_dict(self) -> dict:
        return {"inserted": self.inserted, "updated": self.updated, "unchanged": self.unchanged, "stale": self.stale,
                "calls": [{"call_id": item.call_id, "outcome": item.outcome} for item in self.calls]}


@dataclass(frozen=True)
class ScanRecord:
    input_id: str
    context_event_id: str
    business_id: str | None
    status: str
    error: dict | None
    checked_at: str

    def as_dict(self) -> dict:
        return {"input_id": self.input_id, "context_event_id": self.context_event_id, "business_id": self.business_id,
                "status": self.status, "error": self.error, "checked_at": self.checked_at}


def _system(uow, driver_id) -> None:
    uow.require_write()
    if not isinstance(driver_id, str) or uow.connection.execute(
            "SELECT 1 FROM principals WHERE id=? AND kind='system' AND active=1", (driver_id,)).fetchone() is None:
        raise DomainError("forbidden", "Only the backend system principal may write the Context usage ledger")


def _input(uow, input_id) -> Mapping[str, Any]:
    row = uow.connection.execute("SELECT id, business_id FROM context_inputs WHERE id=?", (input_id,)).fetchone() \
        if isinstance(input_id, str) else None
    if row is None:
        raise DomainError("not_found", "Context input does not exist")
    return row


def _reserved(uow, input_id: str, event_id: str) -> bool:
    return uow.connection.execute(
        "SELECT 1 FROM context_input_attempts WHERE input_id=? AND context_event_id=?", (input_id, event_id)).fetchone() is not None


def _existing(uow, call_ids: Sequence[str]) -> dict[str, Mapping[str, Any]]:
    found: dict[str, Mapping[str, Any]] = {}
    for start in range(0, len(call_ids), _LOOKUP_CHUNK):
        chunk = call_ids[start:start + _LOOKUP_CHUNK]
        for row in uow.connection.execute(
                f"SELECT * FROM context_usage_calls WHERE call_id IN ({','.join('?' * len(chunk))})", chunk):
            found[row["call_id"]] = row
    return found


def _referenced_by(uow, bootstrap_ids: Sequence[str]) -> dict[str, set[tuple[str, str]]]:
    """The (input, event) of every stored call that already names each bootstrap call ID, by the bootstrap index."""
    found: dict[str, set[tuple[str, str]]] = {}
    for start in range(0, len(bootstrap_ids), _LOOKUP_CHUNK):
        chunk = bootstrap_ids[start:start + _LOOKUP_CHUNK]
        for row in uow.connection.execute(
                "SELECT DISTINCT bootstrap_call_id, input_id, context_event_id FROM context_usage_calls "
                f"WHERE bootstrap_call_id IN ({','.join('?' * len(chunk))})", chunk):
            found.setdefault(row[0], set()).add((row[1], row[2]))
    return found


def _bootstrap_links(batch: Sequence[CallReceipt], input_id: str, stored: Mapping[str, Mapping[str, Any]],
                     referenced: Mapping[str, set[tuple[str, str]]]) -> dict[str, str]:
    """Field errors for bootstrap links that would leave one input and Context event, in either arrival order.

    A bootstrap call ID belongs to the single (input, event) its stored bootstrap call, its stored references or the batch
    already evidence, whichever of them arrived first; a bootstrap that is not stored yet is never invented."""
    homes = {receipt.call_id: (input_id, receipt.event_id) for receipt in batch if receipt.stage == "bootstrap"}
    homes.update({call_id: (row["input_id"], row["context_event_id"]) for call_id, row in stored.items()})
    links = {call_id: set(pairs) for call_id, pairs in referenced.items()}
    for receipt in batch:
        if receipt.bootstrap_call_id:
            links.setdefault(receipt.bootstrap_call_id, set()).add((input_id, receipt.event_id))
    errors: dict[str, str] = {}
    for index, receipt in enumerate(batch):
        own = (input_id, receipt.event_id)
        reference = receipt.bootstrap_call_id
        if reference and homes.get(reference, own) != own:
            errors[f"receipts[{index}].bootstrap_call_id"] = "names a call of another input or Context event"
        elif reference and links[reference] - {own}:
            errors[f"receipts[{index}].bootstrap_call_id"] = "is already referenced by a call of another input or Context event"
        if receipt.stage == "bootstrap" and links.get(receipt.call_id, set()) - {own}:
            errors[f"receipts[{index}].call_id"] = "is already referenced by a call of another input or Context event"
    return errors


def _identity_of(row: Mapping[str, Any]) -> tuple:
    return (row["context_event_id"], row["attempt_id"], row["library_attempt_sequence"], row["stage"], row["started_at"],
            row["configured_model"], row["configured_effort"], row["configured_service_tier"], row["bootstrap_call_id"])


def _observation_of(row: Mapping[str, Any]) -> tuple:
    return (row["state"], row["finished_at"], row["observed_provider"], row["observed_model"], row["native_thread_id"],
            row["native_turn_id"], *(row[name] for name in TOKEN_FIELDS), row["cost_usd"], row["usage_source"], row["usage_scope"],
            row["usage_updates"], row["ignored_updates"], row["error_type"], row["error_message"])


def _conflict(code: str, message: str, receipt: CallReceipt) -> DomainError:
    return DomainError(code, message, details={"call_id": receipt.call_id})


def _classify(receipt: CallReceipt, row: Mapping[str, Any] | None, input_id: str) -> str:
    """How an incoming receipt relates to the stored call: inserted, updated, unchanged or stale; or a refusal."""
    if row is None:
        return INSERTED
    if row["input_id"] != input_id or _identity_of(row) != receipt.identity:
        raise _conflict("usage_identity_conflict", "A call ID is bound to one input, event, attempt, stage, start, "
                        "configuration and bootstrap reference", receipt)
    for stored, incoming in ((row["native_thread_id"], receipt.thread_id), (row["native_turn_id"], receipt.turn_id)):
        if stored is not None and incoming is not None and stored != incoming:
            raise _conflict("usage_identity_conflict", "A native thread or turn identity cannot be reassigned", receipt)
    final = row["state"] != "started"
    if final and receipt.state != "started" and receipt.state != row["state"]:
        raise _conflict("usage_outcome_conflict", "A call has one terminal outcome", receipt)
    older = (
        (final and receipt.state == "started")
        or receipt.usage_updates < row["usage_updates"] or receipt.ignored_updates < row["ignored_updates"]
        or any(stored is not None and incoming is None for stored, incoming in (
            (row["native_thread_id"], receipt.thread_id), (row["native_turn_id"], receipt.turn_id),
            (row["observed_provider"], receipt.observed_provider), (row["observed_model"], receipt.observed_model))))
    if older:
        return STALE
    if _observation_of(row) == receipt.observation:
        return UNCHANGED
    if final:
        raise _conflict("usage_outcome_conflict", "A completed or failed call is final", receipt)
    if receipt.usage_updates == row["usage_updates"] and (
            tuple(row[name] for name in TOKEN_FIELDS) != receipt.counters or row["cost_usd"] != receipt.cost_usd
            or row["usage_source"] != receipt.usage_source or row["usage_scope"] != receipt.usage_scope):
        raise _conflict("usage_evidence_conflict", "The same number of usage updates cannot carry different counters", receipt)
    return UPDATED


def ingest(uow, *, driver_id: str, input_id: str, receipts: Sequence[Mapping[str, Any]]) -> IngestResult:
    """Apply at most 500 library call receipts of one queue input atomically.

    The complete batch is validated and classified against the stored calls, and its bootstrap links against every stored and
    incoming reference, before anything is written, so a refused batch changes nothing. A new call is inserted; an identical
    one is unchanged; a started call advances to a later observation (cumulative counters and cost are replaced, never added)
    or to its actual terminal state; an older observation (a started snapshot after a terminal state, a smaller update count,
    a lost native identity) is reported stale and changes nothing. A reused call ID with another immutable identity, a reassigned native identity, conflicting
    terminal outcomes, or one update count with different counters is refused with ``usage_identity_conflict``,
    ``usage_outcome_conflict`` or ``usage_evidence_conflict``."""
    _system(uow, driver_id)
    owner = _input(uow, input_id)
    batch = parse_batch(receipts)
    unreserved = {f"receipts[{index}].event_id": "no queue attempt of this input reserved this Context event"
                  for index, receipt in enumerate(batch) if not _reserved(uow, input_id, receipt.event_id)}
    if unreserved:
        raise DomainError("validation_error", "Receipts must belong to a Context event reserved by this input's queue attempts",
                          field_errors=unreserved)
    existing = _existing(uow, [receipt.call_id for receipt in batch])
    plan = [(receipt, _classify(receipt, existing.get(receipt.call_id), input_id)) for receipt in batch]
    named = {receipt.bootstrap_call_id for receipt in batch if receipt.bootstrap_call_id}
    stored = existing | _existing(uow, sorted(named - set(existing)))
    wanted = sorted(named | {receipt.call_id for receipt in batch if receipt.stage == "bootstrap"})
    broken = _bootstrap_links(batch, input_id, stored, _referenced_by(uow, wanted))
    if broken:
        raise DomainError("validation_error", "A bootstrap call and every call that references it belong to one input and Context "
                          "event", field_errors=broken)
    now = utc_now()
    for receipt, outcome in plan:
        if outcome == INSERTED:
            _insert(uow, receipt, owner["business_id"], input_id, now)
        elif outcome == UPDATED:
            _update(uow, receipt, now)
    counts = {name: sum(1 for _, outcome in plan if outcome == name) for name in (INSERTED, UPDATED, UNCHANGED, STALE)}
    return IngestResult(counts[INSERTED], counts[UPDATED], counts[UNCHANGED], counts[STALE],
                        tuple(CallOutcome(receipt.call_id, outcome) for receipt, outcome in plan))


def _values(receipt: CallReceipt) -> dict:
    return {"state": receipt.state, "finished_at": receipt.finished_at, "observed_provider": receipt.observed_provider,
            "observed_model": receipt.observed_model, "native_thread_id": receipt.thread_id, "native_turn_id": receipt.turn_id,
            **dict(zip(TOKEN_FIELDS, receipt.counters)), "cost_usd": receipt.cost_usd, "usage_source": receipt.usage_source,
            "usage_scope": receipt.usage_scope, "usage_updates": receipt.usage_updates, "ignored_updates": receipt.ignored_updates,
            "error_type": receipt.error_type, "error_message": receipt.error_message}


def _insert(uow, receipt: CallReceipt, business_id, input_id: str, now: str) -> None:
    values = {"call_id": receipt.call_id, "business_id": business_id, "input_id": input_id, "context_event_id": receipt.event_id,
              "attempt_id": receipt.attempt_id, "library_attempt_sequence": receipt.attempt_sequence, "stage": receipt.stage,
              "started_at": receipt.started_at, "configured_model": receipt.configured_model,
              "configured_effort": receipt.configured_effort, "configured_service_tier": receipt.configured_service_tier,
              "bootstrap_call_id": receipt.bootstrap_call_id, **_values(receipt), "recorded_at": now, "updated_at": now}
    uow.connection.execute(f"INSERT INTO context_usage_calls({','.join(_COLUMNS)}) VALUES ({','.join('?' * len(_COLUMNS))})",
                           [values[name] for name in _COLUMNS])


def _update(uow, receipt: CallReceipt, now: str) -> None:
    values = {**_values(receipt), "updated_at": now}
    cursor = uow.connection.execute(
        f"UPDATE context_usage_calls SET {','.join(f'{name}=?' for name in values)} WHERE call_id=?",
        [*values.values(), receipt.call_id])
    if cursor.rowcount != 1:
        raise DomainError("state_conflict", "The usage call changed before this observation was saved")


def record_scan(uow, *, driver_id: str, input_id: str, context_event_id: str, status: str, error: Mapping[str, Any] | None = None) -> ScanRecord:
    """Record the collector's latest truthful status for one queue input and Context event.

    ``ready`` means a complete bounded page traversal reached EOF (it never proves zero calls or zero cost); ``partial`` is an
    intermediate page; ``unavailable`` is an unreadable or corrupt source and needs a bounded ``{code, message}`` error. A
    ``ready`` record carries no error. The event must be one a queue attempt of the input reserved. Previously imported calls
    are never deleted or changed."""
    _system(uow, driver_id)
    owner = _input(uow, input_id)
    if status not in SYNC_STATUSES:
        raise DomainError("validation_error", f"status must be one of {', '.join(SYNC_STATUSES)}")
    if not isinstance(context_event_id, str) or EVENT_ID.fullmatch(context_event_id) is None or not _reserved(uow, input_id, context_event_id):
        raise DomainError("validation_error", "The Context event must be one a queue attempt of this input reserved")
    code = message = None
    if error is not None:
        if (not isinstance(error, Mapping) or set(error) != {"code", "message"} or not isinstance(error["code"], str)
                or not 1 <= len(error["code"]) <= SCAN_CODE_LIMIT or not isinstance(error["message"], str)
                or len(error["message"]) > ERROR_MESSAGE_LIMIT):
            raise DomainError("validation_error", f"error must be {{code, message}} with a code of at most {SCAN_CODE_LIMIT} "
                                                  f"and a message of at most {ERROR_MESSAGE_LIMIT} characters")
        code, message = error["code"], error["message"]
    if status == "ready" and error is not None or status == "unavailable" and error is None:
        raise DomainError("validation_error", "A ready scan carries no error and an unavailable scan names one")
    now = utc_now()
    uow.connection.execute(
        "INSERT INTO context_usage_sync(input_id, context_event_id, business_id, status, error_code, error_message, checked_at) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(input_id, context_event_id) DO UPDATE SET status=excluded.status, "
        "error_code=excluded.error_code, error_message=excluded.error_message, checked_at=excluded.checked_at",
        (input_id, context_event_id, owner["business_id"], status, code, message, now))
    return ScanRecord(input_id, context_event_id, owner["business_id"], status,
                      None if code is None else {"code": code, "message": message}, now)
