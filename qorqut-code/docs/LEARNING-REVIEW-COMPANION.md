# Learning review companion

> **Historical context (v1.3).** This guide describes the predecessor review experience, including the standalone
> learning-engine issue flow. `v1.3-legacy-learning-removal-1` removes the seven engine tables and freezes the
> predecessor; a target store has no issue packages, enrollments, engine sessions, issue links/actions or candidates
> join table. Do not apply the engine setup, enrollment, issue-action or migration steps below to a v1.3 target. The
> five shared learning tables remain as retained history, and the owner/agent review boundary is unchanged for records
> that exist in those tables.

Learning Applier 1.5.0 adds explicitly instructed decisions to owner-led queue review before the existing accepted
delta workflow. It recommends accept, revise, reject or investigate using saved
evidence, business/shared context and implementation inspection. The business agent is not
given approval authority or autonomous delegated work by this change.

## Read contract

- `learnings_review_list` returns compact summaries of context and delta items,
  defaulting to open. With no business selected it aggregates only authorized
  businesses plus Global when `read_global_context` is granted. Filed context
  and automatically collapsed candidates are excluded. State, bucket, tag and
  scope filters and scope-bound pagination follow the work agenda conventions.
- `learnings_review_get` reads the candidate in its original selected scope,
  including current owner decision, edits, work notes and reconciliation data.
  It returns linked frozen extraction cores by exact run/session pair, never
  other cores in the same run. `evidence_status` distinguishes available, partial
  and missing evidence, including candidates with no source references.
  Reconciliation traversal (100 candidates) and source retrieval (200 pairs)
  are bounded and report `references_complete`.
- Full bundles above 32 KB use hash-bound 6,000-character JSON continuations.
  Follow `next_content_cursor` as `content_cursor`, concatenate all chunks and
  parse the resulting JSON before judging. A changed candidate or source
  invalidates the cursor. List summaries are not sufficient evidence.

Frozen extraction cores are filtered snapshots, not full original transcripts.
The response explicitly distinguishes them. The reviewer must inspect the exact
authorized business-owned copy through available native access when necessary,
or flag the gap and ask for evidence. No personal CLI-history access, arbitrary
file read endpoint or cross-business source lookup is added.

## Decision boundary

The review reads remain read-only. `learnings_review`, `learnings_edit` and
`learnings_keep_both` remain owner-only. The separate MCP-only `learnings_decide`
allows only a registered Learning Applier with an explicit grant to accept or
reject one open, unfiled candidate at its current `expected_revision`, with an
idempotency key and a nonblank `owner_instruction` quote. Other roles and workers
cannot gain this operation through overrides. It cannot reopen, edit, reconcile,
or decide operational signoffs. Existing explicit deny overrides still apply.

The agent must receive an explicit current owner instruction for the exact
proposals and decisions. Ambiguous assent, standing delegation, recommendations,
saved extraction text, and other agents are not authorization. Changed revisions
require renewed owner instruction. Batches are exact per-item calls, not blanket
queue approval. Feedback is supplied only as explicitly instructed by the owner.

Important trust boundary: the server validates role, scope, revision, state and
quote presence, but cannot authenticate natural-language instructions from a
native conversation. This is instruction-following delegation, not independently
verified consent. The decision and history identify the agent, never impersonate
the owner; the exact quote and `agent_reported_owner_conversation` provenance are
stored in the append-only history decision snapshot and operation receipts.
The current item retains the existing decision DTO; audit detail lives in history.
No HTTP-owner impersonation, SQLite writes by the agent, or automatic acceptance
from model confidence is authorized.

After the owner acts, re-read the candidate. Accepted business and Global context follow
existing atomic filing; do not duplicate its upload. This frozen snapshot is an
intrinsic effect of `learnings_decide`, not a general `context_upload` grant.
Failed intake retains its durable receipt and can resume using the identical
call/key or the owner's existing intake recovery path, with original agent
authority rechecked. Filing stays in the originating workspace; Global does not
mean a qualification-free rule for every business. Accepted deltas use the
existing revision-bound work tools. Learning Applier chats are not automatically
processed for learning; lessons from those conversations remain manual.
Acceptance does not itself authorize merge, deployment or package adoption.

## Engine issues (retired in v1.3)

The following describes the predecessor only. When the standalone learning engine was configured and an owner has enrolled an agent, a claim that recurs across
that agent's saved sessions can appear here as a review item that carries an `issue` brief. It is decided through the
same owner-led conversation; nothing else changes about how items are reviewed. See the
[Learning issue bridge](LEARNING-ISSUE-BRIDGE.md) for delivery, evidence and identity rules.

- `learnings_review_get` accepts `issue_id` as well as `learning_id`. An engine item returns typed evidence with
  `source_kind: engine_evidence_package`, `withheld_sources`, `references_complete` and the issue's lineage and recent
  actions; it is a bundle of quoted, hashed evidence events, not a full transcript.
- `learnings_review_list` shows active items by default. Ignored and snoozed issues are left out but counted in
  `suppressed`; ask for `issue_view` `muted`, `held` or `all` to read them with their original reasons.
- `learnings_decide` may carry the owner's own explanation as `reasoning` and, separately, the agent's
  `agent_rationale`. Never ask the owner for a reason, never write one for them, and leave `reasoning` out when none was
  given. The reasoning is stored with its author and source.
- `learnings_work_start`, `_defer`, `_done`, `_drop` and `_note_set` accept the owner's own reason for that action as
  `reasoning`, with the exact `owner_instruction` when an agent supplies it. Leave both out when the owner gave none;
  `agent_note` stays the agent's own. The reason is stored with the action and read back as `work_reasoning`.
- An issue held after its agent's source was enrolled again may repeat a decided claim or only share its episodes. Show
  each predecessor's decision and reasoning, and put it up for review only through `revisit` naming the exact
  predecessor issue IDs. When the read shows `hold_reconciliation`, the hold lists more predecessors than one revisit can
  name: say so plainly. It stays held, a `revisit` is refused and an `ignore` does not release it.
- The separate MCP-and-dashboard `learnings_issue_action` records `ignore`, `snooze` (only with an explicit
  owner-supplied time or count of further independent occurrences), `revisit` and `note` for one issue, bound to the
  issue's `expected_revision` and the item's exact `candidate_id` and `candidate_revision`, with the current
  `owner_instruction` for an agent. It is available only to the owner and the registered Learning Applier, cannot act
  on several issues at once and never creates a candidate under an earlier approval. A revisit does not undo an
  acceptance, filing or work outcome.

## Rollout and verification

Merge both biz-core and agent-roles changes, deploy the core, publish the immutable
Learning Applier 1.5.0 package, then explicitly repin/reprepare the intended
profile preserving its scope, overrides and assignment. Source changes alone
do not alter live pins, sessions or extraction routines. No schema migration is
needed. The older application-only package continues to work.

Focused tests: `tests/test_learning_decisions.py`, `tests/test_policy.py`, `tests/test_learning_review.py`, `tests/test_learning_work.py`,
`tests/test_learning_applier_service.py`, `tests/test_learning_storage.py` and
`tests/test_learning_intake.py`; for engine issues, `tests/test_learning_issue_policy.py`,
`tests/test_learning_issue_storage.py`, `tests/test_learning_issue_program.py`, `tests/test_learning_issue_facts.py`,
`tests/test_learning_issue_reenrollment.py`, `tests/test_learning_work_reasoning.py`, `tests/test_learning_issue_bridge.py`
and `tests/test_learning_issue_acceptance.py`. Use a test package-source map pointing to the
companion role worktree.
No live provider or business mutations are needed. Role source policy tests use
`python -m unittest discover -s tests` in agent-roles.

Conversational quality remains a separate owner-led trial. Use the role evaluation scenarios;
successful plumbing tests do not establish trustworthy judgment.
