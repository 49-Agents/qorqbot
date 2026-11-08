# Supervised (manual) Context edits

Source of truth for the supervised variant of `context_edit` (production contract decisions 2 and 3). Code:
`app/qorqut/shared_context/supervised_edit.py` (adapter), `graph_edit.py` (`GraphEditPorts.supervised_pipeline`, ownership rules and
`GraphEditAuthority`, otherwise unchanged) and `app/qorqut/domains/context_sources.py` (the one `context_edit` descriptor). The queued
`qorqut-graph-edit-v1` edit, its Operations `context_graph_edits` binding and its driver are untouched; this variant does not use them.

## What it is

A manual edit is one owner-reviewed batch of node, edge and **note** operations plus the exact Markdown document that explains it.
It is published directly, as one new immutable Context event, with no model, through `Pipeline.submit_supervised` of the Context
engine. The document is preserved byte for byte as `foundation/supervised-edits/<event_id>.md`; the event's execution marker carries
the normalized plan, its digest and the document digest. Nothing is queued, no Operations binding or table is involved, and the host
never builds a filer. Owner approval is conversational: the agent previews, shows the owner the proposed changes and the document,
and only then calls again. There is no approval token, boolean or protection subsystem.

## The tool

`context_edit` accepts either the queued graph edit `{edit: <qorqut-graph-edit-v1 plan>}` (unchanged) or

| field | meaning |
| --- | --- |
| `edit` | a `qorqut-supervised-edit-v1` plan: `expected_revision` (the full published head commit), `rationale`, and operations `put_node`, `put_edge`, `put_note`, `remove_node`, `remove_edge`, `remove_note` with explanations, citations (`self` cites the document) and support. A note is `{id, text, objects: [{kind: node\|edge, id}]}` |
| `document` | the exact UTF-8 Markdown, non-empty, within the configured source bytes |
| `event_id` | a new five-character Context ID chosen by the caller; it names the event and the document path |
| `preview_only` | optional boolean. `true` returns the engine's pure preview and changes no Context file, event or question |

`document`, `event_id` and `preview_only` are refused with a graph-edit plan, and a supervised plan requires `document` and `event_id`.

Results. Preview: the unchanged engine preview (`status` `ready`, `pending` or `published`, `operations` with before and after,
`result_counts`, digests, `source_path`). Publication: the engine receipt `{status: "published", event_id, source_id, base_revision,
published_commit, input_mode, handoff_disposition, execution, result_counts, model: null, recovered}`; `execution` is the
`deterministic_supervised_edit` marker. Deleting a question that motivated the edit is a separate generic tool call made only after
the publication result confirms success.

## Procedure for an agent

1. Read the Context; build the plan against the current head and write the document.
2. Call with `preview_only: true`; show the owner the proposed changes and the document; obtain approval in the conversation.
3. Call again with the same `event_id`, `document` and plan, without `preview_only`.
4. If the outcome is uncertain, send the very same event, document and plan again. An identical already published event is reported
   (`recovered: true`) and never repeated; a different document or plan under that `event_id` is a `conflict`.

| error | meaning and action |
| --- | --- |
| `conflict` | the head moved (stale `expected_revision`), the event ID is used with different input or is reserved by a queued edit, or the records changed ownership: read again and prepare a new reviewed plan. An edit is never rebased or auto-discarded |
| `busy` (retryable) | the Context writer is busy, or another pending Context job must complete first: retry the same edit later |
| `forbidden`, `policy_denied`, `business_archived` | the caller lacks the authority below, or it was revoked before publication |
| `operation_unavailable` | no supervised pipeline is composed, or the Context cannot be read: retryable when so marked |

## Authority

`GraphEditAuthority` is the retained one: the role restrictions of the Context write surface, the installation owner for a human,
an allow for `context_edit` in the admission namespace and in the namespace of every target (agents), and the closed delegation of a
delegated worker. Target analysis (`analyze`) freezes, for every created, updated or removed node, edge or note and for every
edge endpoint and note subject (old and proposed), the ownership taken from the first creation event's controlled
`record_context.scope` in the published history. Current labels never grant anything. An existing record with a missing, malformed
or unregistered scope is unknown and needs the Global namespace; a record the plan creates belongs to the admission namespace; a note
with no linked subject (before or after the edit) needs Global write authority. Reading the shared Context stays open to every
registered agent; this variant grants no read or operational authority.

## Lifecycle and verification

The descriptor's existing prepare / external / apply lifecycle is used, as for calendar operations.

* `prepare` (read-only) normalizes within the source bytes, takes the engine preview (stale, busy and foreign-pending refusals),
  derives the targets, checks the caller's authority and binds the request as the plan's single `context_supervised_edit` preview
  resource: event ID, plan and document digests, source bytes, original attribution and frozen targets.
* `external` runs outside any SQLite write lock. It validates the complete prepared shape against the invocation's own arguments
  (metadata is never executed as a plan), resolves the original caller's current registration, role, grants and delegation, requires
  the re-derived targets to equal the frozen ones, and only then previews or publishes. The attribution and `record_context.scope`
  published with the event are the original caller's and the admission scope.
* `apply` verifies the same bound request and returns exactly the outcome produced for that prepared plan.

An exact already-published edit is reconciled before the base, target and authority checks that govern a new publication; the
caller still needs the admission-namespace authority, and only an identical submission (same caller attribution, event, document and
plan) is reported. Because the attribution is part of the event's submission identity, a retry after the caller's display name or role
changed is a `conflict` that points at the already published event instead of repeating it.

## Wiring

`GraphEditPorts(context, source_bytes=..., supervised_pipeline=None)`; existing callers are unaffected. `compose_context` in
`shared_context/service.py` (owned by the questions work) must pass the composed pipeline:
`GraphEditPorts(context, source_bytes=config.source_bytes, supervised_pipeline=pipeline)`. Without it the supervised variant answers
`operation_unavailable` and the queued graph edit works as before. Uploads cannot reach `foundation/supervised-edits/`: the engine
refuses any ordinary source on that prefix, and no filer fallback exists.

## Focused checks

`app/tests/test_context_supervised_edit.py` (six cases): the pure preview, a direct note and node publication with exact bytes, event,
history, no model and no queue binding, same-event reconciliation, stale / busy / foreign-pending refusals, foreign-scope, revoked and
delegated refusals before publication, and the unchanged queued graph edit without a supervised pipeline. They use a temporary
instance, a model runner that fails the test if called, the exact engine and Operations sources on `PYTHONPATH` and a role tree
selected with `QORQUT_TEST_ROLE_ROOT`; no live interpreter, data or service is involved.
