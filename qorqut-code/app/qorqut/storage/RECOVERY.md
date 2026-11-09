# Operational Recovery

SQLite remains the sole operational authority. These APIs are trusted service
facilities, not agent tools or a second editable data store. Importing or
constructing them does not create a job, start a daemon, or run Git.

## Nightly Program

`NightlyMaintenance(database, paths, *, locks, backup_destination=None,
git_enabled=False, git_runner=None, clock=utc_now).run(control, {})` is the fixed
`maintenance.nightly` program. The service registers it with installation scope, no core
mutation outputs or agent grants, and a 1,800-second limit. Production assembly
selects the reviewed Git setting; the constructor default is useful for isolated
rehearsals. Scheduling is an ordinary explicitly installed global routine.

The program needs a verified backup, publishes deterministic exports from that
saved database, optionally versions those exact saved bytes, then applies the
seven-daily/four-weekly retention policy. Errors remain ordinary failed jobs and
never roll back live business work or create journal entries. The oldest valid
generations are removed only after a new generation has been verified. One newest
generation per available UTC date/week is retained, with the two sets combined.

Each job's UUID identifies its immutable backup generation and private recovery
receipt in `operations/.runtime/maintenance/`. Incomplete retries reuse that saved generation,
not newer live records. A changed destination or Git setting cannot expand a
partially completed job: the original configuration is required for its recovery.
Completed retries return their saved result. These bounded receipts are job
recovery metadata, not an event log or another authorization authority.

### Capture handoff

A backup needs the installation's recovery barrier (`runtime.recovery_quiescence()`), and the
background worker that runs this job is itself one of the writers that barrier drains, so the job
cannot hold it. `run` therefore records a bounded capture request in the job's existing private
receipt (`.runtime/maintenance/<job>.json`, key `capture`: the job, program definition digest,
principal, installation, empty arguments, request time and one fixed deadline) under the job's
application claim, and returns the existing `ProgramPending`. Nothing is reported successful at that
point and a later invocation neither repeats the request nor resets its deadline.

`NightlyMaintenance.bind_runtime(runtime)` is inert and returns the `MaintenanceCaptureService` stored
as `runtime.maintenance_capture`. The service owns one thread (`start()`, `stop(timeout=...)` ->
`{stopped, in_flight}`, `running`; a stopped service starts afresh and a stop requested from its own
thread never joins itself or aborts its capture). For each active request it validates the persisted
job, registered program definition, worker authority, installation and exact saved configuration,
holds the job's application claim, enters `runtime.recovery_quiescence()` from its own thread and calls
`BackupStore.create(job, barrier=<yielded proof>)` (the real proof is bound to that thread and fails anywhere else); it never
reuses the background `JobControl`. The
verified result or the exact failure (`state: complete | failed`, `error` as raised) is saved in the same
receipt. A stop requested mid-capture, or a crash, leaves the same request for the next start; a
generation already sealed is verified and never captured again; a cancelled, ineligible, failed or
expired request is final and is never retried behind the job's back.

The job's next invocation verifies the sealed generation (both components) and continues with exports,
Git history and retention, and returns the one original final result. A failed or expired capture fails
the job with that error; a job whose service never runs fails with `timeout` at its fixed deadline.

## Backups

`BackupStore(database, paths, destination=None)` defaults to `<runtime>/backups`.
An external private destination may be explicitly supplied. A destination inside
`operations/` must remain below `operations/.runtime/backups`.

`create(generation_uuid, *, barrier)` requires the actual Operations `RecoveryBarrierProof` (exactly that
type, from `qorqut_operations.storage.quiescence`) yielded by the installation's `RecoveryQuiescer` window;
nothing is inferred from a gate, claim or lock, and a method-shaped substitute or a subclass is refused as
`invalid_configuration`. The Context library itself stays structural (`WriterBarrier.verify_active`); the host
adapter `ProofBarrier` is the only place that binds the proof to it, asking the proof whether its window is
still the active one in the creating process and thread and whether it froze this very database. The proof is
verified first of all, before any file or database effect: an ended, foreign or other-thread proof fails
immediately and leaves the generation directory and `backup_runs` unchanged, also when the generation is
already sealed (a sealed generation is only re-verified, and only under a valid proof). It is then rechecked
before capture, around the Context capture, after every component is captured and immediately before a
complete generation is sealed; a proof lost mid-capture fails the generation and publishes nothing.
`verify` is the separate read-only API and needs no proof. `FullRecoveryStore.create` enters its quiescence
`as barrier` and passes the same value. Dependency: this requires an Operations containing
`RecoveryBarrierProof` (candidate cc8fc5f6 or its accepted successor).

`create` uses SQLite's backup API, validates integrity, foreign
keys and migration checksums, and writes a private temporary generation. It
captures registered document hashes, complete pinned artifact packages, registered
bootstrap files and scoped credentials, owned business `hq` trees and shared
authored context. Root and registered HQ `.qorqut/legacy/` retirement archives
are included completely as private historical bytes, including nested private
files; they are never traversed by exports or Git versioning. Business `code`
siblings, Git internals, generated exports and
cache/dependency directories are not captured. Private full backups include the
operational DB's exact signoff plans, job results, and file-apply receipts; the
prepared file bytes needed for recovery already live in those durable records.

Claimed file applications, missing authored files, changed document/artifact/
bootstrap hashes, and sources changing during capture reject the generation.
Resolve the reported source drift or incomplete apply, then retry. No incomplete
directory is published as a restore point. Completed generations contain
`state.sqlite3`, `authored/manifest.json`, `authored/sources.tar`, and
`generation.json`, with schema/counts/checksums/artifact digests/capture time.
The shared Context is a required component of every generation, captured with the operational database
under the same proven barrier: `context/` holds the Context repository and its separate runtime as one
self-contained capture (`qorqut_context.recovery`; see `CORE_CONTRACTS.md`, "Recovery capture and relocated
restore"): the published graph, source and event history with the full Git object closure, the pending draft
and its input, execution attempts and usage receipts. The location is `paths.context` / `paths.context_runtime`
and `generation.json` records the capture's manifest digest, identities and cutoffs under `context`
(generation `schema_version` 2). The Context checkout is not walked as authored files, wherever the instance
places it. The queue driver's empty advisory lock file in the runtime is held while it exists and is not
captured. Any other file the capture cannot classify fails it; nothing is silently dropped. The former ContextBase
`contextbase.sqlite3` and `costs.sqlite3` captures no longer exist.

Backup directories are mode 0700 and archives/databases are mode 0600. They contain
secrets and must stay private; they are never Git inputs.

`verify(generation_uuid)` rechecks the saved DB, archive and manifests without
depending on today's authored files, binds the authored projects to the captured database and the target
`projects/<slug>/hq` layout (a previous `businesses/<slug>/hq` archive is recognized but is never restored into this layout without an explicit conversion), and verifies the Context capture against its recorded digest and this
instance's Context identity; a missing, altered or foreign Context makes the generation invalid.
`restore(generation_uuid, empty_directory)`
rehearses into a separate empty root and verifies operational row counts. The SQLite maintenance gate
stays closed. Original absolute business/agent bindings are intentionally retained in the database: this is a
parity rehearsal, not a relocation feature. The Context pair is restored last, at its final locations beneath
the rehearsal root (its two ownership markers name those paths; events and provenance are unchanged), and
`.runtime/restore-incomplete.json` marks the root incomplete until the restored pair passes the library's
ownership, format and event-integrity checks. A partial restore stays incomplete and unusable, keeps the
library's phase journal beside the repository and resumes only for the same capture and targets. No services,
jobs, model calls, queue replays or approvals are started. Activation
belongs to the reviewed recovery runbook at the original registered root.

## Exports And History

Business tables additionally use per-mutation history on `qorqut-tables`, separate
from nightly history. Current records, pending snapshots and original Data Uploads
are included in full backups; already-published historical snapshots remain in
the businesses Git repository, whose `.git` directory is not part of these backups.
Global general tables use the same stores with a NULL business (migration 105), the
same history ref and `global/tables/<table-id>.json` under the businesses root, and
a `global` export scope; retry and recovery never cross between Global and a
business. See [Tables](../../../docs/TABLES.md#git-history) and
[Global tables](../../../docs/TABLES.md#global-tables).

`build_exports(snapshot_path, root)` selects the fixed public operational table
catalog from one SQLite snapshot. `publish_exports(bundle, root)` replaces only
known JSON projections under each `hq/exports/` and root `exports/`, with scope
manifests last. Every file is marked generated, SQLite-authoritative and not
importable. Unchanged state produces byte-identical files. Credentials, sessions,
signoffs, apply/idempotency receipts and raw jobs are excluded. Current profile
policy and artifact references remain ordinary readable configuration.

Private owner/Ordubashy conversation tables (`owner_conversations`,
`owner_conversation_messages`, `owner_conversation_reads`; see
[OWNER-PRIVATE-CONVERSATIONS.md](../../../docs/OWNER-PRIVATE-CONVERSATIONS.md))
are excluded from this export catalog and from `qorqut-history` publication
the same way. Whole-database backups above retain them by design; they are
not an application read path and are never a shared export.

Migrations 078–083 add durable Ordubashy assignment plans, checkpoints, bounded
runtime accounting, hashed worker bindings, action receipts and generation-fenced
inbox event claims. Migration 083 adds a separate failure-retry counter; ordinary
claims and expected wait/resume continuations do not consume it. These tables
are retained in the verified whole-database backup, but excluded from public
operational exports and `qorqut-history`, like other orchestration/security
receipts. A restore rehearsal restores them as database state only; it starts
no worker process.
Migration 079 adds explicit parent, worker-binding and approved-plan-revision
attribution to committed action receipts; it remains in the same private
backup/export boundary.
Migration 080 adds worker/assignment/plan attribution to direct-message rows.
Migration 081 adds claims for exact linked tasks, Stories, documents, and
message threads. Business-only matches and ambiguous matches are not routed.
The parent delivery remains authoritative: a worker must save a handling
outcome before the same transaction acknowledges the parent delivery. A live
claim prevents parent acknowledgement, and pause/revision/completion releases
unhandled claims without clearing their deliveries. Migration 082 tracks active
execution seconds separately from durable wait time. Worker turns and token
usage reported by Codex are audit events in the same private database backup;
they are not cost estimates. A restored database starts no process. The local
supervisor is attached to the trusted parent Codex watcher, so stopping or
replacing that parent fences running workers for later checkpoint recovery.
Codex conversation storage remains in the selected local `CODEX_HOME`, outside
the Qorqut database/export boundary. If a saved thread cannot be reopened before
a new turn starts, the supervisor clears that runtime pointer and recreates a
child from the saved assignment/checkpoint using a fresh generation-bound
credential. If dispatch is unavailable, assignments and parent deliveries
remain durable with their reason visible and ordinary parent inbox handling
usable.
Rollback pauses dispatch, revokes active worker bindings and reconciles
uncertain action receipts while retaining assignments and unhandled events.
Do not downgrade the schema or delete this history as a shortcut; use the
reviewed backup/recovery runbook and a separately approved activation plan.

Enabled Git versioning requires the root's existing repository; it never runs
`git init`. History lives only on `refs/heads/qorqut-history`, seeded once from
existing `HEAD`. A temporary index receives explicit authored-data and export
paths from the verified generation. The user's current branch, worktree, real
index, unrelated staged files and business code repositories are not changed.
Versioning refuses to advance this history branch while it is checked out in any
working tree; inspect it by ref without checking it out for ordinary work.
Credentials, `.env`, DB/backups, sessions, runtime logs and caches are excluded
from new versioned changes. Existing commits are preserved. There is no push
or remote-sync path in the application.

The deterministic commit and expected previous ref are saved before the atomic
ref update. A crashed retry detects an already completed update; it never rewinds
an advanced history branch. An unavailable Git repository or failed ref update
leaves the verified backup and exports intact for diagnosis.

`Database.open_existing()` is the startup path: it validates the exact current
schema and populates repository metadata using read-only connections. It does
not create a database or run migrations. `initialize_target()` remains an explicit
installation/migration API, never an implicit cold-start upgrade.
