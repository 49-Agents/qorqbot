# Hook simplification: findings, decisions, and implementation plan

Reviewed November 9, 2026. Implementation branch: `codex/hook-simplification`.
Baseline: `4d88ab1cf6c688f304fa68a843fb49515073d99f` (current main when work began).
The investigation inspected the running `c1aea4d` build. The newer connector
approval hook on main is part of the baseline and must retain its behavior.

## Decision

Keep five of the nine findings, grouped into four changes. Prefer removing work
and narrowing existing responsibilities over adding infrastructure. Implementation
and synthetic checks happen in an isolated worktree; installing a release and
changing live service configuration are separate work.

| Finding | Decision | Smallest justified change |
| --- | --- | --- |
| 1. Heavy imports on every hook | Fix in A | Stop importing preparation when the bootstrap package is imported; make preparation consumers explicit. |
| 2. Reporting and attention calls exceed the hook budget | Fix in A | Move report replay to the existing host supervisor; limit optional attention requests to a shared remaining budget and keep network waits out of the activity lock. |
| 3. One hook reconciles every attention board | Fix in B | Reconcile the requesting board and its expired claims. Preserve complete background reconciliation. |
| 4. Todo add reads a whole board inside its write transaction | Fix in C | Return a membership/revision receipt, retaining necessary agent guidance and idempotency. |
| 5. Configuration work precedes cache lookup | Defer | File inventories enforce consistency for directly edited instructions. Measure remaining cost before changing invalidation. |
| 6. Global scheduling/admission redesign | Exclude for now | Concurrency limits already exist. Remove accidental work before introducing another scheduler or cancellation protocol. |
| 7. Host resource isolation | Operational follow-up | CPU contention was real, but it does not establish the need for new application resource-management code. |
| 8. Process pins accumulate after exit | Fix in D | Retire only ordinary pins proven exited by the existing lifetime classifier, in background reference maintenance. |
| 9. Additional telemetry | Include in verification | Use focused measurements and behavioral tests, not a tracing platform. |

## Evidence and limits

- Ordinary activity hooks have a three-second outer timeout. The bootstrap
  package eagerly imported preparation. No-op module-entry samples took about
  0.65–0.88 seconds on the contended host; an import trace attributed about
  0.79 seconds to preparation. These are observations, not fixed latency costs.
- Activity recording replayed diagnostic reports while holding the session's
  activity lock. Report replay has a two-second batch budget and per-request
  waits; attention additionally used separate one-second claim, begin, and
  settle requests. Their independent budgets did not fit the outer timeout.
  The report outbox was empty in one inspection, so replay backlog is not proven
  to have caused every timeout.
- `AttentionRuntime.hook_claim` called the all-board `reconcile` inside a write
  transaction. Per-board reconciliation and background delivery already exist.
- `Attention.add_result` read the selected board before the `todo_add` transaction
  committed. One observed board had about 60 memberships. The dashboard's add
  action checks the response envelope but does not render a returned board.
- Database-busy logs establish contention. The reported transaction "hold"
  includes finalizers, commit, publication, and release; a roughly 61-second Todo
  span is **not** a measurement of 61 seconds holding an exclusive write lock.
- Configuration medians previously quoted came from slow-call logs, not all
  calls. An isolated inventory of 363 instruction files took about 93 ms and
  cannot account for the whole multi-second configuration span.
- The instance had about 150,600 process-pin JSON files. All 200 ordinary pins in
  a sample were classified exited by the authoritative lifetime check. The
  target reference probe covers at most 1,000 records. This proves a retention
  and coverage defect; its contribution to hook latency remains unisolated.
- Host CPU pressure was high. These code changes cannot guarantee three-second
  completion under arbitrary process starvation. Do not treat a full swap
  allocation, by itself, as evidence of active swapping.

## Implementation sequence and acceptance

### A. Simplify the existing hook path

1. Make `bootstrap/__init__.py` lightweight and import preparation at its actual
   consumer. Check the real module entry, including later context imports.
2. Give the existing host supervisor one non-overlapping background report pass.
   Discover registrations from its existing supervisor/recovery records, dedupe
   workspaces, and include idle/stopped sessions. Reuse `replay_http`, credentials,
   record validation, exact rejection quarantine, and durable acknowledgments.
   One broken registration must not prevent later registrations from draining.
3. Remove every synchronous report replay from activity/checkpoint hooks. Local
   observations and recovery markers remain durable.
4. Share a monotonic remaining-time budget across optional attention requests,
   reserving time for output and the local receipt. An expired budget skips
   optional delivery. A begun emission retains its uncertainty/receipt semantics;
   failure to settle cannot produce a second output.
5. Keep network requests outside the activity-state lock. Completion checks must
   spend their durable receipt before requesting a correction and must not
   overwrite a newer activity observation when their response arrives.

Acceptance: real no-op entry does not import preparation; ordinary activity can
be recorded with an unavailable service; concurrent hooks preserve monotonic
state; report delivery needs no subsequent user prompt; slow attention requests
share a budget; exact context/attention and completion receipts still work.
The separate connector-approval hook retains its own timeout and fail-closed
behavior.

### B. Scope attention checks to the requesting agent

1. Add an optional agent filter to expired hook-claim recovery.
2. Use that filter and `reconcile_board` from `hook_claim` after existing identity,
   registration, and enrollment validation.
3. Keep all-board background reconciliation, receipt recovery, and idle delivery.

Acceptance: unrelated agents are untouched by a hook; expired unstarted claims
return to pending, begun ones become uncertain, and durable receipts settle only
the exact claim. Background passes still cover every enrolled board.

### C. Return small Todo add receipts

Use one result shape for `todo_add` and `attention_add`: `agent_id`,
`membership_id`, `membership_revision`, the affected `task` reference, and board `revision`, plus existing
agent guidance when applicable. Both newly added and already-present membership
requests return that receipt. Saved idempotency receipts remain replayable as
stored; no board-return compatibility mode is introduced. Callers needing the
board use `todo_get`. Audit repository consumers and document the response.

Acceptance: mutation never calls the full-board reader; duplicate membership
does not duplicate membership events or bump revision; authorization, expected
revision, and saved idempotent replay remain intact. Unrelated board tasks are
not loaded during receipt construction.

### D. Retire only proven-dead process pins

Use the supervisor's existing periodic reference-maintenance pass. Reuse
`process_state`, deployment exclusion, and the reference-record lock. Skip cached
client records, symlinks, malformed/oversized records, and foreign or uncertain
lifetimes. Remove only verified-exited ordinary pin files; never release
artifacts or cached-session references. Keep maintenance separate from launches
and avoid holding a deployment lock across a historical-directory scan.

Acceptance: live/unknown/foreign/cached pins remain; exact exited and prior-boot
ordinary pins retire; a concurrent launch/reference update is protected; repeated
passes make progress without adding a directory scan to a hook. A reference
probe remains conservatively incomplete if retained references exceed coverage.

## Verification and scope guard

Use synthetic registrations, local HTTP stubs, and temporary SQLite/filesystem
fixtures. Run focused existing hook/connector/context and attention tests plus
regressions for these boundaries. Compare actual hook entry before/after under
the same workload; deterministic work-count checks are more useful than fragile
wall-clock thresholds on a loaded host. Record commands and results below.

No new queue service, cache layer, event bus, resource scheduler, pin database,
SQLite replacement, broad timeout increase, or removal of consistency checks.
Reopen configuration or admission redesign only after representative measurements
show a specific remaining problem. Deployment and live-load verification remain
explicitly separate from local implementation checks.

## Implementation log

- Plan recorded before code changes in `513dc02`.
- **A implemented — `8ca814e`.** Preparation is now an explicit consumer import.
  The existing supervisor runs one report replay pass at a time, at five-second
  intervals when its previous pass has finished. It uses the existing scoped
  HTTP ingestion and durable outbox. A failed first bootstrap retains its
  recovery pointer while reports remain queued, even after its native process
  exits. All activity-hook report replays are removed. Attention requests share
  a two-second budget measured from hook `main`, including recording/context
  work, with a small output reserve. Completion checks spend their receipt under
  the activity lock, release it for HTTP, and only publish a result if the
  activity sequence is still current.
- **B implemented — `81be07c`.** Hook claims recover expired claims for their
  own agent and reconcile that board. The background all-board pass remains.
- **C implemented — `5114038`.** New and already-present memberships return the
  same small receipt shape. Membership revision is included so a caller can
  subsequently remove that exact membership. Existing agent guidance is retained,
  with one instruction lookup. The dashboard consumer already ignores the board
  payload. Existing idempotency storage and replay are unchanged.
- **D implemented — `7a4bfbb`.** The existing background reference pass opts into
  retirement; other callers do not. Each candidate is reread and classified
  under deployment and reference-record exclusion. A busy deployment lock skips
  that candidate without waiting. The directory scan holds no deployment lock.
  The installed launcher's ordinary pins omit `cached_client`, so an absent
  flag is accepted only with valid ordinary-pin fields and proven-exited lifetime;
  explicit cached, malformed, foreign, and uncertain records remain. No release
  artifact or cached-client record is removed. Initial backlog processing still
  uses the existing full directory scan, now followed by a smaller retained set.

### Local verification

Python 3.12 checks ran with installed dependencies and these worktree source
roots on `PYTHONPATH`: `qorqut-code/app`, `qorqut-code/qorqut-operations`,
`qorqut-code/qorqut-context`, `qorqut-agentic/src`, and
`dependencies/document-history`.

- **153 selected Python tests: 141 passed, 12 skipped.** The skips are the Zsh
  shell-scope cases because Zsh is unavailable on this host. The selection is
  `test_hook_simplification`, `test_attention_simplification`,
  `test_process_pin_retirement`, `test_connector_gate`,
  `test_response_style_hook`, `test_agent_shell_scope`,
  `test_delegated_claude_runtime`, `test_ordubashy_any_runtime`,
  `test_mcp_bootstrap_query`, `test_compaction_provenance`, `test_qorqbot_guide`,
  and `test_browser_cleanup_defaults`. Each can be rerun with
  `python3 -m unittest discover -s qorqut-code/app/tests -p '<module>.py'`.
- New behavioral coverage includes concurrent local activity; expired attention
  budgets; lost begin responses and failed output flushes; completion replies
  racing newer activity; report retry through a local HTTP server after the
  session stops; failed-bootstrap report discovery; one requesting board plus
  60 unrelated boards; exact expired-claim settlement; a 60-item Todo board;
  and safe pin retirement while launches/reference updates compete. Retiring
  1,001 synthetic exited pins restores the existing probe's complete coverage
  while retaining a live process's reference.
- **Core wheel build passed** using
  `python3 -m pip wheel --no-deps --wheel-dir <temporary-output> ./qorqut-code/app`
  with build isolation. The nine modified Python modules in the wheel match
  their worktree source bytes. The installed runtime environment was not changed.
- **Dashboard selection: 17 passed, 1 existing failure, 2 skipped.** Command:
  `node --test qorqut-code/biz-dashboard/tests/focus-attention.test.js qorqut-code/biz-dashboard/tests/attention-board-owner-toggle.test.js`.
  The `signoff pending-write check` case fails with `reviewEdits is not defined`.
  The same failure was reproduced from a clean archive of baseline `4d88ab1`
  using `--test-name-pattern='signoff pending-write check'`. No dashboard source
  changed in this branch. The two browser cases require unavailable Playwright
  Chromium. These are recorded limits, not claimed passing checks.
- **Controlled no-op startup comparison:** five subprocesses per variant,
  identical input and environment. Explicitly loading preparation before the
  module entry (the old eager-import behavior) took
  `[0.922, 0.795, 0.862, 0.931, 0.811]` seconds. The lightweight entry took
  `[0.622, 0.591, 0.661, 0.606, 0.643]` seconds. Medians: 0.862 versus 0.622
  seconds, about 28% lower in this sample. This measures startup work under
  local contention, not production hook latency or total timeout recovery.
- `git diff --check` passed. The changes use existing services and storage;
  there is no schema migration, new daemon, or extra infrastructure dependency.

### Rollout verification still required

This branch is implemented and locally checked; it is not installed in the live
service. After an authorized release, verify representative hook latency and
timeouts, continued report draining during idle periods, falling ordinary-pin
counts, and contention on the same workload. Keep live/uncertain pin coverage
conservative. Reassess configuration cost and host contention from that evidence;
the no-op benchmark alone is not a reason to expand the architecture.
