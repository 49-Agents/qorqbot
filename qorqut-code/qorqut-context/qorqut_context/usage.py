"""Durable per-call model usage evidence kept with the private run records, and its bounded read-only page API.

Every actual runner call of a pipeline attempt is reserved as a ``started`` receipt before the runner is
launched, updated whenever an attributable cumulative usage snapshot is observed, and settled afterwards.
Unknown is ``None`` and never an inferred zero.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from .engine import atomic_write
from .schema import Conflict, ContextError, encode


RECEIPT_FORMAT = "qorqut-context-call-v1"
STAGES = ("bootstrap", "propagation")
STATES = ("started", "completed", "failed")
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
MAX_PAGE = 500
INT64 = 2 ** 63 - 1
RECEIPT_BYTES, ATTEMPT_BYTES = 64 * 1024, 1024 * 1024
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
ATTEMPT_ID = re.compile(r"[0-9a-f]{32}")
EVIDENCE_KEYS = {"usage_source", "usage_scope", "usage_updates", "ignored_updates", "usage_observed",
                 "counters_complete", "usage_complete", "cost_observed", "limitations"}
RECEIPT_KEYS = {"format", "call_id", "event_id", "attempt_id", "attempt_sequence", "stage", "state", "started_at",
                "finished_at", "configured", "observed", "usage", "evidence", "bootstrap_call_id", "error"}


@dataclass(frozen=True)
class Usage:
    """One cumulative observation of a call's counters. ``None`` is unknown, never zero.

    ``thread_id``/``turn_id`` name the native identity the counters were attributed to (``None`` when the
    runner has none); ``source`` is the provenance label of the evidence.
    """
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    source: str = "runner"


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def count_ok(value):
    return type(value) is int and 0 <= value <= INT64


def cost_ok(value):
    return ((type(value) is int and 0 <= value <= INT64) or
            (type(value) is float and math.isfinite(value) and value >= 0))


def text_ok(value, limit=200):
    return isinstance(value, str) and 0 < len(value) <= limit


def derived_evidence(receipt):
    """Completeness flags computed from a receipt's primary facts (never stored without recomputation).

    ``counters_complete`` is only the presence of all five observed counters. ``usage_complete`` means
    *verified final* usage: no evidence source can prove totals final (the native protocol has no such marker,
    and a finished call or a full snapshot is not one), so it is always ``False`` and is never derived from them.
    """
    usage, primary = receipt["usage"], receipt["evidence"]
    tokens = [usage[name] for name in TOKEN_FIELDS]
    observed = any(value is not None for value in tokens)
    ignored = primary["ignored_updates"]
    limits = (["unfinished_call"] if receipt["state"] == "started" else []) + (
        ["usage_unreported"] if not observed else ["partial_counters"] if None in tokens else []) + (
        ["final_totals_unproven"] if observed else []) + (
        ["cost_unreported"] if usage["cost_usd"] is None else []) + (
        ["ignored_updates"] if ignored else [])
    return {"usage_source": primary["usage_source"], "usage_scope": primary["usage_scope"],
            "usage_updates": primary["usage_updates"], "ignored_updates": ignored, "usage_observed": observed,
            "counters_complete": None not in tokens, "usage_complete": False,
            "cost_observed": usage["cost_usd"] is not None, "limitations": limits}


class CallMeter:
    """The durable receipt of one actual runner call."""

    def __init__(self, path, receipt):
        self.path, self.receipt = path, receipt
        self.delivered = None
        self.runner_ignored = self.meter_ignored = 0

    @property
    def call_id(self):
        return self.receipt["call_id"]

    def write(self):
        receipt = self.receipt
        receipt["evidence"]["ignored_updates"] = self.runner_ignored + self.meter_ignored
        receipt["evidence"] = derived_evidence(receipt)
        atomic_write(self.path, encode(receipt))

    def identity(self, value):
        """Take only verified native identities the runner announces; never copy its other identity detail.

        A non-null native thread/turn identity is immutable once announced. Provider and model evidence may
        change (a reroute changes the observed model), but never relabels an earlier usage snapshot.
        """
        observed, changed = self.receipt["observed"], False
        for key, name in (("thread_id", "thread_id"), ("turn_id", "turn_id"),
                          ("model_provider", "provider"), ("observed_model", "model")):
            if text_ok(value.get(key)) and observed[name] != value[key] and (
                    observed[name] is None or name not in ("thread_id", "turn_id")):
                observed[name], changed = value[key], True
        ignored = value.get("usage_updates_ignored")
        if count_ok(ignored) and ignored != self.runner_ignored:
            self.runner_ignored, changed = ignored, True
        if changed:
            self.write()

    def accept(self, usage):
        """Replace the cumulative snapshot; returns False (and counts it) when the update is unusable.

        Evidence claiming native IDs is accepted only when both match the call's already verified pair
        exactly (scope ``native``); evidence carrying neither is a trusted runner's call-scoped evidence
        (scope ``call``). An early, partial or foreign claim is never kept or relabelled by a later identity.
        """
        if not isinstance(usage, Usage):
            return False
        counters = [getattr(usage, name) for name in TOKEN_FIELDS]
        observed = self.receipt["observed"]
        claim = (usage.thread_id, usage.turn_id)
        if claim == (None, None):
            scope = "call"
        elif all(text_ok(item) for item in claim) and claim == (observed["thread_id"], observed["turn_id"]):
            scope = "native"
        else:
            return False
        if (any(value is not None and not count_ok(value) for value in counters) or
                (usage.cost_usd is not None and not cost_ok(usage.cost_usd)) or not text_ok(usage.source, 64)):
            return False
        self.receipt["usage"] = {**dict(zip(TOKEN_FIELDS, counters)),
                                 "cost_usd": None if usage.cost_usd is None else float(usage.cost_usd)}
        evidence = self.receipt["evidence"]
        evidence["usage_source"], evidence["usage_scope"] = usage.source, scope
        evidence["usage_updates"] += 1
        return True

    def observe(self, usage):
        """Runner callback: persist each attributable snapshot at once so a later failure cannot erase it."""
        self.delivered = usage
        if isinstance(usage, Usage) and all(getattr(usage, name) is None for name in (*TOKEN_FIELDS, "cost_usd")):
            return                                  # carries no evidence
        if not self.accept(usage):
            self.meter_ignored += 1
        self.write()

    def settle(self, state, error=None):
        self.receipt.update(state=state, finished_at=utc_now(), error=error)
        self.write()

    def complete(self, result):
        if result.usage is not None and result.usage != self.delivered:
            self.observe(result.usage)
        if result.status == "completed":
            self.settle("completed")
        else:
            self.settle("failed", {"type": "TurnStatus",
                                   "message": ("Model turn ended with status " + str(result.status))[:300]})

    def fail(self, error):
        self.settle("failed", {"type": type(error).__name__[:80], "message": str(error)[:300]})


class AttemptCalls:
    """The runner as one pipeline attempt sees it: every actual call is reserved before launch and settled after.

    ``run`` has the runner protocol, so the bootstrap sampler can use it unchanged.
    """

    def __init__(self, runner, directory, attempt, configured):
        self.runner, self.directory, self.attempt, self.configured = runner, Path(directory), attempt, configured
        self.bootstrap_call_id = None

    def reserve(self, stage):
        if stage not in STAGES:
            raise ContextError("Unknown model stage")
        calls = self.directory / "calls"
        calls.mkdir(mode=0o700, exist_ok=True)
        calls.chmod(0o700)
        path = calls / (stage + ".json")
        if probe(path, "A call receipt") is not None:
            raise Conflict(f"This attempt already reserved its {stage} call")
        attempt = self.attempt
        meter = CallMeter(path, {
            "format": RECEIPT_FORMAT, "call_id": f"{attempt['attempt_id']}:{stage}", "event_id": attempt["event_id"],
            "attempt_id": attempt["attempt_id"], "attempt_sequence": attempt["sequence"], "stage": stage,
            "state": "started", "started_at": utc_now(), "finished_at": None, "configured": dict(self.configured),
            "observed": {"provider": None, "model": None, "thread_id": None, "turn_id": None},
            "usage": {**{name: None for name in TOKEN_FIELDS}, "cost_usd": None},
            "evidence": {"usage_source": None, "usage_scope": None, "usage_updates": 0, "ignored_updates": 0},
            "bootstrap_call_id": self.bootstrap_call_id if stage == "propagation" else None, "error": None})
        meter.write()
        return meter

    def run(self, *, stage, on_identity=None, **arguments):
        meter = self.reserve(stage)
        if stage == "bootstrap":
            self.bootstrap_call_id = meter.call_id

        def identity(value):
            meter.identity(value)
            if on_identity:
                on_identity(value)

        try:
            result = self.runner.run(stage=stage, on_identity=identity, on_usage=meter.observe, **arguments)
        except Exception as error:
            try:
                meter.fail(error)
            except OSError:
                pass                                # the receipt stays ``started``: unknown, not free
            raise
        meter.complete(result)
        return result


class Unavailable(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def corrupt(message):
    return Unavailable("corrupt_evidence", message)


def read_bounded(path, limit, label):
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise corrupt(f"{label} is not a regular file")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(limit + 1)
    except FileNotFoundError as error:
        raise corrupt(f"{label} is missing") from error
    except OSError as error:
        raise Unavailable("unreadable_evidence", f"{label} cannot be read") from error
    if len(data) > limit:
        raise corrupt(f"{label} exceeds its size bound")
    try:
        value = json.loads(data, parse_constant=lambda constant: (_ for _ in ()).throw(ValueError(constant)))
    except (ValueError, UnicodeError) as error:
        raise corrupt(f"{label} is not valid JSON") from error
    if type(value) is not dict:
        raise corrupt(f"{label} is not an object")
    return value


def check(condition, label):
    if not condition:
        raise corrupt(f"{label} is invalid")


def probe(path, label):
    """The ``lstat`` of a path, or ``None`` only when it is genuinely absent; any other failure is unreadable."""
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise Unavailable("unreadable_evidence", f"{label} cannot be inspected") from error


def directory_present(path, label):
    """True for a real directory, False when absent. A symbolic link or any other type is corrupt evidence."""
    state = probe(path, label)
    if state is None:
        return False
    check(stat.S_ISDIR(state.st_mode), label)       # ``lstat``: a symlinked directory is never followed
    return True


def validated_item(value, event_id, attempt_id, sequence, stage):
    label = f"Call receipt {attempt_id}:{stage}"
    check(set(value) == RECEIPT_KEYS and value["format"] == RECEIPT_FORMAT, label)
    check(value["call_id"] == f"{attempt_id}:{stage}" and value["event_id"] == event_id and
          value["attempt_id"] == attempt_id and value["attempt_sequence"] == sequence and
          value["stage"] == stage and value["state"] in STATES, label)
    check(type(value["started_at"]) is str and TIME.fullmatch(value["started_at"]), label)
    check(value["finished_at"] is None or (type(value["finished_at"]) is str and TIME.fullmatch(value["finished_at"])), label)
    configured, observed, usage, primary = value["configured"], value["observed"], value["usage"], value["evidence"]
    check(type(configured) is dict and set(configured) == {"model", "effort", "service_tier"} and
          all(text_ok(item) for item in configured.values()), label)
    check(type(observed) is dict and set(observed) == {"provider", "model", "thread_id", "turn_id"} and
          all(item is None or text_ok(item) for item in observed.values()), label)
    check(type(usage) is dict and set(usage) == {*TOKEN_FIELDS, "cost_usd"} and
          all(usage[name] is None or count_ok(usage[name]) for name in TOKEN_FIELDS) and
          (usage["cost_usd"] is None or cost_ok(usage["cost_usd"])), label)
    check(type(primary) is dict and set(primary) == EVIDENCE_KEYS and
          (primary["usage_source"] is None or text_ok(primary["usage_source"], 64)) and
          primary["usage_scope"] in (None, "native", "call") and
          count_ok(primary["usage_updates"]) and count_ok(primary["ignored_updates"]) and
          (primary["usage_updates"] > 0) == (primary["usage_source"] is not None) == (primary["usage_scope"] is not None), label)
    check(primary["usage_scope"] != "native" or (observed["thread_id"] is not None and observed["turn_id"] is not None), label)
    check(primary == derived_evidence(value), label)
    bootstrap = value["bootstrap_call_id"]
    check(bootstrap is None or (stage == "propagation" and type(bootstrap) is str and
                                re.fullmatch(r"[0-9a-f]{32}:bootstrap", bootstrap)), label)
    error = value["error"]
    check((value["state"] == "started") == (value["finished_at"] is None), label)
    check(error is None if value["state"] != "failed" else
          type(error) is dict and set(error) == {"type", "message"} and all(isinstance(item, str) for item in error.values()), label)
    return {key: item for key, item in value.items() if key != "format"}


def attempts(runs, event_id):
    """Attempt directories of one event in immutable sequence order, validated against their own records.

    Only a genuinely absent run or event directory is empty; unreadable or redirected evidence is not.
    """
    if not directory_present(runs, "The run directory"):
        return []
    root = runs / event_id
    if not directory_present(root, "The event run directory"):
        return []
    try:
        names = sorted(os.listdir(root))
    except OSError as error:
        raise Unavailable("unreadable_evidence", "The event run directory cannot be listed") from error
    found = {}
    for name in names:
        directory = root / name
        state = probe(directory, "An attempt directory")
        if state is None or not (stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode)):
            continue                                  # not an attempt directory
        path = directory / "attempt.json"
        if probe(path, "An attempt record") is None:
            continue
        check(ATTEMPT_ID.fullmatch(name) is not None and directory_present(directory, "An attempt directory"),
              "An attempt directory")
        record = read_bounded(path, ATTEMPT_BYTES, "An attempt record")
        sequence = record.get("sequence")
        check(record.get("event_id") == event_id and record.get("attempt_id") == name and
              type(sequence) is int and sequence >= 1 and sequence not in found, "An attempt record")
        found[sequence] = directory
    return sorted(found.items())


def receipt_entries(runs, event_id):
    """``(sequence, stage rank, path, attempt id, stage)`` of every recorded call receipt of the event."""
    entries = []
    for sequence, directory in attempts(Path(runs), event_id):
        calls = directory / "calls"
        if not directory_present(calls, "A calls directory"):
            continue
        for rank, stage in enumerate(STAGES):
            path = calls / (stage + ".json")
            if probe(path, "A call receipt") is not None:
                entries.append((sequence, rank, path, directory.name, stage))
    return entries


def encode_cursor(event_id, limit, key):
    payload = json.dumps({"v": 1, "event": event_id, "limit": limit, "after": list(key)},
                         separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + hashlib.sha256(payload).hexdigest()[:16]


def decode_cursor(cursor, event_id, limit):
    problem = ContextError("Invalid usage receipt cursor")
    if type(cursor) is not str or len(cursor) > 512 or cursor.count(".") != 1:
        raise problem
    body, check_value = cursor.split(".")
    try:
        payload = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True)
        value = json.loads(payload)
    except (ValueError, UnicodeError) as error:
        raise problem from error
    if (hashlib.sha256(payload).hexdigest()[:16] != check_value or type(value) is not dict or
            set(value) != {"v", "event", "limit", "after"} or value["v"] != 1 or
            type(value["after"]) is not list or len(value["after"]) != 2 or
            not all(type(item) is int for item in value["after"]) or value["after"][0] < 1 or
            value["after"][1] not in range(len(STAGES))):
        raise problem
    if value["event"] != event_id or value["limit"] != limit:
        raise ContextError("This usage receipt cursor was issued for a different event or page size")
    return tuple(value["after"])


def unavailable(event_id, code, message):
    return {"status": "unavailable", "event_id": event_id, "items": [], "next_cursor": None,
            "error": {"code": code, "message": message[:300]}}


def page(runs, event_id, *, limit, cursor):
    """One bounded page of an event's call receipts: ``{status, event_id, items, next_cursor, error}``.

    Invalid arguments raise ``ContextError``; unreadable or inconsistent evidence is ``unavailable``.
    Nothing is created, locked, started or retried.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ContextError(f"limit must be an integer from 1 to {MAX_PAGE}")
    after = decode_cursor(cursor, event_id, limit) if cursor is not None else None
    try:
        later = [entry for entry in receipt_entries(runs, event_id) if after is None or entry[:2] > after]
        items = [validated_item(read_bounded(path, RECEIPT_BYTES, f"Call receipt {attempt}:{stage}"),
                                event_id, attempt, sequence, stage)
                 for sequence, rank, path, attempt, stage in later[:limit]]
    except Unavailable as problem:
        return unavailable(event_id, problem.code, str(problem))
    except OSError:                                    # defensive: no raw operating-system error escapes
        return unavailable(event_id, "unreadable_evidence", "The usage evidence cannot be read")
    more = len(later) > limit
    return {"status": "ready", "event_id": event_id, "items": items,
            "next_cursor": encode_cursor(event_id, limit, later[limit - 1][:2]) if more else None, "error": None}
