# Bounded Background Work

Business agents remain Claude Code sessions in mirrored terminals. These programs
never launch, wake, schedule, or impersonate a business agent. They run fixed,
bounded application work through the shared backend.

## Assembly

The generic machinery (`ProgramRegistry`, `BackgroundAuthority`, the serial `BackgroundService`, `RoutineScheduler`
and `RoutineOperations`) is the Operations engine's (`qorqut_operations.background`/`.routines`). This package holds the
host's concrete programs and the attachment below; the registry is frozen with the operation registry before traffic.

```python
from qorqut_operations.background import ProgramRegistry
from bazarlyk.background.integration import attach_background

programs = ProgramRegistry()
runtime = build_runtime(workspace, paths=paths, job_programs=programs)
background = attach_background(
    runtime, programs,
    model_client_factory=business_model_client,
    transcript_source=registered_transcripts,
)
```

Assembly performs no writes, starts no threads, and makes no provider calls. All
handlers are installed and the program registry is frozen (`Runtime.freeze_registry`) before accepting traffic. There
is one `BackgroundAuthority`, built by `build_runtime`; `attach_background` reuses it, wires the incident reporter as
the service's `JobObserver` and gives it the `RoutineScheduler`. `Runtime.start()` (see
`bazarlyk/service/lifetime.py`) verifies storage authority, recovers prepared file receipts, then starts the services
in their fixed order (Signoff application, then this service, ...). `Runtime.stop()` drains them, closes write
admission and only then SQLite; a timeout means a bounded external call is still in flight, so the shutdown is
reported incomplete (`ShutdownIncomplete`) and the database stays open. Process restart recovers durable jobs;
service shutdown does not owner-cancel them.

An explicit `maintenance=NightlyMaintenance(...)` additionally registers the
root-only `maintenance.nightly` program with empty arguments and no domain output
operations. Its run and recovery use the same stable job ID as the maintenance
generation ID. Its worker still needs normal installation registration. Neither
program registration nor attachment creates a maintenance routine or job.

Installation/import registers the system principals and memberships:

```python
from qorqut_operations.background import BackgroundAuthority, ProgramRegistry
from bazarlyk.background.integration import register_programs

programs = register_programs(ProgramRegistry())
authority = BackgroundAuthority(database, programs)
with database.transaction() as uow:
    authority.register_workers(uow, registered_business_ids)
```

The Context service also registers missing `context.file` and `context.instruct`
workers and their unarchived-business memberships during owned startup. This
repairs converted installations that retained the old upload worker but lacked
the new instruction worker. Reads and submissions never register workers;
disabled identities and memberships remain disabled. No job or input is replayed.

Worker IDs are `uuid5(installation_id, "program:" + program_id)`. Registration
never reactivates an existing revoked principal or membership. The exact program
definition and grants are hashed into every job. Runtime configuration does not
load executable modules, shell commands, or arbitrary program definitions.

## Programs

| Program | Computation and declared effects |
| --- | --- |
| `context.file` | Completion program (version 2) for a document source version (upload, note, journal or accepted learning). The source version, this job and the GENERAL Context queue input are created in one prepared operation; the backend Context driver alone propagates the input. The job never reads a body or calls a model: it returns `ProgramPending` (15 seconds) while the input is queued, processing or blocked, then freezes its one output, `context_filing_receipt(receipt_id)`, which settles the document status. A discarded input fails the job with `context_discarded`. See [Context filing completion](../../../docs/v1.2/CONTEXT_FILING.md). |
| `context.instruct` | Completion program (version 1) for one focused owner instruction. Same shape as `context.file`: the instruction is frozen as its own source version under `foundation/instructions/{job-id}.txt`. |
| `learnings.extract` | Preserved transcript pruning, repeated extraction, exact speaker evidence checks, deduplication, variants/votes, candidates and scan/run provenance. Acceptance never applies a learning. Global only (v1.2 G022): one Global stream, no business job, routine target or worker membership; business learning records stay readable and reviewable as history. |
| `learnings.engine_extract` (removed) | The standalone procedural learning engine and its review-issue program are not registered in this target. Its historical jobs, candidates and decisions stay readable as history, but no routine, configuration or worker can resolve or run it, and no engine evidence or package is imported. Factual Global learning stays with `learnings.extract`; procedural work belongs to Overseer. |
| `repository.status` | Fixed read-only `git status --short` in the registered business HQ. No argv input, hooks, commits, or global Git configuration. |
| `tables.version` | Publish a captured shared-table revision to the businesses-root version branch, without changing the owner's branch or index. |
| `tables.global-version` | Internal Global-only twin of `tables.version` (same runner and recovery, own worker principal): publishes Global table revisions under `global/tables/`, never a business's. |

Provider calls use `background.providers.BackgroundModelClient`, explicit business
configuration, bounded retries and the job's remaining timeout budget. Known usage
is persisted before cancellation/output checks, including a known late completion.
Unknown transport outcomes are not assigned invented token costs. Learning budgets
are soft: a started call may exceed the limit; another call is not started afterward.

Context jobs bind every consumed registered document revision and content hash.
Filing accepts at most 80,000 source characters and 4,000 paragraphs, and fails
explicitly rather than dropping text. Outputs are schema-checked and frozen before
any application effect. Failures can themselves have an exact typed cleanup effect;
the job remains failed after that effect is recorded.

## Authority And Recovery

`BackgroundAuthority` validates job ID, system principal, business membership,
initiator attribution, registered definition digest, exact frozen operation args,
source revisions, and declared resource types. A caller-supplied worker label is
not authority. Job IDs are internal context fields, never accepted from transports.
Dedicated declared consequences can finish after an initiating agent is deactivated.
Discretionary work still intersects that agent's current authority.

All writes use `Application.invoke`, `PreparedFileExecutor`, and `SignoffService`.
Frozen outputs use stable per-job/per-output idempotency keys. A restart never asks
the model to regenerate an approved or frozen output. Before output freezing,
read-only computation may be retried; already known provider usage is retained.
There is no separate file writer or operational JSONL authority.

Dispatch persists the final specification and hash in `dispatch_intent` in the
same short transaction as current request/grant validation. Owner reject/replace
and the dispatch claim therefore serialize. No network call occurs in that
transaction. Once an intent exists, recovery only looks up its `client_ref`; it
never resubmits. `external_receipt` is immutable. An unresolved lookup stays visibly
pending for reconciliation, not falsely failed or successfully resent. The client
must implement server-side `client_ref` deduplication, `submit`, `upload_asset`,
`find_job`, `get_job`, and `cancel_job`. Missing clients fail explicitly.

Cancellation uses the same immutable intent/receipt checkpoints, binding the
business, superseded request and saved external job reference before the DELETE
call. A saved receipt completes recovery without another call; an uncertain
DELETE may retry only that same reference. Outcomes are `cancelled`,
`already_terminal` (successful no-op), or `too_late` (the cancellation job fails
with `external_cancel_too_late`). The original remains superseded and its
replacement remains submitted. This program has no domain mutation outputs.
Temporary timeout/5xx errors use at most two 30-second queue deferrals; a third
failure remains visible. Authentication, binding and unknown-conflict errors
fail immediately. No general job-retry service or configuration is added.

## Routines And UI Ports

Routines have typed program arguments, cron/timezone, active/paused status, target
and timeout. Agent activity never delays a routine or an
already queued job; there is no activity waiting option. Runs are globally serial. Manual runs
can execute paused definitions and never advance schedule slots. First-seen or
changed schedules establish a baseline without retroactive dispatch. UTC traversal
handles DST; the repeated wall hour is not run twice. Missed slots coalesce to the
latest due slot, while unfinished each-business fanout finishes its old slot first.

`routine_slots.state_json` retains `done`, `job_id`, `missed`, and `missed_capped`.
An imported/deferred `{done: false}` without a job ID resumes the saved slot. A
completed job marks its slot done atomically with the run result. Unknown legacy
in-flight processes require the owning adapter's explicit export/reconciliation,
never PID reuse, process killing, or inferred success.

Owner stop is permitted before frozen effects or external dispatch begin. After
that boundary it refuses clearly; pending Signoffs can be rejected separately.
Cancellation is not rollback of already prepared/applied effects.

The dashboard's routine status is independent of errors from other background
programs. Authorized global history is grouped before pagination, using scheduled
slots or explicit manual invocation IDs, with one combined cost. Raw executions
remain in `routine_runs`. Run details retain names, timezones, business names,
structured outputs, errors, and recorded models; failure notifications use stable
run IDs. Stop targets the registered namespace and exact job/business, including
older jobs whose immutable saved definitions omit presentation scope.

- `routines_list` and `routines_runs`: `{items, next_cursor}`.
- `routines_get`: `{routine}`; `routines_preview`: `{slots, timezone}`.
- `routines_costs`: `{usd, runs, routines, businesses}` with retained per-run calls.
- Routine mutations: `{records, job_ids}`; CAS uses `options.expected_revision`.
- `learnings_list`: `{items, counts, next_cursor}`; `learnings_get`: `{item, cores}`.
- Owner learning review/edit: `{item}`; extraction: `{job_id, state}`.
- `jobs_get`: `{job}`, selected business by default. Global jobs require explicit
  `scope: "global"` and global routine rights. The optional Fastmail provider's mutation jobs are observed through its
  own `fastmail_receipt_get`, which needs only the `manage_fastmail` account grant.
- `background.status(business_id)` returns scoped current/recent jobs and invalid
  definitions. It is an internal already-authorized adapter port.

No signoff business log, journal, toast, task, or agent wake is emitted. The only
filing logs and tasks describe actual completed work or fulfillment failure.

## Learning sources

See [multi-source learning](../../../docs/MULTISOURCE-LEARNING.md). Extraction runs in Global only (v1.2 G022):
a root `learnings.extract` routine processes registered Global agent transcripts and the submitted owner Signoff responses and
Task notes of Global and every active business (G023), under one bound and with each contribution's original business
attribution, into its native Global review queue. A saved each-business learning routine is refused,
never read as root, until it is explicitly changed to a root target. Notifications,
drafts and generated projections are excluded. Comparisons include accepted and
implemented lessons without changing their decisions.

## Transcript Input

`transcript_source(business_id, agent_id=None)` supplies explicitly registered
snapshots: `session_id`, `source_id`, `agent_id`, `agent_kind`, UTF-8 JSONL `content`,
optional SHA-256 `transcript_hash`, and optional pre-pruned `subagents` metadata.
Each source is capped at 16 MiB, each run at 64 MiB and 100 selected sessions.
New snapshots are processed; unchanged processed snapshots are skipped; changed
snapshots are processed in full after pruning. Agent activity does not filter
snapshots. The saved hash identifies the consumed snapshot, so growth during
extraction is eligible on the next run.
Business membership is checked and the hash is recomputed. The adapter may read
selected historical sessions; this service never scans personal Claude directories.
Legacy routine/learning caches outside the approved roots need an explicitly
authorized owning-adapter export. Missing exports must be reported, not reset silently.
## Preserved Progress

Previously imported routine slots, unfinished runs, learning decisions, and
transcript fingerprints remain database records. Removing the retired importer
does not reset them. Existing `legacy_progress='unavailable'` and
`recovery_required` markers still prevent automatic scheduling until reconciled;
manual runs remain separate. Do not invent missing scheduling baselines.
