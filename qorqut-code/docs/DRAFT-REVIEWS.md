# Editable draft reviews

Draft review is a mode of the existing `input_request` Signoff. An agent supplies
editable content; the owner saves versions and returns an edited draft with
feedback. The request becomes `answered`, never `approved` or `applied`.
Returning content executes no send, publication, campaign update or schedule.

## Creating a review

Use the existing scoped `signoffs_input_create` operation and mutation
idempotency key. For example (synthetic content):

```json
{
  "request_key": "launch-email-copy",
  "form": {
    "kind": "draft_review",
    "title": "Review launch email",
    "explanation": "Edit the subject and body, and leave any feedback below.",
    "selection_mode": "none",
    "draft_fields": [
      {"id": "subject", "label": "Subject", "value": "Hello {{name}}", "multiline": false, "required": true},
      {"id": "body", "label": "Body", "value": "Here is the first draft for {{company}}.", "required": true}
    ]
  },
  "idempotency_key": "create-launch-email-review-1"
}
```

There are 1–12 uniquely named fields, each up to 16000 characters and 32000
characters combined. IDs start with an ASCII letter and use letters, digits,
underscores or hyphens (40 characters maximum). Labels permit 100 characters.
Multiline defaults to true; single-line values reject CR/LF. Required fields may
be blank during editing, but must be nonblank on return. Content is literal
text: placeholders are preserved and markup is displayed as text.

Questions/choices can accompany a draft with the existing `choices`,
`selection_mode`, `selection_required` and `notes_required` fields. The effective
response policy is always `owner_only`, even if the caller supplies
`management_allowed`. Management cannot answer draft reviews. Existing scope,
visibility, notification, Focus and cancellation rules apply. An identical
repeat request reuses the pending review without replacing the owner's edits.

## Saving and returning

Draft reviews reuse the existing in-chat Signoff card, message editors and
action row. Inline multiline content starts as the same six-line preview; click
the preview to expand and edit it. Fields grow with the text. While editing,
the action row shows **Save** and **Discard**. Save durably stores a version
without answering; Discard restores the saved content. Once saved, **Return
draft** submits the edited content, accompanying choices and feedback together.
Feedback uses the existing one-line input, and version history stays collapsed.
A returned draft is read-only.

Owner HTTP routes use the review's own business or Global namespace:

| Route | Body |
|---|---|
| `POST /api/biz/<slug>/signoffs/<id>/draft` (or `/api/root/signoffs/<id>/draft`) | Current `proposal_revision`, `payload_hash`, `expected_status_revision`, `idempotency_key`, and complete `values` map keyed by field ID |
| `POST .../signoffs/<id>/respond` | Same binding, complete `draft_values`, optional `notes` and `selected_choices` |
| `GET .../signoffs/<id>?draft_history_before=<revision>` | Read an older page of version history |

These writes are owner-only dashboard/worker commands, not agent MCP methods.
Values must name exactly the declared fields; callers cannot add fields, alter
labels or change the proposal. The original proposal/hash remains immutable.
Every changed save appends a version and advances the status revision. An
unchanged save adds no version. Retries reuse their receipt; changing a retry's
body conflicts. Concurrent edits and responses use the current status revision;
the losing write is refused and local edits remain available. After comparing
the current saved version, **Keep my edits** explicitly rebases local edits for
a subsequent save. An uncertain save must be retried before another action.

`signoffs_get` returns `draft_review` with current `revision`, `values`,
`created_by` and `created_at`. `draft_history.items` is newest-first, 20 saved
versions per page, with the original at revision 0 on the final page. Pass
`draft_history_before` from `next_before` to `signoffs_get` for older versions.
History is immutable; reads use the parent Signoff's current visibility. Raw
version storage is not a separate public read interface.

After return, the existing `decision` includes `draft_values`, `draft_revision`,
feedback in `notes`, selected choices, actor and time. Agents should consume
these only after `state: answered`. `signoffs_list` includes the final decision
without duplicating the version history. Saved versions are working material,
not owner instructions or permission to act.

The optional Telegram connector directs the owner to the dashboard for editing
and returning fields. Written replies are comments; neither replies nor
reactions can return a draft, even when its accompanying choices are Yes/No.

For another review round, create a new request with `form.previous_review_id`
and the returned values as its initial draft. The referenced review must be
answered, from the same proposer, and in the same review scope. The dashboard
links the previous review. Keep its existing Task and conversation references.

## Storage and installation

The Signoff storage contribution adds `signoff_draft_revisions`, an append-only
table with `(signoff_id, revision)` identity and update/delete refusal triggers.
The proposal stays in `signoffs.preview_json`; returned content is also sealed
in its immutable decision. No provider credential, transport binding or send
job is created by this feature.

Fresh instances use the composed target schema. Existing instances require the
explicit offline `qorqut.migration.draft_reviews` command with the intended
component and extension selection. `inspect` is read-only. `apply` takes the
exclusive instance lock, requires the exact predecessor or target fingerprint,
adds only the table/triggers, verifies schema and foreign keys, and advances the
target identity atomically. Repeating apply is a verified no-op. Foreign or
partially changed schemas are refused. It never runs during service startup;
deployment and migration require their own release authorization.
