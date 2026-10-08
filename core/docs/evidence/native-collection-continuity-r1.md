# D48 native collection continuity — implementation evidence

2026-10-09 UTC. Branch: `codex/build-native-collection-continuity-r1`.
Exact implementation base: `601d42bf288a7713eb75d7b0df07f451091494c9`.
Implements the fixed [D48 assignment](../NATIVE-COLLECTION-CONTINUITY.md).
Manager review is pending. This is synthetic collector evidence, not actual
native rollover or complete-history acceptance.

## Implemented behavior

`runtime/native_collector.py` continues discovering sources after a successful
scan. Monotonic per-owner minimum intervals are one second for missing sources
or requested reobservation, five seconds for other non-closed owners, and sixty
seconds for other closed owners. Closing an owner makes its next scan due;
reopening the collector starts with no scan timestamps, including for owners
whose source mappings are already durable. The existing fair queues still admit
at most sixteen discovery owners and service at most sixteen source IDs per
tick. These intervals do not promise completion by a deadline.

Ordinary scans skip paths already mapped to the exact owner. Explicit
reobservation tracks the source epoch that met the existing bounded descriptor
drain prerequisite. One ready source cannot cause another still-draining source
of the same owner to be re-registered. Fresh native ownership proofs remain the
registration authority; schedules do not establish identity. New paths still
require the active recovery epoch, historical reobservation remains restricted
to already admitted paths, and all existing workspace/recovery guards remain.

A moved Codex file can now be independently admitted at an eligible
`archived_sessions` path after reopening. The original path/source/epoch and
its missing-source gap remain. The new path retains all its bytes under a
different source identity. No alias lineage, raw-content deduplication or
missing-tail recovery is claimed.

The frozen private `NativeInputObservation` has exactly `binding: RunBinding`,
`observed_at: str`, `source_count: int`, and `healthy: bool`.
`input_observation(run_id)` reads only cached immutable memory under the
existing lock. It performs no SQL, filesystem, discovery, capture, process or
input work. Unknown/unobserved runs, closed collectors, and observations or
contributing successful source polls older than five monotonic seconds return
`None`. The UTC observation timestamp comes from the database clock on the
collector thread. Starting a tick clears the input cache until a coherent
replacement is published.

Health checks the current mapped primary epochs for the exact admitted
current-recovery-epoch binding, including all full host run/source/epoch links,
fresh poll/transfer service, current raw gaps, operational storage errors,
conflicts and recovery holds. Source service clocks are independent of
aggregate-view rebuilding. A new epoch's historical divergence metadata alone
does not make its successful current poll unhealthy. Pending archive bytes may
be nonzero. Semantic parser gaps, child/attachment coverage warnings and a
different retired run's failures remain visible in the public aggregate view
without invalidating an independently verified primary stream.

Only this collector, its existing owning test file, and this evidence document
changed. NativeCapture/spool/discovery/parser semantics, monitor/shutdown,
Application/Jobs, public runtime models, schema and UI are unchanged. The
verified base inventory remains schema 8, seventeen operations and five job
handlers. D46's caller/schema 9 integration belongs to Manager and its separate
implementation lane.

## Verification

The final owning suite passed: **12 tests, 42.593 seconds, exit 0**.
`git diff --check` also passed. Command, from this worktree using the existing
private test environment without an install or dependency change:

```sh
PYTHONPATH=src:tests PYTHONDONTWRITEBYTECODE=1 \
TMPDIR=/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04 \
/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/console-test-venv/bin/python \
  -m unittest -v runtime.test_native_collector
```

Original logs are retained under
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/native-collection-continuity-r1/`.
`collector-tests-03.log` is the final result. `collector-tests-01.log` retains
the initial assertion failures: former discovery assumptions, expected parser
diagnostics and a floating-point scan-boundary comparison. These were corrected;
`collector-tests-02.log` passed before the final current-epoch operational
storage-failure check was added. No earlier evidence was overwritten.

The tests verify:

- Late eligible primary files and later child diagnostics without another owner;
  five-second active scans, immediate closure scans, sixty-second closed scans,
  and one-second retries for never-discovered closed owners. Diagnostics refresh
  without re-registering the existing primary mappings.
- Fresh discovery after collector/application reopen while retaining partial
  UTF-8/JSONL archival, completion, original run/source lineage, native resume
  links, immutable chunk hashes/coordinates and dated publication assertions for
  both pinned provider fixture formats.
- Full moved-source bytes under the new path after descriptor loss on reopen,
  preserving the original missing-source gap and both identities across another
  reopen.
- Two replaced sources of one owner, including an unread tail larger than three
  MiB. Ordinary scans and the other source's completed drain preserve the held
  descriptor until bounded service drains it. Old/new epochs retain their bytes;
  current raw health can recover while historical divergence remains public.
- Eighteen-owner/source fairness and unchanged capture, transfer and projection
  budgets; recovery after a real host archive receipt commit retains pending
  bytes/intents without ACK/reclaim or new late-path admission.
- Frozen full bindings, UTC timestamps, unknown/closed/stale reads, and a read
  from another thread while SQL, filesystem, capture, discovery, monitoring and
  database-clock access are patched to fail.
- Eighteen current primary mappings: not-yet-serviced sources prevent health,
  continuing service becomes healthy, and a contributing source older than five
  seconds expires the observation even when the public aggregate was rebuilt
  recently. Rebuilding the aggregate cannot refresh source service evidence.
- Immediate cache clearing during a tick; poll, transfer, current-epoch storage,
  discovery-storage, workspace ownership, snapshot-storage and host-conflict
  failures refuse healthy evidence. A bounded partial transfer can retain
  pending bytes while fresh primary raw evidence remains healthy.
- A resumed run cannot borrow an earlier run's host link. After exact linkage,
  both bindings remain distinct. Current primary evidence remains healthy
  despite stored parser/coverage gaps and an independent retired-source failure.

These use disposable temporary installations, synthetic pinned-format records,
and owned Python child processes for the existing admission fixtures. Publication
is driven by the owning fixture's explicit job pump. A synthetic no-op transfer
isolates the exact resumed host-link prerequisite; a reduced transfer batch
checks the allowed nonzero backlog. No native CLI, paid inference, owner session,
personal HOME, browser profile or live service is used.

## Limits

The original [coverage audit](native-coverage-audit-r1.md) and its
[Manager acceptance](native-coverage-manager-acceptance.md) are unchanged.
This correction does not repair the audit's original native suffix loss or
reclassify any original evidence. It grants no reusable input permission and
does not establish producer EOF, zero lag, per-byte authorship, semantic
deduplication or all-source completeness.

Ordinary VM collection, child/attachment capture, mandatory final shutdown
drain/checkpoint, rename deduplication, actual native rollover and the composed
D46/D48 native-input acceptance remain outstanding. No merge, PR, main edit,
deployment, global service change or VM decision is part of this assignment.
