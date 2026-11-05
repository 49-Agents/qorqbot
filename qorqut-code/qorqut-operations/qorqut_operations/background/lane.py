"""The automation lane: bounded, claim-based concurrent execution of ``automation.run`` jobs.

Every other program stays on the service's serial path (one installation-wide claim, one job at a time). An
automation job instead takes two non-blocking cross-process file claims, both stable per installation: its own
per-job claim (so no two executors, in this process or another, work the same job) and one of ``COMPUTE_SLOTS``
compute-slot claims (so at most three scripts compute at once across every service instance). Neither the serial
claim nor an SQL write transaction is held across the program's child or provider calls: each write is a short
unit under writer admission, and the computation between them runs unadmitted, so a recovery window can fence
writers and ``stop`` still settles the compute threads.

A run that is waiting holds nothing. Its saved ``automation.wait`` is reconciled by the program's ``poll`` in a
bounded scan that spawns no child and takes no slot; a pending answer advances the job's next eligible time, so the
scan reaches the other waiting runs in the same pass and a repeatedly deferred run cannot starve later ones. Jobs
that polled ready, or never waited, are handed to at most ``COMPUTE_SLOTS`` local compute threads.

Cancellation: an owner stop (``cancel_requested``) is never made terminal while the run's effects are unresolved. Under the
job's claim the lane asks the program's optional ``cancel`` callback to settle them from canonical records (no child, no
compute slot, no new mutation); while any survives the job stays ``cancel_requested`` with an advanced recheck time, and only
when none does is the stop finalized. The same path settles a ``cancelled`` raised mid-flight for a requested stop.

Ownership and shutdown: every state transition of a job -- compute, poll, deferral, cancellation -- happens under
that job's own claim. Every lane invocation (a dispatched compute thread, a tick poll, an inline ``run_one``) is
accounted from entry to exit, so ``close`` stops new entry, ``join`` waits for all of them within the stop bound, and
a later ``reopen`` (``start``) refuses while any survive.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from datetime import timedelta
from threading import Condition, Lock, Thread, current_thread, get_ident
import time
from uuid import UUID, uuid5

from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.storage.executor import application_claim
from qorqut_operations.storage.repositories import decode_row
from .programs import AUTOMATION_PROGRAM, ProgramPending, ProgramResult

COMPUTE_SLOTS = 3          # script processes computing at once, across service instances
SCAN_LIMIT = 25            # jobs examined per category in one tick: the bound on a wait scan
POLL_SECONDS = 30          # a poll is a small database/receipt check, never a long wait
SLOT_RETRY_SECONDS = 2     # a ready run that found every slot busy is offered again shortly, behind the others
TERMINAL = frozenset({"succeeded", "cancelled", "failed"})
# Whether the run saved a pending wait: ``null`` or an absent ``wait`` means none.
WAITING = "(COALESCE(json_type(result_json,'$.automation.wait'),'null')<>'null')"


class AutomationLane:
    def __init__(self, service):
        self.service = service
        self._guard = Lock()
        self._idle = Condition(self._guard)   # signalled whenever an invocation exits
        self._active = {}          # job id -> compute thread handed off by ``tick``
        self._calls = {}           # thread id -> lane invocations in flight on it (compute, poll, inline run_one)
        self._closed = False       # set by ``stop``: no invocation may begin until ``reopen``
        self._installation = None

    # -- claims ----------------------------------------------------------------------------------------------
    def _claim(self, label):
        """A non-blocking process-wide claim on a stable identity derived from the installation."""
        service = self.service
        if self._installation is None:
            with service.database.read() as uow:
                self._installation = UUID(service.authority.installation(uow))
        return application_claim(service.locks, str(uuid5(self._installation, label)))

    @contextmanager
    def _slot(self):
        with ExitStack() as stack:
            for index in range(COMPUTE_SLOTS):
                try:
                    stack.enter_context(self._claim(f"automation-compute-slot:{index}"))
                    break
                except DomainError as exc:
                    if exc.code != "busy":
                        raise
            else:
                raise DomainError("busy", "Every automation compute slot is in use", retryable=True)
            yield

    def _claimable(self, job_id):
        try:
            with self._claim("automation-job:" + job_id):
                return True
        except DomainError as exc:
            if exc.code == "busy":
                return False
            raise

    # -- reading ---------------------------------------------------------------------------------------------
    def _now(self):
        return self.service.clock().isoformat().replace('+00:00', 'Z')

    def _program(self):
        try:
            return self.service.programs.require(AUTOMATION_PROGRAM, available=False)
        except DomainError:
            return None

    def _load(self, job_id):
        with self.service.database.read() as uow:
            return decode_row(uow.connection.execute("SELECT * FROM jobs WHERE id=? AND kind=?",
                (job_id, AUTOMATION_PROGRAM)).fetchone())

    @staticmethod
    def _select(uow, where, parameters, limit):
        """Oldest-due first: a deferred job's advanced ``next_retry_at`` moves it behind every other due job."""
        return [decode_row(row) for row in uow.connection.execute(
            "SELECT * FROM jobs WHERE kind=? AND (" + where + ") "
            "ORDER BY COALESCE(next_retry_at,created_at), created_at, rowid LIMIT ?",
            (AUTOMATION_PROGRAM, *parameters, limit))]

    @staticmethod
    def _waiting(row):
        automation = (row["result_json"] or {}).get("automation")
        return isinstance(automation, Mapping) and automation.get("wait") is not None

    def _running(self):
        with self._guard:
            return set(self._active)

    def alive(self):
        with self._guard:
            return bool(self._calls) or any(thread.is_alive() for thread in self._active.values())

    def close(self):
        """Refuse every new invocation (``stop`` calls this once it has asked running programs to cancel)."""
        with self._idle:
            self._closed = True

    def reopen(self):
        """Admit invocations again (``start``). Refused while work from before the stop is still running."""
        with self._idle:
            if self._closed and (self._calls or any(thread.is_alive() for thread in self._active.values())):
                raise RuntimeError("Automation work from before the last stop has not settled")
            self._closed = False

    def _enter(self):
        with self._idle:
            if self._closed:
                return False
            ident = get_ident()
            self._calls[ident] = self._calls.get(ident, 0) + 1
            return True

    def _exit(self):
        with self._idle:
            ident = get_ident()
            left = self._calls[ident] - 1
            if left:
                self._calls[ident] = left
            else:
                del self._calls[ident]
            self._idle.notify_all()

    def join(self, timeout):
        """Wait, within ``timeout``, for every lane invocation to exit and for the dispatched threads to finish. An
        invocation on the calling thread itself is not waited for (a thread cannot wait for itself)."""
        deadline = time.monotonic() + timeout
        me = get_ident()
        with self._idle:
            while any(ident != me for ident in self._calls):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Automation compute did not finish its bounded cancellation")
                self._idle.wait(remaining)
            threads = [thread for thread in self._active.values() if thread is not current_thread()]
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise TimeoutError("Automation compute did not finish its bounded cancellation")

    # -- the background pass -----------------------------------------------------------------------------------
    def tick(self):
        """One bounded pass: finalize stopped runs and resume frozen outputs, reconcile waiting runs, then hand ready
        runs to compute threads. The first three never wait for a slot, so they proceed while every slot is busy."""
        service = self.service
        program = self._program()
        if program is None or service._stop.is_set():
            return
        now, due = self._now(), "(next_retry_at IS NULL OR next_retry_at<=?)"
        polling = program.poll is not None
        with service.database.read() as uow:
            closing = self._select(uow, f"state='cancel_requested' AND {due}", (now,), SCAN_LIMIT)
            applying = self._select(uow, f"state IN ('applying','waiting_signoff') AND {due}", (now,), SCAN_LIMIT)
            waiting = self._select(uow, f"state IN ('queued','running') AND {WAITING} AND {due}", (now,), SCAN_LIMIT) if polling else []
            ready = self._select(uow, f"state IN ('queued','running') AND {'NOT ' + WAITING if polling else '1'} AND {due}",
                                 (now,), SCAN_LIMIT)
        running = self._running()
        for row in (*closing, *applying):
            if service._stop.is_set():
                return
            if row["id"] not in running:
                self._guarded(row["id"], poll=False, compute=False)
        polled = []
        for row in waiting:
            if service._stop.is_set():
                return
            if row["id"] not in running:
                outcome = self._guarded(row["id"], poll=True, compute=False)
                if outcome and outcome.get("state") == "ready":
                    polled.append(row["id"])
        fresh = [row["id"] for row in ready if row["id"] not in running]
        for job_id in (*polled, *fresh):
            if service._stop.is_set():
                return
            if not self._claimable(job_id) or self._dispatch(job_id):
                continue
            if job_id in polled:
                self._rotate(job_id)          # no local slot: rotate behind the other waiting runs
            else:
                break                         # no local slot: the rest of the ready runs keep their order

    def _guarded(self, job_id, *, poll, compute):
        """One job's fault is saved on that job and must not end the scan of the others."""
        try:
            return self.run(job_id, poll=poll, compute=compute)
        except Exception as exc:
            self.service.last_error = type(exc).__name__
            return None

    def _rotate(self, job_id):
        """Offer a ready run again shortly, behind the other waiting runs. Like every transition this takes the job's
        own claim, re-reads under it and only moves a run that is still unclaimed, waiting or ready, and due: a run
        another executor picked up in the meantime keeps its claim marks and its state."""
        if not self._enter():
            return
        try:
            with ExitStack() as stack:
                try:
                    stack.enter_context(self._claim("automation-job:" + job_id))
                except DomainError as exc:
                    if exc.code == "busy":
                        return
                    raise
                row = self._load(job_id)
                if (row is None or row["state"] not in {"queued", "running"} or "outputs" in (row["result_json"] or {})
                        or (row["next_retry_at"] or "") > self._now() or job_id in self._running()):
                    return
                self._defer(job_id, SLOT_RETRY_SECONDS)
        except Exception as exc:
            if not (isinstance(exc, DomainError) and exc.code == "maintenance"):
                self.service.last_error = type(exc).__name__
        finally:
            self._exit()

    def _dispatch(self, job_id):
        with self._guard:
            if self._closed or job_id in self._active or len(self._active) >= COMPUTE_SLOTS:
                return False
            thread = Thread(target=self._compute, args=(job_id,), name="operations-automation", daemon=True)
            self._active[job_id] = thread
            try:
                thread.start()
            except BaseException:
                del self._active[job_id]
                raise
        return True

    def _compute(self, job_id):
        service, moved = self.service, False
        try:
            moved = self.run(job_id, poll=False, compute=True) is not None
        except Exception as exc:
            service.last_error = type(exc).__name__
        finally:
            with self._guard:
                self._active.pop(job_id, None)
        if moved:
            service.kick()   # a slot is free again, and the run's successor may be ready

    # -- explicit execution ------------------------------------------------------------------------------------
    def run_one(self, job_id=None):
        """Execute in the caller's thread under the same claims and slot bound; ``None`` when nothing can proceed."""
        if job_id is not None:
            return self.run(job_id, poll=True, compute=True)
        if self._program() is None:
            return None
        with self.service.database.read() as uow:
            candidates = self._select(uow, "state IN ('cancel_requested','queued','running','applying','waiting_signoff')"
                                      " AND (next_retry_at IS NULL OR next_retry_at<=?)", (self._now(),), SCAN_LIMIT)
        running = self._running()
        for row in candidates:
            if row["id"] not in running:
                outcome = self.run(row["id"], poll=True, compute=True)
                if outcome is not None:
                    return outcome
        return None

    def run(self, job_id, *, poll, compute):
        """Take the job's claim and advance it once: ``poll`` reconciles a saved wait, ``compute`` may then run the
        program. Returns the serial path's result shapes, ``{"state": "ready"}`` for a job a non-computing caller
        found ready, or ``None`` when the job is claimed elsewhere, finished, has no free slot or the fence is closed."""
        service = self.service
        if not self._enter():
            return None   # stopped: nothing new may begin until ``start``
        try:
            with ExitStack() as stack:
                try:
                    stack.enter_context(self._claim("automation-job:" + job_id))
                except DomainError as exc:
                    if exc.code == "busy":
                        return None
                    raise
                with service.database.observe("jobs_run"):
                    return self._run_claimed(job_id, poll=poll, compute=compute)
        except DomainError as exc:
            if exc.code == "maintenance":
                return None
            raise
        finally:
            self._exit()   # after the claims above are released

    def _run_claimed(self, job_id, *, poll, compute):
        service, row = self.service, None
        try:
            row = self._load(job_id)
            if row is None or row["state"] in TERMINAL:
                return None
            if row["state"] == "cancel_requested":
                return self._cancel(row)   # settles outstanding effects first; never computes, never takes a slot
            program = service.programs.require(row["kind"])
            frozen = "outputs" in (row["result_json"] or {})
            control = None
            if poll and not frozen and program.poll is not None and self._waiting(row):
                with service.database.read() as uow:
                    service.authority.require_worker(row["principal_id"], row["business_id"], uow)
                if row["definition_ref_json"].get("digest") != program.definition["digest"]:
                    raise DomainError("policy_denied", "Registered program definition changed before dispatch")
                control = service.control(row, POLL_SECONDS)
                outcome = program.poll(control, plain(row["arguments_json"]))
                if isinstance(outcome, ProgramPending):
                    self._defer(row["id"], outcome.retry_after)
                    return {"job_id": row["id"], "state": "queued"}
                if isinstance(outcome, ProgramResult):
                    # Reconciliation completed the run: freeze and apply its outputs without any child.
                    with service._admitted():
                        row = service._freeze(row, outcome)
                    return self._complete(row, control)
                if outcome is not None:
                    raise DomainError("invalid_result", "A poll returns pending, a result or nothing")
            if not frozen and not compute:
                return {"job_id": row["id"], "state": "ready"}
            slot = ExitStack()
            try:
                if not frozen:
                    try:
                        slot.enter_context(self._slot())
                    except DomainError as exc:
                        if exc.code != "busy":
                            raise
                        self._defer(row["id"], SLOT_RETRY_SECONDS)
                        return None
                with service._admitted():
                    with service.database.transaction() as uow:
                        service.app.ensure_writes_open(uow)
                        row = decode_row(uow.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
                        if not row or row["state"] in TERMINAL:
                            return None
                        program, row, timeout, frozen = service._begin(row, uow)
                control = service.control(row, timeout)
                if not frozen:
                    control.check()
                    step = program.recover if program.external and row["attempts"] > 1 else program.run
                    result = step(control, plain(row["arguments_json"]))
                    if isinstance(result, ProgramPending):
                        self._defer(row["id"], result.retry_after)
                        return {"job_id": row["id"], "state": "queued"}
                    with service._admitted():
                        row = service._freeze(row, result)
            finally:
                slot.close()   # computation is over: the slot is free while outputs are applied
            return self._complete(row, control)
        except DomainError as exc:
            return self._settle(exc, row, job_id)
        except Exception:
            if row:
                with service._admitted():
                    service._fail(row["id"], DomainError("background_failed", "Registered background program failed"))
            service._observe(service._committed(row), {"code": "background_failed"})
            raise

    def _cancel(self, row):
        """Complete an owner stop under the job's claim. The program's ``cancel`` callback settles the run's unresolved
        effects from canonical records alone; while it reports any (``ProgramPending``) the job stays ``cancel_requested``
        and is asked again after the callback's ``retry_after``. Only when it reports none (or the program has no callback)
        is the stop made terminal. The job is never computed, so no compute slot, child or new call is involved, and a
        callback fault leaves the run requested rather than finishing it with an effect unaccounted for."""
        service, job_id = self.service, row["id"]
        program = self._program()
        cancel = getattr(program, "cancel", None)
        if cancel is not None:
            try:
                outcome = cancel(service.control(row, POLL_SECONDS), plain(row["arguments_json"]))
            except DomainError as exc:
                if exc.code in {"busy", "maintenance", "service_stopping"}:
                    return self._settle(exc, row, job_id)
                return self._unsettled(row, exc.to_dict(), exc.code)
            except Exception as exc:
                return self._unsettled(row, {"code": "background_failed"}, type(exc).__name__)
            if isinstance(outcome, ProgramPending):
                self._defer(job_id, outcome.retry_after)
                return {"job_id": job_id, "state": "cancel_requested"}
            if outcome is not None:
                return self._unsettled(row, DomainError("invalid_result", "A cancel callback returns pending or nothing").to_dict(),
                                       "invalid_result")
        return self._settle(DomainError("cancelled", "Owner stopped this routine"), row, job_id, final=True)

    def _unsettled(self, row, error, label):
        """A cancel callback that failed or returned something invalid has not shown the run safe to finish: it stays
        requested and is asked again later, and the fault is recorded for the service."""
        self.service.last_error = label
        self._defer(row["id"], POLL_SECONDS)
        return {"job_id": row["id"], "error": error}

    def _complete(self, row, control):
        service = self.service
        with service._admitted():
            done = service._apply_outputs(row, control)
        if done:
            committed = service._committed(row)
            service._observe(committed, (committed["result_json"] or {}).get("completion_error") if committed else None)
        return {"job_id": row["id"], "state": ("failed" if row["result_json"].get("completion_error") else "succeeded")
                if done else "waiting_signoff"}

    def _defer(self, job_id, seconds):
        """Release the job's claim marks and make it eligible again after ``seconds`` -- unless it is finished. An owner
        stop requested meanwhile stays requested (never reverted to queued): only its recheck time advances, so the
        closing scan neither spins on it nor lets the stop be lost."""
        service = self.service
        with service._admitted(), service.database.transaction() as uow:
            row = decode_row(uow.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if row is None or row["state"] in TERMINAL:
                return
            values = {"claimed_by": None, "lease_until": None,
                      "next_retry_at": (service.clock() + timedelta(seconds=max(1, min(seconds, 3600)))).isoformat().replace('+00:00', 'Z')}
            if row["state"] != "cancel_requested":
                values["state"] = "applying" if "outputs" in (row["result_json"] or {}) else "queued"
            service._update(row, values, uow)

    def _requested(self, job_id):
        row = self._load(job_id)
        return row is not None and row["state"] == "cancel_requested"

    def _settle(self, exc, row, job_id, *, final=False):
        """The serial path's outcome mapping. ``busy`` and a closed maintenance fence leave the job to be recovered
        shortly, ``service_stopping`` pauses it for restart, and anything else fails it. A ``cancelled`` seen for a job
        whose owner stop is requested is not yet terminal: it is settled through ``_cancel`` first (``final`` is that
        path's own, already-settled completion)."""
        service = self.service
        if exc.code == "cancelled" and not final and row and self._requested(row["id"]):
            return self._cancel(row)
        if exc.code in {"busy", "service_stopping"}:
            service._observe(row, exc.to_dict())
        try:
            if exc.code in {"busy", "maintenance"}:
                if row:
                    self._defer(row["id"], 5)
                return None
            if exc.code == "service_stopping" and row:
                self._defer(row["id"], 1)
                return {"job_id": row["id"], "state": "paused_for_restart"}
            if row:
                with service._admitted():
                    service._fail(row["id"], exc)
        except DomainError as inner:
            if inner.code != "maintenance":
                raise
            return None  # the fence is closed: the claim marks stay and the next pass recovers the job
        service._observe(service._committed(row), exc.to_dict())
        service.last_error = exc.code
        return {"job_id": row["id"] if row else job_id, "error": exc.to_dict()}
