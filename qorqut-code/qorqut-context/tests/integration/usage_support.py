"""Synthetic ``qorqut-context-call-v1`` receipts in exactly the shape the Context library's ``Pipeline.usage_receipts`` returns,
and disposable queue inputs and attempts to bind them to. No model, provider or Context checkout is involved here."""

from __future__ import annotations

from operational_support import COORDINATOR, OperationalHost
from qorqut_context.integration import usage as context_usage
from qorqut_context.integration.queue import transitions
from support import WORKER

EVENT = "ab12c"
TOKENS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
FULL = {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2, "reasoning_output_tokens": 1, "total_tokens": 12}
SOURCE = "thread/tokenUsage/updated"
NOON = "2026-10-12T12:00:00.000000Z"


def attempt_id(number: int) -> str:
    """A 32-character lowercase hexadecimal library attempt ID."""
    return f"{number:032x}"


def receipt(attempt: int = 1, stage: str = "bootstrap", *, event: str = EVENT, sequence: int | None = None, state: str = "started",
            started: str = NOON, finished: str | None = None, tokens: dict | None = None, cost=None, updates: int | None = None,
            ignored: int = 0, thread: str | None = None, turn: str | None = None, provider: str | None = None,
            model: str | None = None, scope: str | None = None, bootstrap: str | None = None, error: dict | None = None,
            configured: dict | None = None) -> dict:
    """One library item. The completeness flags are restated here from the documented contract, not imported."""
    counters = {name: None for name in TOKENS} | (tokens or {})
    updates = (1 if tokens or cost is not None else 0) if updates is None else updates
    scope = scope or (None if not updates else "native" if thread and turn else "call")
    observed = any(value is not None for value in counters.values())
    limitations = (["unfinished_call"] if state == "started" else []) + (
        ["usage_unreported"] if not observed else ["partial_counters"] if None in counters.values() else []) + (
        ["final_totals_unproven"] if observed else []) + (["cost_unreported"] if cost is None else []) + (
        ["ignored_updates"] if ignored else [])
    identity = attempt_id(attempt)
    if state != "started" and finished is None:
        finished = "2026-10-12T12:00:01.000000Z"
    if state == "failed" and error is None:
        error = {"type": "RuntimeError", "message": "transport disappeared"}
    return {
        "call_id": f"{identity}:{stage}", "event_id": event, "attempt_id": identity, "attempt_sequence": sequence or attempt,
        "stage": stage, "state": state, "started_at": started, "finished_at": finished,
        "configured": configured or {"model": "gpt-6-astra", "effort": "max", "service_tier": "default"},
        "observed": {"provider": provider, "model": model, "thread_id": thread, "turn_id": turn},
        "usage": {**counters, "cost_usd": cost},
        "evidence": {"usage_source": SOURCE if updates else None, "usage_scope": scope, "usage_updates": updates,
                     "ignored_updates": ignored, "usage_observed": observed, "counters_complete": None not in counters.values(),
                     "usage_complete": False, "cost_observed": cost is not None, "limitations": limitations},
        "bootstrap_call_id": bootstrap, "error": error}


def native(attempt=1, stage="bootstrap", **kwargs) -> dict:
    """A receipt whose full counters were attributed to a verified native thread and turn."""
    return receipt(attempt, stage, tokens=kwargs.pop("tokens", FULL), thread=kwargs.pop("thread", "thread-1"),
                   turn=kwargs.pop("turn", "turn-1"), provider=kwargs.pop("provider", "openai"),
                   model=kwargs.pop("model", "gpt-6-observed"), **kwargs)


class Ledger:
    """A queue-backed host with the usage contribution, and the three calls a Context driver and its collector make."""

    def __init__(self, tmp_path):
        self.host = OperationalHost(tmp_path, extra=(context_usage.storage_contribution(),))
        self.database = self.host.database

    def submit(self, ctx=None, path="foundation/notes/a.md") -> str:
        arguments = {"operation": "add", "path": path, "content": "Body", "metadata": {"kind": "note", "title": "A note", "origin": "agent"}}
        return self.host.data("context_queue_submit", arguments, ctx or self.host.agent())["input"]["id"]

    def attempt(self, input_id: str, event: str = EVENT, *, finish: str | None = None) -> None:
        """Start the queue's next attempt of the input on ``event``; ``finish`` ends it ``published`` or ``blocked``."""
        with self.database.transaction() as uow:
            transitions.start_attempt(uow, driver_id=WORKER, input_id=input_id, context_event_id=event)
            if finish == "published":
                transitions.record_published(uow, driver_id=WORKER, input_id=input_id, ordubashy_id=COORDINATOR, outcomes=self.host.outcomes,
                                             result={"context_event_id": event, "published_commit": "c0ffee"},
                                             handoff_note={"disposition": "empty", "text": ""})
            elif finish == "blocked":
                transitions.record_blocked(uow, driver_id=WORKER, input_id=input_id, ordubashy_id=COORDINATOR,
                                           error={"code": "context_failed", "message": "refused", "retryable": True})

    def retry(self, input_id: str) -> None:
        self.host.data("context_queue_retry", {"input_id": input_id, "expected_state": "blocked"}, self.host.owner())

    def ingest(self, input_id: str, *receipts, driver_id: str = WORKER):
        with self.database.transaction() as uow:
            return context_usage.ingest(uow, driver_id=driver_id, input_id=input_id, receipts=list(receipts))

    def scan(self, input_id: str, status: str, error=None, event: str = EVENT, driver_id: str = WORKER):
        with self.database.transaction() as uow:
            return context_usage.record_scan(uow, driver_id=driver_id, input_id=input_id, context_event_id=event, status=status, error=error)

    def page(self, **filters):
        with self.database.read() as uow:
            return context_usage.list_calls(uow, **filters)

    def totals(self, **filters):
        with self.database.read() as uow:
            return context_usage.aggregates(uow, **filters)

    def row(self, call_id: str) -> dict:
        rows = self.host.query("SELECT * FROM context_usage_calls WHERE call_id=?", call_id)
        return rows[0] if rows else {}
