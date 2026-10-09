# D54 native exit checkpoints: implementation evidence

2026-10-09. Branch `codex/build-native-exit-checkpoints-r1`, based on
`693c18b239e390f2064153d615e0a113506c91ae`. Implements the frozen
[D54 correction](../NATIVE-EXIT-CHECKPOINTS.md); Manager review remains separate.

`SessionMonitor` performs one final reconciliation and bounded collector tick
on its own thread after an orderly stop settles the current iteration. It skips
native setup. An unexpected iteration failure skips this final pass and remains
unhealthy. Final-pass and cleanup exceptions use fixed diagnostics; both services
are always offered cleanup. No native process is launched, signalled or released.

`NativeCollector` records the existing durable prefix checkpoint after bounded
poll/transfer work for admitted, verified exited owners. It checks current owners
before and after checkpoint work, including unlinked resumes and retired owners
of the same agent/provider/native UUID. Unchanged successful proofs are reused
across ticks/reopen only after the latest relevant closure. File identity, size
and modification metadata must still match; normal prefix/ownership verification
remains in the capture primitive. Saved evidence never grants input authority.

EOF is evaluated separately from recording a finite checkpoint: proof offset,
file size, local capture and local/host archive watermarks must agree, with no
raw error/gap or ownership/recovery/conflict hold. A fully scanned but unhandled
suffix exposes `gap/parser_gap` and the existing pending byte count. It creates
no parser-gap row, entry or cursor advance. Append/resume reevaluates the condition.
Sources remain resumable; NativeCapture, prefix/spool/archive, parser semantics,
schemas, server cleanup order and configuration are unchanged.

## Focused checks

Private logs are under
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-native-exit-checkpoints-r1`.
Commands used `PYTHONPATH=src:tests`, `PYTHONDONTWRITEBYTECODE=1` and that directory
as `TMPDIR`, without installing dependencies. From this worktree, the final two
grouped checks used the existing `console-test-venv/bin/python` in the parent
state directory with `-m unittest -v` and these exact selectors:

- `runtime.test_native_collector.NativeCollection.test_exited_checkpoints_are_finite_reusable_and_resumable`
- `runtime.test_monitor.Monitor.test_final_collection_order_failure_cleanup_and_producer_survival`

Both passed (`grouped-checks-05.log`). They cover bounded capture/ACK/scan backlog
independently, partial UTF-8/JSONL EOF, durable proof reuse and newer closure,
unlinked resume during transfer/checkpoint, late completion, errors, ownership,
retirement/recovery/conflict refusal and unchanged source/epoch. The monitor check
archives a fsynced suffix after its ordinary tick, verifies exact order/thread,
failure cleanup and unexpected-failure exclusion, leaves an owned Python producer
alive, and verifies queued daily publication catches up after application restart.

The three existing `runtime.test_monitor` checks passed in
`focused-checks-02.log`. Its two new checks also passed, but the command exited 1:
the setup lifetime module could not import `websockets` in that environment.
Only `runtime.test_native_setup.Observations.test_passive_disabled_cached_views_and_monitor_thread_lifetime`
was retried using the already completed `build-native-startup-connection-r1/venv/bin/python`;
it passed in 0.595 s (`setup-lifetime-03.log`). Thus all six selected checks have
passing results. `git diff --check` passed.

All earlier logs remain. `grouped-checks-01.log` has two corrected fixture
assertions: a mismatched record session UUID and an incorrect large-message entry
count that ignored existing parser text splitting. `collector-check-04.log` passed
after strengthening the separate backlog assertions; the last run also covers a
source waiting in the bounded collection ring. New fixtures include the pinned
`cli_version` header. The original failed exit exploration was not edited or rerun.

## Limits

These are temporary synthetic fixtures, with no native CLI/provider/browser or
owner session. The final pass keeps existing budgets and may leave backlog;
proof through N cannot certify future bytes, descendant quiescence, all-source
completeness, exit success, child/media coverage or a backup barrier. No broad
suite, nested reviewer, PR, merge or deployment is part of this candidate.
