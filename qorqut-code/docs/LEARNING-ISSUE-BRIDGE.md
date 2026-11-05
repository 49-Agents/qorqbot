# Learning issue bridge

> **Historical record (retired 2026-10-28).** This document describes the procedural learning engine that was removed
> from Qorqbot. Its repository history is preserved, but `qorqut-learning`, the `learnings.engine_extract` program, the
> ten `learnings_*` engine/work operations and the seven engine tables are not part of a current installation. Factual
> Context learning and read-only historical procedural records remain. See the frozen removal specification.

The standalone learning engine (`qorqut-learning`, `qorqut_learning`) finds claims that recur across saved agent
transcripts and keeps a stable identity for each one. This bridge lets an eligible recurring issue appear in the
existing Learning queue, be decided in the existing owner-led Learning Applier conversation, and keep that decision
when more evidence arrives. It is one vertical integration. It adds no agent, dashboard, scheduler, event bus or
second decision store, and it connects neither the tool-pattern nor the usage-pattern studies.

This is source only. Nothing here migrates a database, enrolls an agent, configures an engine provider, publishes a
role, restarts the service or runs a model. See [Rollout](#rollout-and-what-is-not-done).

## Who owns what

| Owner | Responsibility |
|---|---|
| Engine, `qorqut-learning` at `d9c39c9` | Source generations, typed evidence, observations, clustering, opaque stable issue identity (`iss_…`), lineage through splits and merges, and versioned review packages. No Qorqut imports, business constants, paths or credential discovery. An optional caller-supplied decision view lets it skip packaging issues the host has muted; without one it behaves standalone. |
| Host, biz-core | Which saved sessions are sources (enrollment), when the engine runs, mapping an issue to a review candidate, every owner decision with its reasoning, and resurfacing policy. Host records are the only authoritative decision store. |

The engine's package is `qorqut.learning.review-package` version 1, kind `engine_cluster`; its compact issue record is
`qorqut.learning.issue` version 1. Both name the stable `issue_id`, `issue_revision`, source scope, cluster
bindings, eligibility and support snapshot, `material_evidence_revision` and lineage. Identity is never a hash of
summary wording or member list. See the engine's own documentation for how identity survives re-extraction.

## Enrollment and the program

- `learnings_engine_enrollment_set` (`{agent_id, engine_id, active}`) is owner-only, reachable from the dashboard and
  worker surfaces and absent from agent MCP. It creates no job, routine or agent. Each re-activation of an agent
  starts a new epoch, because a disabled engine source cannot be revived; the engine source is
  `agent:<agent_id>:<epoch>` (Codex rollouts are a separate `…:codex` source). Global Learning Applier chats cannot
  be enrolled.
- `learnings.engine_extract` is an ordinary background program that a routine names like any other
  (`max_sessions` 1–100, default 100, and `retry_failed`). It is registered but reports `operation_unavailable`
  until the installation passes an `EngineAdapter` (`engine_id`, `open`, `workdir`, `extractor`, `embedder`,
  `policy`, `processing_policy` and the optional `source_facts`) to `assemble(..., learning_engine=...)`. Nothing is
  discovered from the environment, a credential store or a source import. Enrollment rows are inert without an adapter.
- The program reads only enrolled agents' saved sessions through the existing authorized snapshot selection
  (`max_sessions`, 16 MiB per transcript, 64 MiB per pass over the adapter). Each snapshot is frozen as typed engine events, not
  pruned prose. Transient copies live in the adapter's private work directory and are removed after ingest.
- **No dual processing.** Enrolled agents leave the legacy path: `learnings.extract` skips them, and asking for one
  by `agent_id` is `forbidden`. Engine review items never enter the legacy duplicate comparison. The engine's
  cursor, `learning_engine_state`, is separate from `learning_scan_state` and neither ever marks the other complete.
  Legacy non-transcript sources (Signoff answers, task and Story notes) keep their existing processor. Un-enrolling
  returns an agent to the legacy path.
- **Source facts.** Whether a saved session is a finished conversation, and whether it is its own task, are facts the
  saved file cannot prove, so the host never guesses them. An installation may pass a trusted
  `source_facts(control, snapshot)` in its adapter. For each snapshot it returns one versioned object (`version` 1)
  bound to that exact `agent_id`, `session_id` and `transcript_hash`, so an attestation cannot outlive a change in the
  content. It states `completion_state` (`complete`, `incomplete` or `unknown`) with the `completion_rule` and
  `rule_version` that decided it (required whenever the state is not `unknown`), and `independence`
  (`established`, `dependent` or `unknown`). An established one adds its `independence_key` and `lineage_basis`, and
  only an established one may carry a key. A key names independently attested task or episode lineage: it is stable
  across copies and enrollment epochs, so two sessions of one parent or delegated task carry one key and count once,
  and it is never made from an agent or session ID. A missing, unbound, malformed or unknown-versioned attestation is
  not a failure: the snapshot is recorded with `facts: absent` or `invalid` and the reason, and its lifecycle and
  independence are unknown.
- **Lifecycle, coverage and extraction result stay separate.** *Lifecycle* is the attestation that the snapshot is a
  finished conversation, by a documented installation rule. Only a snapshot attested `complete` is processed. Every
  other snapshot is deferred: recorded in the engine cursor as `status: deferred` with `coverage: unknown` and no
  attempts (the schema refuses any other combination), never parsed, never sent to a provider and never counted as
  processed or failed. A deferred snapshot is read again on later runs (within the metadata bounds below: at most 200
  recorded snapshots per agent and run, the stalest check first), so it is processed once trusted metadata says it is
  complete, even when its content hash has not changed. Deferral costs no session budget (a metadata-only
  look, up to 1,000 more per agent and run), an identical deferral is counted but not written again, and job data
  reports `lifecycle` counts next to the status counts. *Coverage* is what the parser saw of a processed snapshot:
  `complete`, `partial` (unsupported records, truncation markers or diagnostics) or `unknown`; it says nothing about
  whether the conversation had ended, so a clean parse of a snapshot whose lifecycle is unknown or incomplete stays
  deferred. The *extraction result* (`processed`, `retry`, `needs_attention`) is the engine's outcome. Failed
  extraction retries up to three attempts, then needs attention until an explicit `retry_failed`; a transcript the
  engine rejects is paused at once. A failed run never marks anything complete. The Learning Applier's own chats stay
  excluded. A snapshot that was already processed is not exempt from any of this: see the next bullet.
- **Freshness: a content hash never stands in for the attestation.** Every run judges each snapshot it selects from the
  facts the installation attests now, whatever the host recorded before, processed snapshots included. Beside each cursor the
  host keeps `freshness`: a canonical fingerprint of the validated completion and independence facts (bound to agent,
  session and content hash) and, once the engine has tombstoned or ingested a revision for that episode, the highest
  tombstone fence and revision ever seen (`tombstone`, `high_water`), below which no revision may fall. A recorded snapshot
  meets one of these outcomes:
  - *Unchanged*: same content, same fingerprint, evidence still in force in the engine. No engine change, no provider call and
    no cursor write. Only when the bound below leaves other recorded snapshots for a later run does the run stamp the ones it
    did read again (`freshness.checked`) so they wait behind the rest.
  - *Refreshed*: the facts are worded differently (rule, rule version or lineage basis) while the engine holds exactly the
    same evidence and independence. Only the cursor is brought up to date; nothing is reprocessed.
  - *Withdrawn*: the attestation is missing, invalid, unknown, incomplete or otherwise no longer `complete`. The engine's
    evidence for the episode is retracted before the run collects any package (it stays in the engine as history and stops
    counting), then the snapshot is recorded as deferred. An open item that loses its support becomes `stale` and cannot be
    accepted; a decision is never removed.
  - *Reactivated*: attested complete again, it is taken up at a revision above everything the engine tombstoned or ingested,
    never a retracted one. The issue keeps its item or decision, so a rejection stays a rejection and no second item appears.
    The attestation is the only authority over what counts. The engine keeps no reason or author for a retraction, so the
    host cannot tell its own withdrawal from an operator's retraction through the library or from a run that stopped before the
    host recorded its withdrawal: evidence that is not in force for a snapshot attested complete is taken up again (a new
    revision above the tombstone, at the cost of an extraction of that session) whoever retracted it. Evidence is withdrawn by
    withdrawing the attestation or the enrollment, and stays withdrawn for as long as that holds.
  - *Independence changed*: a corrected lineage key or kind retracts the old contribution first, then the new revision is
    taken. When the run's session budget is already spent, the old contribution stays invalidated until a later run takes the
    replacement, so it can never count twice or count on a stale key.
  - *Crash or retry*: after a stop between the engine's write and the host's, the next run reads the engine's own state and
    takes up an ingested revision the host never recorded only when it holds exactly this content and independence. Otherwise
    it takes the next revision above every one either side has seen. Nothing is derived from the host's cursor alone.

  Reading metadata is bounded separately from paid processing: up to 1,000 unseen not-complete snapshots per agent and run
  beyond the session budget, and up to 200 recorded snapshots per agent and run whose attestation is read again, the stalest
  check first (and no more than one pass's 64 MiB of them) so that a backlog rotates and neither starves nor is starved by later
  eligible work. No bound is silent. When the count or the byte bound stops the listing with a selected snapshot unread,
  `more_sessions_may_remain` is true and `scan_complete` is false, and the next run takes up what it left. A recorded snapshot
  that a stopped listing never reached is `not_reached`, not `unoffered` (which is only what the adapter did not offer at all);
  one that disappeared, or grew past the bound, between the run's two looks at the adapter is `not_reached` too. Whenever others
  wait, each snapshot read is stamped (`freshness.checked`) so the rotation reaches the rest next. Job data reports
  `revalidation` (`examined`, `unchanged`, `refreshed`, `withdrawn`, `replaced`, `not_reached`, `unoffered`) and
  `scan_complete`, which is false whenever a bound left a snapshot unread or the adapter no longer offered a recorded one.
  Nothing scans globally, and no scheduler, thread or routine is added: the existing program simply does this on each run.
- **Independence.** Only unique, established independence keys count toward support, and establishing one is the
  installation's attestation alone. With no attestation, independence is unknown: the snapshot's evidence is recorded
  but never counts, so missing facts can never make four. Native detection can only downgrade: a Claude session that
  replays another session's records (a resumed or forked copy) is dependent whatever was attested. Nothing native can
  establish independence or upgrade an unknown one, and Codex rollouts have no special case: they count only through
  an attestation. Raw observations, duplicate copies and unknown independence never count. The first review is
  eligible at four established independent occurrences; the host refuses an adapter, and a package, whose engine
  threshold is lower. Which lineage rule an installation attests is its own decision and outside this source.
- **Deauthorized sources.** Before each run, sources that no longer belong to an enrollment are disabled in the
  engine, so their observations stop counting. Nothing is deleted from host decisions or history.
- Cost: known provider cost joins the job's usage history. Enforcing a spending limit belongs to the adapter's
  providers.

## Import and review candidates

`learnings_engine_import` is a worker-only operation and the program's only output. It validates the package
schema, hash, typed evidence (event IDs, offsets, evidence class, quote hashes), bounds and scope. Each cited source
must be enrolled in its current epoch, so a forged scope, an unenrolled source or a stale package is refused. Legacy
`validate_item` is not weakened and no `owner_quote` or `said_by` is manufactured; owner-intent categories
(`preference`, `instruction`) additionally require owner-grounded observations containing owner speech.

- **Candidate.** The initial text is the engine's representative claim followed by
  `[Claim under review; N independent occurrences when first proposed]`, never model-regenerated wording. Routing is
  `issue`/`workflow` → delta fact, `instruction` → delta rule, `preference` → context rule, `context` → context
  fact; the owner can still edit bucket, tag, scope and text. The frozen package hash, revisions, support counts,
  eligibility, completeness and lineage are stored in the item's `engine` block.
- **Idempotent delivery.** A receipt in `learning_engine_packages`, keyed by (engine, issue, package revision,
  candidate ordinal), is written in the same transaction as the candidate. A replay or import retry finds it and
  returns the original receipt and candidate, even after human edits and even when a linked successor was later made
  at the same package revision (only an import that itself made a receipt reports that one). A different hash for the
  same revision is `revision_conflict` (`identity_conflict`). No cross-database transaction is claimed: after a
  crash the next run derives its view from committed host rows and receipts.
- **Decided inside the write.** The delivery verdict is recomputed inside the host transaction from the host's own
  policy rows, clock and enrollments. A rejection or ignore that lands first wins over an in-flight delivery.
- **One open candidate per issue.** While it is open, advancing evidence is appended (a new bound evidence revision,
  an `append_evidence` action and candidate history) without replacing the wording the owner reviewed. The older
  frozen package stays readable. A stale acceptance fails and requires a re-read.
- **Evidence loss.** Retracted, deauthorized or no-longer-eligible evidence marks an open item `stale`; a stale item
  cannot be accepted. Reads never expose sources outside current enrollments; they count them as `withheld_sources`.
  Decisions and history are never deleted.
- **Identity and lineage.** An issue unbound or superseded by the engine keeps its decisions, and its open or muted
  item becomes stale. Splits, merges and re-embedding (the engine treats a re-embedding as a split) never reset a
  dismissal: descendants are held, keep links to all predecessor issues and decisions, and are readable through
  `learnings_review_get`. The host never guesses identity for legacy rows, and never links a legacy candidate to an
  issue afterwards: see [Legacy candidates](#legacy-candidates-and-the-bridge).
- **Source re-enrollment never resets a decision.** See [the next section](#source-re-enrollment-continuity).

### Source re-enrollment continuity

Re-activating an agent starts a new source epoch because the engine's `disable_source` is irreversible, and the engine
hashes the source into its evidence anchors. The same saved sessions therefore re-extract under the new epoch as a new
issue with no engine lineage to the earlier one. The engine cannot see this; the host can. Before it makes the first
review item for a newly seen issue, the host decides, in the same write transaction as the delivery, whether the issue
may be an earlier decided or held issue seen again. If it may, no item is made and the issue is held.

- **Trigger.** Only a newly seen issue (the host has no row for it yet, however many runs it took to reach four
  established occurrences) whose package is deliverable, and only when one of its cited source generations
  (`agent`, `epoch`) is later than a generation an earlier issue of the same engine and workspace was built from. A
  package that is truncated cannot hide a re-activated source: it is judged against every currently enrolled
  generation as well. An issue of a source enrolled for the first time, and a sibling of the same generation, are never
  held on account of one another.
- **History used.** Only what the host itself froze about each earlier issue, never the engine's current projection
  (it is empty once the old source is disabled): the stored support snapshot, every action's evidence baseline, a
  snooze's frozen baseline keys, every stored package's support, completeness and cited source generations, and the
  generations recorded by an earlier hold (a hold has no package of its own). Ignored, rejected, accepted, snoozed and
  held earlier issues all count.
- **Relation, never identity.** A shared established occurrence key relates the two issues as
  `source_reenrollment_overlap`. It is evidence of ambiguity only: several distinct atomic issues can occur in one
  episode, so an overlap never says the issues are the same, is never a split or merge, transfers no rejection,
  reasoning, filing or work, and never merges identities. When the histories share no key but either side cannot list
  all of its keys (an incomplete or truncated history), continuity cannot be ruled out and the relation is
  `source_reenrollment_incomplete_lineage`, with its own hold reason. Complete disjoint histories are unrelated and the
  new issue proceeds normally.
- **The hold.** The issue is recorded with `hold_state: held`, `policy_state: tracking`, no review item, no reminder
  and no task. Later imports keep collecting its evidence and counters. The `hold` action stores the reason
  (`source_reenrollment_overlap` or `source_reenrollment_incomplete_lineage`), every matching predecessor with its
  relation, the shared-key count for each, and the source generations judged. One deterministic
  `learning_issue_links` row (`reenroll_…`) per predecessor keeps the reference, next to any lineage the engine itself
  reported. Only when more than 200 predecessors match, which is the most one revisit can name, the first 200 (sorted)
  are linked and named, and the rest are still recorded in the action's `predecessors_omitted` (each with its
  relation), so no matching reference is lost. The held issue is listed by
  `learnings_review_list` with `issue_view: held` and readable with `learnings_review_get` by `issue_id`, whose
  lineage shows each predecessor's state and its latest recorded reasoning.
- **Incomplete reconciliation.** A hold whose predecessors (the union of the engine's own lineage and the host's
  re-enrollment relations, each counted once) number more than 200 is marked, never silently truncated. The marker is kept
  in `learning_issues.hold_json` and grows only: the total, how many are linked and named, how many are not, the limit,
  and the unlinked predecessor IDs (the IDs stay in the row and the action's `predecessors_omitted`; reads show counts
  only). Every read of the issue and the hold's own action details carry `hold_reconciliation` with those counts and a
  plain reason: the hold stays until a separately authorized reconciliation accounts for every predecessor. The marker
  survives later imports, including an engine hold that arrives beside earlier host links, and cannot shrink the total.
- **An issue the engine already holds.** A newly seen issue that arrives held by the engine's own lineage (a split or
  merge) is checked the same way. When it also overlaps an earlier decided or held issue of an earlier generation, its
  hold links those predecessors as well, keeps the engine's predecessors in `predecessor_issue_ids` and records the
  re-enrollment details beside them under `source_reenrollment`, so its release names every predecessor and the owner
  sees the earlier decision. A compact record has no package, so its completeness is unknown and every currently enrolled
  source generation is considered, as for a truncated package.
- **Release.** The existing explicit `revisit`, naming exactly the predecessor issue IDs, releases the hold once. The next
  delivery then makes one candidate. A repeated import never recreates a resolved hold, and a partial or wrong set of
  predecessors is refused. A hold with the incomplete-reconciliation marker is never released by `revisit`, whichever
  IDs it names: it is refused `invalid_transition` (`details.reason` is `incomplete_reconciliation`, next to the
  `hold_reconciliation` counts), stays held with its reason and keeps every reference, so partial approval cannot happen.
  This version adds no operation that reconciles the rest. The owner can also `ignore` a held issue; an ignore
  records the owner's decision only (it changes the policy state and never the hold), so it cannot release one, and
  the hold and its marker read exactly as before.
- **Race with an owner decision.** Every related earlier issue is pinned in the plan (`expect`). A decision recorded
  after the package was prepared fails the write closed with `revision_conflict` (the job fails; nothing is
  delivered), and the retry prepares again against the new state.
- **Deliberate limits.** An earlier issue that was never decided (no rejection, ignore, snooze or acceptance, and not
  held) does not create a hold, so the new issue is delivered normally; its evidence-loss handling applies to the old
  item. An earlier issue with no recorded source generations is treated as possibly earlier than any re-activated
  source, which can hold more than strictly needed rather than reset a decision. Only established occurrence keys and
  the recorded completeness are compared; no exact evidence anchors or model are involved.

## Decisions

| Action | Effect |
|---|---|
| First review | Eligible at four established independent occurrences; one open candidate. |
| Accept (`learnings_decide` or owner review) | Existing context filing and delta work semantics, exactly once. The outcome stays visible; more evidence accumulates without reminders or new work tickets. |
| Reject | Proposal and reason are preserved. No automatic further candidate for that issue, even when evidence grows or wording changes. |
| Ignore | Muted indefinitely. New observations update evidence and counters silently. On an issue that is already held it records the decision and nothing else: the hold and its marker stay exactly as they were, and only a `revisit` naming the exact predecessors can release an ordinary hold. |
| Snooze | Needs an explicit owner revisit condition: a future UTC time, or a number of additional independent occurrences beyond a frozen baseline of keys. Without one, record a recommendation with `note`. When the condition holds and the issue is still eligible, it is reviewable exactly once. Replayed, re-extracted or shared-lineage observations do not advance the count; retracted or deauthorized evidence cannot trigger it. |
| Revisit | The owner explicitly releases a mute, or asks for a closed issue to be reviewed again. An ignored, snoozed, rejected or accepted issue whose item is no longer open gets one linked successor at the next delivery, bound to the new exact item. The old item, its decision, filing and work stay exactly as they were: revisit never reverses an acceptance, filing or work outcome, and a second revisit while the successor is pending is `invalid_transition`. Ignore and snooze apply only to an issue that is not yet decided. Revisit also releases a held lineage, and only when given the exact predecessor issue IDs; it never releases a hold marked incomplete ([the hold](#source-re-enrollment-continuity)). |
| Work start, defer, done, drop, note_set | Permissions, transitions, the required agent note, pushed-target checks and accept and filing semantics are unchanged; a deferral is not a review snooze and a drop does not erase the acceptance. Each of the five operations takes optional owner `reasoning`, recorded with the action: see [Owner reasoning for work](#owner-reasoning-for-work). |
| Note | Adds reasoning or an agent rationale, or corrects an earlier action by naming it (`corrects_action_id`). It remains the way to record a standalone note or a correction for an issue. |

Ten independent occurrences becoming twenty-five is fifteen new occurrences only when the engine actually
established them, and the item stays muted. The raw difference is never used.

## Owner reasoning for work

`learnings_work_start`, `learnings_work_defer`, `learnings_work_done`, `learnings_work_drop` and
`learnings_work_note_set` accept an optional `reasoning`: the owner's own words for that one action. Nothing else
about them changes.

- **Omitted stays empty.** Reasoning is never asked for, invented, inferred, copied from the agent's `agent_note` or
  inherited from an earlier action. Without it no reasoning object is written, and for a linked item the appended issue
  action has `reasoning`, `reasoning_source` and `owner_instruction` all NULL.
- **Attribution.** When given, the record is `{text, source, actor_id, action, target_revision}` plus
  `owner_instruction`. `text` is the exact string (1 to 8,000 characters, not blank). `source` is `owner_direct` when the
  owner acts from the dashboard and `owner_conversation_via_agent` when an agent supplies it. An agent must then also
  quote the owner's exact current `owner_instruction` for this action (the existing convention, not authenticated
  consent); `reasoning` without it is a `validation_error`, `owner_instruction` without `reasoning` is a
  `validation_error`, and a human caller cannot send it. `action` is the recorded work action (`work_start`,
  `work_defer`, `work_done`, `work_drop` or `work_note`) and `target_revision` the candidate revision it acted on.
- **Storage, for every candidate.** The record is kept as `work_reasoning` in the action's append-only
  `learning_history` row, in the same transaction as the state, note and target change, for legacy and engine-linked
  candidates alike. It is never edited: the projections read it in full and never abbreviate it (the work history
  compaction exempts it), and a replay of the same idempotency key appends nothing again.
- **Linked items.** The corresponding `work_start`, `work_defer`, `work_done`, `work_drop` or `work_note` action is also
  appended to `learning_issue_actions` in that same transaction, with the reasoning columns above. The issue's policy is
  untouched: its state, hold, revision and policy revision do not move, and the issue's own `reasoning` still means the
  owner's review reasoning (a work action never replaces it).
- **The agent's account stays its own.** `agent_note` remains agent-authored and keeps its required use; a done or dropped
  work item's note is still read as `outcome_reasoning` from `agent_work_explanation`.
- **Reads.** `learnings_work_get` shows `work_reasoning` in its history entries. `learnings_get` and
  `learnings_review_get` return a top-level `work_reasoning` list, newest first, for legacy and linked items alike
  (`work_reasoning_complete: false` when older ones are not listed), and a linked item's recent issue actions show the
  `work_*` rows.
- **Corrections.** A record is never rewritten. While work is accepted or in progress a later `learnings_work_note_set`
  with the corrected reasoning appends a new record; for a linked item the `note` action can also name the action it
  corrects (`corrects_action_id`), which leaves that action as it was. Once work is finished, only the linked item's
  `note` action can correct it; an item with no issue has none.

## Action history

`learning_issue_actions` is append-only (database triggers). Every action stores its `action_id`, `issue_id`,
`issue_revision`, `candidate_id` and `candidate_revision` when applicable, the action, the actor, timestamp,
idempotency key, the policy revision before and after, and the support and evidence baseline it was evaluated against
(independent keys, counts, eligibility, evidence revision and fingerprint), not just a count. User-facing actions add:

- `reasoning`: the owner's exact words, or NULL when none were given. It is never asked for and never inferred.
- `reasoning_source`: `owner_conversation_via_agent`, `owner_direct` or `agent_work_explanation`; NULL exactly when
  `reasoning` is NULL.
- `owner_instruction`: for an Applier-recorded decision, the exact current instruction (the existing convention; it
  is not authenticated consent).
- `agent_rationale`: separately attributed and never substituted for the owner's reasoning.
- `revisit_condition`: NULL, an explicit UTC time, or an additional-occurrence count with its frozen baseline keys.

Machine events (`deliver`, `successor`, `append_evidence`, `resurface`, `evidence_update`, `hold`, `stale`,
`supersede`) have actor kind `system` and an `operational_reason`; a source re-enrollment hold is a `hold` whose
details carry the [continuity](#source-re-enrollment-continuity) reason, relations and predecessors. The work actions
`work_start`, `work_defer`, `work_done`, `work_drop` and `work_note` are appended, for an issue-linked item only, in the
same transaction as the work change, with the owner's [reasoning](#owner-reasoning-for-work) when there is one and the
policy revision unchanged. Corrections append a `note` naming the corrected
action; nothing is rewritten. A replayed action finds its stored row and appends no second one. The existing decision
`feedback` and `agent_note` are exposed through an explicit read projection: feedback that predates any recorded
source stays visible as `legacy_feedback_unattributed` and is never relabelled as owner reasoning; feedback given now
(`reasoning`, or its legacy name `feedback`) is read with the source its decision's history record carries
(`owner_direct` or `owner_conversation_via_agent`), for a legacy item as for a linked one; and a done or dropped
work item's required final note appears as `outcome_reasoning` from `agent_work_explanation`.

## Reads and operations

| Operation | Notes |
|---|---|
| `learnings_review_get` | Exactly one of `learning_id` or `issue_id` (validated in the handler). A legacy frozen core keeps its existing bundle. An engine item returns the typed bundle with `source_kind: engine_evidence_package`, `bindings`, `bound_current`, `withheld_sources`, `references_complete`, `evidence_status`, the issue's lineage and its recent actions. `package_revision` selects an older bound package. Bundles above 32 KB use the existing hash-bound cursors, which fail once the evidence moves. |
| `learnings_review_list`, `learnings_list` | Compact rows carry an `issue` brief (ID, count, muted or hold state and, for a hold marked incomplete, its `hold_reconciliation` counts and reason, never the unlinked IDs). `issue_view` is `active` (default; leaves out ignored and snoozed), `muted`, `held` or `all`; every response reports `suppressed` counts. Held issues that have no item yet are listed separately. |
| `learnings_decide` | Accept or reject, now with optional `reasoning` (`feedback` remains its legacy name) and `agent_rationale`. The linked issue's policy moves in the same transaction. Unlinked legacy candidates are unchanged. |
| `learnings_issue_action` | `ignore`, `snooze`, `revisit`, `note`. Owner (dashboard) and the registered Learning Applier only, with the issue's `expected_revision`, the item's exact `candidate_id` and `candidate_revision`, an `idempotency_key` and, for an agent, the current `owner_instruction`. One issue per call: no bulk IDs and no new candidates under a prior approval. A `revisit` of a hold marked incomplete is refused `invalid_transition` however it is phrased. Other roles are denied `learning_applier_only` and overrides cannot grant it. |
| `learnings_work_start`, `_defer`, `_done`, `_drop`, `_note_set` | Existing permissions, transitions and required agent note, now with optional owner `reasoning` and, from an agent, the required `owner_instruction`, recorded in the work transaction ([Owner reasoning for work](#owner-reasoning-for-work)). `learnings_work_get` shows it in history in full. |
| `learnings_get` | Also returns the item's `work_reasoning` list. |
| `learnings_engine_enrollment_set` | Owner only. |
| `learnings_engine_import` | Worker only, from the registered program. |

## Learning Applier role

The companion role source (agent-roles branch `feat/applier-issue-bridge`, version 1.10.0) changes only the Learning
Applier's owning role and learning-application procedure: it grants `learnings_issue_action`, explains cluster
evidence and source distinctions, keeps `reasoning` with the owner and recommendations with the agent (an owner's own
reason for starting, deferring, finishing, dropping or annotating work goes in the optional `reasoning` of that work
operation with the owner's exact `owner_instruction`, and is left out when the owner gave none; the work's
`agent_note` stays the agent's, and the `note` action stays for standalone notes and corrections), explains a held
issue after a source re-enrollment (a possible repeat of a decided claim, never evidence of one, to be put up for review
only through an explicit revisit), and adds examples ("ignore this, repetition is intentional", "reject — wrong
conclusion", "revisit next month", "show ignored issues and their original reasons", "accept these exact two", "start
that one, the release needs it", "drop it, we chose another approach", "mark it done", "that held issue is a different
claim from the rejected one; put it up for review"). The owner-led agenda, exact decision binding, filing,
notes and work tracking and the Dev/human implementation boundary are unchanged; there is no new automatic permission,
monitoring loop, auto-accept category or implementation agent. Publishing 1.10.0 and adopting it are later release
steps; the published 1.9.0 artifact is not altered.

The role's own base text contradicted its first paragraph: the brief opened with "Learning Applier ... does not implement
or delegate implementation" and then told the same role to use a branch per repository, "test, commit and push" and
prefix its commits. This revision resolves that in favour of the current Dev/human boundary and does not restore the old
broad self-implementation permission. The brief, the `learning-application` procedure and its workflow, issues,
review and evaluation references now say one thing: for an accepted delta the Applier reviews and records the
authorized outcome from evidence the responsible human developer supplies, after
checking the pushed commit itself with read-only Git; it never builds, tests, commits or pushes software, design,
landing, visual, script, test, CI or IaC changes. The work operations, exact-target and pushed-target checks and
the fresh final note required for done are unchanged, and a permitted noncode context or instruction change keeps its
bounded path in the procedure. The role manifest still grants only what it granted; a focused test in the role source
(`test_learning_review_policy.py`) asserts the boundary in every one of those texts.

## Legacy candidates and the bridge

This version has no operation that maps an existing legacy candidate to an engine issue, and no migration screen,
mapping API or bulk relinking. A legacy candidate keeps its decisions, feedback, filing, work and history exactly as
they are and has no issue. The bridge is restricted to newly linked engine items: a candidate is linked to an issue only
when the engine's package first becomes its review item, and never afterwards. When an engine issue covers a claim a legacy
candidate already recorded, the two remain separate records; the host compares no wording and infers no identity between
them, and engine review items never enter the legacy duplicate comparison. A legacy candidate does gain the optional
[owner reasoning for work](#owner-reasoning-for-work), stored in its own history and with no issue action. If mapping
ever becomes a requirement it needs its own design and explicit owner identity decisions (who decides equivalence,
what happens to a rejection, how a mistaken mapping is undone); nothing here starts it.

## Migration 103 and recovery

`103_learning_issue_bridge.sql` is purely additive: it creates `learning_issues`, `learning_issue_candidates`,
`learning_issue_actions`, `learning_issue_links`, `learning_engine_packages`, `learning_engine_enrollments` and
`learning_engine_state`, and inserts no row, job, routine, agent or candidate. Legacy candidates, decisions, history,
work state and `learning_scan_state` are untouched, and a legacy candidate has no issue. Existing rows are never
linked automatically. The history tables, links and receipts are append-only.

Migration 103 has not been released, so this revision edits it in place rather than adding 104, and nothing has been
applied to a live database. It adds, inside the same new tables: the action kinds `work_start`, `work_defer`,
`work_done`, `work_drop` and `work_note`; the link relations `source_reenrollment_overlap` and
`source_reenrollment_incomplete_lineage` next to `split` and `merge`; on `learning_issues`, a nullable `hold_json` (JSON
only when valid) that stays NULL except for a hold marked incomplete ([the hold](#source-re-enrollment-continuity)); and,
on `learning_engine_state`, a `lifecycle` column with the `deferred` status, zero attempts, and the checks that a deferred
snapshot is neither attested complete nor parsed and that only a snapshot attested complete is processed. The cursor's
`freshness` record (fingerprint, tombstone and high-water revisions, rotation stamp) lives in its existing JSON detail, so
freshness needs no further column. The migration runner records each applied
migration's checksum and refuses a changed one, so a scratch database that already applied an earlier draft of 103 has
to be recreated; no live database has been migrated to any version of 103.

- The first five tables are in the SQLite backups and the business and root exports (older snapshots export them
  empty). Enrollments and the engine cursor are operational state carried in database backups only, like
  `learning_scan_state`.
- There is no automatic downgrade or live-database restoration. Recovery uses the reviewed maintenance workflow and a
  backup taken before migrating. Older code refuses a database already at 103 with `unsupported_schema`.
- Tests run the migration against fresh databases and a version-102 fixture that already holds legacy learning
  rows.

## Rollout and what is not done

Source only. Neither the migration, enrollment, an installation adapter, a routine, role publication nor profile
adoption is performed by this source. A release would:

1. Migrate with the reviewed maintenance workflow, then register the new program's worker principals.
2. Install the `learning-engine` extra in the service environment and pass an explicit `EngineAdapter` to `assemble`.
3. Have the owner enroll chosen agents and add a routine that names `learnings.engine_extract`.
4. Publish and adopt the Learning Applier role, when the owner chooses.

**Library pin.** `pyproject.toml` binds the optional `learning-engine` extra to the versioned package
`qorqut-learning==0.1.0`. Its wheel is built from the selected local source export and installed by file path
([Installation](INSTALLATION.md)); change the version there, and repeat the build and clean-install checks, when the
library is released.

**Operational activation.** An installation needs: (1) an `EngineAdapter` whose providers, work directory, costs and limits
it has chosen; (2) a `source_facts` rule that documents how it decides a saved session is a finished conversation and which
lineage rule gives each session its independence key, since nothing counts without one; (3) an agent enrolled by the owner
and a routine that names `learnings.engine_extract`; (4) an owner-run model trial on real transcripts; and (5) publication
and adoption of the Learning Applier role and, separately, a decision on how to reconcile a hold marked incomplete, which
this version can only keep visible.

## Known limitations and open decisions

- **Re-enrollment needs the owner once.** A new issue from a re-activated source whose recorded history overlaps, or
  cannot be ruled out against, an earlier decided or held issue is held and reaches review only through an explicit
  revisit that names those predecessors ([continuity](#source-re-enrollment-continuity)). The host cannot tell a repeat
  of the decided claim from a different claim that shares an episode, so it asks; nothing about the earlier decision is
  transferred, and an owner who wants the earlier decision to apply can `ignore` the held issue. An earlier issue nobody
  had decided creates no hold. An earlier issue with no recorded source generations can over-hold. Exact
  evidence-anchor lineage is not compared, only established occurrence keys and recorded completeness.
- **Lifecycle and lineage are the installation's facts.** This source ships no `source_facts` rule and no default. Until
  an installation supplies one that documents its completion rule and its lineage rule, every snapshot is deferred and
  nothing reaches four; choosing the rules is release scope, owned by the owner. Each run reads the attestation again for
  the snapshots it selects, processed ones included ([freshness](#enrollment-and-the-program)), so a withdrawn, incomplete
  or corrected attestation stops counting at the next run. It cannot do so between runs. A recorded snapshot the adapter
  no longer offers, or one a bound left unread, is reported (`unoffered`, `not_reached`, `scan_complete: false`) and is
  neither retracted nor guessed about, and the 200-per-agent (and one pass's bytes) revalidation window means a large backlog
  is read again over several runs. The attestation is also the only way to keep evidence out: a retraction made directly in
  the engine is not an attestation and is not honored for a snapshot still attested complete (the engine records no reason
  or author for it), so the next run takes the evidence up again at a higher revision. Resumed copies without a replayed
  `sessionId`, and copy-pasted content, are not detected natively: they count only if the attested lineage rule keeps them out.
- **The engine's revision state is read, not called.** Freshness needs which revision the engine tombstoned or holds active
  and what content and independence it was ingested with. The pinned library (`d9c39c9`, unchanged here) has no public
  accessor for that, so the host reads its `episode_state` and `episodes` tables through the open engine handle and writes
  nothing there; every change goes through `ingest`, `process` and `retract`. A later library release that exposes the
  state should replace this read.
- **Engine projections beyond the host's bounds are refused whole.** The host accepts at most 200 lineage links and 200 held
  predecessors in one engine projection. More is refused with its reason (`issue.lineage`, or `hold predecessors`, "has more than
  200 entries: it is refused whole and never truncated"), so that run's job fails closed with `learning_engine_output_invalid`
  while its other outputs are still applied, and no truncated hold is ever recorded. A hold that the engine lists within the bound
  and the host's own re-enrollment relations enlarge past it is the marked hold below. Raising the bound, or reconciling a
  projection that large, needs a separate decision.
- A held lineage is released only by an explicit revisit naming the exact predecessor IDs; nothing matches issues
  with a model or exempts a substantially different proposal. A hold marked incomplete (more than 200 predecessors) has no
  release in this version: it stays held, visibly, until a separately authorized reconciliation exists, and an `ignore`
  does not release it.
- **Work reasoning.** It is optional and per action. A finished item's recorded reasoning can be corrected only
  through the `note` action of an issue-linked item; a legacy item's finished work has no correction operation, and
  each correction is a new record, never an edit. The issue action a work action appends is planned against the issue
  as it was read and is deliberately not fenced on the issue row (unlike a decision): evidence that accumulates on an
  accepted issue during the plan's life must not fail the owner's work action, so the record can show the issue
  revision and baseline it was planned against, and the candidate row stays fenced as before.
- **No legacy mapping.** Existing candidates are never linked to issues and this version adds no way to do that
  ([Legacy candidates](#legacy-candidates-and-the-bridge)).
- Sessions that disappear are not automatically retracted.
- Category-to-bucket routing is a default; the owner edits it during review.
- Dashboard rendering of engine items is unchanged and out of scope.
- The scripted extractor used in tests emits no owner-intent observations, so owner-intent strictness is proved at the
  package validator.
