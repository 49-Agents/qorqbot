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

A question Signoff is the item's open question: the owner sees it on the item's timeline and answers it in
the sign-off queue, so never file it on the timeline as well. Link it to the item with `source`, and offer the likely
answers as choices; the owner can always write something else. Only the coordinator writes timelines: once the owner
answers, the coordinator is told as for any Signoff, reads the answer with `signoffs_get` and decides what it means: the
owner may answer, or say the question is not needed or was settled already. When it settles a decision or a fact, the
coordinator files it in its own words with `timeline_file_answer` (filed at once without review); otherwise it files
nothing. If unclear, please ask the user. A worker reports such a point to the coordinator and does not call `timeline_file`.

Collect every explicit request for owner input through a Signoff, including clarifications,
access, missing information and review. Chat and progress notes can explain the request, but
are not its response record. Use `form.response_policy: owner_only` for owner input;
`management_allowed` remains for internal delegated questions.

Use `signoffs_input_create` for an owner response that is not an approval to
execute a prepared action. Keep platform and internal-change approvals on their
existing tools. An answer to a question does not bypass those approvals.

When work or a record thread is waiting, pass `source: {type, id}` at creation.
Use the actual source identity (`task`, `table`, `project`, or `reference`): a reference is the
record's type and id, and its project is found from the id. A Project source names its own UUID as
`id`. Source and Signoff keep their own scopes. Omit source for a standalone question.
Link an existing pending approval with `signoffs_source_link` instead of creating another
question. Call it again for each additional dependent source. Repeated links are idempotent.
Only an explicit pending dependency produces Needs your input; contextual `concerns` links,
owner worker assignment, discussion and old free-form task statuses do not. Never manually
set `status: needs_input`. The original work status is preserved while the indicator is shown.

After the last pending request is answered, cancelled, rejected or superseded, the indicator
clears automatically. Read the exact Signoff decision and reassess the work; this does not
mark the Task done. Replacement questions need their own explicit source link. Remove an
obsolete dependency with `signoffs_source_unlink(source, signoff_id, link_id, expected_link_revision)` using
`link_id` and `link_revision` from the source's `input_requests`; it does not cancel the Signoff. Preserve
answers in Signoffs. Never infer a missing question from a legacy status: read the work,
link an existing question, create the concrete missing one, or correct an obsolete work status.

Choose the current project; use `scope: global` only for an ecosystem-wide
question when your registered scope permits it. Do not relocate a project
request to Global to get more attention.

Give the request a stable `request_key` describing the same outstanding issue.
Repeated identical questions return the existing pending item. If the question
or choices materially change, withdraw the old input with
`signoffs_input_cancel`, then submit its replacement. Avoid duplicate requests
for each retry of a blocked operation.

The owner states a fact about themselves once. Before asking the owner anything about
themselves, read the personal record (`personal_get`) and
your own open questions (`signoffs_list` with `category: input_request`,
`states: [pending]`). Leave out what the record answers, what the owner already answered
on an earlier form, and what an open question already asks; ask a question that several
pieces of work need once, in one request. When the owner answers a question that another
open form also asks, withdraw that form with `signoffs_input_cancel` and post it again
with that question filled in from the answer, or without it. The personal-record skill
explains the record's tools.

The unversioned `form` contains `title`, `explanation`, `kind` (`question`, `request`, `draft_review`, or
Ordubashy's `acknowledge` and `browser_action`, each described under its own heading), and
`selection_mode` (`none`, `single`, or `multiple`). For choices provide at least
two entries with unique stable `id`, a one/two-word `label`, and `explanation`.
Use `selection_required` and `notes_required` only when that information is
necessary to proceed. Notes are always available; the owner submits notes and
selections together. Put your recommendation and the consequences of waiting
in the explanation.

## Questions with separate choices

Use a version-2 question form when each question needs its own single-select or
multi-select choices. Keep content editing and text questionnaires that benefit
from prefilled fields or saved versions on `draft_review`; do not replace an
existing pending review just to use the new form.

Set `form.schema_version: 2`, `kind: question`, `response_policy: owner_only`,
`title`, `explanation`, and `questions` (1–10). Every question has a unique stable
`id`, a `prompt`, and `selection_mode` (`none`, `single`, or `multiple`). Its
`explanation` is optional. For choices, supply 2–26 entries with `id`, a one/two-word
`label`, and `explanation`. Choice IDs need only be unique inside that question.
Do not include the unversioned form's top-level `choices`, `selection_mode`,
`selection_required`, `notes_required`, or `draft_fields` in a version-2 form.

Questions default to `required: true`; use `required: false` when skipping is
acceptable. A selection **or a written alternative** answers a required question.
Use per-question `notes_required: true` only when a written answer is necessary;
an optional question can still be skipped entirely. Each written answer allows
8000 characters, with 32000 combined and a 64 KiB normalized answer limit. The
normalized form also has a 64 KiB limit. Put only related questions in one request.

The owner edits everything locally, then clicks one **Done** button. There is no
saved partial response, no automatic submission, and no per-question notification.
Local edits survive polling and switching dashboard surfaces, but not a full
browser reload. Until the whole submission succeeds, the Signoff and its linked
work remain pending. Telegram replies are comments; reactions cannot answer it.

After `state: answered`, read the complete `signoffs_get` evidence:
`decision.schema_version: 2` and `decision.answers` in the original question
order. Each answer has `question_id`, `selected_choices`, verbatim `notes`,
`status` (`answered` or `skipped`), and its conversation `message_id`. Read both
selections and notes; a written alternative can differ from the suggested options.
Only the final decision is the response record. One response event covers the
whole form. It grants no authority to execute a prepared action.

Discover the installed `signoffs_input_create` schema before using this variant.
Older deployments may only support the unversioned form. Existing draft reviews
and single-question Signoffs continue to work unchanged.

## Editable draft review

Use `form.kind: draft_review` when the owner should edit proposed content, such
as email copy, a document, a brief, a script or a text questionnaire. Supply `draft_fields`: 1–12 fields
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

## Read and acknowledge

`form.kind: acknowledge` is the attention Signoff, shown to the owner under **For
your attention**. It asks the owner nothing: it puts an answer, a report or a document
in front of them and records that they read it. Only Ordubashy can post one. A worker
with a report or document the owner must see hands it to Ordubashy, with the file,
the Task it belongs to and the reason the owner needs it.

`report_task` names the real Task the answer or report belongs to, in its Project
or Global. One Task can hold many. Omit it when there is no such Task; never create
a Task only to hold one.

Whenever a message from the owner contains a question, or asks to be told, shown or
updated on something, answer in an attention Signoff, one-line answers included.
Look the answer up in the records first. Post one Signoff per owner message, under
its own new `request_key`: quote each question at the top and answer it in full
under it. In chat write one short line and show the Signoff as a card under it
with `request_user_input`; do not repeat the answer there. Never use it to obtain
a yes: an acknowledgement approves nothing. Ask a question when you need a
decision.

The form is `title`, `explanation`, `kind: acknowledge` and `selection_mode: none`.
The explanation is the text the owner reads, shown as Markdown (headings, lists,
tables, links), up to 8000 characters. Put a longer document in a file and link it
from the explanation with the Markdown link `files_link` returns, naming the
document version when it has one, and keep the points the owner must not miss in
the explanation itself. Choices, required notes, `source`, `urgent` and `blocking`
are refused: nothing can wait on it, and it never shows as Needs your input.

It notifies the owner once when it is posted and then waits in the dashboard's
**For your attention** group, apart from decisions. The owner clicks **Acknowledge**. A report
has no conversation of its own: opening it opens its Task's thread beside it, so
the owner writes any comment there; treat a comment as a new owner input. Posting another
one with the same `request_key` replaces the one the owner has not acknowledged
yet, so a recurring report keeps one key. One the owner already acknowledged is
kept as it is.

After the owner acknowledges, the Signoff is `state: answered` with
`decision_state: acknowledged`; `decision.by` and `decision.at` say who and when.
There are no notes or choices to read.

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
exact events. Events that arrive while your session is closed wait. Qorqbot opens it for them by
itself unless you are turned off or run in a terminal; then they wait until you are started. Retain submitted answers with the signoff; lasting project
instructions can enter Context through its existing authorized source input
(`context_upload` or a focused `context_instruct`) when appropriate.

## Browser action

When the owner must do something in a browser page (sign in, solve a captcha, type a code, confirm a payment), Ordubashy
files `form.kind: browser_action` with the tab: run `tab-reference` in the page and put its reference id in `tab`. A worker
gives Ordubashy the reference, the page and what the owner must do. The tab must be open; the result's `tab` gives its
`title` and `url`, so check it is the page you mean. Say in plain words what to do in the page.

```
signoffs_input_create(request_key="sign-in-mercury", form={"title": "Sign in to Mercury", "kind": "browser_action",
    "explanation": "Sign in with your usual account and approve the code on your phone.", "selection_mode": "none",
    "tab": "<agent uuid>:<tab id>"}, attention={"notify": true, "blocking": true, "reason": "The payout run waits on it"},
    source={"type": "task", "id": "<task id>"})
```

Send no choices or required notes: the owner opens the tab from the attention queue and answers **Done** or **Could not
do it**, with an optional note (`decision.selected_choices` is `["done"]` or `["not_done"]`). After Done, check the page
yourself before going on; after Could not do it, read the note. The answer approves nothing.

## Work, questions, and discussion

New substantive owner work is an explicitly owner-assigned task of
`kind=request_to_owner`, with ordinary dependencies, snooze and completion. Keep
existing request Signoffs operable; do not duplicate a live request. Small login
help uses a configured desktop Reference; collect its owner response through an owner-only
Signoff linked to the Reference. Questions remain
input Signoffs with explicit answers. Use `signoffs_comment` for ordinary scoped
discussion before or after a decision. Comments neither answer nor approve.
Keep the encompassing parent Task association when escalating coherent work.
