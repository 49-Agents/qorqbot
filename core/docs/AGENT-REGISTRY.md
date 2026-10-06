# Agent registration, authored workspace and status: first increment

Manager design, 2026-10-08. Base: merged core `f168369d553f8378df1ba8bc2470a4e1105bf25c`
([PR #1](https://github.com/qorqutbot/qorqut-core/pull/1)), with this planning
commit applied. This implements M01.01–03 and the initial directory portion of
M09.01. Presence ingestion, runtime/VM readiness, archive/restore, general file
APIs, credential issuance and browser/message integration are later assignments.
Do not infer readiness from successful registration or directory creation.

## Ownership and application boundary

Add a small `qorqut_core.agents` package for typed values, SQL operations and the
workspace job. This assignment is the sole writer of host migration 3,
`Application` assembly and the shared wait-reason enum. No other lane changes
those files concurrently. Keep the current dependency lock and package metadata.
Reuse `Model`, `Mutation`, `Operation`, `Change`, `register_actor`, `append_event`
and fixed `Jobs` handlers. No dynamic plugin/registration system or extra store.
All public operations go through `Application.invoke`; HTTP/MCP/operator
inventory follows the existing registry automatically, with no transport SQL.

Add migration 3 without changing migrations 1/2 or their checksums. It adds the
agent records, the reserved installation pointer and workspace/projection
progress needed below. Existing synthetic registered actors need not already
have agent rows: do not retrofit a foreign key that invalidates them.
Use explicit installation INSERT columns after extending that table.

An agent record owns permanent UUIDs for `id`, `actor_id` and `conversation_id`;
unique conversation and actor columns suffice in this increment. Do not add an
empty conversation-entry system. Register its `Actor(kind="agent")` in the same
transaction. Store creator actor/time, display name/icon, kind, resolved CLI and
resource configuration, `revision=1`, `lifecycle_revision=1`, `start_requested`,
`lifecycle=provisioning`, initial persona seed, current status fields, and
workspace/projection progress. A partial unique index permits one `kind=host`
row; `installation.onbash_agent_id` references that permanent row. DB constraints
and application validation enforce the invariant, independent of its name.

## Public operations and views

All mutations retain the shared idempotency key and immutable operation result.
UUID inputs are typed UUIDs; counters/revisions use the shared strict bounded
types. Do not accept caller identity, filesystem roots or kind from arguments.

| Operation | Arguments | Successful data |
| --- | --- | --- |
| `agents_create` | `display_name`, required complete `cli: CLIDefaults`, optional complete `resources: ResourceDefaults`, `icon_key="circle"`, optional `persona`, strict `start=true`, idempotency key | `{agent: AgentView, provisioning_job_id}`; `outcome=accepted` |
| `agents_get` | `agent_id` | `AgentView` |
| `agents_list` | `limit=100` (1–200), optional `after_id`, optional `recovery_epoch` | `{items, next_after_id, has_more, recovery_epoch, onbash_agent_id}` |
| `agents_update` | `agent_id`, `expected_revision`, at least one of `display_name`/`icon_key`, idempotency key | `AgentView`; accepted when a changed presentation queues projection work, completed for unchanged confirmation |
| `agents_status_set` | `agent_id`, `expected_revision` of the status, `text`, idempotency key | `AgentView`; completed |

List by canonical immutable UUID ascending, with keyset pagination and one extra
row for `has_more`. `after_id` requires its recovery epoch; supplied wrong epoch
is `target_changed`. Empty pages preserve the supplied cursor. This is a live
directory, not a snapshot of concurrent registration; restart listing to see
concurrent inserts before a cursor. Names never determine ordering/routing.

`AgentView` includes `id`, `actor_id`, `conversation_id`, `display_name`,
`icon_key`, `kind`, data-relative `workspace_path`, resolved `cli`/`resources`,
`revision`, `lifecycle_revision`, `lifecycle`, `start_requested`, `created_by`,
`created_at`, `updated_at`, `workspace_initialized_at` and `projection_revision`.
It includes `status` with `text`, `revision`, `updated_at`, `reviewed_at` and
`actor_id`. Observation fields are `activity="unknown"`, null
`activity_observed_at`, null `last_contact_at`, null `current_run`,
`capture_health="unknown"`, and `needs_user_input=false` until their owning
modules exist. These unknowns are explicit, never fabricated idle/healthy/ready
observations. Do not return the initial persona seed in directory/results.

Display names use Unicode trim followed by 1–120 characters and valid UTF-8;
duplicates are allowed. Validate icons against the existing twenty-key catalog.
`persona` is exact valid UTF-8 text of at most 64 KiB, including empty text if
explicitly supplied. Omission uses a small constant plain-text seed describing
the editable persona/role and persistent notes; no role catalog or permissions.
Resources resolve once from current installation defaults when omitted. Save
the resulting configuration; changes to defaults never rewrite existing agents.

Creation is one transaction: agent/actor/conversation identity, resolved config,
initial status `Initializing.` at revision 1, `agent.created` and
`agent.status_changed` events, and one concrete `agent_provision` job. Events
carry IDs, configuration/revisions and original attribution; record persona
seed hash/length instead of duplicating the full text in an event. Initial status
has `old_text=null`; later changes include old/new text and status revision.
All public create calls create `kind=vm`; host creation is reserved to setup.

Updates require the current metadata revision even when the normalized values
are unchanged. Actual presentation changes advance only that revision and
timestamp, append one `agent.presentation_changed` event with old/new values,
and queue a fixed `agent_project` job. They preserve IDs/paths/configuration and
lifecycle intent. No-op updates add no domain event/job/revision.

Status normalization is `" ".join(text.split())`, then validate 1–100 words,
at most 4,096 Unicode characters and valid UTF-8. Check the expected status
revision first. Changed text advances status revision, update/review timestamps
and actor in the same transaction as its event. Same-text confirmation updates
only `reviewed_at`; no status-change event or revision. Identical-key replay
returns the original receipt without refreshing a timestamp again. Metadata
revision is independent. Do not accept unverified run attribution in this
increment. Existing `events_list` retrieves the permanent history with agent
attribution; no second history table/TTL/filter framework is required.

## Reserved onbash and setup

`Application.initialize` explicitly ensures onbash after opening/migrating and
registering the owner. Use the same private registration transaction/handler
and durable operation receipt, with fixed internal operation name
`installation_ensure_onbash` and key `onbash:v1`. It is not a public registry tool.
The installation pointer and partial unique index are authoritative. Retries,
renames, changed defaults and service restart cannot allocate another host ID.

First setup captures current defaults, display name `onbash`, icon `terminal`,
default persona and `start_requested=false`. It queues the same provisioning
handler; setup must not launch a provider or create directories inside SQL.
A crash between installation setup and onbash registration is repaired by
explicit `initialize` retry. `Application.open` neither repairs nor creates it:
missing/inconsistent reserved identity is `not_ready`/`target_changed` and asks
for explicit setup. Migrating an existing v2 installation follows this same
path. Renaming onbash does not change the pointer or special uniqueness rule.

## Workspace effect and generated projection

`agent_provision` is a real bounded external-effect handler: it prepares the
owned authored workspace and derived identity projection. It does not launch
tmux, CLIs, VMs, Chrome, networking or services. Its arguments contain the
agent ID and expected lifecycle revision; retrieve seed/config from committed
records. Capture the registration operation/job identity for file ownership.

Use the existing installation effect gate and lock; revalidate job/recovery and
lifecycle coordinates before effects and finalization. No filesystem I/O runs
inside an SQL transaction. Descriptor-relative paths reject symlinks and
non-directory ancestors; directory names derive only from stored UUIDs.

Prepare one private sibling staging directory beneath `agents/`, uniquely named
by the provisioning job. It contains a small versioned ownership marker with
installation, agent, creation operation and job IDs. A staged marker has a
preparing/complete phase; atomically mark complete only after all initial files
and directories have been fsynced. A pre-existing staging directory without a
valid matching marker is unresolved, never silently adopted or deleted.

The initial layout contains `workspace/{notes,research,data,files,downloads,scratch}`,
`workspace/persona.md`, `workspace/memory.md`, `runtime/`, and the history
subdirectories in ARCHITECTURE. Leave provider entrypoints/native homes/disks
to their owners. Seed memory with an empty file and persona with the saved exact
seed. Preserve existing regular seed bytes when resuming a positively owned
stage; reject links/special files. No data or profile is copied from Qorqut.

Publish the complete directory to `agents/<agent-id>` with atomic no-replace
semantics and fsync its parent. On this Linux target a small lazy
`renameat2(RENAME_NOREPLACE)` wrapper is acceptable; never use replacing rename
after an existence check. Unsupported primitive/filesystem fails explicitly.
If the final directory exists, require a matching complete ownership marker
before using it. Never adopt an unmarked/unrelated directory, even if empty.
An interrupted mkdir before its marker can remain uncertain for explicit repair;
do not guess ownership to make a fault test green.

After publication, authored files are ordinary user/agent files: retries must
never reset them, recreate intentionally removed seeds, or prune scratch/data.
The complete marker establishes initial seeding, not current file hashes.
Record `workspace_initialized_at` and a single `agent.workspace_initialized`
event after verifying publication; retry/reconciliation deduplicates that event.

`agent.json` is a bounded versioned projection of committed identity/configuration,
including its revision, IDs and layout version; SQLite remains authoritative.
Both provisioning and `agent_project` use one narrow projection writer under the
effect gate. Read a committed snapshot, stage/fsync bytes beside the target,
atomically replace only the generated projection, fsync the parent, then commit
the observed projection revision. If the row changed meanwhile, retain the
observed revision and arrange/reuse queued projection work; never mark a stale
file current. All projection jobs read the latest row, so an older queued job
cannot overwrite a newer committed projection. A projection job before the
workspace exists waits with ordinary bounded `retry_delay`.

These directory/projection effects use their concrete M00 job receipts and
ownership marker, with the existing descriptor-safe primitives. Do not fork the
general `Files` API or create a second generic receipt subsystem. M09's later
authored-file writes will use the accepted single-file receipt path. Crash
inspection can complete evidence or return uncertainty; it never resets authored
files. Recovery-review inspection is read-only and preserves its hold; it cannot
publish/repair files or release a restored provisioning intent automatically.

## Honest resource dependency

After the workspace is prepared, provisioning remains `waiting` with typed
reason `dependency_unavailable`, phase `runtime_integration_required`, and a
bounded `unsupported_capability` explanation. Preserve the requested start flag
and `lifecycle=provisioning` for both VM agents and onbash. Add this wait reason
to the shared enum; `Jobs.claim` must continue excluding it. Its required
`next_attempt_at` is an earliest explicit inspection time, not permission to
poll/retry effects automatically. There is no fake executor, readiness default
or placeholder runtime operation. A later explicit integration/reconciliation
will settle resource readiness and any launch intent under its own reviewed
contract. Workspace completion is inspectable separately from that dependency.

No worker loop starts during import, read, setup or HTTP startup in this
increment. Focused tests/explicit in-process setup drive the actual fixed jobs.
Do not add a public job-dispatch tool to compensate for the later worker service.

## Focused verification and delivery

Reuse foundation fixtures and current real HTTP/SDK support. Cover these owning
boundaries with a compact suite, not copies of the complete foundation matrix:

- Atomic create/replay/conflict/concurrent keys; creator attribution for owner
  and registered agent; copied defaults; UUID/conversation/actor stability.
- Explicit v2 migration/setup retry/onbash rename and uniqueness; open never
  performs setup; failure before/after registration yields one durable identity.
- Icon catalog, trimmed duplicate names, revision conflicts/no-ops, status
  0/1/100/101 words, Unicode whitespace/size, same-text review, independent
  revisions and restart/event pagination with original attribution.
- Workspace crash points around marker/publication/projection and SQL
  finalization; missing/mismatched ownership, pre-existing destination,
  symlink substitution, authored edits/deletions on retry, and recovery holds.
  Assert no automatic resource launch and no replay of an ambiguous effect.
- One shared HTTP/MCP flow for the five operations, identical-key replay,
  schema parity, typed revisions/errors, honest accepted/waiting state and
  reserved onbash discovery. Reuse existing server/authentication fixtures.

Existing tests may need exact bootstrap counts/registry expectations adjusted
to include onbash. Preserve their behavioral assertions, busy timeout, and fault
coverage; use baseline-relative counts/explicit target selection rather than
weakening checks. Fixture setup may drive only its synthetic onbash workspace
job to its dependency hold before testing unrelated jobs. Report a demonstrated
unrelated failure; do not alter runner/recorder/adapters or their tests.

Run the owning new suite and affected foundation/transport suites from source.
Build/install the same lock and run one installed agent/HTTP/MCP smoke flow plus
passive imports/schema checks. Additional whole-package repetitions are needed
only for a demonstrated regression or changed dependency. Record failures and
limits, versions, source and wheel commands, final SHA, notice/provenance and
cleanup in `docs/evidence/agents-r1.md`. No usage examples, UI, new dependencies,
system installs, paid model turns, live owner data, VM/network/service changes
or deployment. Commit/push the dedicated branch; stop edits for a fresh review.
