# Owner input and Signoff notifications

Signoffs is the owner-response inbox. `input_request` extends the existing
`internal_change` and `platform_action` categories without changing their exact
approval or execution bindings. The shared application, SQLite, existing MCP,
and dashboard HTTP transports remain authoritative.

For editable content, use [Draft reviews](DRAFT-REVIEWS.md): the same input
request lifecycle with owner-edited fields, saved versions and a returned draft.
Returning a draft is separate from approval to execute an action.

## Agent operations

`signoffs_input_create` takes a stable `request_key`, a `form`, optional
`attention`, optional `scope: global`, and the usual mutation idempotency key.
Global input requires registered ecosystem scope and `read_global_context` as
well as the operation grant. Business agents cannot choose a different business
or route their request into Global. Human input creation follows current owner
mutation policy. Creation does not need an approval to ask a question.

```json
{
  "request_key": "supplier-choice",
  "form": {
    "kind": "question",
    "response_policy": "owner_only",
    "title": "Which supplier should we use?",
    "explanation": "I recommend the first supplier because stock is available today.",
    "selection_mode": "single",
    "selection_required": true,
    "notes_required": false,
    "choices": [
      {"id": "first", "label": "First supplier", "explanation": "Available today."},
      {"id": "second", "label": "Second supplier", "explanation": "Lower price; delivery next week."}
    ]
  },
  "attention": {"notify": true, "blocking": true, "reason": "Ordering waits for this choice"},
  "idempotency_key": "a-unique-command-key"
}
```

`selection_mode` is `none`, `single`, or `multiple`. Choice-based forms have 2–26
choices with unique IDs, one/two-word labels, and explanations. `selection_required`
accepts either a selection or a nonblank written alternative; `notes_required`
requires text. All text and choice counts are bounded by the installed JSON
schemas. The owner can always supply notes.

### Several questions, one Done

Use `schema_version: 2` when each question needs its own choices. Existing
`draft_review` forms remain appropriate for editable content and text
questionnaires with prefilled fields or saved versions.

```json
{
  "request_key": "workshop-arrangements",
  "form": {
    "schema_version": 2,
    "kind": "question",
    "title": "Confirm the workshop arrangements",
    "explanation": "Answer these together before we prepare the final plan.",
    "response_policy": "owner_only",
    "questions": [
      {
        "id": "room", "prompt": "Which room?", "selection_mode": "single",
        "choices": [
          {"id": "main", "label": "Main room", "explanation": "More space."},
          {"id": "small", "label": "Small room", "explanation": "Quieter setting."}
        ]
      },
      {"id": "details", "prompt": "Anything else?", "selection_mode": "none", "required": false}
    ]
  }
}
```

Supply 1–10 questions with unique IDs, prompts up to 200 characters, and an
optional explanation. `selection_mode` is `none`, `single`, or `multiple`;
choice modes need 2–26 choices whose IDs are unique within that question.
`required` defaults to true and accepts a selection or a written alternative.
`notes_required` defaults to false and requires text when that question is
answered; an optional question may still be skipped entirely. Version 2 only
supports `kind: question` and owner-only responses. Legacy top-level selection
and draft fields cannot be mixed into it. The normalized form must fit 64 KiB.

One **Done** button submits the whole form. Before that, edits are local to the
browser session, shared across Signoff views and retained during polling; a
full browser reload loses them. They do not update the Signoff, notify an agent,
or release linked work. Each written answer permits 8000 Unicode characters,
with at most 32000 across the form and 64 KiB for the normalized answer array.

The same `/respond` endpoint takes the four binding/idempotency fields below
plus `answers`, containing exactly one entry for every question, including
skipped optional questions:

```json
{
  "answers": [
    {"question_id": "room", "selected_choices": ["main"], "notes": ""},
    {"question_id": "details", "selected_choices": [], "notes": ""}
  ]
}
```

Do not also send top-level `notes`, `selected_choices`, or `draft_values`.
Validation completes before any write. The server stores one immutable
`decision` with `schema_version: 2` and ordered `answers`; each includes the
submitted fields, `status: answered|skipped`, and a conversation `message_id`.
Text is retained verbatim. Reordered answers and selections normalize to the
same retry hash. A lost response is retried with the same body and key; a stale
binding requires rereading and a new deliberate submission. Telegram replies
remain comments and reactions cannot complete a grouped form.

Same proposer + scope + request key deduplicates an outstanding identical form;
a changed form is rejected rather than altering a question behind a draft.
An unchanged repeat does not advance the response revision or reminder timer.
Withdraw an obsolete question with `signoffs_input_cancel` before replacing it.
The old row remains available through scoped reads. Cancellation is restricted
to its proposer or the owner and cannot cancel a prepared platform action.

`signoffs_attention_set` replaces notification settings on the proposer's own
pending signoff, including prepared approvals. Owners may also set them.
Active ecosystem Ordubashy may additionally reprioritize other agents' accessible
pending signoffs. This is attention-only authority; cancellation, answers,
approvals and execution controls do not expand. Owner snoozes are preserved.
The settings are `notify`, `urgent`, `blocking`, `reason`, and optional timezone-
aware `deadline`. Urgent/blocking requires a reason. Deadline is explanatory
metadata; it does not silently escalate priority. Omitting attention on a
repeat input creation preserves the existing settings.

## Responses and history

The owner posts to `/api/biz/<slug>/signoffs/<id>/respond` or
`/api/root/signoffs/<id>/respond` with `proposal_revision`, `payload_hash`,
`expected_status_revision`, `idempotency_key`, `notes`, and `selected_choices`.
Only an active authorized owner can answer; agents cannot use the response
endpoint through MCP. Input requests move from pending to answered atomically.
Required answers, membership, cardinality, duplicates, stale revisions and
idempotency conflicts are validated before accepting a response.

Approval and rejection retain nonblank feedback verbatim (up to 8000 characters);
input responses retain notes and selected choice IDs. `decision` includes actor
and timestamp. Submitted decisions are immutable in SQLite. Input responses
never create apply receipts or run a prepared action; approve/reject reject an
input request. Existing approval and execution semantics remain unchanged.

Agents retrieve answers with `signoffs_get` and `signoffs_list`; `responded: true`
filters saved decisions. The dashboard offers Waiting and Responded. Reading is
not acknowledgment, and drafts are not submitted. The proposer receives one
`response_submitted` inbox event for a completed response, including a grouped
form. Its per-question conversation notes do not create separate answer events.
Configured native inbox notices may alert an existing session; they acknowledge
nothing and do not start an inactive agent. Answers are not automatically filed
as context, tasks, or Stories.

## Notification lifecycle

`signoff_attention` holds scheduling separately from immutable proposal content.
Urgent OR blocking notified items deliver immediately and hourly until resolved.
There are no quiet hours. Ordinary notified items collect into one summary at
08:00 `America/New_York` each day, including previously reported pending items.
The zone handles daylight saving changes. Empty summaries are skipped. On restart,
missed intervals coalesce into a current delivery rather than replaying a backlog.

The existing Signoff service worker checks notification scheduling every 15
seconds, in short serialized database transactions. Assembly/imports do not start
workers or create deliveries. Each delivery has a stable unique key. A new
reminder replaces an older undismissed delivery for the same item/owner; a new
summary replaces its previous summary. Delivery reads recheck ownership,
notification settings, pending status and snooze. Answered, cancelled, superseded,
and inaccessible items disappear from pending deliveries without deleting history.

`GET /api/me/attention` returns authorized business totals (pending signoffs plus
incomplete tasks assigned to this human), plus urgent/blocking Signoffs across
businesses and Global. Task completion follows configured closed states; archived
and unassigned tasks are excluded. `GET /api/me/attention/deliveries` returns
currently deliverable items. The same owner/scope checks apply to every read.

`POST /api/me/attention/deliveries/<id>/dismiss` takes `{}` and acknowledges only
that delivery, idempotently. `POST /api/me/attention/signoffs/<id>/snooze` takes an
absolute timezone-aware `until` in the next 30 days. Retrying the same future
value is safe. Snooze defers delivery, not resolution or red indicators. The UI
provides a four-hour snooze shortcut; ordinary snoozes resume with a future daily
summary. Normal hourly repetition is automatic and requires no snooze action.

The dashboard polls attention every 15 seconds and on window focus. Bell toasts
appear bottom-right across workspaces, never navigate automatically, and provide
explicit navigation, dismiss and snooze controls. Ordinary summaries contain
clickable business rows. One toast is displayed at a time, prioritizing urgent/
blocking individual deliveries. Business badges and the pane's urgent section
include pending urgent/blocking items even when snoozed or notification is off.
The configured [Telegram bridge](TELEGRAM-OWNER-BRIDGE.md) can also deliver Signoff
notifications. Owner reminders and agent response-inbox delivery are separate.

## Installation and verification

Fresh local preparation and manual startup are the supported path; no numbered migration or automatic host upgrade
is supplied (see [Release worker](release/README.md)). Dashboard assets ship with core. Roles that use input Signoffs
carry the on-demand `owner-input` procedure and the mutation grants; those grants do not expand scope, and a source
change adopts no agent pin. Existing roles remain usable with explicit overrides.

Offline tests cover input/decision integrity, response races, scope,
owner/task counts, reminders, snooze, summary timing, and HTTP/MCP. Dashboard
browser tests exercise notes, fixed choice geometry, keyboard interaction,
response history, badges, cross-business navigation and toast actions with
synthetic data. No test uses production businesses or provider credentials.
