# External Context review questions

Branch `v1.2/context-notes-questions-host`, based on `367f84c`, against Context engine `e96b131` (`Pipeline.question_output` and
`Pipeline.acknowledge_question_output`) and the deployed Operations `164a670`. No Operations schema, binding table, queue
semantic or role publication changed.

Open review questions the automatic filer asks about the published Context live in a host-owned SQLite file, outside the Context
repository, its runtime and every Context capture. A question only names Context records (event, node, edge or note IDs). It is
never a note, graph attribute, source evidence or event payload. Only the filer creates questions; two generic tools let the
owner and the Learning Applier maintain them. There is no dashboard question UI, no answer-question operation, no
status/priority/archive and no automatic resolution. Resolving a question means deleting its row.

## The store

`InstancePaths.context_questions` is `operations/.runtime/context-questions.sqlite3`, explicitly resolved like every other
instance path (a symlinked path is refused). It is a sibling of `.runtime/context/`, not inside it.

`qorqut.context_questions.QuestionStore(path)` is the approved standalone question store, ported with lazy initialization:

| Call | Result |
| --- | --- |
| `QuestionStore(path)` | resolves the path; touches nothing |
| `exists()` / `initialize()` | whether the file exists / create it (mode 0600) and verify its schema, idempotent |
| `list(*, related_kind, related_id, offset, limit)` | `{items, next_offset}`; an absent or never-initialized file is `{items: [], next_offset: None}` and is not created |
| `read(question_id)` | the record, or `None` (also for an absent file) |
| `create` / `update` / `delete` | the plain store calls; the first write creates the file and schema |
| `mutate(request_key, action, *, question_id, text, related)` | one create, update or delete exactly once per request key (below) |
| `deliver(event_id, published_commit, questions)` | one transaction for an event batch and its ledger entry; a repeat inserts nothing |
| `delivered(event_id, published_commit)` | whether the ledger holds that event and commit |

Records are `{id, text, related: [{kind, id}], origin_event_id}`. Tables: `questions`, `question_related`, `deliveries` and
`mutations`. An existing file is accepted only with `user_version = 1` and exactly this schema; anything else is a
`QuestionStoreError` and is left untouched. The rollback journal and `secure_delete` are used, so deleting a question zeroes its
text in the file. Deleted text is not scrubbed from OS caches, filesystem snapshots or backups.

* **Delivery ledger.** `deliveries` keeps event ID, published commit and count, so a redelivered batch never brings back a
  question that was deleted. The same event with another commit is a conflict and inserts nothing.
* **Mutation receipt.** `mutations` keeps a request key, a digest of the requested change, the action, the question ID and
  whether the effect happened, never question text. A repeat of the same key and change returns the recorded result and does
  not touch the question, whether it still exists or was deleted since; the same key with a different change is a conflict.
* **Backup boundary.** The store is independent of any Context capture and of the operations history export. Nothing here backs
  it up or claims it is covered by another backup; a copy of `operations/.runtime/context-questions.sqlite3` taken while the
  service is stopped is the only consistent one.

## Delivery from the Context outbox

`ContextServices` (`shared_context/service.py`) drains the engine's published-question outbox on its one worker thread, after
mechanical recovery and on every step of a ready published Context, independently of whether the queue recorded its last outcome
and whether anything new was filed. `QuestionOutbox.drain()` lists the event IDs that have an entry in
`<context runtime>/questions`, takes at most 16 in rotating order, and for each one:

1. `Pipeline.question_output(event_id)`: the engine validates that the event is fully published (a still-completing event is
   counted as `waiting` and tried again later);
2. `QuestionStore.deliver(event_id, commit, questions)`: one transaction, a no-op when the ledger already holds that event;
3. `Pipeline.acknowledge_question_output(event_id, commit)`: removes exactly that payload, idempotent.

A failure at any stage leaves the batch in the outbox and is reported in `ContextServices.diagnostics()["questions"]["last"]`
(`examined`, `delivered`, `inserted`, `waiting`, `failed`, and up to five `{event_id, stage, error}` entries, never question
text). It is retried on the next steps. It never refiles, never changes a published event, never runs a model and never fails
the filing. A delivered-but-unacknowledged batch is acknowledged on the next pass without inserting anything again, including
after the questions were deleted. Composition, a read and a preview never create the store file; the first delivery or question
mutation does.

## Tools

Both are Global-scope MCP/worker tools (not on the dashboard route and not in the business-bound owner-session catalog). Each
registered operation is `supports_signoff=False` with the `read_global_context` capability, like `context_edit`.

`context_questions`: `{action: "list" | "get", question_id?, related_kind?, related_id?, offset?, limit?}`. `get` takes only
`question_id`; `list` takes an optional `related_kind` with `related_id` (Context IDs), `offset` and `limit` (1 to 100, default
50). It returns `{items, next_offset}` or `{question}` with explicit question IDs and `related` links.

`context_question_edit`: `{action: "create" | "update" | "delete", question_id?, text?, related?}`. `create` needs `text`
(1 to 20000 characters) and may carry up to 20 `related` links; `update` needs `question_id` and `text`, `related` or both
(`related: []` clears the links); `delete` needs only `question_id`. Results are identifiers only:
`{action, question_id, applied, replayed, related}`. Read a question with `context_questions` for its text.

Their descriptions tell the Learning Applier to review manually: start only when the owner asks, read the questions, preview
and propose a supervised `context_edit`, get the owner's approval, apply it, delete only the questions the owner's answers
resolved, and leave a question unchanged when an answer is unknown.

### Authority

* Access is the installation owner or the current Learning Applier role only, in the Global namespace. Any other role, any
  other human, a system worker and a delegated worker whose assignment does not also allow `context_edit` are refused.
* The policy basis is the caller's current `context_edit` authorization. `composition/policy.py` resolves both tools in the
  `before_override` phase from `resolve("context_edit")`: not the Learning Applier is a deny, an explicit deny on the tool (an
  override or a role default) is a deny, and otherwise the tool is allowed exactly when `context_edit` is. An `allow` override
  on a question tool cannot turn a `context_edit` deny or revocation into access, no role is republished, no profile edited and
  no capability granted. Discovery follows the same decision, so only the Learning Applier is offered the tools while its
  `context_edit` is allowed.
* Every handler re-authorizes the existing `context_edit` operation under the caller's refreshed identity and its frozen
  delegation: in the read, in `prepare`, immediately before the store write, and again in the applying transaction and any
  replay.

### Lifecycle and retries

`context_question_edit` follows the host's external-operation lifecycle (calendar is the other user): `prepare` validates the
change and binds a request identity derived from the principal and the idempotency key; `external` writes the store exactly once
for that identity; `apply` returns the recorded outcome. If the process is interrupted after the store write but before the
Operations receipt is saved, the retry with the same key is a replay (`replayed: true`) of the same effect: no second question,
no second update, and a question deleted in between is not recreated. Reusing a key for a different change is a `conflict`.

## Checks

`app/tests/test_context_questions.py` (six cases, synthetic host, scripted runner that stages one question, no model, network or
live data): delivery and acknowledgement; a failed delivery retried on later steps without a model and with the queue step
failing; no resurrection after a delete and a delivered-but-unacknowledged batch; exactly-once mutations across an interrupted
receipt; owner and Learning Applier access with Ordubashy, other-human, deny-override and revocation refusals and discovery; and
inert composition, preview and absent-store reads.

```
QORQUT_TEST_ROLE_ROOT=<role-source checkout> QORQUT_TEST_TEMPLATE_ROOT=<template checkout at the pin> \
  PYTHONPATH=<operations>:<context engine e96b131> python -m pytest app/tests/test_context_questions.py
```

## Integration

The one-line wiring `GraphEditPorts(..., supervised_pipeline=pipeline)` in `compose_context` belongs to this branch's final
commit and needs the separately committed manual-edit worker's `GraphEditPorts.supervised_pipeline` field. Without that field
the composition fails with a `TypeError`.
