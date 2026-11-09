# Owner and Ordubashy private conversations

Every supported object discussion has two logically separate channels: a
private owner/Ordubashy channel and the existing team (agent) discussion. This
guide documents the implemented core contract.

> **Current v1.2 contract.** The private channel is the owner's conversation with
> the registered Ordubashy and nothing else. There is no worker or other-agent
> selector (an explicit `agent_id` other than Ordubashy is refused; `peer_role`
> accepts only `ordubashy`), no agent has a default channel, owner messages are
> not forwarded as notices, and `owner_conversation_peers` returns only Ordubashy.
> Conversations earlier releases stored for other peers are retired history that no
> read, count, notification or inbox projection serves; a separately invoked
> maintenance step removes them.
> Passages below that describe per-agent peer channels are historical.

## Boundaries

| Channel | Readers | Writers |
| --- | --- | --- |
| Private with Ordubashy | Installation owner and the configured Ordubashy | Installation owner and the configured Ordubashy |
| Agent discussion (team) | Existing source-authorized readers and agents | Existing source-authorized writers; the owner is read-only |

Applies to business and Global tasks, Stories, Signoffs,
standalone Focus References and existing Projects (`{type: "project", id: PROJECT_UUID, business_id: PROJECT_UUID}`,
keyed as `source:<PROJECT_UUID>:project:<PROJECT_UUID>`; the Project itself has no team discussion — see
[Project records](PROJECT-RECORDS.md)). A Project source is re-authorized through the one Project read guard on every
read, directory projection and Focus card; a revoked membership or archived Project makes it unavailable without
deleting the stored conversation, peer bindings, messages or read positions. Signoff approval/rejection
feedback stays a shared, formal Signoff decision note; it is never redirected
into the private channel. Existing comments and history are unchanged; there
is no backfill or visibility rewrite. Ordubashy may start a private
conversation; when useful, it relays an owner message to the team as its own
attributed note or message, never by automatic copying.

## Identity

Participants are server-resolved, never client-supplied. Installation owner:
`installation.owner_principal_id` (row id `'service'`); owner actor ⇔
`ctx.principal_kind == 'human' and ctx.principal_id == owner_principal_id`.
Private coordinator: `installation.management_coordinator_id`, whose
`agent_profiles` row is `status='active'`, whose principal is active with
`kind='agent'`, and whose `profile_json.role.id == 'ordubashy'` — with **no
runtime check**.

`qorqut/management/identity.py` holds this once: `installation_owner(uow)`,
`ordubashy_profile(uow, identity)`, and `configured_ordubashy(uow)`
(`private_participants(uow)` uses it). `ManagementService._coordinator` and
`require_coordinator` ask the same question: the coordinator is the registered
Ordubashy on any supported runtime.

Session IDs, runtimes (`claude-code`/`codex`), `agent_activity` rows and
continuation state never influence the ACL: every session of the one
registered Ordubashy principal shares one private history. A missing,
inactive, or malformed coordinator means Ordubashy cannot participate; the
owner can still read (empty or historical) and send (stored; not delivered).

## Canonical source binding

`source = {type: 'task'|'story'|'signoff'|'table'|'reference', id, business_id?}`. (`email` remains in the stored `source_type`
vocabulary as retired history: the host serves no email source.)

- Business-scoped call: `source.business_id` must be omitted or equal to it,
  else `validation_error`.
- Global call: `source.business_id` may name a business — re-selected the
  same way `AttentionBoard.reply_plan` does (`ctx.select_business` plus an
  active-membership check; outside membership is `forbidden`) — or be null
  for a Global task/signoff or a Reference.
- `story` is business-only. `task`, `signoff` and `table` may be business
  or Global. A `table` source (a general or CRM Table) re-authorizes
  `tables_get` on every call and is the same single owner + Ordubashy channel: it never
  reposts into the Table's team discussion ([CRM](CRM.md)).
- `reference` is a standalone Focus Reference (a `focus_cards` row with
  `source_json IS NULL` and no `context_json`); `id` is the card id and
  business must be null. A Reference bound to a context is discussed under
  that context's own source; the card id is `validation_error` there.
- Source key (unique): `'source:' + (business_id or 'global') + ':' + type +
  ':' + id` for task/story/signoff/table; `'reference:' + card_id` for a
  standalone Reference. Editing a Reference's target/title never changes it.
- Every call re-authorizes the caller's canonical read of the object before
  using the pair-ACL result: `tasks_get`/`stories_get`/`signoffs_get`/`tables_get` (with a
  row-existence and, for signoffs, `review_business_id` check), or a
  Reference's `focus_cards` existence (archived allowed). Missing → `not_found`.

## Storage

The host schema (`composition/schema_ddl.py`) creates three raw-SQL tables (not in `storage/repositories.py` `TABLES`, not in
`storage/exports.py` catalogs): `owner_conversations` (one row per canonical
source, `audience='owner_ordubashy'`, `revision`/`latest_sequence` counters);
`owner_conversation_messages` (append-only, `author_role in
('owner','coordinator')`, `body` 1–20000 chars, optional `summary` ≤500 chars,
autoincrement `sequence`; `BEFORE UPDATE`/`BEFORE DELETE` triggers
`RAISE(ABORT)`, matching `069_email_comments.sql`, and **no**
`inbox_capture_*` trigger — private rows never enter `inbox_changes`);
`owner_conversation_reads` (one durable `last_read_sequence` cursor per
`(conversation_id, principal_id)`). Sending never bumps a task/story/signoff/
email/Focus revision. Whole-database backups
([RECOVERY.md](../app/qorqut/storage/RECOVERY.md)) retain these tables by
design; exports and history publication never include them.

## Operations

Registered by `PrivateConversations` in
`qorqut/inbox/private_conversations.py`. Common argument `scope:
'business'|'global'`, same semantics as `signoffs_comment`. Routes are
`/api/biz/<slug>/operations/<name>` when the business is known, or
`/api/root/operations/<name>` with `scope:'global'` and `source.business_id`
otherwise (for example, a Global Focus card bound to a business object).

| Operation | Args | Result | Exposure |
| --- | --- | --- | --- |
| `owner_conversation_get` | `scope, source, before?(int≥1), limit?(1–100, default 20)` | `conversation, messages, next_before, participants, read_state` | mcp, dashboard, worker |
| `owner_conversation_send` | `scope, source, text(1–20000, nonblank), summary?, expected_conversation_revision?` | `message, conversation` | mcp, dashboard, worker |
| `owner_conversation_mark_read` | `scope, source, sequence(int≥0)` | `read_state` | dashboard, worker |

```json
POST /api/biz/<slug>/operations/owner_conversation_get
{"scope": "business", "source": {"type": "task", "id": "<task-uuid>"}}
→ {"conversation": {"id": "...", "source": {...}, "audience": "owner_ordubashy",
     "revision": 3, "latest_sequence": 3, "message_count": 3},
   "messages": [{"id": "...", "sequence": 1, "body": "...", "summary": null,
     "created_by": "...", "created_at": "...", "author_role": "owner",
     "created_by_name": "...", "author_kind": "human", "is_owner": true}],
   "next_before": null, "participants": [...],
   "read_state": {"last_read_sequence": 2, "unread": true}}

POST /api/root/operations/owner_conversation_send
{"scope": "global", "source": {"type": "task", "id": "<task-uuid>", "business_id": "<business-uuid>"},
 "text": "Hold the price change until Friday."}
→ {"message": {"id": "...", "sequence": 4, "body": "...", "author_role": "owner", ...},
   "conversation": {"id": "...", "revision": 4, "latest_sequence": 4, "message_count": 4}}

POST /api/biz/<slug>/operations/owner_conversation_mark_read
{"scope": "business", "source": {"type": "task", "id": "<task-uuid>"}, "sequence": 4}
→ {"read_state": {"last_read_sequence": 4, "unread": false}}
```

`text`/`summary` reuse the shared summary contract
([MESSAGE-SUMMARIES.md](MESSAGE-SUMMARIES.md), `normalize_summary`). A read of
an object with no private messages returns the empty virtual thread
(`conversation.id: null`) and creates no row. Pagination is oldest-first
within a page via `before`/`next_before`, like `email_comments_list`. `send`
uses `INSERT OR IGNORE` on `source_key` so concurrent first sends converge on
one row; a stale `expected_conversation_revision` returns `revision_conflict`
with the current revision. Retrying `send`/`mark_read` with the same
idempotency key returns the prior result. `mark_read`'s `owner_only=True` is a
coarse framework short-circuit for a human business/global owner; the
operation itself still resolves the pair ACL and rejects anyone but the
installation owner (including a non-installation business owner) with
`forbidden`.

Known limitation: on a business route the generic human policy still requires
the installation owner to hold an active owner membership in that business, as
every other business mutation does. In this single-owner installation the
registered owner is the owner-member of every business by construction, so
the Global route with `source.business_id` and the business route agree; no
special policy rule is added for the hypothetical plain-member case.

## Error codes

| Code | HTTP | When |
| --- | --- | --- |
| `forbidden` | 403 | Caller is not the pair; business outside active membership; `mark_read` by anyone but the installation owner |
| `not_found` | 404 | Bound object does not exist, or a signoff's `review_business_id` mismatches the effective scope |
| `validation_error` | 422 | Business mismatch/missing for the source type; a Reference given a business or a context-bound card id; blank `text`; bad `summary`; `mark_read.sequence` above `latest_sequence` |
| `revision_conflict` | 409 | `send.expected_conversation_revision` mismatches the current revision |
| `channel_required` | 422 | Raised on the **team** write operations below, not on `owner_conversation_*` itself |

## Notifications

An owner-authored `send` inserts one `inbox_events` row (`source_type:
'owner_conversations'`, `event_type: 'private_message'`, `summary: 'Private
message from the owner'`, `visibility_json: {"source": ...}` — never message
text or the source title) and one `inbox_deliveries` row for the coordinator
only. A coordinator-authored `send` creates neither; the owner's unread state
comes only from the durable read cursor. Neither direction touches task
subscribers, assignees, email handlers, subscriptions, activity, source
fan-out, agent wakes, or a source revision. `inbox_list` returns the event's
bound `source` in both compact and full detail; the coordinator passes it
verbatim to `owner_conversation_get`.

`InboxService.visible()` special-cases `owner_conversations` before the
`ITEM_TYPES` check: visible only to the configured coordinator, and only
while the bound source is still readable — a refusal the coordinator can no
longer act on (including a stale `context_refresh_required`) hides that one
item instead of failing the page. It is absent from
`ITEM_TYPES`/`RESOURCE_TYPES` and from `inbox_subscribe`'s `source_type` enum,
so a subscription attempt is refused by argument validation
(`validation_error` with `field_errors.source_type`) and
`inbox_subscriptions_list` can never list one. `global_progress_get` excludes
these deliveries from its `inbox_messages` count.

## Focus projection

`focus_get` card projection adds `private_unread` (bool; true only for the
installation-owner caller when a `coordinator` message has
`sequence > last_read_sequence`; always false otherwise) and
`private_message_count` (int, both participants). A board read prefetches both
counters for every listed card in one `private_index` query; the single-card
path keeps `private_state`. Computing these writes nothing; private messages never create, promote, restore, or unarchive a
card. An installation-owner `focus_reply` returns `channel_required` for
every binding; agents' `focus_reply` is unchanged.

## Owner team-write guard

`qorqut/domains/team_channel.py`'s `reject_owner_team_write(ctx, uow,
operation)` raises `channel_required` (details:
`{'channel':'owner_ordubashy','operation':'owner_conversation_send',
'refused_operation': <the team write>}`; `operation` is always the private
operation to use instead) only
for the installation owner. Called from prepare and apply at: `tasks_comment`
and the `note` argument on `tasks_create`/`tasks_update`/`tasks_complete`
(`domains/tasks/operations.py`); `stories_note_add`, `stories_draft_save` and
`stories_draft_commit`
(`domains/stories/reviews.py`, `operations.py`); `signoffs_comment`
(`signoffs/notes.py`); `email_comment` (`domains/email/comments.py`); and
`focus_reply` (`inbox/attention.py`). Legacy HTTP adapters in
`transport/compatibility.py` that forward a task `note` route through
`tasks_comment`/`tasks_update`, inheriting the same guard.

The line is the row, not the wording: a separate discussion row is refused,
while a reason that is inseparable from a formal state change or decision stays
shared — the `note` on `stories_update`/`stories_close`/`stories_reopen` is the
body of that state-change event, not a discussion note, and a Story draft is
refused because its only outcome is a committed discussion note. The apply-side
re-check looks at the prepared rows (a `task_notes` change, a `story_events`
change of kind `note`), so a stored plan cannot be replayed past the guard.
For the same reason a nested `TaskOperations.prepare(..., team_note=False)`
stays shared: a task note generated from an action's own record (a routed
reply or an approved batch) is that action's record rather than the caller's
comment.

Unchanged exceptions: `SignoffService.review` approve/reject feedback,
`respond()` input answers, `signoffs_reject` (agent),
`signoffs_input_answer_delegated`, email decision replacement reasons, every
agent's team writes, and team writes by non-installation-owner humans.
`channel_required` maps to 422, the `validation_error` status family; it is an
ordinary outcome, not an incident.

## Dashboard behaviour

See the `biz-dashboard` repository's `README.md` ("Every object discussion
has two channels" paragraph) for the shipped UI. Summary: a two-button
segmented control (tabs, not a menu) switches Private/Agent discussion, shown
even with no team notes; the owner opens on Private with Ordubashy; Agent
discussion stays readable with its composer removed and a read-only label.
Each channel keeps independent messages, pagination, participants, draft,
submission key and timers, keyed `${type}:${biz||'global'}:${id}:${channel}`.
Loading the private channel through its current `latest_sequence` without
error calls `owner_conversation_mark_read`; polling, card preload, the agent
discussion, and inbox acknowledgement never do, and a failed/partial load
leaves the unread marker set. A white dot on the Focus message icon marks
`card.private_unread`, distinct from the green agent-pane dots, clearing only
after a successful `mark_read`. Signoff feedback keeps its own decision
field, never a channel draft. Owner task mutations send no `note`; a
`channel_required` response switches the dashboard to the private channel and
keeps the draft.

## Role guidance

The Ordubashy `role.yaml` (`agent-roles`) adds `owner_conversation_get:
allow` and `owner_conversation_send: allow` (no `mark_read`; it is owner-only
and absent from agent MCP). The `owner-focus`
skill and `BRIEF.md` add guidance: the private channel is the owner's
default, Ordubashy may initiate it, and relays only relevant instructions to
the team by judgment as a new attributed note/message — never automatic
quotation. Team discussion and formal Signoff behavior for every other role
are unchanged.

## Verification

Core: `tests/test_owner_private_conversations.py` (identity, canonical
binding, pair ACL, pagination, idempotency, concurrent first sends, Focus
projection, notifications, privacy sinks), `tests/test_owner_team_channel_guard.py`
(guard coverage and exceptions across every writer and the legacy adapter). Dashboard:
`tests/test_private_channels_browser.js`. Roles: `tests/test_ordubashy_policy.py`.

## Rollout

The core schema and operations, the dashboard and the role
instructions/preparation are separate source components. Publication,
adoption, merge, and deployment remain separate, explicitly approved steps;
this guide does not authorize any of them, and no numbered migration or
automatic upgrade is supplied. Rollback keeps existing private
rows and ACLs, and must not expose private messages through an older composer
or a shared projection.
