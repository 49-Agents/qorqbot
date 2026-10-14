# D48 — Continue discovering native sources and separate raw capture health

Manager decision, 2026-10-09, after the accepted
[coverage audit](evidence/native-coverage-manager-acceptance.md). Base is core
`6e7b67b53c826dd5e995c522d2e3fa12de019fb6`. This is a bounded correction to
NativeCollector, not child/attachment support or a claim of final shutdown drain.

## Repeated discovery

Keep the existing monitor, native identity proofs, recovery guards and finite
owner/source queues. No new thread, watcher, database table or public setting.
Successful discovery must no longer permanently exclude an owner from scans.
Use monotonic per-owner scan intervals: one second for never-discovered owners
or explicitly requested reobservation, five seconds for other non-closed owners,
and sixty seconds for other closed owners. These are minimum retry intervals,
not promises of completion by a deadline. The existing fair queue still selects
at most sixteen owners per tick. On collector reopen, scan eligible owners on
their first scheduled turn even when durable source mappings already exist.
A transition to closed makes that owner's next scan immediately due; retain
periodic closed-owner discovery for late emitted files. Do not use file mtime,
cwd, newest-file ordering or a lease to infer session identity.

Periodic scans register newly discovered, independently verified paths and
refresh discovery-gap observations. They must not re-register every existing
mapping: NativeCapture.register can replace its held descriptor. For a previously
discovered owner, skip already mapped paths during an ordinary periodic scan.
Retain the existing explicit divergence/reobservation path, which drains the held
descriptor in bounded service turns before admitting its replacement. New owner
registration/resume links keep their existing contract. Periodic scheduling must
not bypass the drain prerequisite or enlarge the existing per-source capture,
transfer or projection budgets. Preserve incomplete-record retry and existing
run/source lineage. Cache only scheduling state, never authority.

The existing recovery rule remains: new paths require the owner's recovery epoch
to equal the active installation epoch. Historical-owner reobservation may only
use its already admitted paths under the existing recovery guards. A periodic
scan must not turn restore-held history into new admission or current execution.

When a Codex file moves to an eligible archived_sessions path, ordinary scans
can now admit that independently verified path after reopening. The current
source identity includes path: preserve both source identities and the original
source_missing gap. Do not invent rename/alias lineage, rewrite old receipts,
deduplicate raw bytes by content, clear an unproven gap or infer per-byte author.
The new path can preserve its complete bytes; this does not prove missing tails
of an unavailable old descriptor or semantic deduplication across moved sources.
Child-looking and unsupported sources remain explicit diagnostics, not admitted
primary files. The global runtime observation continues to expose these gaps.

## Private raw capture observation for D46

The aggregate native history view includes semantic parser gaps and unsupported
child/attachment coverage. The original Claude evidence contains these even
when its primary raw bytes are collected. Therefore that aggregate view must
not be D46's raw-recording prerequisite. Do not hide these limitations in the UI.

Add a frozen private dataclass `NativeInputObservation` in native_collector.py:
`binding: RunBinding` (the existing transcripts.spool type), `observed_at: str`
(UTC database clock), `source_count: int`, `healthy: bool`. Add
`NativeCollector.input_observation(run_id: str) -> NativeInputObservation | None`.
It reads immutable cached memory under the existing lock, with no SQL, file I/O,
capture calls, discovery, monitoring, process checks or input. It returns None
before an observation, after close, for an unknown run, or if the observation or
any contributing successful source poll is more than five monotonic seconds old.
This is capture evidence only; it never grants reusable input permission.

The collector thread builds this observation for admitted current-epoch owners.
Healthy requires at least one current primary source epoch linked to the exact
RunBinding, and all current mapped primary epochs for that run to have a fresh
successful service observation. Use the existing ownership/admission checks.
Require no active raw source gap, observation_required, transfer/storage error,
host epoch conflict or recovery hold; require a host archive link for that exact
run/source/epoch and installation epoch. A new epoch's historical divergence
metadata is provenance, not by itself a current polling failure. Pending bytes
may be nonzero: do not claim producer EOF, zero lag or all-source completeness.

Track source service freshness by source_id/source_epoch on the collector thread;
merely rebuilding the aggregate view must not refresh an unserviced source. A
failed source poll/transfer or enclosing ownership/storage failure invalidates
the affected health evidence immediately. Clear cached input observations when
starting a tick and publish a coherent replacement after it finishes; readers
may temporarily receive None. Retired owners' errors and semantic parser gaps
do not invalidate a different current run's verified raw sources. Discovery
coverage warnings remain visible in the public aggregate view but do not alone
invalidate a positively collected primary stream. No access to NativeCapture
from the job-worker thread is introduced.

D46 consumes this exact private method, checks healthy, matching full binding,
and observed_at at or after connected_at, while retaining its own fresh terminal
recorder, native process/ancestry, accepted idle hook, archived primary linkage,
control/composer and final callback checks. No public runtime_get field changes.

## Ownership and acceptance

Own native_collector.py, its existing owning tests and one evidence document.
Do not change discovery eligibility/parsers, NativeCapture/spool semantics,
monitor/server shutdown, Application/Jobs, schema, message dispatcher, UI or
native setup. D46 owns the caller and schema 9 separately. Report missing design
decisions instead of adding cross-module behavior. Ordinary VM collection,
child/attachment capture, a mandatory final drain/checkpoint, rename deduplication
and actual native rollover remain explicitly outstanding.

Focused checks using existing fixtures: late eligible source and later child
diagnostic discovered without another owner; reopening forces a fresh scan;
closed-owner cadence and transition; moved source bytes captured after reopen
while old gap remains; periodic scans do not replace a still-draining descriptor
with more than one tick of unread data; owner/source fairness and recovery holds.
Update the former assertions that forbid all restart discovery, preserving their
partial-record/archive/publication checks. For input observations prove no I/O
on reads, five-second source freshness, exact binding, current raw failure refusal,
and healthy primary evidence despite parser/coverage warnings or another retired
run's failure. Run the owning collector suite; do not duplicate runner, UI or
native-provider workflows. Keep original audit evidence unchanged and distinguish
synthetic correction checks from actual native coverage.

## 2026-10-14 amendment — invalidate only under the effect gate

The clause above cleared the cached input observations when `tick` began, before
that tick owned the process-wide effect gate. `RuntimeInput.submit` owns the same
gate through paste and Enter and rereads the observation several times, so a tick
that was only waiting behind an attempt could erase still-fresh evidence in the
middle of it and could not restore it until the attempt ended. This is a source
finding; the QA delivery that was pasted and then became uncertain without Enter
is consistent with it but is not conclusively attributed to it.

`tick` keeps its owning-thread and closed checks, then takes `db.lock.use` and
`db.lock.effects`, clears the cache and runs the whole collection under that one
gate. There is no new lock, thread, lease, permit or cache lifetime, and no change
to `RuntimeInput`, hooks, terminal transport, schema or public API. A tick that
is merely waiting leaves the old immutable observation readable, still expiring
five monotonic seconds after its oldest contributing service time; nothing renews,
pins or lengthens it. The tick that owns the gate invalidates before any work and
then publishes the replacement, or leaves the evidence empty on a failure or a
disabled collector. An exception, or a refusal before admission, clears the
collector-thread maps at once and the cache under the effect gate (memory only,
no I/O or transaction) and re-raises the original error. The uncertainty receipt
is untouched and an uncertain attempt is still never retried. See the
[bounded evidence](evidence/native-input-cache-race-qa.md).
