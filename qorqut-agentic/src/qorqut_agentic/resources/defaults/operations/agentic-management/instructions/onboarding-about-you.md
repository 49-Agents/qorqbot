# About you

Learn the basics about the owner, and which tools they want connected, from one
short set-up form. This should take the owner about a minute. It is a first
conversation, not a full profile: more will be captured later from ordinary
work.

## Before asking

Read the personal context node: `context_home` returns `personal_context.ref`;
read it with `context_object`. Do not ask for anything the node already holds.

If `context_home` returns no personal context pointer, do not create a profile.
Ask only the tools question below, tell the owner the personal store is
unavailable on this installation, and complete the step with that as the reason.

## The set-up form

Create one Signoff with `signoffs_input_create`: a version-2 question form as
Owner input describes, `response_policy: owner_only`, request key `get-set-up`,
title "Getting set up", explanation "Everything is optional. Answer what you
like and press Done.", and the set-up Task as its `source`. Leave out each
question the personal context node already answers. Every question has
`required: false`:

1. `name`, no choices: "What should I call you?"
2. `work`, no choices: "What do you do?", explained as "Your work or business
   and your role, in a sentence."
3. `help`, no choices: "What would you like help with first?"
4. `tools`, multiple choice: "Which tools should I connect?", explained as
   "Email and calendar are a good start." Choices `email` (Email: Gmail, Outlook
   or similar), `calendar` (Calendar: Google Calendar or similar), `chat` (Chat:
   Slack or similar) and `documents` (Documents: Google Drive, Notion or
   similar).

Show it in Chat with `request_user_input` and the form's Signoff reference, with
one short line such as "A few quick questions to get you set up, all optional."
Do not repeat the questions in Chat. Then wait for the answer.

When the form is answered, read the decision with `signoffs_get`. Use each
answer's notes as written; the tools answer is its selected choices plus
anything the owner wrote in its notes. If the owner answers in Chat instead,
use those answers and cancel the form with `signoffs_input_cancel`. If they
skip everything, save nothing and do not ask again in this run.

## Saving

Save the answers to the personal context node with one `context_edit` for the
whole step, following your Personal context rules: keep the node ID, its person
type and every unrelated attribute; choose plain attribute names; put the
owner's statement, this conversation as its source and today's date in the
rationale; cite `self`. Record only what the owner said. Do not infer a
timezone from a city without saying so, and do not invent values.

A queued edit is not yet published. Check its result before telling the owner
anything was saved. If the base revision moved, rebuild the edit on the current
revision. If it cannot be saved, say so and keep the answers in the Task thread
for follow-up.

Things the owner says about how they want you to work (tone, approvals, rules)
are not personal facts. Note them in the Task thread and handle them through
the existing preferences workflow after this run.

## Completing

Complete the step with output:

- `saved`: the attribute names written or already current
- `skipped`: what the owner chose not to answer
- `edit`: the queue input ID of the edit and its outcome, or null when nothing changed
- `tools`: the tools the owner chose, as written (an empty list when none)
- `signoff`: the form's Signoff ID

Then move to the next ready step without waiting to be asked.
