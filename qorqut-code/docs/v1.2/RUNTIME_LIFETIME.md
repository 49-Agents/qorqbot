# Runtime lifetime, write admission and the recovery barrier (lane A, branch `v1.2/runtime-composition`)

Current contract of `bazarlyk/service/{ownership,lifetime,stopping,runtime}.py` over the Operations `storage/{admission,quiescence}.py`.
Evidence for it comes from source-import checks over disposable instances with inert recorders for the services; nothing here
was run as an installed combined application.

## Ownership and admission

- One OS-backed exclusive lock, `operations/.runtime/instance-lifetime.lock` (independent of `locks/recovery.lock`, never
  unlinked), is the backend's lifetime ownership. It is taken first by `Runtime.start` and by the CLI preparation
  (`Runtime.owned_preparation`, no service started) and released only after a proven clean stop (or the preparation).
- `build_runtime` assembles `RecoveryAdmission(serving=False)`: every Application mutation is refused (`maintenance`) from
  assembly. The owned path is ownership, then target validation (`validate_target`), then `admission.admit()`, then prepared
  effect and native-input recovery, then the services. A failed start calls `admission.withhold()` before unwinding, so
  receipts already admitted finish and no new mutation is accepted.
- Starting a running runtime verifies the held ownership and does nothing else. A stopping, stopped or incomplete lifetime
  refuses to start; a stopped lifetime is single-use.
- Admission has four independent refusals: start (`withhold`/`admit`), recovery (`refuse_new`/`close_and_wait`/`open`),
  shutdown (`begin_shutdown`, persistent; `open` and `admit` cannot lift it) and the final `seal`. Admitted work and nested
  receipt writes are never blocked by any of them.

## Unwind and stop

- Recovery orchestrators (`recovery_admin`, `maintenance_capture`; steps with `recovery="caller"`) are stopped first, both
  asked, in a failed-start unwind and in `stop`. An orchestrator that does not prove its stop keeps every other service,
  write admission and SQLite where they are (`ShutdownReport.held`, `LifetimeState.incomplete`): its capture or thaw owns
  them. Only after every orchestrator is proved are the other services drained, admission sealed and SQLite closed.
- Shutdown begins with `begin_shutdown()`: a recovery window thawing while the stop waits cannot reopen mutations.
- Every stop is judged by typed evidence (`service/stopping.py`): `void`, `context_loop`, `capture_service`
  (`{stopped, in_flight}`) and `history_observer`; a stop that only returned is not a drain.
- `runtime.maintenance_capture` (lane C's service, bound at final integration) is optional and absent in this branch.

## The recovery window

`Runtime.recovery_quiescence(**kwargs)` is a context manager judged at entry: one window at a time (a concurrent or
recursive attempt is refused before the inventory is synchronized, anything is refused, drained or frozen), instance ownership
verified at entry, the writer inventory re-derived from the composition, then `RecoveryQuiescer.quiesce`. It yields the
Operations `RecoveryBarrierProof` (issued only after every drain/freeze proved, the boundary is closed, claims are clear and
the installation lock is held; bound to the issuing window, process, thread and the frozen database file; revoked before any
thaw, resume or unlock). No window starts once shutdown has begun. Required writer classes without a controller (Context,
history) make the barrier refuse.

## Snooze reconciliation

The nudge backend's reconcile hook takes `runtime.application.writer_admission.writer()` before its SQLite transaction, so a
pass begun before refusal settles and one begun afterwards writes nothing.
