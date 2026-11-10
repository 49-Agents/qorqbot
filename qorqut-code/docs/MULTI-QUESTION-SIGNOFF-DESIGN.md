# Multi-question Signoff design

Status: proposed design, November 10, 2026. No runtime implementation or live
record changes. Source baseline: remote `main`, commit `306bb5b`.

An input Signoff can contain several independently answerable questions. The
owner edits the answers freely, then clicks one **Done** button to submit the
whole response. All answers become visible to agents in one transaction and
the Signoff moves from `pending` to `answered` once.

The owner's final direction is one submission for the whole form. Per-question
submission, persisted partial completion and incremental answer notifications
are outside this design. Any progress shown before Done describes a local
draft, not a recorded response.

## What the code already provides

| Surface | Current behavior | Design consequence |
| --- | --- | --- |
| [Input forms](../qorqut-operations/qorqut_operations/signoffs/inputs.py), `FORM`, `validate_form`, `validate_response` | One title/explanation, one choice group and one notes field. Unknown form properties are rejected. Questions default to management-allowed responses. | Add an explicitly versioned form variant; keep the legacy shape and its defaults unchanged. |
| [Response service](../qorqut-operations/qorqut_operations/signoffs/service.py), `respond` | Validates the exact proposal, owner, current status revision and idempotency key; writes the response and changes state atomically. | Extend this path to validate and save a complete answer set. |
| [Core schema](../qorqut-operations/qorqut_operations/storage/core_schema.py) | Proposal bytes and submitted `decision_json` are immutable. Both preview and decision are general JSON. | Keep questions in the frozen form and the complete answer snapshot in the final decision. No new response table is needed for this design. |
| [Draft reviews](../qorqut-operations/qorqut_operations/signoffs/drafts.py) | Multiple editable text fields, immutable draft revisions, separate save and return actions. | Preserve this existing product. Its fields are draft content, not independent questions with choices. Do not reinterpret them. |
| [Signoff notes](../qorqut-operations/qorqut_operations/signoffs/notes.py) | Immutable conversation notes, with a 20,000-character limit. | Generate bounded human-readable answer notes only inside the final submission transaction. |
| [Inbox events](../qorqut-operations/qorqut_operations/inbox/events.py), `EventFinalizer` | A final decision emits `response_submitted`; notes on that Signoff in the transaction are coalesced with it. | Reuse one completion event. Verify coalescing when several question notes are written together. |
| [Dashboard](../biz-dashboard/js/signoffs.js), `sgCreateView` | One shared renderer serves drawer, Focus, Chat and record references. Drafts/choices live in shared in-memory maps; retry state belongs to the whole Signoff. | Add a question-form renderer and a structured draft map to that shared implementation. Keep one submission receipt. |
| [Owner HTTP route](../app/qorqut/transport/http.py), `signoff_route` | `/respond` accepts one notes/choices response or draft-review values. | Accept `answers` for the new form variant and reject mixed response shapes. |
| [MCP presentation](../app/qorqut/transport/mcp_presentation.py) | `signoffs_get` retains decision evidence; compact lists and write receipts select fixed fields. | Keep exact question/answer content on detail reads; add bounded question counts to compact indexes if needed. |

The older checkout used for the first investigation was `1f1ae90`. Current
`main` also contains draft reviews, the host package rename from `bazarlyk` to
`qorqut`, and the [Signoff-backed owner-input plan](SIGNOFF-BACKED-INPUT-PLAN.md).
Implementation should start from current `main`, preserve those additions, and
coordinate with that plan's task/thread bindings.

## Owner experience

One Signoff has one overall title and explanation, followed by numbered
questions. Each question has its own prompt, optional explanation, choices
when appropriate, and an always-available written-answer field. Required and
optional questions are clearly marked. No choice is preselected.

Use the current text, single-select and multi-select interactions. Keep the
full question wording visible. In narrow Chat cards, wrap choices and expand
the form within the existing card; do not open a separate workflow or hide
unanswered questions behind a wizard.

One **Done** button sits below all questions. Selecting a choice, typing,
blurring a field, collapsing the card or switching panes does not submit.
The optional counter says **2 of 3 filled · Not submitted**, based on local
validation; it never says that two answers have been submitted.

Done validates the entire form. Invalid questions receive field-level errors,
the first invalid question receives focus, and all entered values remain.
During submission, freeze the complete snapshot and disable its answer controls.
Success shows each question beside its submitted answer and one submission
timestamp. The existing terminal Signoff state handles completion, queue
removal and Focus release.

Drafts survive polling, collapse/reopen and movement between existing surfaces
within the same dashboard page. Keep the current in-memory privacy boundary;
server draft autosave and cross-reload persistence are separate enhancements.
The interface must not claim that these drafts are durably saved. After Done,
responses have the same immutability as current input Signoffs.

## Form contract

Keep `signoffs_input_create`, `category: input_request`, the existing request-key
deduplication and the normal proposal binding. Add a disjoint `oneOf` to the
form schema: the current unversioned form, or a new `schema_version: 2` form.
Both variants reject unknown fields. `schema_version` describes the form; it
is not the operation version or the proposal revision.

Proposed creation example:

```json
{
  "request_key": "supplier-order-details",
  "form": {
    "schema_version": 2,
    "kind": "question",
    "title": "Confirm the order details",
    "explanation": "These answers will let me prepare the order for review.",
    "response_policy": "owner_only",
    "questions": [
      {
        "id": "supplier",
        "prompt": "Which supplier should we use?",
        "explanation": "I recommend North because the items are in stock.",
        "selection_mode": "single",
        "required": true,
        "choices": [
          {"id": "north", "label": "North", "explanation": "In stock now."},
          {"id": "south", "label": "South", "explanation": "Lower price; delivery next week."}
        ]
      },
      {
        "id": "delivery",
        "prompt": "Where should it be delivered?",
        "selection_mode": "none",
        "required": true
      },
      {
        "id": "extras",
        "prompt": "Which extras should be included?",
        "selection_mode": "multiple",
        "required": false,
        "choices": [
          {"id": "packing", "label": "Packing", "explanation": "Protective packing."},
          {"id": "insurance", "label": "Insurance", "explanation": "Shipment insurance."}
        ]
      }
    ]
  }
}
```

Proposed bounds and defaults:

- 1–10 questions, with a unique stable ID per question, using the existing
  bounded alphanumeric/underscore/hyphen ID vocabulary. Preserve authored order.
  Agent guidance should usually group only 2–5 closely related questions.
- Required nonblank `prompt`, at most 200 characters. Optional question
  `explanation`, at most 8,000 characters. Overall title/explanation retain
  their existing bounds. Limit the complete normalized form to 64 KiB of
  canonical UTF-8 JSON, checked before preparing a write.
- `selection_mode`: `none`, `single`, `multiple`. Choice-based questions retain
  2–26 bounded choices with unique IDs within their own question. The same
  choice ID may occur in different questions. Text questions have no choices.
- `required` defaults to `true`. `notes_required` defaults to `false` and means
  that an answered question must include written text. An optional question
  can still be left entirely blank.
- Every required question needs a nonblank written answer or a valid selection.
  Written text remains an alternative to predefined choices, matching current
  `validate_response` semantics. `notes_required` additionally requires text
  whenever the question is answered. A text-only required question always
  needs nonblank text.
- At most 8,000 characters of notes per answer, 32,000 across all answers,
  and 64 KiB for the normalized response JSON. Preserve submitted text verbatim;
  use trimming only to decide whether it is blank.
- Version 2 initially accepts only `kind: question` and `owner_only` policy.
  Reject an explicit management-allowed value rather than silently changing it.
  Legacy questions, requests and draft reviews retain their current behavior.

Version 2 uses `required` to express whether that question needs a response;
it does not introduce a second `selection_required` flag with an overlapping
meaning. Keep legacy `selection_required` behavior intact. These limits are
proposals, not capabilities that have shipped.

## One atomic response

Extend the existing owner `/signoffs/<id>/respond` route with one `answers`
array. The route and service require the existing four binding fields and
exactly one answer entry for every question, including empty optional answers:

```json
{
  "proposal_revision": 1,
  "payload_hash": "<from the current Signoff>",
  "expected_status_revision": 1,
  "idempotency_key": "<one key for this Done click and its retries>",
  "answers": [
    {"question_id": "supplier", "selected_choices": ["north"], "notes": ""},
    {"question_id": "delivery", "selected_choices": [], "notes": "The main office."},
    {"question_id": "extras", "selected_choices": [], "notes": ""}
  ]
}
```

Reject missing, duplicate or unknown question IDs; choices from the wrong
question; duplicate choices; invalid types; excessive sizes; and combinations
of `answers` with top-level `notes`, `selected_choices` or `draft_values`.
Reject `answers` for legacy forms and legacy response fields for version 2.
At the service boundary, distinguish omitted legacy arguments from explicitly
supplied empty values so mixed shapes are rejected consistently for HTTP and
direct callers.
Normalize answer order to the frozen question order and choice IDs to the
existing canonical sorted order before hashing and writing. Form order remains
part of the immutable proposal; response array ordering is not meaningful.

Use a version-aware validator that returns either a complete normalized
response or structured errors keyed by `question_id`. Validate again on the
server; client validation is for useful feedback. Direct callers of the old
validator, including Overseer, need an explicit unsupported-format error for
version 2 rather than a missing-key exception or a guessed flattened answer.

The transaction performs the existing owner/scope and writer-admission checks,
loads the immutable form, validates all answers, checks the idempotency receipt,
checks the exact current proposal/status binding, and then writes all response
evidence. No note, answer, event or state change survives if any part fails.

The final `decision_json` keeps `kind: respond`, `by` and `at`, and adds
`schema_version: 2` plus ordered `answers`. Each saved answer contains
`question_id`, `selected_choices`, verbatim `notes`, `status` (`answered` or
`skipped`), and its conversation `message_id`. Only an entirely blank optional
answer is `skipped`. Do not infer an answer from the explanation, fill defaults,
or label omission as rejection.

Write one bounded conversation note per question in the same transaction. Each
note names the question, selected labels and submitted text, or explicitly
records an unanswered optional question. The immutable structured decision is
the authoritative answer snapshot; notes are its readable projection. With
the proposed per-question limits, one note fits the existing 20,000-character
bound even when all choices are selected. Validate the rendered length before
writing. Keep the top-level legacy `message_id`, `notes` and `selected_choices`
absent for version 2 so they cannot masquerade as a single answer.

Update the parent once to `answered`, using the existing status revision and
Signoff update sequence. Save one idempotency receipt. Existing note-event
coalescing should yield one `response_submitted` event per recipient; explicitly
verify this with multiple notes, including rollback and retries.

## Compatibility and other consumers

| Consumer | Required handling |
| --- | --- |
| Existing forms and saved decisions | Keep the current API shape, validation, rendering and legacy hash construction. Do not rewrite stored proposal bytes or old decisions. |
| Request-key reuse | Comparing normalized version-2 forms must include question order, IDs, wording, choices and requirements. Reusing the same pending form preserves any local draft; a changed form conflicts as today. |
| Draft reviews | Continue using their save/return endpoints and history. Reject mixed questionnaire/draft fields. Returning content remains separate from approving an action. |
| Agent reads | `signoffs_get` returns every frozen question and its final structured answer. Compact/text presentation must preserve empty notes, empty choice arrays and skipped status. Compact lists may expose `question_count`, but no server-side partial-answer count exists. |
| Management delegation | Keep version 2 owner-only initially. Reject it in the delegated answer path. Leave existing version-1 management routing untouched. |
| Overseer workflows | Their bindings currently map one choice set to actions and use one response. Keep those forms version 1; reject unsupported grouped forms at their creation/gateway boundary. Never flatten questionnaire responses into an action choice. |
| Telegram | Display the group title, question count and dashboard link. Replies remain comments; reactions cannot answer a grouped form. Extend the existing draft-review guard so `answers_alone` cannot treat a free-text reply as a completed questionnaire. |
| Focus, reminders and counts | One parent Signoff remains pending until Done succeeds. Typing changes no server attention, snooze, count or reminder. Existing completion behavior runs once. |
| Task/thread input bindings | The companion owner-input plan binds tasks to the parent Signoff. Draft activity cannot resolve that dependency. One final completion triggers reassessment; it does not mark the task's work done. |
| Cancellation or replacement | Preserve the existing request lifecycle. A stale form cannot submit. Keep local text available for review/copy, but never copy answers automatically into a different proposal. |
| Old browser assets | An old renderer assumes `input.choices` and `selection_mode`; a version-2 form can break it. Deploy a supported-version guard before enabling grouped creation, or ship host/UI together with a verified refresh requirement. Unsupported forms must show a reload/read-only state and offer no decision controls. |

The core schema already accepts the proposed JSON shapes, so no new SQLite
table or state migration is expected. Confirm schema identity remains unchanged
in implementation and prove old rows load unchanged. Compatibility still needs
coordinated engine, host, dashboard, Telegram and agent-guidance changes;
additive JSON support alone is insufficient for an older renderer or gateway.

## Dashboard implementation shape

Extract a small pure form adapter and validator from `signoffs.js`, plus a
question-form renderer used by all existing Signoff surfaces. Keep action
approval and draft-review components on their current paths.
Make `completePreview` version-aware as well: it currently requires one
top-level `input.choices` array and cannot admit the proposed nested form.

Use one structured draft map, keyed by owner, review scope, Signoff ID,
proposal revision and payload hash, with nested answers keyed by question ID.
Do not include `status_revision` in the draft key: an attention update must
not erase text. DOM/control IDs also need the mounted-view prefix and question
ID; choice IDs alone are insufficient when several questions reuse `yes`/`no`.

Preserve active field nodes, caret position and IME composition during polling
and sibling-view refreshes. Sync values across shared surfaces without replacing
a field being edited or confusing the Signoff conversation composer with an
answer. Group radio buttons and their keyboard movement within each question.
Link errors to the corresponding prompt and field with accessible labels.

Ctrl/Cmd+Enter can invoke the one Done action from an answer field when not
composing; plain Enter edits text. It must validate the entire form and obey
the same busy guard. The demo uses a mouse/keyboard-accessible Done button;
it is not evidence that these production keyboard integrations exist yet.

Freeze all answers under one receipt when Done is clicked. Retry the same
normalized payload with the same key after an uncertain network result.
`finishReceipt` must verify the returned version-2 answer snapshot against that
receipt before clearing drafts, as the current draft-review path verifies its
returned values. Another tab's different final response must show an explicit
conflict and preserve the local draft. A newer attention/status revision is a
normal stale-binding case: refresh, keep text and require a fresh deliberate
submission; do not generate a new key and silently replay changed intent.

## Implementation sequence and acceptance checks

1. Add legacy/version-2 schema dispatch and pure normalization/validation.
   Cover repeated question IDs, reused choice IDs across questions, text-only
   answers, optional omissions, all choice modes, sizes, Unicode and mixed shapes.
2. Extend the existing response transaction and DTO, then owner HTTP handling.
   Prove all-or-nothing writes, unchanged legacy idempotency, canonical response
   ordering, duplicate Done clicks, stale revisions, cancellation races,
   unauthorized/global/cross-project requests, and immutable final decisions.
3. Add the shared dashboard form and structured draft store. Exercise drawer,
   Focus and inline Chat; required errors; one Done; polling during typing;
   same IDs in different questions; narrow layouts; lost-response retries;
   two-tab conflicts; and preservation of ordinary approvals and draft reviews.
4. Update MCP evidence/presentation, Telegram guards, management and Overseer
   boundaries, owner-input guidance and the stale notification documentation.
   Prove one proposer response event, no answer events before Done, exact
   per-question evidence, and no action execution from question answers.
5. Ship the compatibility guard and coordinated feature through the existing
   release workflow when separately authorized. Publishing source does not
   update running agents' pinned instructions or reload old browser tabs.

Current focused dashboard harnesses include
[signoff-chat-reference.test.js](../biz-dashboard/tests/signoff-chat-reference.test.js),
[work-signoff-link.test.js](../biz-dashboard/tests/work-signoff-link.test.js),
and [signoff-attention-pill.test.js](../biz-dashboard/tests/signoff-attention-pill.test.js).
The published Operations package does not include the full test directory
named by older documentation. Add narrowly scoped executable engine/host
fixtures in the implementation branch rather than claiming those tests exist.

Investigation checks run against `306bb5b`: existing multi-select accepts
several choices; a `questions` array is rejected; written text satisfies legacy
`selection_required`; draft reviews accept several editable fields and enforce
owner-only policy; the real SQLite decision trigger rejects an incremental
second response write. All five checks passed using source imports and an
in-memory database. These verify current constraints, not the future feature.

## Remaining implementation choices

The one-Done submission rule is settled. The numeric bounds, helper/module
names and whether to expose a compact question count remain implementation
choices. Durable draft recovery, conditional questions, attachments,
per-question permissions and grouped action approvals are separate features.
They are not needed to deliver the requested form.
