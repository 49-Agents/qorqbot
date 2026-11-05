# Shared Context writer checkpoint (P4B): the ordered Context queue driver and its source ownership

This is a **driver and its isolated proof, not the finished Context writer**: it is not wired into the service, it replaces no old writer, and no pin, manifest, receipt or lifecycle step was advanced. Only synthetic repositories, a disposable SQLite database and an injected fake model runner ran. No operational Context, business file, transcript, registration, credential, model, provider or network was used, and no user instance, native agent or server was started.

## Components and their exact dependencies

| Piece | Where | Needs |
| --- | --- | --- |
| Queue driver | `app/bazarlyk/shared_context/driver.py`: `ContextQueueDriver` | Operations at `5e856de` (`context_queue.queue`/`transitions`) **plus** the `queued -> blocked` correction (`5faa01e`) and the `context_filing` contribution (`e008efd`); Context library `2e59b525ddc0cedba3bc342d901520b0643a7c54` (`record_context` on `begin`/`begin_action`/`Pipeline.submit*`); `bazarlyk.management.identity.configured_ordubashy` |
| Bounded loop | `app/bazarlyk/shared_context/loop.py`: `ContextDriverLoop` | the driver |
| Source ownership | Operations `qorqut_operations/context_filing/` | `context_inputs`; composed after `context_queue` |
| Isolated fixture | `app/tests/test_shared_context_driver.py` | the Operations synthetic host (`tests/operational_support.py`, path in `QORQUT_OPERATIONS_TESTS`) and a library with `record_context`; the module skips otherwise |

`bazarlyk.shared_context.__init__` deliberately does **not** import the driver or the loop. The driver imports `qorqut_operations.context_filing` and the queue transitions, which the current foundation pin (`138f40c`) does not carry; composition imports `bazarlyk.shared_context.driver` and `.loop` explicitly once the Operations pin carries both. The P3 binding of the host (`0943145`) is not merged here, so this was proven against the real Operations queue and unit of work, not a combined installed host.

## Operations: `context_filing` (source ownership)

`context_filing.storage_contribution()` (owner `context-filing`, `required_tables={"context_inputs"}`) adds `context_source_bindings(source_kind, source_key, business_id, bound_by_input, bound_at)`. A GENERAL source's key is its canonical path; an Action's key is its `action_key`. The scope is the admitting input's namespace (for an Action the frozen `action_revisions` row's). Rows are insert-only. Internal call, same `system`-principal rule as `transitions`:

```
context_filing.reserve_source(uow, *, driver_id, input_id, exists) -> {kind, key, business_id, binding: "reserved" | "verified"}
# DomainError: source_scope_conflict | source_unbound
```

Only an `add` of a source that does not yet exist binds; a bound source serves only its own scope; an unowned source cannot be edited, removed or added over; a removed path stays reserved. Nothing public writes it, and reading the shared Context never authorizes a write (all agents may read; filing is by input scope). Composition adds `context_filing.storage_contribution()` next to `context_queue.storage_contribution()`; nothing else is needed from the host.

## The driver

```
ContextQueueDriver(database, context, pipeline, *, driver_id, id_source=None)
    process_next() -> dict     # advance the oldest unfinished input by one step (the only call that can start a model)
    recover() -> dict          # mechanical reconciliation: never starts a model or a new attempt
    coordinator() -> str|None  # configured_ordubashy(uow, require_codex=False), resolved each time
    diagnostics() -> dict      # driver id, coordinator readiness, head summary and the last result
```

`database` is the Operations database, `context` a `qorqut_context.Context`, `pipeline` a `qorqut_context.Pipeline` over that same Context (it carries the only runner ever used), `driver_id` the active registered `system` principal. `id_source` exists only for deterministic collision checks. One driver per Context across processes: a lock file `context-queue-driver.lock` in `context.runtime` (non-blocking `flock`); a rival step returns `busy`.

**Results (`status`).** `idle`; `busy` (`reason` `driver_locked`, `context_busy` — a transient Context writer conflict — or `context_unavailable`; no attempt or failure is invented); `blocked` (head blocked, no retry requested: later inputs wait, no model); `unchanged` (`recover` only: nothing to reconcile); `not_ready` (`no_coordinator` or `coordinator_ineligible`; with `pending_recovery: true` plus `context_event_id`, `published_commit`, `handoff_note` when a publication awaits its record); `published`; `input_blocked` (with `error {code, message, phase?, retryable}`); `unrecorded` (published but SQLite recording failed: the head stays `processing` and the next step records it); `discarded` (`context` is `draft_discarded`, `no_draft`, or `foreign_event_id`); `discard_failed`.

**One step of `process_next`** (the head is the oldest input that is neither published nor discarded):

| Head state | Step |
| --- | --- |
| `processing` | The lock proves no live driver: look up the exact event ID in the published Context, **without any runner call**. Published: verify and record. Not published: record the attempt blocked (`interrupted`) for explicit retry or discard. |
| `discard_requested` | Complete the discard mechanically (below). |
| `blocked`, no retry pending | `blocked`; nothing else moves. |
| `queued`, or `blocked` with a pending retry | Run one attempt. |

**An attempt.** (1) Resolve the coordinator; none means `not_ready` and nothing starts. (2) Read Context outside SQLite: the snapshot, the pending draft, whether the source exists, and the event ID: the input's saved ID if it is still this exact input's (published or its own pending draft) or unused, else a fresh five-character ID not in the queue's attempt history, the published history or the pending reservation. (3) One transaction: `context_filing.reserve_source` then `transitions.start_attempt(context_event_id)`. An ownership refusal is the queue's `record_preflight_failure` in that same transaction: no source binding, no event ID, no attempt that started, and Context and the model are never reached. (4) Outside any SQLite transaction: `pipeline.submit` (GENERAL) or `pipeline.submit_action` with the frozen input. (5) Verify the published event against the frozen input and record it once with `transitions.record_published`; a failed submission first looks up the exact event ID: a publication that raised afterwards is recorded, never blocked; a lookup that cannot read Context proves nothing, so the `processing` attempt and its reserved event stay untouched (`unrecorded`, `context_unavailable`, no blocked notice, no new attempt) until a later step can read Context; only a successful lookup that finds nothing records `record_blocked`.

**What is frozen and passed to Context.** GENERAL: the stored `operation`, `path`, content as the exact UTF-8 bytes, stored metadata, `submitted_by`. Action: `action_ref`, `revision_ref`, `operation`, text (none for removal), `related_objects`, `tags` exactly the frozen `{id, name, business_id}` snapshots (`business_id` null; scope is separate), `submitted_by`. Edit and removal are submitted as edit and removal; a missing prior add is a blocked input (Context's own refusal), never an inferred add. `record_context` is `{scope, provenance}`: for an Action the revision's own scope and provenance from the frozen payload; for GENERAL the input's namespace with `{input_id, submitted_by, queued_at}` taken from the queue row (the caller's metadata, including `origin_ref`, stays source metadata and is not copied into controlled evidence). It is a pure function of the stored input (`record_context_of`).

**Verification before `record_published`.** The event under the exact ID must be published at the commit that Context reports and must match the input: `input_mode`, source path or `action_ref`/`revision_ref`, `operation`, `submitted_by`, content digest (GENERAL), normalized tags and pointers (Action), `record_context`, plus Context's own exact submission-digest check (`begin` replay), and an explicit handoff disposition (`prepared` with text or `empty` without). The same ID published with different evidence is a conflict (`event_conflict`, block for explicit retry; a retry takes a fresh ID); a foreign event is never touched, rewritten or recorded. The stored receipt is bounded (event, commit, source, base, mode, counts, model name, handoff disposition, `recovered`): no runtime path, transcript or diagnostics path.

**Crash and recording failures.** Everything between saving the ID and `record_published` is repairable by looking up the exact event: a crash after real Git publication (before or after Context's own cleanup) is repaired by `recover()` or the next `process_next()` with zero model calls; Context's own pending record is completed mechanically (`pipeline.recover()`) before the queue records, so the next input is never blocked by a stale pending job. A SQLite failure while recording, or an unreadable Context while looking for the publication, leaves the head `processing`, never `blocked`.

**Context cannot be checked before an attempt.** The snapshot, pending-draft and event-ID checks run before anything starts. A failure there (other than a transient writer conflict, which is `busy` with no invented attempt) is the queue's `record_preflight_failure`: a queued head goes `queued -> blocked`, a blocked head restarting on an explicit retry stays blocked; one blocked attempt and one notice are recorded, the event ID is null for an input that never had one and the original ID for one that did, and no model or source binding results. An explicit retry or discard follows as for any blocked head (a head with no event ID is discarded with `record_discarded`, `no_event`).

**Retry and discard.** Retry and discard are the queue's explicit controls (`context_queue_retry`, `context_queue_discard`); the driver never retries on its own. A retry reuses the same event ID when the event is absent or its own pending draft (Context resumes the draft; a ready record publishes without a model). A discard: no event ID or no draft in Context means `record_discarded` with no Context change; the input's own pending draft is discarded with `pipeline.discard`; an event under that ID that is not this input is left alone; **an already published event is never discarded** — the request closes as published (the queue records the discard control as rejected).

**Handoff.** Only the committed `handoff_note` of the published event creates the queue's normal coordinator notice, once (`record_published` is final). An explicit empty note is recorded as such and creates no notice; a draft note staged by a run that later fails or is interrupted never notifies and is not in the blocked notice. A delivery to the coordinator is a queue notice, not an operational Action.

**Coordinator.** Resolved from the registration each time (`configured_ordubashy(uow, require_codex=False)`, any runtime). Absent before an attempt: `not_ready`, no attempt, no model. Lost after publication: the exact publication stays pending (`not_ready`, `pending_recovery`) until a coordinator can receive the notice; propagation is never rerun to repair notification bookkeeping, and no notice is fabricated. An empty handoff records without a coordinator because nobody is notified. A failed attempt with no coordinator stays `processing` (the blocked notice has no recipient) and is blocked on the first step that has one.

## The loop

```
ContextDriverLoop(driver, *, poll_seconds=2.0, max_backoff_seconds=60.0)
    start() -> dict          # runs driver.recover() once (no model), then one daemon thread repeats process_next(); idempotent; returns the recovery result
    wake() -> None           # new input was admitted or a control applied: look now instead of at the next tick
    stop(timeout=5.0) -> {"stopped": bool, "in_flight": bool}
    running, diagnostics()
```

`stop` cannot cancel a model run in flight: it reports `in_flight: true` and the next `start()` reconciles whatever the interrupted process left (`start()` while a stopping thread is still running answers `stopping` and starts nothing). **Lifetime contract for final wiring:** while `stop` reports `in_flight`, a writer (a Context draft, a Git publication or a SQLite recording) is still running, so the database and the Context must stay open until a later `stop` reports `stopped`; a timeout does not make closing them safe, and neither the driver nor the loop closes anything. Only `published`, `discarded` and `input_blocked` steps are followed immediately by another step. A step that leaves the same input where it was (`discard_failed`, `unrecorded`) or raises waits with bounded exponential backoff (doubling from `poll_seconds`, never above `max_backoff_seconds`; the exponent itself stops growing at the ceiling, so a sustained failure cannot spin or overflow); `wake()` and `stop()` interrupt any wait. No timer authorizes a model retry: a blocked head still waits for its explicit retry.

## Exact bindings for composition (P4C / P5 / H1)

1. Add `context_filing.storage_contribution()` beside the other Operations storage contributions (after `context_queue`).
2. Construct once per instance: `context = Context(paths.context, paths.context_runtime)` of an already initialized graph (reads and the loop never initialize a missing production Context; instance initialization is the separately planned explicit composition step), `pipeline = Pipeline(context, runner=..., config=...)` with the product's runner, `driver = ContextQueueDriver(database, context, pipeline, driver_id=<the backend system principal>)`, `loop = ContextDriverLoop(driver)`.
3. Start: `loop.start()` after the database is migrated and the service holds its writer admission; stop: `loop.stop()` at shutdown. Treat `in_flight: true` as an honest report that a writer is still running: keep the database and the Context open and alive until `stop` reports `stopped`, and do not claim a timeout makes closing them safe (the contract the final lifetime wiring must honor). After admitting an input or applying a retry/discard call `loop.wake()` (optional; the poll covers it).
4. The reader (`SharedContextReader`) and the driver must use the same `paths.context` and `paths.context_runtime`; the reader keeps reading only the published graph.
5. The host must keep exactly one `ContextQueueDriver` per Context (the lock enforces it across processes) and must not add a second writer: the old `ContextBaseService` queue and filing paths are replaced, not run beside this driver.
6. `DocumentOperations` should get an explicit typed Context dependency at final composition; it still tolerates the plain `documents.shared_context` attribute set by P4A.

## Decisions

1. **Driver-time ownership enforcement is sufficient for P4B.** `context_queue_submit` is unchanged: a conflicting admission may enter the queue and then block visibly (`source_scope_conflict` / `source_unbound`) until an explicit discard. It authorizes no source mutation. P4C's typed document/job admission verifies its own source and filing association separately; the public admission contract is not expanded here, and no ownership is inferred from a caller's metadata.
2. **GENERAL `record_context.provenance`** is the frozen queue facts `{input_id, submitted_by, queued_at}`; `origin_ref` and other caller metadata stay source evidence, never authority. Action scope and provenance are exactly the frozen accepted revision. `record_context_of` is the single derivation point and is part of the submission digest.
3. **Preflight uses the queue's own transition.** The queue schema now permits `queued -> blocked` (Operations `5faa01e`, a target-schema change, not a migration), so `record_preflight_failure` blocks a refused first attempt or an explicitly retried one. `system`-principal, oldest-head, explicit-retry and one-notice rules are unchanged. No event ID is manufactured and no attempt passes through `processing`.
4. **An unavailable publication lookup is not evidence of absence.** The processing attempt and reserved event are preserved and mechanically reconciled when Context is readable; only a successful lookup that finds nothing may lead to a blocked unpublished attempt.
5. **A failed discard is not forward progress** and the loop backs off (bounded); no timer authorizes a model retry.
6. `expected_revision` and `expected_previous_commit` are not used: ordinary ordered source and Action inputs need no new base field here, edit and removal never become adds, and deterministic graph edits (P4C) will use `expected_revision`.
7. An empty committed handoff needs no recipient; a missing coordinator before new work stays `not_ready`, and loss after publication preserves mechanical recovery.
8. The library's `global_only` Action history filter is based on linked business tags and must not be read as owning scope; any later owning-scope filter must use the frozen `record_context.scope`.
9. A conditional fixture skip is not a passing gate. The fixture module skips when `QORQUT_OPERATIONS_TESTS`, `context_filing` or the library's `record_context` is missing; the final installed validation must assert that these cases execute with the declared packages. That limitation stays explicit until then.

## Not done here (mandatory next work: P4C / P5 / H1)

- The document and job completion adapter with its own source/filing association check at admission, usage and cost diagnostics, and the deterministic `context_edit` admission (typed edit verbs, using `expected_revision`).
- Deleting the old writers: `ContextBaseService` and `domains.context_base`, `background/contextbase.py` and `background/integration.py`, `context_instruct`/`context_edit`, filing receipts and costs (`context_filings`, `context_upload_map`, `context_filing_costs`, `domains/costs.py`, `domains/platform_actions/delivery.py`), `ideas.context_base`, `business_setup.context_exists`, the learning and maintenance writers (`nightly_maintenance...context_base_path`, `maintenance/history_baseline.py`), the mixed `legacy_read` compatibility in `domains/documents/operations.py` and the global `operations/context/_people`, `_inbox`, `_dumps`, `_maps` writers.
- The final writer switch also removes the global `context/` document-root allowance in `documents/library.py` (`eligible()`), which is still there for those writers.
- Cost UI, backup capture of the Context repository and runtime, the dependency manifest (declaring the accepted Context pin `2e59b52…` and the Operations pin that carries `context_filing`), lifecycle `close()`/start/stop wiring, and any migration preparation (starts only after the whole code milestone).
- The legacy tests listed in `SHARED_CONTEXT_READS.md` that do not collect in this layout still could not run.

## Focused checks run

Environment: the P4A disposable virtual environment (Python 3.12.3) with `PYTHONPATH` placing an independent clone of Operations at `5faa01e` and of the Context library at `2e59b525ddc0cedba3bc342d901520b0643a7c54` ahead of the installed pins, the template clone for the reader cases, and `QORQUT_OPERATIONS_TESTS` naming the Operations tests directory. Run from `app/`:

```
python -m pytest tests/test_shared_context_driver.py -p no:cacheprovider
python -m pytest tests/test_shared_context_operations.py -k "pillar or navigation or owner_bootstrap" -p no:cacheprovider
```

The driver fixture (35 cases) covers: ordered GENERAL, Action add, correction and withdrawal from frozen input with the exact bytes, tags and `record_context`; Global scope; a blocked head holding later inputs while Tasks and Actions keep working; explicit retry and discard (including a draft discard and no published-event discard); cross-scope, unowned and Action-edit-after-discarded-add refusals before Context or a model, with the refusal's explicit retry and discard, null event ID, no binding and one notice per attempt; a Context that cannot be checked blocking a first attempt with no event and a retry keeping the prior event ID; an unreadable publication lookup after a really published submit raising (no false failure notice, no second model call, recovered later); event ID reuse and a same-ID/different-evidence conflict; a crash after real Git publication in both Pipeline and Context seams with zero second model calls; a SQLite recording failure; an interrupted unpublished attempt; committed, draft and empty handoffs; the coordinator absent, lost after publication and lost with an empty handoff; and the loop (recovery first, ordered processing, wake, stop, in-flight stop, a rival driver, repeated `discard_failed`/`unrecorded`/exception steps that back off instead of spinning and still stop, the capped exponent, and an interrupted long wait). The Operations cases are `tests/test_context_filing.py` (9 cases) and `tests/test_context_queue.py` (16 cases, including first-queued-head and retry preflight failure and discard). The six reader presentation cases pass. No broad suite was run.
