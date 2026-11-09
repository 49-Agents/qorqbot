"""The coordinator that captures a nightly job's generation under the installation's actual recovery barrier.

``NightlyMaintenance.run`` is a background job: while it runs, the background writer is not quiescent, so it
cannot hold the recovery barrier. It records a bounded capture request in its private maintenance receipt and
yields. This service is a separate writer-free thread of the host runtime: it finds such requests, validates
each against the persisted job, registered program, worker authority and the exact saved configuration, holds
the job's application claim, enters ``runtime.recovery_quiescence()`` from its own thread and calls
``BackupStore.create`` with the yielded proof for the same job and generation. The verified result, or the
exact failure, is saved in the same receipt; the job's next background invocation verifies the sealed generation
and continues. The capture never reuses a ``JobControl`` of the background service (its stop flag is exactly
what the quiescence drain sets), keeps the request's single deadline, and is never repeated for a generation
that is already sealed. Constructing the service starts nothing.
"""

from __future__ import annotations

import re
import threading

from qorqut_operations.contracts import DomainError
from qorqut_operations.storage.executor import application_claim

PROGRAM = "maintenance.nightly"
ACTIVE = ("requested", "capturing")
ELIGIBLE_JOB_STATES = ("queued", "running")
RECEIPT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json")
MAX_RECEIPTS = 10000
REQUEST_KEYS = frozenset({"schema_version", "job_id", "kind", "installation", "definition_digest", "principal_id",
                          "arguments", "requested_at", "deadline_at"})


class _Run:
    """One start of the service: its own stop and wake signals, never reused by a later start."""

    def __init__(self):
        self.stop, self.wake, self.thread = threading.Event(), threading.Event(), None


class MaintenanceCaptureService:
    def __init__(self, maintenance, runtime, *, interval=2.0, quiesce_timeout=30):
        if type(interval) not in (int, float) or interval <= 0 or type(quiesce_timeout) is not int or not 1 <= quiesce_timeout <= 600:
            raise ValueError("The capture service needs a positive poll interval and a bounded quiescence timeout")
        self.maintenance, self.runtime = maintenance, runtime
        self.interval, self.quiesce_timeout = interval, quiesce_timeout
        self.last_error = None
        self._run, self._lock = None, threading.Lock()
        self._busy = threading.Event()

    # -- lifecycle -----------------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        run = self._run
        return bool(run and run.thread and run.thread.is_alive() and not run.stop.is_set())

    def start(self):
        """Start the coordinator thread; a stopped service starts afresh with new signals."""
        with self._lock:
            run = self._run
            if run and run.thread and run.thread.is_alive():
                if not run.stop.is_set():
                    return self
                raise DomainError("busy", "The previous maintenance capture is still stopping", retryable=True)
            run = _Run()
            run.thread = threading.Thread(target=self._work, args=(run,), name="maintenance-capture", daemon=True)
            self._run = run
            run.thread.start()
        return self

    def stop(self, timeout=10):
        """Ask the coordinator to stop and wait up to ``timeout``; reports whether it did and whether it is mid-capture.

        Called from the coordinator's own thread (a quiescence drain runs there while a capture is in flight)
        it cannot join itself and must not abort the capture it is performing: it reports the capture in flight.
        """
        with self._lock:
            run = self._run
        if run is None or run.thread is None or not run.thread.is_alive():
            return {"stopped": True, "in_flight": False}
        if run.thread is threading.current_thread():
            return {"stopped": False, "in_flight": True}
        run.stop.set()
        run.wake.set()
        run.thread.join(timeout)
        alive = run.thread.is_alive()
        return {"stopped": not alive, "in_flight": alive and self._busy.is_set()}

    def wake(self):
        run = self._run
        if run is not None:
            run.wake.set()

    def _work(self, run):
        while not run.stop.is_set():
            try:
                progressed = self.poll_once(run)
                self.last_error = None
            except Exception as exc:
                self.last_error, progressed = type(exc).__name__, False
            if not progressed:
                run.wake.wait(self.interval)
                run.wake.clear()

    # -- requests ------------------------------------------------------------------------------------------
    def _pending(self):
        directory = self.maintenance.receipt_directory()
        found = []
        for index, path in enumerate(sorted(directory.iterdir())):
            if index >= MAX_RECEIPTS:
                break
            if not RECEIPT.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            try:
                state = self.maintenance.read_receipt(path.stem)
            except (OSError, ValueError, DomainError):
                continue
            capture = (state or {}).get("capture")
            if isinstance(capture, dict) and capture.get("state") in ACTIVE and isinstance(capture.get("request"), dict):
                found.append((capture["request"].get("requested_at", 0), path.stem))
        return [generation for _, generation in sorted(found)]

    def poll_once(self, run=None) -> bool:
        """Capture at most one requested generation; ``True`` when a request was resolved (sealed or failed)."""
        run = run or self._run or _Run()
        for generation in self._pending():
            if run.stop.is_set():
                return False
            try:
                if self._capture(generation, run):
                    return True
            except Exception as exc:
                self.last_error = type(exc).__name__  # one unreadable request never blocks the others
        return False

    # -- one capture ---------------------------------------------------------------------------------------
    def _check_request(self, state, request, generation):
        """Raise unless the persisted request still names an eligible job under the exact saved configuration."""
        if (not isinstance(request, dict) or set(request) != REQUEST_KEYS or request["schema_version"] != 1
                or any(not isinstance(request[key], str) for key in ("job_id", "kind", "installation", "definition_digest", "principal_id"))
                or any(type(request[key]) not in (int, float) for key in ("requested_at", "deadline_at"))):
            raise DomainError("invalid_plan", "The capture request is not a current bounded request")
        background = self.runtime.background
        if background is None:
            raise DomainError("operation_unavailable", "The background service is not attached")
        if state["configuration"] != self.maintenance.configuration():
            raise DomainError("configuration_changed", "Resume this nightly job with its original configuration; new settings cannot expand its effects")
        if request.get("job_id") != generation or request.get("kind") != PROGRAM or request.get("arguments") != {}:
            raise DomainError("invalid_plan", "The capture request does not name this nightly job")
        if self.maintenance.wall_clock() >= request["deadline_at"]:
            raise DomainError("timeout", "The nightly backup capture did not complete within its time limit")
        with background.database.read() as uow:
            row = uow.repository("jobs", business_id=None).get(generation)
            if row is None:
                raise DomainError("not_found", "The nightly job is gone")
            if row["state"] == "cancel_requested":
                raise DomainError("cancelled", "Owner stopped this routine")
            program = background.programs.require(PROGRAM)
            if (row["kind"] != PROGRAM or row["business_id"] is not None or row["arguments_json"] != {}
                    or row["state"] not in ELIGIBLE_JOB_STATES):
                raise DomainError("policy_denied", "The nightly job is not eligible for capture")
            if not (row["definition_ref_json"].get("digest") == program.definition["digest"] == request["definition_digest"]
                    and row["principal_id"] == request["principal_id"]
                    and background.authority.installation(uow) == request["installation"]):
                raise DomainError("policy_denied", "Registered program definition or worker changed since the request")
            background.authority.require_worker(row["principal_id"], row["business_id"], uow)

    def _check(self, run, state, request, generation):
        """The per-capture ``check`` for ``BackupStore.create``: stop, deadline, cancellation and eligibility."""
        def check():
            if run.stop.is_set():
                raise DomainError("service_stopping", "The maintenance capture service is stopping")
            self._check_request(state, request, generation)
        return check

    def _capture(self, generation, run) -> bool:
        try:
            with application_claim(self.maintenance.locks, generation):
                return self._claimed(generation, run)
        except DomainError as error:
            if error.code == "busy":
                return False  # the job's own invocation holds the claim; look again shortly
            raise

    def _claimed(self, generation, run) -> bool:
        state = self.maintenance.read_receipt(generation)
        capture = (state or {}).get("capture")
        if not isinstance(capture, dict) or capture.get("state") not in ACTIVE:
            return False
        request = capture["request"]

        def save(**values):
            capture.update(values)
            self.maintenance.write_receipt(generation, state)

        sealed = self.maintenance.backups.destination / generation
        if sealed.exists():
            # Sealed before the result was recorded (a restart between the two): verify, never capture again.
            try:
                metadata = self.maintenance.backups.verify(generation)
            except DomainError as error:
                save(state="failed", error=error.to_dict())
                return True
            save(state="complete", result=self._result(metadata))
            return True
        try:
            self._check_request(state, request, generation)
        except DomainError as error:
            save(state="failed", error=error.to_dict())
            return True
        save(state="capturing", attempts=capture.get("attempts", 0) + 1)
        metadata, leave = None, None
        try:
            remaining = max(1, int(request["deadline_at"] - self.maintenance.wall_clock()))
            with self.runtime.recovery_quiescence(timeout=min(self.quiesce_timeout, remaining)) as barrier:
                self._busy.set()
                try:
                    check = self._check(run, state, request, generation)
                    check()
                    metadata = self.maintenance.backups.create(generation, barrier=barrier, check=check,
                                                               timeout=min(60, remaining))
                finally:
                    self._busy.clear()
        except DomainError as error:
            if metadata is None:
                if error.code == "service_stopping":
                    save(state="requested")  # not a failure: the next start captures the same generation
                    return False
                save(state="failed", error=error.to_dict())
                return True
            leave = error
        except Exception as error:
            if metadata is None:
                failure = DomainError("backup_incomplete", "The nightly capture failed unexpectedly",
                                      details={"exception": type(error).__name__})
                save(state="failed", error=failure.to_dict())
                return True
            leave = error
        result = self._result(metadata)
        if leave is not None:
            # The generation is sealed and verified; only leaving the quiescence failed. Keep both facts visible.
            result["quiescence_exit_error"] = leave.to_dict() if isinstance(leave, DomainError) else {"code": "background_failed"}
        save(state="complete", result=result)
        if leave is not None:
            raise leave
        return True

    @staticmethod
    def _result(metadata):
        return {"checksum": metadata["database"]["checksum"], "context": metadata["context"]["manifest_sha256"]}
