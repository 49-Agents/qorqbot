"""Machine-readable errors shared by the Python facade and the JSON CLI."""

from __future__ import annotations

from typing import Any

# Every code the library can return, with a one-line meaning. docs/INTERFACES.md
# documents these; tests assert the two stay in step.
ERROR_CODES: dict[str, str] = {
    "protected_agentic_area": "A requested write targets a protected agentic area.",
    "invalid_request": "The request is malformed: unknown or missing parameter, wrong type, bad identifier.",
    "validation_failed": "Content failed validation (catalog, schema, argument); nothing was written or started.",
    "unsafe_path": "A path is absolute, escapes the root, names .git, or traverses a symlink.",
    "not_found": "A package, entry, resource, run, step, receipt, revision or consumer does not exist.",
    "conflict": "An expected SHA-256 did not match the current file; the current revision is returned.",
    "unavailable": "The entry exists but is not available to this selection (its package or a dependency is not enabled).",
    "revision_conflict": "A tool definition or code revision differs from the one a run recorded.",
    "operation_conflict": "An operation or invocation ID was reused with different content.",
    "state_conflict": "The requested transition is not allowed in the current run/step/receipt state.",
    "recovery_required": "An interrupted mutation must be recovered before further authoring changes.",
    "recovery_conflict": "Recovery found an affected file matching neither its recorded before nor after hash.",
    "runtime_unavailable": "No runtime is registered for the tool's runtime alias.",
    "verification_blocked": "A configured verifier could not produce a verdict; the step is not verified.",
    "store_not_initialized": "No initialized state exists at the given path; open never creates one.",
    "already_initialized": "The state path already holds an initialized store.",
    "invalid_root": "The authoring root is missing, not a dedicated Git working tree, or overlaps the state path.",
    "invalid_state_path": "The state path is unusable (inside the root, or a non-empty foreign directory).",
    "state_mismatch": "The state store is bound to a different authoring root or has an unsupported version.",
    "migration_required": "The state store requires an explicit offline schema migration.",
    "stale_revision": "The record revision has changed since the operation was prepared.",
    "invalid_transition": "The requested Overseer state transition is not allowed.",
    "owner_input_required": "An explicit owner response is required for this operation.",
    "evidence_unavailable": "The referenced evidence is unavailable.",
    "coverage_incomplete": "Evidence coverage is incomplete.",
    "active_tree_changed": "The active authoring tree changed during application.",
    "publication_recovery_required": "An interrupted publication needs recovery.",
    "git_unavailable": "The Git executable is missing or not runnable.",
    "git_failed": "A Git command failed; bounded stderr is included.",
    "git_identity_required": "A commit needs an author identity, which was neither supplied nor configured locally.",
    "nothing_to_commit": "The selected paths have no changes to commit.",
    "lock_timeout": "The authoring lock could not be acquired in time.",
    "limit_exceeded": "A size, count or output limit was exceeded.",
    "internal_error": "An unexpected failure; the exception type and message are included.",
}


class AgenticError(Exception):
    """Base error. ``code`` is one of :data:`ERROR_CODES`; ``details`` is JSON-able."""

    code = "internal_error"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.code!r}, {self.message!r})"


class InvalidRequestError(AgenticError):
    code = "invalid_request"


class ValidationFailedError(AgenticError):
    code = "validation_failed"


class UnsafePathError(AgenticError):
    code = "unsafe_path"


class NotFoundError(AgenticError):
    code = "not_found"


class ConflictError(AgenticError):
    code = "conflict"


class UnavailableError(AgenticError):
    code = "unavailable"


class RevisionConflictError(AgenticError):
    code = "revision_conflict"


class OperationConflictError(AgenticError):
    code = "operation_conflict"


class StateConflictError(AgenticError):
    code = "state_conflict"


class RecoveryRequiredError(AgenticError):
    code = "recovery_required"


class RecoveryConflictError(AgenticError):
    code = "recovery_conflict"


class RuntimeUnavailableError(AgenticError):
    code = "runtime_unavailable"


class VerificationBlockedError(AgenticError):
    code = "verification_blocked"


class StoreError(AgenticError):
    """Initialization/open problems; ``code`` is passed explicitly."""

    code = "store_not_initialized"


class GitError(AgenticError):
    code = "git_failed"
