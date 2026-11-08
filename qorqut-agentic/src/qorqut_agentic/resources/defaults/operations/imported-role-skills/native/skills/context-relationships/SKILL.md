---
name: qorqut-role-context-relationships
description: Relate project items, file and correct shared Context, and diagnose blocked Context inputs.
---

# Relationships and Context Review

Use typed item identities (`type`, `id`) from `context_items_list` or
`context_items_get`. People, topics, tasks, tables, agents and materials
share directional relationships. Materials include notes, documents, excerpts,
posts and conversations. Original uploads remain evidence sources, off the map;
background processing of an upload never reviews or acknowledges it for you.

Materials have a primary `material_type` (such as `conversation`, `document` or
`content`), a separate `origin` (such as `upload` or `email`), and free-form
`tags`. Filter `context_items_list` by these fields (`tag` for one tag).
Use `materials_update` with `material_id`, `changes` and `expected_revision`
to replace tags or correct an extracted material's type. This changes metadata
only, never the source text. Email remains conversation, published content stays
content, and original uploads remain mixed and off-map. Tags grant no access and
do not assign work. Personal/agent notes, plans and Learnings keep their own tools.
Read registered documents through `context_get`/`context_search` and published
content through `content_get`; these are raw document and item reads, not graph
reads. Read email through the access method in Email Management.

Inspect `relationship_roles_list` before creating an edge. Prefer a predefined
role; use a descriptive snake_case role when none fits. Explain the relationship
in `context`. Duplicate edges, multiple parents and reciprocal edges are allowed.
Read Task assignees; do not infer responsibility merely from someone being
mentioned. Create, edit or
remove edges through `relationships_create`, `relationships_update` and
`relationships_remove`, supplying expected revisions for edits and removals.

Use `tasks_assign` with `assignee_ids` for task assignment and normal task tools
for blockers. Multiple assignees share one task and status. Generic relationship tools cannot bypass these
workflow rules. Assigning a task creates inbox work; the backend can notify an existing native session,
but never starts an agent.
Archive items instead of deleting their content. Archiving preserves edges and
history; `context_items_archive` retains the item's normal mutation permissions.

## The Shared Context Graph and Source Input

Published knowledge lives in one shared graph. There is no per-project Context
graph and no living brief to maintain. Read it with `context_home`,
`context_find`, `context_object` and `context_expand`, following the node, edge and
source refs they return. Every response names the actual published revision it was
read at and is bounded into pages: pass the returned opaque cursor back unchanged to
keep reading that revision. An empty or truncated page is not proof that something
is absent. Published source evidence is part of the shared graph but does not
authorize raw operational reads: documents, notes and People records still use their
normal tools (`context_list`, `context_get`, `context_search`, item tools) and your
existing grants.

Exact source input enters through the existing authorized operations: upload, note,
journal (the owner's `journal_upload_to_filer` action keeps that historical name) or
accepted learning. Use `context_upload` for exact new evidence. For a concrete,
source-backed correction use `context_instruct` with the exact text, the people involved
and the rationale. Keep it focused and within the selected scope (a project, or Global);
do not use it for hypotheses, tasks, approvals or broad extraction. These operations
only accept a frozen source into an ordered background queue: accepted or queued is not
processed and not published. A blocked input, a published result and a completed source
or job are different states; read the actual result before saying which one you have. A changed source is a distinct revision, not an edit of the
accepted one. Do not promise generated topic files, maps, summaries or a per-project
index.

### Editing the graph with `context_edit`

`context_edit` has two variants. Read `operations_describe` for the actual current
arguments; never guess JSON.

- **Queued `qorqut-graph-edit-v1`**: an exact deterministic node and edge plan with the
  expected published revision, a rationale and valid citation or support evidence. It
  enters the ordered queue, so it is not published until the backend runs it. A queued
  edit cannot skip a blocked input or change its recorded base. A delegated edit keeps its original
  assignment, binding and generation and is authorized again when it runs.
- **Direct `qorqut-supervised-edit-v1`**: prefer this for an exact edit the owner
  directs. It supports nodes, edges, notes, full records, explanations, citations and
  support, a supporting `document`, the current `expected_revision` and an unused five-character `event_id`
  you choose. It publishes without a model and does not join the Operations queue.

For a direct edit, first call with `preview_only: true`. Show the owner the exact document and
the proposed before and after changes, and wait until they approve those exact changes.
Then apply the identical plan, document and `event_id` once. Approval they already gave for
exactly that plan needs no repeat request. This creates no Signoff or token and removes
no approval. If the plan, document or `event_id` must change (for example because the
published revision moved), preview again and get approval for the new exact changes.
The engine refuses a direct edit while another pending event exists: it is not a way to
skip or rewrite pending work, and a permission failure never authorizes editing Context
storage.

### When input or an edit does not publish

Diagnose with `context_queue_list` and `context_queue_get` in the right scope: they read
only the inputs of the scope you selected (one project, or Global), never another
scope's. `queue_blocked` says whether the shared queue is stopped; the blocked input is
named only when it belongs to your scope. For an interrupted or blocked input, read its
current state with `context_queue_get`, then request `context_queue_retry` for the same
input with the observed `expected_state` and a stable idempotency key. The request only
records that another attempt is wanted; it is not proof of publication, so read the
input again. Never upload the same material again to get around a block, edit Context
storage or discard queued work. If a failure repeats, stays unresolved or you are unsure
you are authorized, report the concrete blocker to the owner (input, state, error) instead of
retrying in a loop.

Report success only with a confirmed published result: a published input, or a direct
edit receipt with its event and commit, plus a readback of the changed objects. Keep the
original source attribution and scope rules.

## Catch Up Before Resuming Work

1. Read the item and all pages of `context_reviews_list` for that item.
2. Read each relevant source through its normal domain tool. Opening, searching,
   filing or reading never counts as review.
3. Account for the evidence in your work. Choose `incorporated` with a concise
   explanation of what changed, or `no_change` explaining why none was needed.
   Never manufacture changes just to clear a pending flag.
4. Call `context_review` with the item's `expected_revision` and an explicit
   `items` list containing the observed `relationship_id`,
   `relationship_revision`, `review_revision`, `source_token`, `result` and
   `explanation` for each source you actually reviewed.

An optional `edit` (`operation`, `arguments`) commits a supported edit to this
same item atomically with its reviews. It retains normal permissions and signoff
policy. Finish unrelated operations before acknowledging their evidence. Never
claim a proposed signoff has already applied.

Reviews are shared between agents for a given item and relationship. The other
endpoint has its own independent review: a parent Task reviewing a child does not mean
the child has reviewed its parent. Changed source content or an edited relationship
becomes pending again. Reading receipts and filing bookkeeping do not.

Use `include_reviewed: true` to inspect completed reviews and
`context_reviews_history` for immutable decisions, reviewers and timestamps.
`context_review_reopen` requires the observed versions and an explanation.
After a conflict, reread and reconsider the changed source; never merely replace
tokens on an old decision. Retry a lost response with the same idempotency key
and unchanged arguments. No operation clears unseen arrivals in bulk.

Human-authored Task descriptions are not machine-owned. Published graph knowledge
and source evidence keep their separate authority. Preserve original quoted
material; conversation text is exact, not a model-written paraphrase.
