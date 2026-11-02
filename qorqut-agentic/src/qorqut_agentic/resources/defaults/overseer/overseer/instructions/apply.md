# Application procedure

Application requires a verified owner invocation in the continuing Overseer chat;
acceptance alone creates dormant queued work. Use `overseer.applications.list`
to inspect the accepted queue, then `overseer_application_get` for selected work and
read the exact accepted delta, decision, related evidence and current implementation.
An `application_id` supplied at start may bind directly only when the verified
owner invocation explicitly selected it; otherwise obtain selection through an
owner-only Signoff. Selection is not plan approval. Use an actual owner-only
`overseer_input_request` Signoff for selection and every
requested planning input; reconcile the canonical response through
`overseer_input_reconcile`. Do not manufacture owner decisions or interpret silence
as agreement. If already satisfied, retain concrete evidence and use the
owner-confirmed close-satisfied input; do not fabricate a commit.

Agree a concrete plan with the owner: intended outcome, managed entries and files,
base/hash expectations, dependencies and meaningful quick checks. Save each revision
with `overseer.applications.save_plan`. Bind the plan Signoff to its exact revision
and displayed delta; if the accepted meaning or scope changes, one explicit owner
response may approve the revised delta and the plan together, retaining prior history.
Inspect any saved revision and its own application-event history with
`overseer_application_get` and `plan_revision`; completed step receipts stay bound
to the plan that produced them, and a revised plan never rewinds or rewrites them.
For free text or a choice with notes, preserve the canonical answer and provide
the agent's interpretation as clarity (explicit, ambiguous or conditional), action
and rationale. A conflicting mapped choice cannot be overridden. Missing,
ambiguous, conditional or contradictory interpretation requires a new clarification
Signoff, not a keyword approval parser or silent reinterpretation. Link the new
binding to the earlier ambiguous input using `resolves_input_ids`; a valid explicit
review or plan response can clear that hold and perform its normal decision in one
step, without a redundant second approval. Keep the earlier request and answer.
A stale response
cannot authorize execution.

`overseer_application_replan` is a pre-merge transition: it returns a working
draft to planning when the current facts no longer support the agreed plan. Record
the reason, preserve the original plan receipts, and agree the revised plan through
its ordinary owner Signoff. Before merge, a failed or uncertain check also needs
the owner's explicit disposition through its failure Signoff: retry one bounded
attempt, record the owner-authorized replan and return to planning, or leave it.
Never replay an interrupted process because its lock disappeared, and never mark
an uncertain check passed from a PID, time guess or agent claim. Reconcile an
authoritative existing tool receipt separately. After merge there is no replan and
no automatic rollback: retain the merged facts, create the owner failure Signoff
and await the next choice; a repair or revert is later follow-up work. An explicit
owner disposition permits progress with acknowledged uncertainty but is not
factual proof of the earlier attempt.

Use the host operation `overseer_application_prepare` to establish an isolated
agentic branch, worktree and state for the agreed work. Implement the plan and
check the draft. Do not edit application/native-extension source as part of the
agentic worktree. Protected or host problems may instead be recorded as outside
observations and retrieved only on owner request; recording does not automatically
create development tasks, enter the top-five review or apply a change. A task
or dependency record is not delivered implementation; keep dependencies incomplete
until delivered and checked. An outside-only observation must not fabricate an
agentic commit.

Use the host operation `overseer_application_prepare_merge` to prepare one
new commit containing only the agreed delta and carrying its ID, preserving unrelated history and concurrent
active edits. Use `overseer_application_publish` to merge the verified candidate
and finalize the existing changefeed; it coordinates publication under its own
lock and records one publication event. It is an agentic publication operation,
not a Git push or package release. Re-planning, pausing or cancelling wins before
publication admission and never lands inside a publication; never replay a
publication because its lock disappeared. Run the agreed quick checks on the
actual merged revision and record receipts with
`overseer_application_record_checks` and its phase. Preserve acceptance, merge,
checks and session refresh as separate facts: report the refresh state derived
from the durable merge fact, and never claim the native session reread
instructions. A check attempt that was interrupted or whose outcome is unknown
stays uncertain until an authoritative receipt or an explicit owner disposition
resolves it. If post-merge checks fail, leave the merge in place, create an
owner-only failure Signoff and await the next choice; do not auto-revert, repair,
complete or reapply it. Revert is not offered for an unmerged application; after a
merge it is an instruction to plan a follow-up, not an automatic rollback. Use
recovery status and recovery operations for an interrupted publication instead of
guessing or resetting others' edits. No repository push, deployment or external
publication is authorized.
