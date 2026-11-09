"""Canonical domain errors; adapters should translate these without rewriting them."""

from qorqut_operations.contracts import DomainError

__all__ = ["DomainError", "AGENTIC_CODE_MAP", "domain_error_from_agentic"]

# qorqut-agentic codes that have a host equivalent, as (domain code, retryable).
# Domain codes are the ones qorqut.transport.results.http_status already maps,
# so an agentic failure gets the same HTTP status and retry advice as a host one.
AGENTIC_CODE_MAP = {
    "not_found": ("not_found", False),
    "revision_conflict": ("revision_conflict", True),
    "stale_revision": ("revision_conflict", True),
    "invalid_request": ("invalid_request", False),
    "validation_failed": ("validation_error", False),
    "unavailable": ("operation_unavailable", True),
    "runtime_unavailable": ("operation_unavailable", True),
    "lock_timeout": ("operation_unavailable", True),
    "internal_error": ("internal_error", False),
}


def domain_error_from_agentic(error):
    """Translate a qorqut-agentic ``AgenticError`` into a host ``DomainError``.

    Codes without a host equivalent keep their meaning under an ``agentic_`` prefix
    so they cannot collide with host codes. The original code is always kept in
    ``details.agentic_code``. Duck-typed on ``code``/``message``/``details`` so
    callers keep importing qorqut-agentic lazily.
    """
    agentic_code = getattr(error, "code", None) or "internal_error"
    code, retryable = AGENTIC_CODE_MAP.get(agentic_code, (f"agentic_{agentic_code}", False))
    message = getattr(error, "message", None) or str(error)
    details = {**(getattr(error, "details", None) or {}), "agentic_code": agentic_code}
    return DomainError(code, message, retryable=retryable, details=details)
