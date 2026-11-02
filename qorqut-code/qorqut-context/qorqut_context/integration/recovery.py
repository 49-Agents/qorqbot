"""The required Context recovery component: the shared Context repository and runtime as one capture.

``qorqut_context.recovery`` owns capture, verification and relocated restore of the Context pair. This module
binds it to an instance: the repository, runtime and operations root are explicit values the host supplies (from its
``InstancePaths`` through ``ContextLocations``), the writer barrier is the actual
Operations ``RecoveryBarrierProof`` (never inferred from a gate, claim or lock observation, and never a
method-shaped substitute; the library itself stays structural), library failures become the
host's stable ``DomainError`` codes, and a rehearsal restore records an explicit incomplete marker until the
restored pair has passed the library's final ownership and event-integrity checks.
"""

from __future__ import annotations

import os
from pathlib import Path

from qorqut_context import recovery as component
from qorqut_operations.contracts import DomainError, canonical_bytes
from qorqut_operations.storage.quiescence import RecoveryBarrierProof
from qorqut_operations.storage.safe_io import atomic_write

from qorqut_context.integration.driver import LOCK_NAME as QUEUE_DRIVER_LOCK

CAPTURE_DIRECTORY = "context"
# The host's own advisory lock files directly in the Context runtime: the queue driver's. Each is empty and is held
# by the capture while it exists, so a driver that is still processing a queue item refuses the capture.
RUNTIME_LOCKS = (QUEUE_DRIVER_LOCK,)
INCOMPLETE_MARKER = ".runtime/restore-incomplete.json"
SUMMARY_KEYS = ("component", "format", "format_version", "manifest_sha256", "captured_at", "source_repository",
                "source_runtime", "published_commit", "events", "pending_event", "closure_sha256", "closure_objects",
                "runtime_files", "complete")


class ProofBarrier:
    """The library's structural ``WriterBarrier`` over the one actual ``RecoveryBarrierProof`` and the operational database.

    Only the exact Operations proof type, yielded by a ``RecoveryQuiescer`` window, is accepted: a method-shaped
    substitute, a subclass or anything else is refused at construction. Every check asks the proof itself whether
    its window is still the active one in this thread and that it froze this very database; an ended or foreign
    proof raises its own ``recovery_not_quiescent``, with no fallback inference.
    """

    def __init__(self, proof, database):
        if type(proof) is not RecoveryBarrierProof:
            raise DomainError("invalid_configuration",
                              "Recovery capture requires the active recovery barrier proof of its quiescence")
        self.proof, self.database = proof, database

    def verify_active(self):
        self.proof.verify_active()
        self.proof.require_database(self.database)
        return {"kind": "operations-recovery-barrier", "database": str(self.database.path)}


def _translate(error, phase):
    """A stable host error for a library refusal; a refusal by the proof itself is passed through unchanged."""
    cause = error.__cause__
    if isinstance(cause, DomainError):
        return cause
    code = {"capture": "backup_incomplete", "verify": "invalid_backup", "restore": "invalid_restore"}[phase]
    if error.code == "invalid_capture":
        code = "invalid_backup"
    elif phase == "restore" and error.code == "restore_incomplete":
        code = "restore_incomplete"
    return DomainError(code, f"Context recovery {phase} refused: {error}", retryable=bool(error.retryable),
                       details={"context_code": error.code, **error.details})


class ContextRecovery:
    """Capture, verify and restore the instance's Context pair at the repository and runtime the host names, both beneath
    ``operations_root`` (restored pairs keep their position relative to it)."""

    def __init__(self, repository, runtime, operations_root):
        self.repository, self.runtime, self.operations_root = Path(repository), Path(runtime), Path(operations_root)

    def barrier(self, proof, database):
        return ProofBarrier(proof, database)

    def capture(self, destination, barrier) -> dict:
        """Capture into the new ``destination`` under a ``ProofBarrier``; returns the recorded summary."""
        try:
            return component.capture(self.repository, self.runtime, destination, barrier=barrier,
                                     runtime_locks=RUNTIME_LOCKS)
        except component.RecoveryError as error:
            raise _translate(error, "capture") from error

    def verify(self, directory, recorded) -> dict:
        """Verify ``directory`` against the recorded summary and this instance's Context identity."""
        directory = Path(directory)
        try:
            if directory.is_symlink() or not isinstance(recorded, dict) or set(recorded) != set(SUMMARY_KEYS):
                raise component.RecoveryError("invalid_capture", "The recorded Context component is not a current summary")
            verified = component.verify(directory, expected_manifest_sha256=recorded["manifest_sha256"])
        except component.RecoveryError as error:
            raise _translate(error, "verify") from error
        if (verified != recorded or recorded["source_repository"] != str(self.repository)
                or recorded["source_runtime"] != str(self.runtime)):
            raise DomainError("invalid_backup", "The captured Context does not match the generation's recorded Context")
        return verified

    def locations(self, root) -> tuple[Path, Path]:
        """The Context repository and runtime of a restored instance rooted at ``root``."""
        root = Path(root)
        return (root / self.repository.relative_to(self.operations_root),
                root / self.runtime.relative_to(self.operations_root))

    def mark_incomplete(self, root, generation_id) -> Path:
        """Record, in a staged restore root, that its Context has not yet been restored and verified."""
        marker = Path(root) / INCOMPLETE_MARKER
        atomic_write(marker, canonical_bytes({"schema_version": 1, "generation_id": generation_id,
                                              "pending": "context"}) + b"\n")
        return marker

    def restore(self, directory, recorded, root, marker=None) -> dict:
        """Restore the verified capture to the Context locations beneath ``root`` and clear ``marker``.

        ``root`` is the final restored operations root: the library rebinds the two ownership markers to the
        paths it is given, so the pair is restored where it will live, never in a staging directory. Nothing
        is started, repaired or replayed; a failure leaves the marker and the library's phase journal.
        """
        self.verify(directory, recorded)
        repository, runtime = self.locations(root)
        try:
            report = component.restore(Path(directory), target_repository=repository, target_runtime=runtime,
                                       expected_manifest_sha256=recorded["manifest_sha256"])
        except component.RecoveryError as error:
            raise _translate(error, "restore") from error
        if marker is not None:
            Path(marker).unlink()
            descriptor = os.open(Path(marker).parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return {key: report[key] for key in ("status", "published_commit", "manifest_sha256", "pending_event")
                if key in report} | {"repository": str(repository), "runtime": str(runtime)}
