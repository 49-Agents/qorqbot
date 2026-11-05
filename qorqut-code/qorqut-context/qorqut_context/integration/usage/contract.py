"""The engine-owned typed contract of one Context model-call receipt (``qorqut-context-call-v1``).

A receipt is one closed object of JSON primitives, exactly the item the Context library's ``Pipeline.usage_receipts`` returns
(an item may also carry the stored file's ``format`` marker, which must then equal ``RECEIPT_FORMAT``). This module restates
that contract with its own strict validation and imports nothing from the Context package: unknown is ``None`` and never an
inferred zero, counters and the cost are cumulative snapshots, the configured and observed models are separate facts,
``usage_complete`` is always ``False`` (no source proves final totals), and ``counters_complete`` only says all five counters
are present. The whole batch is validated before anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any

from qorqut_operations.contracts import DomainError
from qorqut_context.integration.usage.schema import STAGES, STATES, TOKEN_FIELDS

RECEIPT_FORMAT = "qorqut-context-call-v1"
MAX_BATCH = 500
INT64 = 2 ** 63 - 1
MAX_COST = 10 ** 9
MAX_COST_TEXT = 40
TEXT_LIMIT, SOURCE_LIMIT, ERROR_TYPE_LIMIT, ERROR_MESSAGE_LIMIT = 200, 64, 80, 300
RECEIPT_KEYS = frozenset({"call_id", "event_id", "attempt_id", "attempt_sequence", "stage", "state", "started_at", "finished_at",
                          "configured", "observed", "usage", "evidence", "bootstrap_call_id", "error"})
EVIDENCE_KEYS = frozenset({"usage_source", "usage_scope", "usage_updates", "ignored_updates", "usage_observed", "counters_complete",
                           "usage_complete", "cost_observed", "limitations"})
LIMITATIONS = ("unfinished_call", "usage_unreported", "partial_counters", "final_totals_unproven", "cost_unreported",
               "ignored_updates")
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
EVENT_ID = re.compile(r"[a-z0-9]{5}")
ATTEMPT_ID = re.compile(r"[0-9a-f]{32}")
BOOTSTRAP_CALL_ID = re.compile(r"[0-9a-f]{32}:bootstrap")


@dataclass(frozen=True)
class CallReceipt:
    """One validated receipt as flat, immutable facts. ``cost_usd`` is canonical plain decimal text or ``None``."""

    call_id: str
    event_id: str
    attempt_id: str
    attempt_sequence: int
    stage: str
    state: str
    started_at: str
    finished_at: str | None
    configured_model: str
    configured_effort: str
    configured_service_tier: str
    observed_provider: str | None
    observed_model: str | None
    thread_id: str | None
    turn_id: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None
    cost_usd: str | None
    usage_source: str | None
    usage_scope: str | None
    usage_updates: int
    ignored_updates: int
    bootstrap_call_id: str | None
    error_type: str | None
    error_message: str | None

    @property
    def identity(self) -> tuple:
        """What never changes for a call: its binding, start and configuration, and its bootstrap reference."""
        return (self.event_id, self.attempt_id, self.attempt_sequence, self.stage, self.started_at, self.configured_model,
                self.configured_effort, self.configured_service_tier, self.bootstrap_call_id)

    @property
    def counters(self) -> tuple:
        return tuple(getattr(self, name) for name in TOKEN_FIELDS)

    @property
    def usage(self) -> tuple:
        """The cumulative snapshot: counters, cost and its provenance."""
        return (*self.counters, self.cost_usd, self.usage_source, self.usage_scope, self.usage_updates)

    @property
    def observation(self) -> tuple:
        """Everything that may advance with a later observation of the same call."""
        return (self.state, self.finished_at, self.observed_provider, self.observed_model, self.thread_id, self.turn_id,
                *self.usage, self.ignored_updates, self.error_type, self.error_message)


def derived_flags(*, state: str, counters, cost_usd, ignored_updates: int) -> dict:
    """The completeness facts a receipt must carry, computed from its primary facts exactly as the library does."""
    observed = any(value is not None for value in counters)
    limitations = (["unfinished_call"] if state == "started" else []) + (
        ["usage_unreported"] if not observed else ["partial_counters"] if None in counters else []) + (
        ["final_totals_unproven"] if observed else []) + (
        ["cost_unreported"] if cost_usd is None else []) + (["ignored_updates"] if ignored_updates else [])
    return {"usage_observed": observed, "counters_complete": None not in counters, "usage_complete": False,
            "cost_observed": cost_usd is not None, "limitations": limitations}


def decimal_text(value: Decimal) -> str:
    """The canonical plain text of a finite decimal: no exponent, no trailing fractional zeros, ``0`` for any zero.

    Built from the exact coefficient and exponent, never through ``normalize`` or any other operation that rounds under the
    ambient decimal context, so the text is exact at every context precision."""
    if not value.is_finite():
        raise ValueError("A decimal text needs a finite number")
    sign, digits, exponent = value.as_tuple()
    coefficient = "".join(map(str, digits)).lstrip("0")
    if not coefficient:
        return "0"
    if exponent >= 0:
        text = coefficient + "0" * exponent
    else:
        padded = coefficient.rjust(1 - exponent, "0")
        whole, fraction = padded[:exponent], padded[exponent:].rstrip("0")
        text = f"{whole}.{fraction}" if fraction else whole
    return f"-{text}" if sign else text


def _refuse(errors: dict) -> None:
    raise DomainError("validation_error", "The usage receipts are not valid qorqut-context-call-v1 receipts",
                      field_errors=errors)


class _Reader:
    """Collects field errors for one receipt; every check names its path so a refusal says exactly what was wrong."""

    def __init__(self, path: str, errors: dict):
        self.path, self.errors = path, errors

    def fail(self, name: str, message: str) -> None:
        self.errors.setdefault(f"{self.path}.{name}" if name else self.path, message)

    def closed(self, value, keys: frozenset, name: str):
        if type(value) is not dict:
            self.fail(name, "must be an object")
            return None
        path = f"{self.path}.{name}" if name else self.path
        if set(value) != keys:
            extra, missing = sorted(map(str, set(value) - keys)), sorted(keys - set(value))
            self.errors.setdefault(path, "must have exactly the documented fields" + (
                f" (unexpected: {', '.join(extra)})" if extra else "") + (f" (missing: {', '.join(missing)})" if missing else ""))
            return None
        return value

    def text(self, value, name: str, limit: int = TEXT_LIMIT, *, nullable: bool = False, empty: bool = False):
        if value is None and nullable:
            return None
        if type(value) is not str or not (empty or value) or len(value) > limit:
            self.fail(name, f"must be {'null or ' if nullable else ''}a string of at most {limit} characters")
            return None
        return value

    def count(self, value, name: str, *, nullable: bool = False, minimum: int = 0):
        if value is None and nullable:
            return None
        if type(value) is not int or not minimum <= value <= INT64:
            self.fail(name, f"must be {'null or ' if nullable else ''}an integer from {minimum} to {INT64}")
            return None
        return value

    def pattern(self, value, name: str, pattern: re.Pattern, *, nullable: bool = False):
        if value is None and nullable:
            return None
        if type(value) is not str or pattern.fullmatch(value) is None:
            self.fail(name, "has an invalid format")
            return None
        return value

    def cost(self, value, name: str):
        if value is None:
            return None
        if type(value) is float and not math.isfinite(value):
            self.fail(name, "must be finite")
            return None
        if type(value) not in (int, float) or value < 0 or value > MAX_COST:
            self.fail(name, f"must be null or a nonnegative number of at most {MAX_COST}")
            return None
        try:
            text = decimal_text(Decimal(repr(value) if type(value) is float else value))
        except (InvalidOperation, ValueError):
            self.fail(name, "must be a finite number")
            return None
        if len(text) > MAX_COST_TEXT:
            self.fail(name, f"has more than {MAX_COST_TEXT} characters in plain decimal form")
            return None
        return text


def parse_call(value: Any, path: str, errors: dict) -> CallReceipt | None:
    """One receipt as a ``CallReceipt``, or ``None`` after recording every problem under ``path``."""
    reader = _Reader(path, errors)
    if type(value) is dict and "format" in value:
        if value["format"] != RECEIPT_FORMAT:
            reader.fail("format", f"must be {RECEIPT_FORMAT}")
            return None
        value = {key: item for key, item in value.items() if key != "format"}
    value = reader.closed(value, RECEIPT_KEYS, "")
    if value is None:
        return None
    before = len(errors)
    attempt_id = reader.pattern(value["attempt_id"], "attempt_id", ATTEMPT_ID)
    stage = value["stage"] if value["stage"] in STAGES else reader.fail("stage", f"must be one of {', '.join(STAGES)}")
    state = value["state"] if value["state"] in STATES else reader.fail("state", f"must be one of {', '.join(STATES)}")
    event_id = reader.pattern(value["event_id"], "event_id", EVENT_ID)
    sequence = reader.count(value["attempt_sequence"], "attempt_sequence", minimum=1)
    if attempt_id and stage and value["call_id"] != f"{attempt_id}:{stage}":
        reader.fail("call_id", "must be '<attempt_id>:<stage>'")
    started_at = reader.pattern(value["started_at"], "started_at", TIME)
    finished_at = reader.pattern(value["finished_at"], "finished_at", TIME, nullable=True)
    if state and (state == "started") != (value["finished_at"] is None):
        reader.fail("finished_at", "is null exactly while the call is started")
    configured = reader.closed(value["configured"], frozenset({"model", "effort", "service_tier"}), "configured")
    observed = reader.closed(value["observed"], frozenset({"provider", "model", "thread_id", "turn_id"}), "observed")
    usage = reader.closed(value["usage"], frozenset({*TOKEN_FIELDS, "cost_usd"}), "usage")
    evidence = reader.closed(value["evidence"], EVIDENCE_KEYS, "evidence")
    settings = tuple(reader.text(configured[key], f"configured.{key}") for key in ("model", "effort", "service_tier")) if configured else (None,) * 3
    seen = tuple(reader.text(observed[key], f"observed.{key}", nullable=True) for key in ("provider", "model", "thread_id", "turn_id")) if observed else (None,) * 4
    counters = tuple(reader.count(usage[name], f"usage.{name}", nullable=True) for name in TOKEN_FIELDS) if usage else (None,) * 5
    cost = reader.cost(usage["cost_usd"], "usage.cost_usd") if usage else None
    source = scope = None
    updates = ignored = 0
    if evidence:
        source = reader.text(evidence["usage_source"], "evidence.usage_source", SOURCE_LIMIT, nullable=True)
        scope = evidence["usage_scope"]
        if scope not in (None, "native", "call"):
            reader.fail("evidence.usage_scope", "must be null, native or call")
        updates = reader.count(evidence["usage_updates"], "evidence.usage_updates") or 0
        ignored = reader.count(evidence["ignored_updates"], "evidence.ignored_updates") or 0
        for flag in ("usage_observed", "counters_complete", "usage_complete", "cost_observed"):
            if type(evidence[flag]) is not bool:
                reader.fail(f"evidence.{flag}", "must be a real boolean")
        if (updates > 0) != (evidence["usage_source"] is not None) or (evidence["usage_source"] is not None) != (scope is not None):
            reader.fail("evidence", "usage updates, source and scope must be present together")
        if scope == "native" and not (seen[2] and seen[3]):
            reader.fail("evidence.usage_scope", "native evidence needs the verified thread and turn")
        limitations = evidence["limitations"]
        if type(limitations) is not list or any(type(item) is not str for item in limitations):
            reader.fail("evidence.limitations", "must be a list of strings")
    bootstrap = reader.pattern(value["bootstrap_call_id"], "bootstrap_call_id", BOOTSTRAP_CALL_ID, nullable=True)
    if bootstrap and stage != "propagation":
        reader.fail("bootstrap_call_id", "belongs to a propagation call only")
    error_type = error_message = None
    error = value["error"]
    if state == "failed":
        error = reader.closed(error, frozenset({"type", "message"}), "error")
        if error:
            error_type = reader.text(error["type"], "error.type", ERROR_TYPE_LIMIT)
            error_message = reader.text(error["message"], "error.message", ERROR_MESSAGE_LIMIT, empty=True)
    elif state and error is not None:
        reader.fail("error", "is null unless the call failed")
    if len(errors) > before:
        return None
    if evidence:
        expected = derived_flags(state=state, counters=counters, cost_usd=cost, ignored_updates=ignored)
        if any(evidence[name] is not expected[name] for name in ("usage_observed", "counters_complete", "usage_complete", "cost_observed")):
            reader.fail("evidence", "the completeness flags must follow from the counters (usage_complete is always false)")
        if evidence["limitations"] != expected["limitations"]:
            reader.fail("evidence.limitations", "must follow from the call state, counters and cost")
        if len(errors) > before:
            return None
    return CallReceipt(
        call_id=value["call_id"], event_id=event_id, attempt_id=attempt_id, attempt_sequence=sequence, stage=stage, state=state,
        started_at=started_at, finished_at=finished_at, configured_model=settings[0], configured_effort=settings[1],
        configured_service_tier=settings[2], observed_provider=seen[0], observed_model=seen[1], thread_id=seen[2], turn_id=seen[3],
        input_tokens=counters[0], cached_input_tokens=counters[1], output_tokens=counters[2], reasoning_output_tokens=counters[3],
        total_tokens=counters[4], cost_usd=cost, usage_source=source, usage_scope=scope, usage_updates=updates,
        ignored_updates=ignored, bootstrap_call_id=bootstrap, error_type=error_type, error_message=error_message)


def parse_batch(receipts: Any) -> tuple[CallReceipt, ...]:
    """Validate a whole batch (at most ``MAX_BATCH`` receipts, no repeated call ID) before anything is written."""
    if type(receipts) not in (list, tuple):
        _refuse({"receipts": "must be a list of receipt objects"})
    if len(receipts) > MAX_BATCH:
        _refuse({"receipts": f"at most {MAX_BATCH} receipts per batch"})
    errors: dict = {}
    parsed = [parse_call(item, f"receipts[{index}]", errors) for index, item in enumerate(receipts)]
    if errors:
        _refuse(errors)
    seen: dict[str, int] = {}
    for index, item in enumerate(parsed):
        if item.call_id in seen:
            errors[f"receipts[{index}].call_id"] = f"repeats receipts[{seen[item.call_id]}]"
        seen.setdefault(item.call_id, index)
    if errors:
        _refuse(errors)
    return tuple(parsed)
