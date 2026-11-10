---
name: qorqut-role-owner-input
description: Ask the owner a question or request a missing decision, access, or input through Signoffs; retrieve the submitted response and manage notification urgency.
---

# Owner input

Global agents decide what needs the owner's attention. Read existing decisions needed
for your work and keep the original thread, Task and artifact references.

Pushing any branch, merging and deploying need the owner's clear approval for that exact
work. A clear chat approval, including one that covers a branch push, needs no extra
Signoff; an implementation request alone is not push, merge or deployment approval.
When push or merge authorization is unclear, create one blocking owner-only input
request (`attention.notify: true`, `blocking: true`, a `reason`) naming the exact
work, repository, branch or target and every proposed action. Combine merge and push
in that one request when both are proposed; a review-branch push never becomes a
merge proposal. Deployment is its own scope: when its authorization is unclear,
request a blocking Signoff before deploying. A native Git/deployment decision is an
input request: it records the answer and executes nothing, so read the decision and
then perform exactly that work yourself. Use the existing prepared approval for a
service-managed protected action. Never replace a required decision with a chat reply.

Use `signoffs_input_create` for an owner response that is not an approval to
execute a prepared action. Keep platform and internal-change approvals on their
existing tools. An answer to a question does not bypass those approvals.

Choose the current project; use `scope: global` only for an ecosystem-wide
question when your registered scope permits it. Do not relocate a project
request to Global to get more attention.

Give the request a stable `request_key` describing the same outstanding issue.
Repeated identical questions return the existing pending item. If the question
or choices materially change, withdraw the old input with
`signoffs_input_cancel`, then submit its replacement. Avoid duplicate requests
for each retry of a blocked operation.

The `form` contains `title`, `explanation`, `kind` (`question`, `request`, or `draft_review`), and
`selection_mode` (`none`, `single`, or `multiple`). For choices provide at least
two entries with unique stable `id`, a one/two-word `label`, and `explanation`.
Use `selection_required` and `notes_required` only when that information is
necessary to proceed. Notes are always available; the owner submits notes and
selections together. Put your recommendation and the consequences of waiting
in the explanation.

## Editable draft review

Use `form.kind: draft_review` when the owner should edit proposed content, such
as email copy, a document, a brief or a script. Supply `draft_fields`: 1–12 fields
with unique stable `id`, a readable `label`, and the initial text in `value`.
Use `multiline: false` for a subject or other single line (the default is
multiline); use `required: true` only for fields that must be nonblank when
returned. Each value permits 16000 characters and the combined values permit
32000 characters. Preserve literal placeholders such as `{{name}}`.

Draft reviews are always `owner_only`. The owner edits the fields, can save
versions without submitting, and uses **Return draft** to submit content and
optional feedback. Choices and required notes work as they do for other input
requests. A saved version is unfinished work: consume the content only once
the request is `answered`, from `decision.draft_values`; `decision.draft_revision`
identifies the returned version. The original and all saved versions remain
available in `signoffs_get` (`draft_review` and paginated `draft_history`).

Keep one pending request per review, using its stable request key. Do not
withdraw it merely because the owner saved an edit. For a further review round
after an answer, use the returned values as the new initial values and include
`form.previous_review_id` with the answered review's ID. That prior review must
belong to you in the same scope. Keep the existing Task/thread association and
present the review there using its typed Signoff reference.

Returning a draft gives you content and feedback. It does not authorize sending
the email, publishing, activating a campaign or setting its schedule. Obtain
the appropriate separate action approval when needed.

Set `attention.notify: true` when the item should notify the owner. Delivery
uses the available notification connector; do not promise a fixed daily summary.
Mark `urgent` when delay is time-sensitive, or `blocking` when work cannot
continue; supply `reason` for either and `deadline` when relevant. A blocked low-priority task need
not be described as urgent. The owner controls snooze. Do not create reminders
as additional signoffs or send separate messages.

Use `signoffs_attention_set` to update attention settings on your own pending
signoff, including an existing prepared approval. This replaces its notification
settings; specify the flags you intend to retain. Clearing a resolved blocking
condition does not answer or approve the underlying signoff.

On your next work check, read `signoffs_get` or `signoffs_list` (optionally
`responded: true`). Submitted responses are in `decision`, with notes/feedback,
selected choice IDs, actor and timestamp. The agent inbox delivers submitted
responses to the proposer; a backend notice may type a short system-generated
reminder into your native session, even while you are busy, but it names no event
and acknowledges nothing. Keep working on the direct user request, check
`inbox_list` at a suitable checkpoint or on completion, and follow the built-in
inbox instructions in mandatory bootstrap context to handle and acknowledge the
exact events. Events that arrive while no native session is running wait for the
next manual start. Retain submitted answers with the signoff; lasting project
instructions can enter Context through its existing authorized source input
(`context_upload` or a focused `context_instruct`) when appropriate.

## Work, questions, and discussion

New substantive owner work is an explicitly owner-assigned task of
`kind=request_to_owner`, with ordinary dependencies, snooze and completion. Keep
existing request Signoffs operable; do not duplicate a live request. Small login
help is raised to Ordubashy for a configured desktop Reference. Questions remain
input Signoffs with explicit answers. Use `signoffs_comment` for ordinary scoped
discussion before or after a decision. Comments neither answer nor approve.
Keep the encompassing parent Task association when escalating coherent work.
