# Bounded Dispatcher admission — D49

Candidate branch `codex/fix-dispatch-admission-r1`, based on
`ac607b80a40cc90ab57c6f9636bfd9e9e579accd`.

Both application calls and native hooks share admission: four executing calls,
up to twelve event-loop waiters, and a one-second waiting deadline. Overflow,
timeout and closing refuse before executor submission; application refusals are
retryable. There is no replay. Post-invocation exceptions retain the existing
possibly-executed result. Waiting cancellation withdraws the call; running
cancellation retains the slot until its original future completes. Closing
wakes/refuses waiters and preserves the existing cancellation-safe executor drain.

Five focused source tests passed in **2.226s** using Python 3.12.3 and all 30
locked runtime distributions. From the candidate, with `PYTHONPATH=src:tests`,
`python -m unittest -v` ran these methods of
`transports.test_transports.AdmissionAndLifetime`:

- `test_short_burst_waits_then_executes_each_invocation_and_hook_once`
- `test_waiting_overflow_timeout_and_cancellation_never_execute`
- `test_cancelled_waiter_retains_slot_and_shutdown_drains_running_calls`
- `test_cancelled_startup_cannot_lose_application_ownership`
- `test_readiness_failure_is_minimal_and_lifespan_releases_lock`

The burst proves four executor submissions while twelve mixed calls wait, then
one execution per call. The refusal case covers both entry points, cancelled
waiter replacement, timeout, and continued admission afterward. The shutdown
case cancels running callers of both kinds, refuses waiting/new calls, and drains
before propagating close cancellation. The error case preserves nonretryable
uncertainty without retry. `git diff --check` passed.

Private commands, logs, environment details and failed attempts are retained at
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/fix-dispatch-admission-r1-validation/`.
The initial test run had two fixture errors: a synthetic successful
`OperationResult` omitted required `outcome`, preventing the test gate from being
entered. Adding `outcome="completed"` resolved them. Setup checks also recorded
two nonexistent read/search paths, missing build-only packages, a shared
`pycparser` version mismatch, and absent system pip. A task-owned venv reused
the accepted dependency environment and overrode only `pycparser` to locked 3.0;
the shared environment was unchanged. No build/package matrix was run.

These are Dispatcher boundary checks with synthetic native hooks, plus existing
temporary-installation startup/error checks. They are not an integrated native
CLI/backend or combined UI/backlog test. Manager owns combined acceptance.
Cold-start, echo-association, final-drain, browser/VM and full-system limitations
remain open. No real provider calls, owner stores, live services, merge or
deployment were used.
