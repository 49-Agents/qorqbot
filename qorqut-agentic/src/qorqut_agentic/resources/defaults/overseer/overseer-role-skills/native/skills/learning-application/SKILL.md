---
name: qorqut-role-learning-application
description: Apply an accepted agentic change through the managed Overseer application workflow, only when the owner invokes it. Not for the scheduled experience extraction or the review and application, which have their own skills.
---

# Overseer application

Use the managed catalog package `overseer` and its `apply` workflow entry. Read
its frozen instructions and `record-direct-instruction` when applicable. Work in
the chat the owner invoked you in. Application requires a verified owner
invocation. Acceptance alone queues dormant work.

The package's `extract` and `review` entries are no longer started. The scheduled
experience extraction and the review and application replace them; follow the
experience-extraction and delta-proposals skills for those. This procedure applies
only what is in the accepted queue: a change or a delta the owner accepted
earlier. An approved delta proposal
of the review and application is not in that queue and is not applied here.

When the owner should decide whether to begin application, use the dedicated
`overseer_invocation_request` Signoff for exactly that request. Accepting a delta,
agreeing a plan and ordinary discussion do not authorize starting it. To show
existing records in the owner's chat, use the read-only `request_user_input`
presentation; it cannot answer a Signoff, approve a plan or start a workflow.

Every requested owner input uses an actual owner-only input Signoff, bound to the
exact run, question and delta/plan revisions. Reconcile its canonical response;
free text and conditions may require clarification. Discussion, silence, later,
cancellation and agent-authored summaries do not authorize acceptance or execution.
Never forge a trusted response.

For owner-invoked application, select accepted work, inspect current behavior and
jointly save a concrete plan and Signoff agreement. A single explicit Signoff may
approve a revised delta and its exact plan together. Each plan revision keeps its
own application-event history; inspect it with `overseer_application_get` and its
`plan_revision` and never rewind earlier receipts. Use an isolated agentic
worktree, preserve concurrent changes, publish only this delta as one new commit with
its ID, merge safely under the publication lock, then record real quick checks with
`overseer_application_record_checks` and their phase. Report the native-session
refresh state derived from the durable merge fact; never claim the session reread
instructions. If an attempt is interrupted or uncertain, or current facts no longer
support the plan, return to planning with `overseer_application_replan`, preserve the
uncertain attempt and agree a revised plan; never replay an interrupted process or
mark an unknown check passed. A merged failure remains in place and waits for an
owner Signoff; no automatic repair or revert (revert is a follow-up plan). Record protected or host problems as outside observations when appropriate;
retrieve them only on owner request. They do not automatically enter the top-five
review, apply changes or create development tasks. Never push, publish or deploy as part of this workflow.
See [application](references/workflow.md).
