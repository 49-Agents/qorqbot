"""Bounded ordered dispatch; every inner operation owns its transaction/receipt."""

import logging
import time

from bazarlyk.application.batches import NAME
from bazarlyk.contracts import DomainError, OperationResult, new_id

START_BUDGET_SECONDS = 30
logger = logging.getLogger(__name__)


def validate_entries(arguments):
    # The registry validates the entire envelope's schema before this check.
    entries = arguments["operations"]
    if len({entry["id"] for entry in entries}) != len(entries):
        raise DomainError("validation_error", "Batch operation IDs must be unique")
    if any(entry["operation"] == NAME for entry in entries):
        raise DomainError("validation_error", "Nested batches are not supported")
    if any(isinstance(entry.get("arguments"), dict) and entry["arguments"].get("response_detail") == "text"
           for entry in entries):
        raise DomainError("validation_error", "response_detail=text is not available inside a batch; use compact or full")


async def execute(arguments, *, invoke, authorize, clock=None):
    clock = clock or time.monotonic
    started, results, attempted = clock(), [], 0
    stopped_after, stop_reason = None, None
    for entry in arguments["operations"]:
        row = {"id": entry["id"], "operation": entry["operation"]}
        if stop_reason is None and clock() - started >= START_BUDGET_SECONDS:
            stop_reason = "start_budget_exhausted"
        if stop_reason is None:
            try:
                actor = await authorize()
            except DomainError as exc:
                stop_reason = "authorization_changed"
                row["error"] = exc.to_dict()
            except Exception:
                request_id = new_id()
                logger.exception("Batch authorization failed: %s", request_id)
                stop_reason = "authorization_failed"
                row["error"] = {"code": "internal_error", "message": "Authorization could not be checked", "request_id": request_id}
        if stop_reason is None and clock() - started >= START_BUDGET_SECONDS:
            stop_reason = "start_budget_exhausted"
        if stop_reason is not None:
            results.append({**row, "state": "not_executed", "reason": stop_reason})
            continue
        try:
            result = await invoke(entry["operation"], entry["arguments"], actor)
        except Exception:
            # Keep prior receipts even if dispatch unexpectedly fails. The current
            # effect may be unknown: never retry it with a fresh idempotency key.
            request_id = new_id()
            logger.exception("Batch dispatch failed: %s", request_id)
            result = OperationResult.failure(DomainError("internal_error", "Dispatch failed; retry with the same write key"), request_id=request_id).to_dict()
            stop_reason, stopped_after = "dispatch_failed", entry["id"]
        attempted += 1
        results.append({**row, "state": "returned", "result": result})
        if result["status"] != "ok" and arguments.get("on_non_ok", "stop") == "stop":
            stop_reason, stopped_after = "operation_" + result["status"], entry["id"]
    incomplete = attempted != len(results) or any(row.get("result", {}).get("status") == "error" for row in results)
    all_ok = all(row.get("result", {}).get("status") == "ok" for row in results)
    data = {"results": results, "attempted": attempted, "total": len(results), "all_ok": all_ok,
            "stopped_after": stopped_after, "stop_reason": stop_reason}
    error = DomainError("batch_incomplete", "Inspect individual results: earlier successes remain; skipped operations did not run").to_dict() if incomplete else None
    return OperationResult(request_id=new_id(), status="error" if incomplete else "ok", data=data,
        error=error, warnings=() if all_ok else ("Not every operation finished successfully; approval/async acceptance is not application.",)).to_dict()
