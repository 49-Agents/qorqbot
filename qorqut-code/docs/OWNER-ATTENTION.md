# Owner attention and conversations

Focus is a persistent, curated board for the installation owner and the exact
configured management coordinator. It does not own task completion, Story state,
proposal approval, or a record of what the owner viewed.

## Storage and tools

Migration 066 adds persistent cards, standalone Reference notes, signoff notes,
task kind, and compact semantic task changes. Existing task notes and committed
Story note events remain canonical. Migration preserves the configured
coordinator's legacy ordered references and pending browser handoff. Historical
proposal feedback remains readable without rewriting immutable decisions or
sending historical notifications.

The six Global tools are `focus_get`, `focus_present`, `focus_edit`, `focus_move`,
`focus_set_archived`, and `focus_reply`. Current role grants and source access
are checked on reads and writes. Source access must also permit the owner and
configured coordinator to inspect the presented source.

Focus holds the owner's highest-importance and highest-priority action items, which the owner wants done.
A card need not require an immediate owner response. The owner and authorized global
curators can order cards with `focus_move`, subject to ordinary grants and revision
checks. Business agents and system principals do not become global curators through
an explicit tool allow. New cards append; restore keeps the saved position unless a
curator explicitly moves it. Existing explicit denies remain effective.

Present a typed task, Story, signoff, or email thread, or a Reference with an explicit stable
key, headline, and opening target. References optionally bind a separate task,
Story, signoff, or email conversation. Opening targets can change; the conversation
binding cannot. Use a new Reference key for a different conversation. A Reference
without context has a small card-owned thread. The `desktop` target uses only
Global `DESKTOP_CONTROL_URL` and reports missing configuration explicitly.

Migration 068 adds optional `display_name` to every Focus card. Ordubashy writes
this separate presentation name with `focus_present`, `focus_edit.changes`, or
the `focus` arguments on source creation. Aim for 2 very simple words, at most 4
when possible, and omit the business name because the card already shows its
business tag. For example, "Review the example project's landing page and share feedback"
becomes "Landing page review". These are instructions, not word-count or
business-name validation rules. Source titles, revisions, conversations and
other task/Signoff lists remain unchanged. Reads retain the full source `label`;
the dashboard uses `display_name` only for the Focus heading. Omission preserves
an existing name; an empty name restores the full title. Older cards fall back
to their original title until the curator names them.

`blocker` and `urgent` are separate booleans (default false). Ordubashy marks
actual blocked work and time-sensitive items, clearing each flag when it no
longer applies. They affect presentation only: they do not change source state,
approvals, deadlines or scheduling. `recommendation` has been removed from the
Focus input schemas and read projections; old stored framing is retained only
for recovery. The management inbox's independent recommendation field remains
separate and is no longer copied onto Focus.

`requested_response` is optional when there is no action for the owner. When needed, give one
brief, direct instruction in very simple words, without greetings, filler or
repeated business names. UI review and design feedback are assigned to the owner
unless the owner explicitly delegates the review. Agents' checks do not replace
the owner's review or approval.

Write external links as `[short label](https://url)`. Reads preserve the original
text and add `requested_response_parts`, a list of tagged parts:

```json
[
  {"type":"text","text":"Review "},
  {"type":"link","text":"the page","url":"https://example.com"},
  {"type":"text","text":" and flag issues."}
]
```

The UI can render link parts as underlined anchors and text parts as text nodes.
Only HTTP(S) URLs without embedded credentials become links. Other markup stays
plain text; no raw HTML is emitted or fetched. Use existing typed `references`
for internal items. Flag badges and link styling are left to the UI work.

Re-presenting a source/key reuses its card and can restore it. Archive, restore,
reorder, deferral and source completion preserve the conversation. Relative moves
check the moved card's revision and the current anchor; moving another card does
not invalidate an otherwise current command. An archived anchor conflicts.

Signoff cards are the one automatic exception. When a signoff whose card is its
source stops awaiting a decision (approved, rejected, answered, cancelled or
replaced), the same transaction archives that card. A replacement proposal,
including a revision after a rejection, gets a new card with the old card's
name, action, flags, references, deferral and position. Cards that only list the signoff in `references` stay. A later apply
failure does not restore the card; the curator re-presents it if the owner must
act. Other sources, including completed tasks, are archived only by the curator.
`AttentionBoard.archive_resolved_signoffs` is the one-time cleanup for cards
decided before this rule shipped.

`focus_get` defaults to 30 compact cards (maximum 100) with offset pagination and
archive/deferred filters. Conversations load only on an explicit item read with
`conversation: true`, with bounded source cursors or note sequence pagination.
No capacity guarantee is implied by these page sizes.

## Canonical conversations

- Email threads have no host comments or Focus cards: the core keeps no email conversation records
  ([Email ownership](EMAIL-OWNERSHIP.md)).

- Tasks: `tasks_thread_get` combines existing notes with semantic change entries.
  A status change records from/to; other changes say “Task details changed.”
  A multi-field save creates one compact entry. Comments, no-ops and revision
  bookkeeping create no detail-change entry. All writers share transaction
  capture and normal source delivery; replay does not duplicate history.
- Stories: `stories_timeline` with `notes_only: true` projects committed notes.
  Existing operational history, rich notes, drafts and relationships remain.
- Signoffs: `signoffs_comment` appends an ordinary note; `signoffs_notes_list`
  reads the exact proposal's conversation. Notes do not change the frozen
  proposal or its approval status revision.

Rejection requires a nonblank note from the installation owner or current
configured coordinator on that exact signoff. A closing message supplied with
a decision is appended atomically with it; stale/failed decisions leave no note.
Approval has no note gate. Explicit answers remain separate from ordinary notes.
New decision prose lives in the note, referenced by immutable `message_id`;
compatibility readers resolve that reference without duplicating display.

Source notes use existing inbox subscriptions and responsible recipients. Owner
activity on a Focus source also reaches the configured coordinator. Standalone
Reference replies have a scoped card-note event. A note plus a decision coalesces
into one source notification. Delivery does not copy prose into direct messages,
acknowledge on page open, or introduce a reminder loop.

## Owner edit and snooze notifications

When the installation owner saves `focus_edit`, the same transaction records one
inbox event if a semantic presentation field actually changed: name, reason,
requested action, blocker or urgent flag, references, deferral, or a Reference's
title or opening link. Stored values are compared after normalization (JSON
content and canonical UTC deferral instants), so rewriting the same values, an
equivalent timestamp offset or an empty edit notifies nobody. Revision,
`updated_at` and position are ignored. Agent and coordinator edits, `focus_move`
(including the positions it shifts on other cards), `focus_present`, archive and
restore, automatic signoff replacement, and stale, rejected or rolled-back writes
create no event. An exact retry replays the stored result without a second event.
The capture is an explicit transaction-local `inbox_changes` row (`focus_edits`);
there is no trigger.

Event types: `focus_snoozed` for a changed non-null deferral (“Owner snoozed Focus
until <UTC time>: <title>”), `focus_resumed` when it is cleared, and
`focus_edited` for other fields, naming them only (for example “Owner edited Focus
name, urgent flag: <title>”). Several on one save form one event with
comma-joined types. Summaries and payloads never copy names, reasons, actions or
links.

Routing follows the card's conversation binding. A task, Story, signoff or email
source, directly or as a Reference's context, receives the event on that source
row with its real revision and the owner as actor, so current source read access
still applies and source-scoped readers see no Global Focus content. Recipients are
the configured coordinator, the source's assignees (task, Story, email) or
proposer (signoff), and existing source subscribers, each once, filtered by the
usual active and scope checks and re-authorized on every inbox read. Dependency
and owner-task followers are not added, because nothing about the task changed. A
standalone Reference uses its `focus_cards` route for the configured coordinator
and its creator while that creator is still the active configured coordinator. If the bound source
is missing, only that coordinator card event is queued, and it stays hidden
unless the Focus reader can still open the card.

These events never change source status, due date or revision, send a direct
message, create a task, or add a reminder or polling loop.

The installation owner's ordinary comments belong to the private channel with
Ordubashy. `tasks_comment`, a task `note` argument, `stories_note_add`,
`signoffs_comment`, `email_comment` and `focus_reply` answer `channel_required`
for the owner and never fall back to the shared thread. Agents and humans who are
not the installation owner keep their team writes, and existing notes keep their
authors and visibility. Formal decisions are the exception: approval and
rejection feedback and input answers still append the shared Signoff note.

## Creating work

Substantive owner handoffs use `tasks_create` with `kind: request_to_owner` and an
explicit owner assignee. Ordinary task assignment, dependencies, snooze and
completion remain unchanged. The configured coordinator can pass optional
`focus` presentation arguments to task or input-request creation: source and
card commit in the same transaction. Management escalation to the owner presents
the original signoff through the same board writer.

Stories represent coherent continuing work, including projects,
repositories, clients and contracts. Task reads/startup carry bounded authorized
Story context. Associations stay explicit per work item; there is no agent-wide
active Story and no copying child conversations into parent Stories.

## Dashboard and compatibility

Focus, task, Global task, Story and signoff views share a collapsed Comment/Send
component. Drafts are keyed by canonical source; new views start collapsed.
Participant identity, author grouping, relative timestamps, older pages and
exact Send retries are shared. Signoff decisions with unsent text are explicitly
labelled “Send and reject” or “Send and approve.” Visual polish is a separate task.
Focus refreshes once per minute while active, manually, or after a mutation.
Reads and opening a card write no viewing telemetry.

Old `focus_show`, `focus_clear`, and `focus_browser_input` calls return an actionable
upgrade error. The old pending browser handoff and exact owner Done route remain
operable, including the browser pause guard. New small login interventions use
configured desktop References and do not invent tasks, approvals or completion
objects.

## Private conversation with Ordubashy

Every supported discussion (business/Global task, Story, Signoff, email thread,
or standalone Focus Reference) has a second, separate channel: a private
conversation between the installation owner and the one configured Ordubashy,
stored independently from the team channel's notes/revisions/notifications.
Focus cards project `private_unread` (owner only) and `private_message_count`.
See [OWNER-PRIVATE-CONVERSATIONS.md](OWNER-PRIVATE-CONVERSATIONS.md) for the
full identity, storage, operation, notification, and guard contract.

## Release boundary

Fresh local preparation and manual startup are the supported path; no numbered migration or automatic host
upgrade is supplied (see [Release worker](release/README.md)). Publishing source packages does not repin agents.
Explicitly adopt/reprepare the selected roles through canonical operations while retaining scope, plugins,
assignments, overrides and terminal sessions. A fresh conversation loads the new
instructions; do not launch a model as a deployment check.


## Full-board completion review

On a supported native Stop, authorized global Focus curators receive one review
continuation with the complete current non-archived board, including postponed
cards. There is no pagination or first-page approximation. Card data includes
framing, order, source identities/states/revisions and private unread indicators;
conversation bodies must be read through their existing authorized tools when needed.
Snapshot generation changes no cards, read positions, inbox acknowledgements or tasks.

The instruction is:

> Before finishing, review the complete current Focus board below. Adjust its contents, wording, flags, visibility or order where warranted by the work just performed or the owner’s instructions. Make only useful changes. Do not start unrelated work or report routine maintenance. If nothing needs changing, finish. Make sure to update all the respective tasks with a message in respective thread if needed.

The native receipt and backend claim allow one continuation per original turn,
including conversation-only turns. Review tool calls cannot create another review
cycle. The snapshot is transient; receipt files keep only its continuation digest.
A failed read or an over-budget board is reported to backend diagnostics and lets
completion proceed; it never claims a partial board is complete. The wire budget is
4 MiB with 3 MiB reserved for the complete board, not a per-card or pagination limit.

Activation requires the companion core, adoption of the current Ordubashy role
and regenerated agent configuration. The preparation selects
`checkpoint_protocol: 2` for supported Focus curators: Focus-only review, without
enabling the general current-work/inbox correction behavior. Protocol 1 retains
that older general contract; 0 remains disabled. Both enabled modes require the
existing executable-bound `stop_hook_correction` evidence. Do not bypass that
capability check or claim activation from a source commit alone. Native loaded
configuration and runtime capability must be verified at release time; no agent
is automatically started by this change.

Focused acceptance: `app/tests/test_focus_completion.py`, using synthetic host data
and native-hook payloads only.


Source validation (2026-10-15): seven focused checks passed for full-board capture
beyond 30 cards, postponed/archived membership, conversation-only review, no inbox
writes, peer reorder permissions and stale revisions, large snapshot delivery with
small hashed receipts, partial-snapshot refusal, and one-shot Claude/Codex hook
routing. The native lifecycle checks use synthetic payloads; they are not live
provider capability evidence. No live configuration or operational data was changed.
