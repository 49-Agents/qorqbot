"""One bounded error/result translation for both transports."""

from qorqut_operations.contracts import DomainError, OperationResult, new_id
from starlette.responses import JSONResponse


def http_status(result, *, created=False):
    if result.status in {"accepted", "signoff_required"}:
        return 202
    if result.status != "error":
        return 201 if created else 200
    code = result.error["code"]
    if code in {"unauthenticated", "invalid_credential", "expired_credential"}:
        return 401
    if code in {"forbidden", "policy_denied", "invalid_origin", "invalid_host", "csrf_required"}:
        return 403
    if code in {"not_found", "unknown_business"}:
        return 404
    if code in {"invalid_json", "invalid_request"}:
        return 400
    if code == "payload_too_large":
        return 413
    if code == "legacy_chat_retired":
        return 410
    if code in {"revision_conflict", "idempotency_conflict", "stale_signoff", "document_conflict", "artifact_conflict", "duplicate_agent_name", "dispatch_in_progress", "memory_drift", "legacy_plan_conflict", "plan_migrated", "content_conflict", "stale_binding"}:
        return 409
    if code in {"busy", "maintenance", "authority_invalid", "authority_not_ready", "operation_unavailable", "bootstrap_unavailable", "apply_incomplete", "creator_unavailable", "content_sessions_unavailable", "content_instruction_source_unavailable"}:
        return 503
    if code in {"internal_error", "operation_failed", "content_storage_error"}:
        return 500
    # 422 covers request-content outcomes, including validation_error and
    # channel_required (a well-formed write sent to the wrong channel).
    return 422


def response(result, *, created=False):
    headers = {"Cache-Control": "no-store"}
    if result.status == "error" and result.error.get("retryable"):
        headers["Retry-After"] = "2"
    return JSONResponse(result.to_dict(), status_code=http_status(result, created=created), headers=headers)


def failure(error, request_id=None):
    if not isinstance(error, DomainError):
        error = DomainError("internal_error", "The request failed; use its correlation ID for diagnosis")
    return response(OperationResult.failure(error, request_id=request_id or new_id()))
