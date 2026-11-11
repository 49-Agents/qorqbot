# Context integration with Operations (`qorqut_context.integration`)

Context's connection to the Operations platform lives in `qorqut_context.integration`, the only part of `qorqut_context` that
imports `qorqut_operations` (install `qorqut-context[integration]`; the base engine needs no Operations). Operations has no
Context import, name or fixture: an Action's revision is admitted through the **required** `on_revision` callable that the
composition hands to `qorqut_operations.actions` (`ActionOperations(..., on_revision=)` and
`settle(ctx, plan, uow, on_revision=)`), and Context supplies `qorqut_context.integration.queue.admit_action_revision`.

The sections below were moved verbatim from the Operations extraction notes together with the code they describe; only the
module names changed.

The ownership map across Operations, this package and the host is the host repository's `docs/v1.3/MODULE-MAP.md`; the table below is
the old-to-new path map for anyone migrating code or notes that still use the former Operations or host module names.

| Was in Operations | Now in Context |
|---|---|
| `qorqut_operations.context_queue` | `qorqut_context.integration.queue` (`ContextQueueOperations`, `enqueue_action`, `admit_action_revision`, driver `transitions`) |
| `qorqut_operations.context_filing` | `qorqut_context.integration.filing` (`FilingOutcomes`, `FilingPorts`, `reserve_source`, `admit`, `envelope`, queue status, publication, source operations, programs) |
| `qorqut_operations.context_graph_edits` | `qorqut_context.integration.graph_edits` (`GraphEdits`, `GraphEditPorts`, `ExecutionFacts`, supervised edit) |
| `qorqut_operations.context_usage` | `qorqut_context.integration.usage` (ledger, collector, operations) |
| host `shared_context` driver, loop, reader, service | `qorqut_context.integration.driver`, `.loop`, `.reading`, `.service` |
| host question store and operations | `qorqut_context.integration.questions` |
| the four storage contributions and their authority, delegation, history and policy declarations | `qorqut_context.integration.registration` |

The host composes `qorqut_context.integration.service.compose_context(application, locations, settings, *, instruct_roles,
coordinator)` and `ContextServices`, supplies explicit `ContextLocations`, and remains the only owner that starts and stops the
services. The storage order is unchanged: `tags -> discussion -> tasks -> actions -> context-queue -> context-filing ->
context-graph-edits -> context-usage -> ...`; the owner labels, DDL, table specs and immutable fields are exactly the ones the
host serialized before the move.

## Queue API

The source filing adapter `FilingOperations(sources, filing, global_upload_agent=...)`
accepts an optional trusted host resolver for Global uploads. The resolver receives
the current unit of work and returns the one currently authorized agent ID, or
`None`. Without it, Global `context_upload` remains owner-only. With it, the
matching agent must still pass ordinary tool policy and `read_global_context`
checks. The identity is checked during preparation and again when validating the
prepared plan. The host resolves its active configured coordinator agent here; the module
does not infer authority from role names. Other filing permissions are unchanged.

Public surface = five registered operations (policy allow/signoff/deny and idempotency as for any v1 operation, in the
caller's own namespace only):

* `context_queue_list` `{limit?, cursor?, state?}` -> `{items:[input], next_cursor, queue_blocked, blocked_input_id}`.
  `queue_blocked` is the only cross-namespace fact; `blocked_input_id` is non-null only for the caller's own input.
* `context_queue_get` `{input_id}` -> `{input: input + payload + attempts[] + controls[]}`. Another namespace's id is
  `not_found`.
* `context_queue_submit` `{operation: add|edit|remove, path, content?, metadata?{kind,title,origin,origin_ref?,created_at?}, rationale?}`
  -> `{input}`. `path` is a `foundation/` text source; `foundation/actions/`, `accepted_action` and
  `controlled_caller` markers are refused; `remove` carries no content or metadata; `content` is bounded by
  `max_content_bytes`. Admission never waits for Context or a model.
* `context_queue_retry` / `context_queue_discard` `{input_id, expected_state, rationale?}` -> `{result:{input_id,
  control_id, intent, status: pending|applied, state}}`. Retry applies to a blocked head. Discard of a blocked head
  becomes `discard_requested` (pending, the driver completes the mechanical discard); a never-started queued input is
  discarded at once. One pending control per input.

`input` = `{id, sequence, input_kind: general|action, operation, item_id, revision, submitted_by, created_at, state,
context_event_id, attempt, scope:{kind:global}|{kind:business,business_id}, result, error, summary}`; `summary` is
`{path}` for general inputs and `{action_ref, revision_ref}` for Action inputs. States: `queued`, `processing`,
`blocked`, `published`, `discard_requested`, `discarded`.

**Internal (not operations).** `queue.enqueue_action(uow, *, action_id, revision, operation, payload) -> input_id`
runs after the immutable revision exists and before commit; a trigger requires the stored revision, the owning
namespace, the operation and the submitter to match, and the payload is a snapshot (`queue.action_payload`: action and
revision refs, operation, submitter, `tags` as `{id,name,business_id}` snapshots, related Context objects, scope,
provenance, text unless removed). `scope` and `provenance` are the published record context; evidence refs and other
business data are not in it. New Action inputs preserve the immutable revision's
`occurred` (UTC anchor, local date/hour, precision and timezone), `refs`, `business_ids`,
`recorded_at` and `withdrawn` in a separate payload member, `action_metadata` (version 1). These are evidence, never
authority or additional graph relationships. The exact base envelope is validated against the stored revision before
enrichment. The composed `on_revision` reader may then add `action_metadata.evidence`: at most one explicitly referenced
Task and one document, observed in the admitting transaction under the registered `tasks_get` / `context_get` read
authority. Task excerpts contain only title, overview and status; document excerpts use the shared source library,
excluding private/agent-owned documents, archived sources and bytes that disagree with their registered hash.

`action_metadata` is model-only. Operations shows evidence refs only inside the owning projects, and an excerpt is read
under one caller's authority, while published Context is shared across projects. So the driver submits it as
`private_context` (see CORE_CONTRACTS.md), which both model stages receive as `source_change.private_context` and no
source document, event or other published file contains; `context_queue_get` shows the payload without it
(`queue.public_payload`). The published source and event of a new Action are byte for byte what an input without
metadata produces, so bootstrap's lexical candidate matching is unchanged by it.

The evidence read holds the Action writer's write transaction, so it is bounded: a Task read is limited by that Task's
own relations, and a document is read through `SourceLibrary.excerpt` (one lookup per selected namespace, the current
bytes and their hash, no heading parse) rather than the registered `context_get` read, which loads the whole library
and parses the whole document.
Each available observation carries its owning business, source revision, content hash (Task title/overview/status
or complete document bytes), observation time and truncation flag. Title and excerpt limits are 1000 and 4000 UTF-8
bytes per source. Missing, denied, ambiguous or stale references get the same generic unavailable result; supplied
reference IDs remain intact. Namespace-local Task IDs prefer the Action's owning scope, then a unique match among
its associated businesses and Global within the caller's grants. No comments, external URLs or models are read.
Removals do not capture fresh excerpts. The queue freezes observations once: processing, retry and recovery use those
same bytes, and existing queued or published filings are not rewritten. An input queued before `action_metadata`
existed has none and submits exactly as before.

The driver uses `transitions` with the active `system` principal
only: `head`, `used_event_ids`, `start_attempt(context_event_id)`, `record_preflight_failure`, `record_published`,
`record_blocked`, `record_discarded`, `record_discard_failure`. They are compare-and-set, keep the single open head,
append the attempt/control histories, enforce the exact 5-lowercase-alphanumeric Context event id (unique, never
reissued) and emit required coordinator notices (Global, source `context_inputs`) for blocked/preflight/published
hand-offs. `record_preflight_failure` blocks the oldest input directly when Context cannot be checked or an ownership check refuses it before any Context work: a queued head goes `queued -> blocked` (the legal transitions include it) and a blocked head restarting on an explicit retry stays blocked; it records one blocked attempt and one notice, with a null event ID for an input that never had one and the original ID for one that did. One `sequence` orders GENERAL and Action inputs together. Source/job completion fields belong to `context_filing` (below); the queue stores only the queue facts above and calls the required
`TerminalOutcomes` contribution when an input becomes final.

### Context source ownership (`context_filing`)

One more storage contribution, `context_filing.storage_contribution()` (owner `context-filing`, requires `context_inputs`;
compose it after `context_queue`), holds the table `context_source_bindings(source_kind general|action, source_key,
business_id NULL=Global, bound_by_input, bound_at)`. A source's key is its canonical GENERAL path or the accepted Action's
key; its scope is the namespace of the input that first added it (for an Action, the namespace of its frozen
`action_revisions` row, never the live Action). Rows are insert-only (update and delete abort) and an insert must name an
admitting input of the same kind and namespace. No registered operation reads or writes the table and no public input
(metadata, `origin_ref`, text, a document or job reference) is read as authority.

`context_filing.reserve_source(uow, *, driver_id, input_id, exists) -> {kind, key, business_id, binding: reserved|verified}`
is internal like `transitions` (`driver_id` must be an active `system` principal) and is called by the backend driver in the
same write transaction that starts the attempt. A bound source is available only to its own scope
(`source_scope_conflict`; the message names no scope). An unbound source is bound only by an `add` of a source that does not
exist in the published Context; every other case fails closed (`source_unbound`): an edit or removal of a source nobody owns
and an add over an existing source nobody owns. A removed path stays reserved to its scope. Reading the shared Context never
authorizes a write.

### Source versions, filings and terminal outcomes (`context_filing`, Context completion)

`context_filing` also owns the completion half of Context filing. It adds three tables (insert-only apart from one
receipt state) and one required queue contribution; no table is a document table and nothing here reads or writes host
documents.

* `context_source_versions(source_kind document|instruction, source_id, source_revision, operation add|edit|remove,
  source_path, source_digest, retracts_revision)`, `UNIQUE(kind, id, revision)`. Revisions are consecutive from 1 per
  `(scope, kind, id)`; a trigger checks the accepted lineage (first version `add`, a later content version `edit`, a `remove`
  names one exact published version) and the scope never changes. The revision is the source's, not a host row revision.
* `context_filings(id, source_version_id, request_id, job_id, program_id, program_version, definition_digest, worker_id,
  initiated_by, input_id)`: one association of a version with its originating request, registered job (program definition
  and worker frozen at admission) and queue input. A trigger requires the job to be a queued job of a registered filing
  program (`context.file`/`context.instruct`) whose arguments are exactly `{"filing_id": id}` and whose principal and
  initiator match; the input foreign key is deferred so the three records are applied in any order in one transaction.
* `context_completions(id, filing_id, job_id, input_id, source_version_id, outcome published|discarded, context_event_id,
  published_commit, reason_json, outcome_digest, state pending|applied|conflict, application_json, revision)`: the
  completion outbox. The receipt id is derived from the filing; the outcome columns are immutable; `applied` is final.

`context_filing.admit(builder, uow, *, kind, source_id, filing_id, job_id, document, event_metadata, retracts=None,
max_content_bytes=None, business_id) -> Admission` is what a filing adapter calls from its own `prepare`, after it prepared
the registered job for this filing in the same builder. It prepares the version, the filing and the queue input as three
records of the adapter's plan, so a source can never be accepted without its job and queue entry (or the reverse). The
authority is the plan: the prepared job must be the program for this filing; a caller's path, filing id, job id or `system`
label grants nothing. The operation comes from the accepted lineage (a discarded earlier add makes a later version an `edit`,
which may block; a retraction is a distinct `remove` of the exact latest published version and a pending version is never
retracted, only discarded). The same bytes as the latest live version are a conflict naming the existing filing
(`details.filing_id`), so a retry reuses its revision, filing, job and input. Controlled paths `foundation/documents/<id>.txt`
and `foundation/instructions/<job id>.txt` are refused to public `context_queue_submit` and by a database trigger unless the input
is its own filing's; `foundation/actions/` stays reserved.

`context_filing.envelope` is the one encoding of a frozen source: canonical UTF-8 JSON
`{"format": "qorqut-source-v1", "body": <exact text>, "metadata": <explicit object>}` (`source_document`, `fingerprint`,
`parse`, `check_size`). The adapter owns which metadata it selects; nothing is truncated or normalized, and a source over the
Context byte limit is refused with `source_too_large` (size, limit and a "split or shorten" instruction).

**Terminal outcomes.** `ContextQueueOperations(outcomes=...)` and `transitions.record_published` / `record_discarded(...,
outcomes=...)` require a `TerminalOutcomes` contribution (`context_queue.outcomes`, checked at construction). The
queue calls `outcomes.record(uow, final_input_row)` in the transaction that makes an input `published` or `discarded`:
the driver's two transitions and an unstarted-input discard control (a queued input discarded before any event). A blocked
attempt is not terminal and records nothing. `context_filing.FilingOutcomes` records, for a filing-bound input only, the
filing's one stable receipt (pending), re-checking the stored payload against the version digest; an identical repeat returns
the same receipt and a different outcome for the filing is `completion_conflict`. `outcomes.record_missing(uow, driver_id=)`
records any final bound input that lacks a receipt (mechanical recovery, atomic, idempotent, no provider run).

**Completion.** `completion.authorize(uow, ctx, receipt_id)` re-resolves the receipt, filing, job, frozen definition, worker
and scope from storage and only accepts the bound job's own claimed worker (`running`/`applying`/`waiting_signoff`, active
worker and membership); a presented receipt of another job, a cancelled or stopped job, a revoked worker or a changed
definition is `forbidden` and stays unapplied. `completion.applied_marker(builder, uow, receipt, application)` is the
fresh-row compare-and-set the host adds to the same prepared plan as its own source bookkeeping.
`completion.verify_pending(uow, driver_id=, programs=)` marks an unapplied receipt `conflict` (`binding_changed`) when its job
is gone or its frozen program definition is no longer registered, and returns it to `pending` when it matches again.
`completion.reconcile_stopped(uow, driver_id=, outcomes=)` is the cancellation seam: for unfinished bound inputs whose job is
`cancel_requested`/`cancelled` it uses the queue's own controls with a system-principal request and the job, its state and
its recorded error as the stored reason evidence (an unstarted input is discarded at once and its receipt recorded; a blocked
head becomes `discard_requested` for the driver's publication check first; a `processing` input and published work are never
touched). The driver is the recorded requester; the human who cancelled is not on the job row and is never invented. The pending
discard freezes the evidence (`reason: job_cancelled`, job ID, job state and recorded error, `requested_by`) in its own
`result_json.request_evidence` when it is created. `transitions.record_discarded` keeps that object and the control ID in the
final control result, the discarded input's result and therefore the terminal receipt's `reason_json`; `record_published`
keeps them on the rejected discard control when publication wins the race (the input stays honestly `published`, with no
request evidence). A control requested without `request_evidence` (an ordinary explicit discard) is unchanged. A retry request
still pending on the cancelled blocked head is rejected in the same reconciliation transaction (`outcome:
superseded_by_cancellation`, the evidence, and the new control's ID) and the discard request is created, so no attempt can start;
both controls keep their own requester. `verify_pending` marks the unapplied receipt of a cancelled job `conflict` (`job_cancelled`): a cancelled job is not a
completion worker and is never revived. No cancellation API or generic executor change is added.
`completion.describe`, `applicable` and `stopped_unfinished` are the read model: `{filing_id, source_revision, input_id, job_id,
state, published_commit, completion_state, source_current, error, queue_position?}`, `state=unfiled` with no identifiers when
the current content has no applicable version, and the unfinished inputs whose job is cancelled (for queue discard).
`source_current` and `applicable` compare the current content fingerprint with the version's frozen digest for every version,
a retraction included: a removal's digest names the exact published content it removes, so changed, missing or archived content
is not current (the document reads unfiled, the removal keeps its real published outcome and its completion does not rewrite
newer bookkeeping) and an explicit retry admits the next version, an `add` after the accepted removal.

### Controlled graph edits (`context_graph_edits`)

A sixth storage contribution, `context_graph_edits.storage_contribution()` (owner `context-graph-edits`, requires
`context_inputs` and `context_input_attempts`), is metadata for the existing queue. It is not another queue, Context input mode
or table of record facts; the engine imports no Context library and never interprets an edit. One immutable row per input:

`context_graph_edits(input_id PK + deferred FK -> context_inputs, reserved_event_id UNIQUE, plan_digest, expected_revision,
plan_json, attribution_json, targets_json)`. The input stays `input_kind="general"`, operation `add`, with the canonical plan
text as its payload content at `foundation/edits/{reserved_event_id}.txt`, the fixed metadata `{kind: graph_edit, title:
"Explicit graph edit", origin: controlled_caller}`, the original `submitted_by` and no other field. Its immutable `business_id`
is the selected owning namespace; nothing is authority here (not the path, the metadata, tags, nor a stored decision).

* `context_graph_edits.admit(builder, uow, *, plan, content, plan_digest, reserved_event_id, attribution, targets, business_id,
  max_content_bytes=None) -> Admission(input_id, reserved_event_id, plan_digest, expected_revision)` prepares the binding first and
  its input second as two records of the host adapter's plan (one transaction; the deferred FK closes the pair). It refuses text that
  is not exactly the JSON plan, a digest that is not the SHA-256 of the text's UTF-8 bytes, a base that is not the plan's
  `expected_revision`, attribution that does not name the builder's caller or that carries a delegation other than the caller's
  own authenticated one, a namespace other than the caller's selected scope and a
  reserved event ID any queue input, attempt or other reservation holds (`conflict`).
* The attribution's `delegation` value is closed and required: `null` for a direct caller, otherwise exactly `{binding_id,
  assignment_id, generation}` (two canonical UUIDs and a positive integer, on an agent principal) copied from the authenticated
  `InvocationContext` by `delegation_of(ctx)`; no credential, hash, lease or decision is ever part of it. `admit`, the database
  (a binding trigger) and every `load` enforce the shape, and `admit` also requires it to equal the builder's context.
* The database refuses: a reserved-path input without its matching binding (the path, `add`, fixed metadata with exactly three
  fields, exactly five payload fields, the same submitter as the attribution, a null event ID and attempt zero); a binding for an
  input that already exists or for a non-GENERAL input; a reservation of an event ID some input or attempt used; an attempt or input
  event ID other than the input's reservation, and any other input carrying a reservation; update or deletion of a binding or of the
  immutable input. The SHA-256 relation is checked in code (SQLite has no hash).
* `context_graph_edits.load(uow, input_id)` is the one read: a verified binding (`plan`, `content`, `attribution`, `targets`, the
  digest, base and reserved ID) or `None` for an ordinary input. An input on the reserved path with no binding (`graph_edit_unbound`)
  or a payload/digest/submitter/base mismatch (`graph_edit_integrity`) is never treated as ordinary.
  `reserved_event_ids(uow)` / `taken_event_ids(uow)` give the host driver and preparation the IDs to exclude.
  The contribution is required by the Context queue: a database without `context_graph_edits` makes `load`, `reserved_event_ids`
  and `ExecutionFacts` raise `composition_error`; nothing treats the input as ordinary or the reservations as empty, and there is
  no probe or optional flag.
* The queue's public path refuses `foundation/edits/` for add/edit/remove (it is in `CONTROLLED_PREFIXES`); `_metadata` is not
  relaxed, so the fixed `controlled_caller` metadata is still refused for every public caller. An ordinary source whose text happens
  to look like an edit stays ordinary text.
* The queue DTO gains `execution` (null for an ordinary input): `{kind: "deterministic_graph_edit", plan_digest,
  expected_revision, reserved_event_id}`, on `context_queue_list`, `context_queue_get` and `context_queue_submit`. It comes from the
  `executions=` contribution of `ContextQueueOperations` (`context_graph_edits.ExecutionFacts()`); without it a stored graph-edit
  input is refused (`composition_error`) rather than shown as ordinary. `transitions` return the unchanged internal view.
* The input's `context_event_id` stays null and `attempt` 0 until its first real attempt, so an unstarted edit discards immediately
  with no Context event. The first attempt carries exactly the reserved ID. Existing retry/discard controls, sequence order and head
  blocking are the only controls; the filing outcome port records nothing for an edit (no filing, job or completion).

## Context model-call usage ledger

An engine-only component: it stores the per-call usage receipts the Context library produces and answers bounded read-only
questions about them. It is not the finished installed costs feature: no collector, catalog endpoint, owner view, thread,
scheduler, background program, retry or output operation exists here, and the contribution is composed only by a host that
adds `context_usage.storage_contribution()` (the installed host does not). The package never imports the Context library.

### Read-only library contract

`Pipeline.usage_receipts(event_id)` items of format `qorqut-context-call-v1`, exactly as in the Context library's `CORE_CONTRACTS.md` "Model-call usage receipts": `call_id`
(`<32 hex attempt_id>:<stage>`), `event_id`, `attempt_id`, `attempt_sequence` (the library's own 1-based number, never the
queue attempt number), `stage` (`bootstrap|propagation`), `state` (`started|completed|failed`), microsecond UTC
`started_at`/`finished_at`, `configured{model,effort,service_tier}`, `observed{provider,model,thread_id,turn_id}`,
`usage{five token counters, cost_usd}` (null is unknown), `evidence{usage_source, usage_scope, usage_updates,
ignored_updates, usage_observed, counters_complete, usage_complete (always false), cost_observed, limitations}`,
`bootstrap_call_id` (propagation only) and `error{type,message}` (failed only). An optional `format` key is accepted when it
equals the documented marker. Cumulative snapshots replace, never add.

### Storage (`storage_contribution()`, owner `context-usage`, requires `context_inputs` and `context_input_attempts`)

* `context_usage_calls`: one row per library `call_id` (primary key, `CHECK call_id = attempt_id || ':' || stage`), with
  `business_id` (NULL is Global, always copied from the queue input), `input_id`, `context_event_id`, `attempt_id`,
  `library_attempt_sequence`, `stage`, `state`, `started_at`, `finished_at`, `configured_*`, `observed_provider/model`,
  `native_thread_id/turn_id`, the five token counters (NULL unknown), `cost_usd` (canonical decimal text, at most 40
  characters, so it sums exactly), `usage_source/scope/updates`, `ignored_updates`, `bootstrap_call_id`, `error_type/message`,
  `recorded_at`, `updated_at`. `CHECK`s hold the closed vocabularies, the state/finish/error consistency and that a native
  scope names both a thread and a turn. No prompt, source, tool-payload or credential column exists.
* `context_usage_sync`: one row per (`input_id`, `context_event_id`): `business_id`, `status`
  (`partial|ready|unavailable`), bounded `error_code/message` (both or neither; `ready` has none, `unavailable` has one) and
  `checked_at`.
* Triggers: a call or sync row must carry its input's namespace and a Context event a queue attempt of that input
  reserved; identity columns are immutable; a `completed`/`failed` call is final; a non-null native thread/turn cannot be
  reassigned; update counters cannot decrease; a bootstrap call and every call that names it belong to one input and
  Context event (an index on `bootstrap_call_id` serves the lookup); calls and sync rows are never deleted.

### Typed API (internal calls inside the caller's transaction; none is a registered operation)

* `ingest(uow, *, driver_id, input_id, receipts) -> IngestResult(inserted, updated, unchanged, stale, calls)`. `driver_id` must
  be an active registered `system` principal (`forbidden` otherwise); the input must exist (`not_found`). At most 500
  receipts (a list or tuple of closed objects); the whole batch is parsed (`parse_batch`, `validation_error` with path-named
  `field_errors`, bad booleans/negatives/NaN/infinite/oversize cost/flag or limitation drift/extra keys refused) and
  classified before anything is written, so any refusal leaves nothing behind. A receipt's event must be one the input's queue
  attempts reserved. A bootstrap call ID belongs to the single (input, event) that its stored bootstrap call, its stored
  references (an indexed lookup by `bootstrap_call_id`) or the incoming batch already evidence, in either arrival order:
  a reference to a call of another input or event, a second unresolved reference from another input or event, and a
  later bootstrap that other inputs' or events' references already name are `validation_error` (`receipts[i].call_id` /
  `receipts[i].bootstrap_call_id`) before any write. A reference may arrive before its bootstrap (no row is invented), a
  late bootstrap of the same input and event is accepted, and a bootstrap reused across library attempts stays one call.
  Classification per call: new -> `inserted`; same observation -> `unchanged`; later observation of a `started` call
  (cumulative counters, cost, observed model and native identity replace as a whole) or its terminal state -> `updated`;
  older observation (a `started` snapshot of a final call, fewer usage or ignored updates, a lost native or observed
  identity) -> `stale`, changing nothing. Refusals: `usage_identity_conflict` (another input, event, attempt, sequence, stage,
  start, configuration or bootstrap reference for that `call_id`; a reassigned native thread/turn), `usage_outcome_conflict`
  (a different terminal outcome, or changed content of a final call), `usage_evidence_conflict` (one update count carrying
  different counters or cost).
* `record_scan(uow, *, driver_id, input_id, context_event_id, status, error=None) -> ScanRecord`: the collector's latest
  truthful status per input and event, system-only, upserted, never deleting or changing calls.
* `list_calls(uow, *, input_id=None, start_date=None, end_date=None, limit=50, cursor=None) -> UsagePage(calls, next_cursor,
  aggregates)`: `limit` 1-100; dates are inclusive UTC `YYYY-MM-DD` compared with `started_at`; ordering is `started_at`
  then `call_id`; the opaque cursor is bound to every filter and the limit and only a token this module issued is accepted.
  `aggregates(uow, *, input_id, start_date, end_date) -> UsageAggregates(calls, unfinished_calls, unknown_cost_calls,
  calls_without_token_usage, known_cost_usd, events, events_not_ready)` is computed in SQL over the whole filtered set (not the
  page); `known_cost_usd` is an exact `Decimal` subtotal, `None` when no call reports a cost. `sync_records(uow, *, input_id)`
  returns the scan records. `UsageCall` carries the flat call facts, the joined `sync` (`None` before any scan) and `as_dict()`.
  These helpers return private facts (native IDs, bounded error text) for the later owner-only adapter and read only the
  operational database.
* Not covered by the aggregates: a missing or `ready` sync record never proves that no other call or cost exists.

### Checks

`tests/integration/test_context_usage.py` covers the explicit contribution, ingest, replay, failed and unfinished calls, identity and
evidence validation, exact cost sums, sync status, pagination and database guards. `tests/integration/test_context_usage_contract.py`
runs against this repository's own library (it needs no sibling checkout), emits
genuine receipts from a small injected-runner pipeline with no model, and imports them into disposable queue inputs. An
unavailable checkout fails the checks rather than skipping them.

### What remains and limitations

* Host work: the collector (bounded traversal of `usage_receipts` pages, per-input serialization, `record_scan` bookkeeping),
  the owner-only cost views and adapters, catalog/operation exposure and the final composition of the contribution.
* Query helpers have no business/Global filter (rows carry `business_id`); costs are limited to non-negative values of at most
  1e9 with at most 40 decimal characters (stored and rendered money is the exact coefficient and exponent, independent of
  the ambient decimal context; the aggregate subtotal is not limited to 40 characters); a terminal row is final, so any differing terminal observation is a conflict, and an
  out-of-order terminal with fewer updates is reported stale.
