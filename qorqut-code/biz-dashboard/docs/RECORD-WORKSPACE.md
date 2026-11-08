# Record workspace (Focus)

Focus uses the shared record workspace: `recordWorkspaceMount(host, options)` in
`js/record-workspace.js`, with layout in `css/record-workspace.css`. Selecting a record
from Focus expands it in the board below the persistent launcher row, filling the board's
height, and shows exactly one full-width pane:

- **Chat** — the record's existing private Ordubashy conversation
  (`js/conversation.js` → `js/chat-view.js`). This is the fresh-selection default for
  every source that has a private thread: Task, Story, Signoff, Reference and Project.
- **Dashboard** — the icon tab named for the record type (`Story dashboard`,
  `Signoff dashboard`, `Project dashboard`, …). Selecting it replaces the chat area; the
  panes are never side by side.
- **Activity** — Task, Story, Customer and Email task progress notes, newest first.
  Each collapsed row is a single line containing a short preview, author and time.
  Expand a row for its original text and references; load older notes without changing
  the private conversation. All rows start collapsed.

The Focus header leads with a pill Close (the same control a Browser/Desktop surface
carries; there is no back-to-Pinned control), then Back, the canonical title, the
record-type icon tab, the Chat icon tab (only when the source has a private thread),
and an Activity icon tab for task-backed records.
Elsewhere the workspace keeps Close as its trailing icon. A record with no thread (an
email reading, table, document or generic reference) opens its reader directly and has
no Chat tab. Related records open in the same workspace on a bounded stack (20 entries), so Back
restores the previous record's selected tab, dashboard edits and dashboard scroll.

Every entry stays mounted while hidden. Switching tabs, opening a related record and
coming Back therefore keep drafts, loaded pages and dashboard scroll instead of
rebuilding them. Dashboard scroll is captured only while that dashboard is visible and
is restored after the pane is shown, so a dashboard → Chat → dashboard round trip keeps
the same position. Leaving a dirty dashboard (Back, another top-level record, Close or
Escape) asks once per dirty entry; a clean entry never asks. A bounded stack eviction or
a revisit that would pop an edited intermediate entry uses the same confirmation
instead of silently dropping it. Returning to an existing ancestor collects only the
finite slice above it (never a loop on the unchanged stack), confirms that slice, then
rechecks liveness and identity before removing exactly those entries. A different
Focus row starts a fresh Chat-first root path after that guard; only the already
selected row keeps its current tab and edits. A save result of `false` keeps the
dashboard and its retry/conflict UI open; a failed close-save closes only the
confirmation, not the record.

A denied, deleted or no-longer-authorized source is an entry-level lifecycle for both a
visible and a hidden retained entry: the workspace disposes that entry's dashboard and
Chat controllers, invalidates pending generations, clears cached source data and removes
the entry. It cannot reappear through Back or a tab change, and a late reply cannot
repaint it. A hidden Chat therefore still refreshes its source authority; it simply does
not acknowledge or mark anything read while hidden. Task, Story, Customer, Signoff,
Action, document, table and Project readers revalidate their own authorized source on
reveal. Transient network errors are different: they keep the last authorized content
and any draft, show a Retry or an inline error, and never discard an edit. Initial
dashboard failures also render a real Retry with loading/liveness guards; switching to
Chat and back can retry the same read rather than getting stuck on an empty dashboard.

## Progress notes and Activity

Task overview and progress notes are not inserted into the Chat feed. Chat reads the
private conversation; Overview remains in the task dashboard. Activity reads canonical
`tasks_get` comments in the task's own scope, using `notes_cursor` and at most ten notes
per page. Previews use the stored summary or a shortened body; expanded text retains the
complete original note, with the shared safe Markdown renderer and reference readers.
Dates and times follow the configured operational timezone. No new message or unread
notification is created by presenting an activity row.

The Activity controller loads only when selected. It retains expanded rows and loaded
pages across tab switches, refreshes that loaded depth when revealed, and shares the
workspace's moved-record and access-loss handling. Late reads after disposal are ignored.
The existing broader CRM/event history stays in the dashboard's **Related history**
disclosure. Legacy task detail dialogs show a separate Activity disclosure above their
private conversation.

## Chat behavior

Chat is mounted exactly once per stack entry and is reused across tab switches. Drafts
are keyed by canonical source (`type:business-or-project:id`), so the same record shares a
draft across Focus selections. The private source is the canonical record scope, not
the Global projection that hosts the pane.

Later related pages follow the thread feed viewport: after the owner leaves the end
and reaches it again, one next page loads. There is no related paging button, retry
banner or toast; a failed page retains no related rows, never blocks the composer,
and waits for a normal refresh or reopen. Counts and ordering stay over the loaded
page only, and source switch or disposal releases the trigger with the thread.

A hidden Chat never acknowledges a message. The conversation's `isVisible` callback
requires all of: the current tab is Chat, its pane is not hidden, the workspace entry is
not hidden, the document is visible and the entry point itself is visible. Selecting Chat
or receiving a message while it is visible then marks it read through the existing
conversation controller.

Project references in a chat message keep the typed Project chip and add an explicit
**Open Project** action into this same workspace; the chip itself creates no channel and
sends nothing. A Project record's Chat is its real private Project thread (canonical ref
`{type:"project", id, business_id}`).

## Dashboards

### Task, Story, Customer and Email

`rwTaskDashboard` reuses the existing renderers in `js/record-panel.js`:

- **Story** — read mode shows the overview as paragraphs with an inline Edit control.
  Edit reveals Save/Cancel; Save sends only the overview with the opened revision, reuses
  the same idempotency key on an uncertain retry, and on a revision conflict keeps the
  typed text and offers **Use latest revision**. Read mode returns after Save/Cancel.
- **Customer** — the existing customer fields and explicit Archive/Unarchive, which never
  changes the Task status.
- **Email** — the existing read-only historical reference. No conversation is fetched and
  no second composer is created.
- **Global Task** — mounts the existing `globalTasksMount` editor in the dashboard pane
  with its embedded option: no nested task conversation and no editor-local Back.
- **Other Tasks** — mount the existing `tDetail` editor in the pane with its embedded
  option (fields, actions, hierarchy, links and a direct related-record reader). The
  workspace Chat tab is the only private conversation; child/parent/dependency/linked
  task and related-record clicks call the workspace navigation callback, so the header,
  Chat source and Back stack always name the record on screen. Outside Focus
  the original modal/editor behavior is unchanged.
- **Customer** exposes `dirty()`; the workspace adapts it without merging Customer's
  separate details and state writes. Story and Task editors expose their own dirty
  checks, and Save is offered only when the controller has an explicit save operation.

Task-to-task children and linked work open by pushing a new workspace entry. Those
related records are fresh selections and therefore open on Chat, with Back returning to
the originating dashboard.

A successful embedded Task save or explicit Reload may return a canonical `task_type`
that differs from the dashboard's current renderer. The workspace passes that exact
canonical response through its stable controller without a second read, disposes only the
replaced dashboard once, and mounts the existing Story/Customer renderer in the same
entry. The Task's source identity, scoped Chat instance/draft, selected dashboard tab and
Back stack stay unchanged; only the dashboard tab's icon and label are repainted. Global
Task conversions go through the same `globalTasksMount` callback and path. A historical
email reference remains the existing read-only email view.

The first authorized `tasks_get` is also the subtype authority: a descriptor whose
`task_type` is absent or stale is normalized for the existing entry header before its
dashboard finishes mounting, so the body and type tab always agree without a recursive
remount. A changed-type notice exposes the existing deliberately-labelled
Reload/Discard control for both editors; the Global action re-reads through
`globalTasksMount.open(..., {adoptType:true})` and then takes the same canonical adoption
path. The action waits for the read and current/disposed guards before replacing anything,
and the separate Chat controller/text/attachments are never touched.

Adoption never precedes the canonical write. A failed or conflicted conversion keeps the
Task editor, its chosen fields and its original type/revision. A clean source
revalidation adopts an externally changed type; a dirty editor keeps its fields and base
revision and shows a visible changed-record notice on Chat/dashboard/Back. Explicit
Reload/Discard is the only route that may replace those dirty fields and adopt the
canonical type; there is no automatic save or tab-switch prompt.

### Signoff

`rwSignoffDashboard` mounts the canonical single-item review view (`sgCreateView`) with
its own execution/detail controls. It omits the card's nested conversation for the
Signoff's private channel because the workspace Chat tab is that channel; this keeps one
composer per record. Approve/Reject/Respond remain the canonical controls.

### Project

`rwProjectDashboard` reads `project_record_get` and shows:

- the Project's **real name** (a Focus display-name override never renames it), summary,
  archived marker and a Link-existing-item action. A rename from the authoritative
  `project_record_get` updates the workspace header, Focus card and Threads row without
  changing the source identity;
- sections Work, Signoffs, Actions, Documents, Tables, Automation, Other, lazily loaded
  and independently paged (25 rows per read) with `project_items_list`;
- **Closed work** and **Archived** as separate boolean opt-ins sent to the read contract
  (the second Archived click requests `false` and the pressed state says `false`);
- re-reading the visible section when it becomes visible, clearing authorized rows and
  cursors on a revoked append, and a working Retry distinct from "no matches";
- association badges and the owning Project/Global scope on every row, keyed by
  `type + scope + id` so two Projects may present the same source id;
- **Unlink** only for an explicit association link, with its link id and expected
  revision. Ownership-derived rows are never unlinked.

Link uses `project_items_candidates` scoped by Global or an accessible Project (archived
Projects are labelled), an optional kind and a query, then `project_items_link` with an
idempotency key shared by an uncertain retry. `project_items_unlink` sends `link_id` and
`expected_revision`. The picker's canonical document/reference kinds are
`material`/`person`/`topic` (the existing neutral resolver), not a fabricated `document`
kind; "Document" is only a presentation label for the Documents section.

A Project is **pinned on Focus only explicitly** via Add to Focus → Existing Project,
which sends `focus_present` with `{scope:"global", source:{type:"project", id,
business_id}}`. Reading a Project or its inventory never pins it.

Project expansion on the Focus board is separate: its chevron lazily reads
`project_items_list` with `section:"work"`, `root_tasks_only:true` and 25 rows per page.
Expansion is collapsed by default and never triggered by selecting the Project. Derived
rows carry their target's own scope, deduplicate by `type + scope + id`, and open the
target record; they are not Focus cards and cannot be pinned, moved or snoozed. Archived
derived work follows the board's Archived filter, and the project expansion does not
poll.

### Readers

A Focus Reference resolves its current target through the authorized `focus_get` when
its cached DTO has no URL, then keeps the sandboxed frame reader and both tabs (Chat and
dashboard) when the reference has a private thread. Entering a Reference through a chat
reference (not only a Focus card) uses the same authorized DTO to derive `can_thread`;
the existing private Chat tab/binding is retained rather than forced off.

Project inventory items use the authorized `project_item_get` read model. Its
per-family envelopes render as the actual record: `{action}` as the existing canonical
Action row/discussion reader (the same reader as Activity), `{document}`/`{note}` as the
full document body (with the existing bounded reader), `{table}` or table metadata as an
in-pane table, `{person}`/`{entity}`/`{asset}` through their fields, `{reminder}` and
`{routine}` as the safe readers below, and historical emails as the existing read-only
reference. No flat `{summary}` preview is substituted for these families and no raw JSON
is dumped. A canonical `{ticket}` Project envelope is rendered by the one small
read-only Dev report reader described below; the retired System Dev queue, navigation
and editors remain absent.

A Project `context_get` `{document,complete,next_cursor}` envelope is handed to that
existing reader as its authorized first page, not collapsed into a one-shot projection.
The exact typed source id is passed as `other.id` (the workspace descriptor is never
used as a document id), the top-level cursor/completion is retained, and `Read more`
continues through the reader's own scope, revision, character-offset and content-hash
checks. A complete short note renders immediately with its completion marker; a changed
source offers a clean restart instead of mixing chunks; a continuation denial follows the
entry-level unavailable lifecycle. `{note}` remains the whole-note case.

Each nested reader's controller is disposed and its generation invalidated when the
Project entry is denied or removed. A Project item revalidation happens on every reveal,
including when the item came from the `project_item_prefetched` cache, so a cached
association can never permanently bypass later Task/Signoff/Project authorization. The
revalidation guard has one contract: `true` lets the child reader's `onShow` run (including
on a transient Project-item failure that retains last-authorized content), `false` means
the entry was invalidated or its host is gone, and a source denial removes the whole
entry rather than clearing one child host.

A `project_items_list` permission denial is inventory-wide: every cached section's rows,
cursor, loading/pending flag and generation is invalidated before another section can
paint. A held response from a different section cannot resurrect its old page. This does
not remove an otherwise authorized Project summary; a `project_record_get` or source
denial still uses the full entry-removal lifecycle. Transient inventory failures may keep
previously authorized rows with a usable Retry, including an append Retry that resumes
the same cursor without duplicating rows.
Ordinary non-Project documents/Context use the same canonical readers in the pane, not
the legacy modal. A non-null business id with no accessible slug is reported unavailable;
it is never routed to `/root` as a Global record.

Tables keep the existing `tables_get`/`tables_rows_list` read in this pane: columns,
rows, bounded More paging and revision checks. Initial row failure is a real Retry inside
the table reader; append failure keeps the already-authorized rows and retries the same
cursor without duplicates. Nested table revalidation reaches the child controller after
a successful `project_item_get` guard. The separate Tables drawer remains for its normal
outside callers; the record dashboard never substitutes an "Open table" button that exits
the pane.

Three Project families have dedicated safe readers:

- **Dev report** (`type:"dev_ticket"`, `resource_type:"dev_tickets"`) — the owner
  `{ticket}` record (from `project_item_get` for Project inventory or the narrow root
  `dev_tickets_list` selector for a standalone reference) renders only its title, kind
  and status, full `details` body (Markdown/paragraphs preserved), created/updated times,
  resolution, and verification/deployment reference when the payload actually supplies
  them. It has no Chat tab. Tags/labels use the existing record typography and safe text
  helpers; arbitrary fields, JSON and item summaries are never shown. Project inventory
  continues to use `project_item_get.record` with the original source scope. A standalone
  reference uses the existing owner-only root `dev_tickets_list`
  (`{ticket_id,status:"all",details:true}`) and requires the returned ticket's
  `business_id` to equal the reference's actual scope (explicit null is Global); a
  mismatch is unavailable, never another scope's report. That owner selector returns
  `{items,next_offset}` with at most the exact ticket; the agent-only
  `{ticket,events,next_offset}`/history contract is not called or invented. If an
  authorized envelope already contains supported event rows (`events` plus
  `events_next_offset`/`next_offset`), they render from that whitelist; the reader does
  not fetch a queue or owner-history API on its own. The owner envelope here is
  `{items:[{...ticket fields...}],next_offset}`; the agent `{ticket,events,
  events_next_offset}` history shape is neither requested nor imitated. Source denial
  while hidden, retry and disposal follow the same lifecycle as other readers.
- **Reminder** (`type:"reminder"`, `resource_type:"owner_reminders"`) — `record.reminder`
  supplies the host's safe projection (`reminder_id`, `revision`, `text`, `at`, `state`).
  The pane mounts the shared reminder detail (`js/reminders.js`), the same view and
  controls as the owner strip: it reads the full reminder with `reminders_get` as soon as
  it mounts and repaints with the result, then offers Dismiss, Snooze, Edit and Cancel
  through the shared `reminders_*` operations on this record's route. Opening or reading
  never dismisses. Losing access removes the controls at once. The host's
  `owner_reminders_list` read remains the inventory adapter, consumed through
  `project_item_get`; without `js/reminders.js` the pane falls back to a read-only view.
- **Routine** (`type:"routine"`, `resource_type:"routines"`) — `record.routine` is the
  unchanged `routines_get` result. The pane shows the routine, then a bounded history by
  calling the existing scoped `routines_runs` with its exact `routine_id`. History has its
  own cursor/More control and preserves the routine's own business or Global scope. Runs
  are not flattened into Project inventory. A run's structured `job_id` can open the
  existing `jobs_get` reader in the same pane; job IDs from arbitrary strings are never
  treated as references, and there is no generic jobs browser or private job thread.

## Contract used by the dashboard

All calls are synthetic-tested; the UI never invents data.

| Operation | Purpose |
| --- | --- |
| `project_record_get` | Project header, real name, summary, archived state |
| `project_items_list` | Sections, closed/archived filters, root-task expansion, cursor paging |
| `project_items_candidates` | Link picker: source scope, kind, query, cursor paging |
| `project_item_get` | Authorized reader model for one inventory item (including the safe Dev report, reminder view and unchanged routine result) |
| `dev_tickets_list` | Existing owner-only root read narrowed to one standalone Dev report (`ticket_id`, `status:"all"`, `details:true`); no queue UI or writes |
| `project_items_link` | Explicit association, idempotency key |
| `project_items_unlink` | Explicit association removal, link id + expected revision |
| `focus_present` | Explicit Project pin with the canonical Project source |

Routine history and job detail use the existing `routines_runs` (`{items,
next_cursor}`) and `jobs_get` (`{job, details_available}`) reads with the routine/job's
own scope. Reminder inventory is backed by the host's `owner_reminders_list`
(`{reminders:[{reminder_id,revision,text,at,state}],next_cursor}`), read from the owner's
shared `reminders` records and filtered to the exact scope; the dashboard does not call it
directly.

Canonical Project source: `{type:"project", id:PROJECT_UUID,
business_id:PROJECT_UUID}`. Chat drafts and workspace entries use
`type:business_id-id`; a bare id never identifies a scope.

## Outside Focus

`recordSurfaceMount` and `record-panel.js` remain for other routes, with their existing
thread-first/optional-panel behavior. Only the old Focus wrapper and Threads Details modal
were removed; those routes are untouched. `tests/test_record_panels_browser.js` covers
them.

## Verification

`tests/test_record_workspace_browser.js` exercises the whole contract against synthetic
localhost fixtures: Story read/edit/save/retry/conflict, Chat acknowledgment only while
visible, drafts, dirty Save/Discard ordering and failed-save retention, a dirty
intermediate Customer Cancel/Discard guard on ancestor navigation, already-current and
ancestor `navigate`/`open({root:false})` paths, bounded-stack eviction, dashboard scroll
through a direct Chat round trip, initial dashboard Retry (Task and Project), same-pane
Task child navigation with no nested conversation, the stable `tDetail` adapter across a
successful save followed by a new unsaved edit and Close, successful business and Global
Task->Story conversion with the same Chat draft and one replacement dashboard, a failed
conversion preserving fields/type, clean external subtype adoption and dirty
Chat/dashboard/Back revalidation preserving the original editor, explicit business/Global
Reload adopting Story while retaining the separate Chat draft, fresh descriptors with
absent/stale task_type normalizing to the canonical Story/Customer header, canonical
Action/document/person/material/table readers, Project sections/filters/paging/link
retry/unlink/Back, archived two-way toggle, denied paging Retry, Project rename, hidden
Story/Chat revocation, append denial and late replies, cached Project-item
reauthorization, Project document multi-page continuation/source change/continuation
denial/complete notes, table initial and append Retry with nested revalidation, all-section
inventory cache invalidation with a held response, explicit Project pinning, scoped
duplicate expansion and foreign scope badges, archived derived work, routine
history/More/global scope/job detail, reminder text/schedule/state, the real-shaped Project
and standalone Dev reports (multi-paragraph details, resolution/optional fields, exact
business scope, no Chat, hidden denial and safe whitelisting), typed Project open action, a
real-shaped email Signoff in both Chat
and dashboard with its exact Global source scope plus input-question coverage, Focus
Reference tabs entered through a chat reference, Threads parity, and the 390px one-pane
layout. Set `RECORD_WORKSPACE_SHOTS=/path` to save screenshots.

Related focused checks:

```sh
PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_record_workspace_browser.js
PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_record_panels_browser.js
PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_record_panels_focus_browser.js
PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_focus_conversations_browser.js
PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_chat_references_browser.js
PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_chat_parity_browser.js
```