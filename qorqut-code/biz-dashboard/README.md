# Qorqut dashboard

The owner dashboard for the Qorqut agent platform. It is a component of the
Qorqut host, not an independent production server: the host service
(`bazarlyk.service`, from `qorqut-code/app`) serves these static assets, the
tmux terminal mirrors and the authenticated HTTP MCP endpoint from one process.
`start.sh` launches that service; `server.py` is only a thin entry point that
forwards its arguments to it. There is no business-chat subprocess, CLI domain
bridge, per-agent MCP server or arbitrary command runner.

The v1.3 dashboard has no Marketing, Accounting, Products, Subscriptions,
Milestones/Research, Personal Context, daily-report or fixed-department views,
menus, routes or styles. Mail has no dashboard screen; it is handled in chat and the browser. Project notes are one ordinary list
sent with an explicit `target_business_id`. Paper and its Outreach workspace are
an optional extension (see [Agent pane workspaces](#agent-pane-workspaces)).

Installation is documented in the host's existing
[`docs/INSTALLATION.md`](../docs/INSTALLATION.md) (`qorqut-code/docs/INSTALLATION.md`).
Nothing in this component installs, deploys or restarts a service.

## Layout

| Path | Contents |
|---|---|
| `index.html`, `file-viewer.html`, `tables-view.html` | Entry pages: the shell, the standalone file reader and the read-only table page. |
| `js/`, `css/`, `app.css` | Static modules and styles. There is no frontend build step. |
| `server.py` | Thin entry point: imports the installed host's `bazarlyk.service.main` and forwards its arguments. It never looks for a sibling host checkout and holds no service, configuration or write logic. |
| `pty_broker.py` | The tmux/PTY mirror. |
| `start.sh`, `.env.dashboard.example` | Launcher and its machine config (see [Setup and lifecycle](#setup-and-lifecycle)). |
| `docs/` | Component contracts (see [Documents](#documents)). |
| `tests/` | Node browser fixtures and Python launcher/broker tests. |
| `tools/` | Perf measurement, icon catalog and the Guacamole click-focus extension. |
| `icons/` | The icon assets. |

## Signoff proposal previews

Signoff details in Chat, the drawer, Focus and email review show the frozen
proposal before the decision buttons. WhatsApp shows the recipient name (or phone number)
and exact message; account email shows recipients, subject, body, attachment names
and saved source content. Other internal changes show labelled before/after values
with a disclosure for additional fields and full text. Sensitive fields are hidden
recursively; bookkeeping metadata and duplicate queue payloads are omitted.
Missing required message content prevents approval. Closed list cards offer Review
to open the prepared detail before Approve becomes available. This reads existing
preview data without fetching providers or changing decision bindings. Message
text stays fully expanded. Technical chat/message IDs are omitted, and inline
message cards keep the proposer explanation under Details.
Check: `node tests/test_signoff_previews_browser.js`.

The Focus toolbar keeps Browser/Desktop fixed on the left and shows pending
Signoffs in a scrolling row on the right. Each opens the existing review window;
only a decision removes it. Viewbox links still open their existing Focus detail.
Check: `node tests/test_focus_signoff_queue_browser.js`.

## Setup and lifecycle

Copy `.env.dashboard.example` to `.env.dashboard` (gitignored) and fill it in.
`start.sh` reads `INTERPRETER`, `WORKSPACE`, `INSTANCE_ROOT`, `COMPONENT_APP`,
`COMPONENT_DASHBOARD`, plus optional
`COMPONENT_OPERATIONS_ENGINE`, `COMPONENT_CONTEXT_ENGINE` and `COMPONENT_ROLES`.
Every selection is an absolute, normalized path; nothing is discovered. It
forwards them as `--workspace`, `--instance-root` and `--component NAME=PATH` to
`python -I -m bazarlyk.service`. `BIZ_ROOT` and `BIZ_DIRS` are retired.

```sh
./start.sh argv        # print the command without starting anything
```

Terminal state lives under `BIZ_DASH_STATE`, else
`$XDG_STATE_HOME/qorqut/terminals`. The default private origin is
`http://127.0.0.1:1049`; a host behind another address lists explicit origins in
`BIZ_SERVICE_ORIGINS` (for example `http://qorqut.example:1071`).

`start.sh foreground` execs the app in the same PID with explicit external
`BIZ_DASH_CONF`, `BIZ_DASH_STATE` and either `BIZ_DASH_LAUNCHER` or
`BIZ_DASH_PYTHON` (or `INTERPRETER` in the config); `BIZ_DASH_WORKSPACE`
overrides `WORKSPACE`. The instance root and component locations come from the
config. The legacy stop command reports a graceful-shutdown timeout instead of
using `kill -9`. Focused check:
`python3 -m pytest -q tests/test_foreground_launcher.py`. The installation and
rollback contract is in the host's [`docs/INSTALLATION.md`](../docs/INSTALLATION.md), not in this source.

## Shell and navigation

- **No main menu.** The Agents navigation (roster, Terminal, Settings below it)
  is the only navigation. A non-Agent top-level page such as Settings offers one
  **Back to agents** action. First visits open Global → Agents with the registered
  Ordubashy selected; explicit URL destinations win over the saved workspace
  route, and a missing saved agent falls back to Ordubashy, then the first
  Global agent or the workspace terminal. Check: `node tests/test_initial_navigation.js`.
- **Global and projects.** The workspace selector keeps **Global** above the
  projects. Global has no selected project, disconnects the terminal mirror and
  keeps the personal task drawer. Global pages use `#/global/<page>` with refresh
  and history support, and the shell works with no registered projects. Project
  entries can be dragged into a custom order, saved by stable id in browser local
  storage.
- **Projects.** The owner UI calls the container a Project everywhere;
  identifiers, routes, storage keys and `business_id` are unchanged. The dashboard
  has no project-creation control and never calls `business_create`: with zero
  projects it says "No projects yet. Ask Ordubashy to create one." Checks:
  `tests/test_project_creation_absent_browser.js`,
  `tests/test_agent_pane_destinations_browser.js`.
- **Archive and restore.** Applied project-archive Signoffs refresh the workspace
  menu while preserving the Global review panel; the drawer Charts view lists
  archived projects with Restore (`GET /api/root/businesses/archived`,
  `POST /api/root/business-restore`). Checks:
  `tests/test_business_archive_browser.js`, `tests/test_business_restore_browser.js`.
- **Global roster.** Agents lists the active Global agents (Ordubashy, the
  Learning Applier and any number of `global-worker` agents) from
  `/api/root/agents`, then one plainly named workspace Terminal below a rule. There
  are no lookup or pin controls. A saved or linked agent that is not an active
  Global agent shows "This agent is no longer active. Its history is kept." and
  falls back to Ordubashy. Terminal opens a plain shell at the configured Qorqut
  workspace with no agent profile, inbox, preparation, credential or transcript
  capture. Check: `node tests/test_global_roster_browser.js`.
- **Agent-pane views.** Context is the Learning Applier's first tab (graph and
  object details, People, Learning, Filed context, source editing and Upload);
  Tables, Calendar, Costs and Charts are shared-drawer tabs. Old `#/global/context`,
  `/learning`, `#/global/tables` and `#/global/calendar` URLs translate to the
  owning tab or drawer view, and the retired Ideas and Fleet routes fall back to the
  generic default view.
- **Shared drawer** (`js/overview-drawer.js`, tab list in `js/tasks.js`). Notes,
  Tasks, Signoffs, Uploads, Routines, Calendar, Charts, Costs, Tables and Activity.
  Calendar, Charts, Costs and Tables are not agent-pane views and are absent from
  embedded panes. `?drawer=calendar|charts|costs|tables` is kept with
  `replaceState`, and Tables takes `table_scope`, `table_id` and
  `table_mode`; an unknown scope or mode shows an unavailable state. Switching
  tab or scope runs the Tables frame's save/discard guard first. Check:
  `tests/test_overview_drawer_browser.js`.
- **Global Routines and Signoffs** read root-scoped routine and `/api/root/signoffs`
  records, refresh every 12 seconds while open and use the same review cards as
  project Signoffs. Routines take registered program ids and typed arguments,
  never shell strings. Check: `node tests/test_global_routines_browser.js`.

## Focus, attention and conversations

Focus is the first tab of Ordubashy's pane: a compact, owner-ordered board of
persistent cards over Tasks, Signoffs and URL references.

- Rows lead with a status circle or type marker, a project chip (a globe for Global),
  the title and a snooze control. The owner alone orders rows (drag by the marker or
  Alt+Up/Down) with revision checks and a frozen idempotency key; server order stays
  authoritative. Archived and Postponed are filters beside the refresh icon, and nested
  child Tasks follow the Archived toggle at every depth.
- Snooze presets are 1h, 3h, Tomorrow, Day after tomorrow, Saturday, Monday, In a
  week and In 2 weeks; hour choices are elapsed time and calendar choices are 6 AM
  San Juan time. Check: `node tests/test_focus_snooze_presets.js`.
- Clicking a row opens its single Ordubashy conversation in the shared record
  workspace dock (height remembered at `ag:focus-thread-height:v1`); Ctrl/Cmd+click
  opens the source in a new tab. Chat is the fresh-selection default; the record-type
  icon tab shows that record's full-width dashboard, Back restores a prior record's tab
  and edits, and Escape closes. There is no worker, peer or agent-discussion channel.
  Reads and sends use `owner_conversation_get`, `owner_conversation_send` and
  `owner_conversation_mark_read`; only a visible Chat selection/load through the latest
  sequence acknowledges it. Source-keyed drafts survive closing and reopening the record. See
  [the record workspace](docs/RECORD-WORKSPACE.md). Checks:
  `tests/test_private_channels_browser.js`, `tests/test_record_workspace_browser.js`,
  `tests/test_focus_conversations_browser.js`.
- The attention strip above the list shows unread reply controls, with pending
  Signoffs in a separate queue below it (`/root/attention-queue`). Reply
  notifications poll `/root/reply-notifications` every 10 seconds while visible and
  every 30 seconds hidden. Checks: `tests/test_attention_queue_browser.js`,
  `tests/test_owner_attention_browser.js`, `tests/test_reply_notifications_browser.js`.
  The shared refresh rule is in [dashboard refresh coalescing](docs/PERFORMANCE_REFRESH.md).
- Focus shows a compact queue of today's calendar events above the attention queue
  (see [Calendar](#calendar)).
- Task state circles update optimistically (a dashed outline marks saving), serialize
  per task and reconcile on failure. Check: `node tests/test_task_state.js`.
- Story and Email Task types show an icon in place of the state ball, with no
  Status editor. Existing Email Tasks show a historical email reference instead of
  fetching the conversation, and Email cannot be chosen as a new Task type. The
  **Link task** picker adds clickable chips to Tasks and Stories. Story, Customer and
  Email Tasks share the record dashboards in `js/record-panel.js` (Story reads as
  paragraphs with inline Edit/Save). Other routes keep their optional left-side panel;
  Focus uses the shared full-width workspace. Task/Global Task dashboards embed
  the existing editor without a nested conversation, and every child/linked/related record
  navigates the same pane (header, Chat source and Back stay on that record). Dirty
  dashboards use one Save/Discard guard for Close, Back, stack eviction and a fresh root
  selection; failed saves keep their retry UI. A Project dashboard lists every inventory
  section, filters Closed/Archived separately, revalidates the visible section, links/
  unlinks only explicit associations and pins on Focus only when the owner adds it.
  Canonical Action/document/table readers and Project per-family envelopes stay in the
  pane. Reminders open as a safe text/schedule/state reader; automation items are routines
  whose runs page through the scoped `routines_runs`, with job detail only through
  `jobs_get`. A typed Project chat reference keeps its chip and adds an Open Project
  action. Checks:
  `tests/test_global_tasks_browser.js`, `tests/test_business_tasks_browser.js`,
  `tests/test_task_links_browser.js`, `tests/test_record_panels_browser.js`,
  `tests/test_record_panels_focus_browser.js`.
- Focus snooze, Task snoozing (Active/Snoozed/All), assignees and dependencies use
  anchored popovers. Check: `node tests/test_task_controls_browser.js`.

### Chat

Every Chat surface renders through `js/chat-view.js` with `css/chat-view.css`
(`mountChatView`); the interface is in
[the shared Chat view contract](docs/SHARED-CHAT-VIEW-CONTRACT.md). Global agents use
the persistent native Chat (`js/global-chat.js`) over
`/api/root/agents/<agent_id>/chat`; the DTO contract is in `qorqut-code`
`docs/v1.2/NATIVE_CHAT_DTO.md`.

- The composer grows to 200px, accepts clipboard images and files (Ctrl/Cmd+V),
  drag and drop or the link button (up to ten files, 25 MB each), copies each
  into the agent's `downloads/` area and sends the local-file reference.
- `request_user_input(message, references)` appears as an agent message with typed
  references: an inline Signoff card, a browser-desktop frame or a link.
- Messages show their original stored time in the browser's time zone; "Load older"
  follows the chronological history cursor. Telegram relays render as a compact
  disclosure. Attachment chips open an in-chat preview only on click.
- `js/safe-links.js` renders `[label](https://…)`, `/files/view?…` and
  `/tables/view?…` links, absolute file paths under a `businesses/` directory and bare
  `http(s)` URLs; everything else stays escaped and links open in a new tab. Any
  page that loads `conversation.js` must also load it.

Checks: `tests/test_global_chat_browser.js`, `tests/test_chat_view_browser.js`,
`tests/test_chat_parity_browser.js`, `node tests/test_safe_links.js`.

### Terminal and composer

Pages other than Global → Agents have a bottom-right quick composer
(`js/workspace-composer.js`). It sends the text as a native Chat message to the
selected global agent (Ordubashy by default) over
`/api/root/agents/<agent_id>/chat/messages`, exactly as the Chat view does: it reads
the agent's current Chat binding, then posts with an `idempotency_key` and
`expected_binding`. A retry of the same text reuses its key and binding; a refusal
stores nothing and the next send starts fresh, while an unconfirmed outcome keeps the
key. Drafts are kept per scope and agent, control characters are rejected, and
Ctrl+Enter does not submit. Show terminal is separate and optional: it opens a
resizable terminal viewer above the composer and never carries the message.

No normal Broadcast or Working Memory entry point is currently exposed in the
navigation. Their modules (`js/broadcast.js`, `css/broadcast.css`,
`js/working-memory.js`) and draft lifecycle are retained, and they are covered only
as components mounted directly in a test page, not as reachable features. Checks:
`tests/test_workspace_composer_browser.js`, `tests/test_terminal_browser.js`,
`tests/test_broadcast_browser.js` and `tests/test_broadcast_real_browser.js`
(direct `renderBroadcast` mounts), `tests/test_working_memory_browser.js` (direct
`workingMemoryMount`), `python3 -m pytest -q tests/test_broadcast_submit.py` (isolated
tmux; `QORQUT_TEST_CODEX_INPUT=1` also checks a local Codex `/status`).

Shift+Tab cycles agents on Global → Agents; hold Tab and press O to jump to
Ordubashy on Global → Agents (not while typing or with a dialog open). Check: `tests/test_agent_shortcuts_browser.js`.

## Agent pane workspaces

The pane's view list is a client-side registry keyed by the host's
`workspace_types` read: each type declares its supported roles, renderer and
presentation actions, and the pane derives its views from the selected agent's role.
Renderers register with `window.registerWorkspaceRenderer(name, {mount, tick?,
dispose?, leading?, views:{key:{label,icon}}})`; duplicate names and malformed
definitions throw. Core registers the `focus_board` renderer (Focus, first tab). The
built-in Focus declaration, which supports the `ordubashy` and `global-worker` roles,
is only a fallback for the time before a server declaration is available (the
best-effort `workspace_types` read is pending, unavailable or malformed). Once the
server returns a list of types, that declaration replaces the fallback entirely,
including an explicit empty list or a declaration that excludes a role. After that, a
view exists for an agent only when the server declares its type for the agent's role;
a declared type with no renderer or an unsupported role shows an explicit
unsupported-workspace panel.

Optional extensions add renderers through the owner-only `GET /api/root/extensions`
manifest (`{extensions:[{id, assets:{scripts, styles}}]}`). Each asset must be a
same-origin `/extensions/<id>/assets/<file>.js|.css` path of that extension and is
loaded once; a failed or malformed entry leaves its workspace unsupported. A server
without the endpoint, or with an empty list, is core-only and loads nothing.

### Paper Outreach

Paper's Outreach script (`campaign_review`, label Outreach, leading) comes from
`qorqut-paper/qorqut_paper/assets/` and embeds Outreach's own build through the fixed
proxy prefix `/integrations/outreach/ui/`. Opening it reads `workspace_get`, mints one
single-use owner assertion (`POST /api/root/integrations/outreach/assertion`), exchanges
it for a path-scoped session at `{prefix}/api/v1/integrations/session-exchange` with the
assertion only in the POST body, then loads the frame at the same prefix. The assertion
is never written to browser storage. Stored presentation state is posted to the frame
as `{type:"qorqut:workspace-presentation", ...}` at the configured frame origin, and the
frame's `outreach:ready` and `outreach:navigate` signals are accepted only from the
mounted frame window at that origin and become pane state as route keys, never URLs.
`QORQUT_OUTREACH_FRAME_ORIGIN` may be set before page scripts run when a host fronts
the prefix on another origin; the default is this dashboard's origin. The dashboard
performs no campaign content calls and no writes: authorization stays server-side.
See [Paper workspace entry](docs/PAPER-CUTOVER.md).

Known limitations: Outreach's pinned bridge types the host envelope and its own
navigation signals, so the presentation message is ignored inside the frame until
Outreach consumes it, and attention stays on the pane tab and header because
`/api/me/attention` has no workspace projection.

Checks: `node tests/test_workspace_dispatch.js` (registry and manifest loader, mocked
API); the Paper handshake tests live in `../qorqut-paper/tests` (`DASHBOARD_ROOT`
defaults to `../biz-dashboard`).

### Viewbox

Every registered agent's pane has a Viewbox tab (`js/agent-viewbox.js`,
`css/agent-viewbox.css`) right after Focus: the agent's latest 30 placed links, newest
first, in a resizable same-pane frame with Back, Fit and Open in new tab. It reads the
owner-only `GET /api/root/agents/<id>/viewbox` on mount and on the visible-pane
cadence. Only exact same-origin `/files/view` and `/tables/view` are framed
unsandboxed; every other URL is sandboxed. `tables-view.html` is the minimal read-only
owner table page. Checks: `tests/test_agent_viewbox_browser.js` (stub API) and
`tests/test_agent_viewbox_real_browser.js` (isolated service).

### Other agent tabs

- **Agent status** keeps session state and declared work, with Claude and OpenAI
  provider marks, and polls while visible. Cards expand to checkpoint, blocker and
  observation details without inferring task state. Provider SVG marks are from Simple
  Icons v14 (CC0); brand rights remain with their respective owners. Check:
  `tests/test_current_work_cards_browser.js`.
- **Working memory** (retained module, no current navigation entry; see
  [Terminal and composer](#terminal-and-composer)) shows the agent's cross-session handoff with a Global/project
  selector; owners can inspect history, correct text and restore a historical version as
  a new checked version. Drafts survive navigation and conflicts preserve edits. Check
  (component mount only): `tests/test_working_memory_browser.js`.
- **Browser and Desktop** show the agent's browser surfaces
  (`GET /root/agents/<uuid>/browser-surfaces`, `GET /api/biz/<slug>/browser-surfaces`).
  Ordubashy's Desktop uses its explicit binding, else the validated
  `DESKTOP_CONTROL_URL`; leaving the tab disconnects the frame. A fixed 1920×1080
  display appends `qorqut_width` and `qorqut_height` query parameters, which the
  dashboard strips before navigation; the Guacamole connection must use the same size
  with dynamic `display-update` resizing disabled (remove `resize-method`). The
  [Guacamole click-focus extension](tools/guacamole-click-focus/README.md) restores
  focus to the iframe and adds a clipboard button.
- **Inbox** shows a green dot while the watcher holds a valid lease. Check:
  `tests/test_agent_inbox_browser.js`.

## Calendar

Global Calendar shows the owner's provider events together on an hourly
grid with Day, 3 days and Week views and a month navigator; overlapping events use
adjacent lanes and all-day events sit above the grid. Basic create, edit, delete and
invitation-response controls use the calendar tools. Each event has an owner-only
**Personal status** (Pending, Completed, Cancelled or Rescheduled) at
`GET/POST /api/root/calendar-statuses`: revision-checked, shared by every Focus pane
and the drawer Calendar, never a provider change, and a recurring event's status
belongs to the clicked occurrence. Checks: `tests/test_calendar_browser.js` (needs the
host fixture) and
`PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_agent_calendar_browser.js`.

## Tasks, notes and uploads

- The task drawer shows open tasks assigned to the current human across authorized
  projects in Global, and only the selected project's tasks in a project workspace
  (`GET /api/me/tasks`). Tasks keep their project; details and edits always use the
  owning project. Check: `tests/test_personal_tasks_browser.js`.
- Tables provides typed inline cell editing, search and sorting; row edits stay local
  drafts until Save, and column settings save separately. Topics is the only tag kind.
  Global → Tables reuses the view for Global general tables through
  `/root/operations/<op>` with an explicit `scope: "global"`. Checks:
  `tests/test_tables_browser.js`, `tests/test_global_tables_browser.js`.
- Notes combine free writing and dash items. Enter starts a block, Shift+Enter
  continues it. **Add to Context** submits a structured snapshot as one complete note;
  filed versions are never replaced silently. An open note keeps up to 200 undo
  snapshots (Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z or Y) and zooms 50–200% with
  Ctrl/Cmd+Plus/Minus. Checks: `tests/test_global_notes_browser.js`,
  `tests/test_business_notes_browser.js`, `tests/test_context_notes_filing_browser.js`.
- The upload form has a **Context | Call | Meeting** switch and participant typeahead
  over People. Global uploads go through `/api/root/context/upload` and
  `/api/root/context/people`, are owner-only and refuse media or data files. Checks:
  `tests/test_uploads.js`, `tests/test_upload_participants_browser.js`,
  `tests/test_global_uploads_browser.js`.
- The completed-actions capture button and owner journal are Global-only.
- Stored email references (Focus cards, Context links, record panels, Signoff
  questions) show a historical-reference note and never call a mail endpoint.
  Signoff input forms remain generic.

## Context

The brain icon opens Context: Learning, Graph, Tasks, Tables and People. Learning
review cards are queued per browser, show saving feedback and keep drafts when
confirmation fails. The Learning page separates factual review from a paginated,
read-only view of retained procedural records: the browser loads one bounded
inventory page at a time, reassembles an oversized detail page's byte stream with
the same evidence/history selection in bounded steps (a Continue control keeps a
partially read page, and a failed chunk is retried from its saved cursor), and then
follows each collection cursor, merging sources by (run, session) and history
events by event ID. Retained deltas have no decision, edit, work or reconciliation
controls. Checks: `node tests/test_learning.js`, `node tests/test_learning_history.js`,
and the real Chromium acceptance with every response intercepted:
`PLAYWRIGHT_MODULE=<node_modules/playwright> node tests/test_learning_history_browser.js`.

The published Context graph and People pages read canonical objects through
`context_home`, `context_find`, `context_object` and `context_expand`
(`js/context-published.js`). Only stored relationship records create graph edges, and a
missing page or expired cursor reports the read error instead of drawing a partial
graph. A refresh stages a candidate from the first page through the pages already
loaded and publishes only if the whole candidate validates; a transient failure keeps
the prior content. Operational relationships (`context_graph_get`, `relationships_list`)
are a separate store for Tasks and other records.

Contracts: [read UI](docs/CONTEXT-READ-UI.md), [source UI](docs/CONTEXT-SOURCE-UI.md),
[usage costs](docs/CONTEXT-USAGE-COSTS.md) and [People callers](docs/V1.2-PEOPLE-CALLERS.md).
Checks: `tests/test_context_published_browser.js`, `tests/test_context_source_browser.js`,
`tests/test_context_usage_costs_browser.js`, `node tests/test_ctxgraph.js`,
`tests/test_people_published_contract.js`.

## File viewer

`GET /files/view` (`file-viewer.html`, `js/file-viewer.js`, `css/file-viewer.css`) is a
standalone owner page for one file with no shell, terminal, polling or edit controls.
`?path=<root-relative path>` shows the current file;
`?document_id=<uuid>&business_id=<uuid>` (omit `business_id` for Global) with optional
`&version=N` opens a registered document or one saved version, with paged history and
version compare. Markdown is rebuilt through an allowlist, CSV shows at most 200 rows and
50 columns, HTML is a sandboxed script-less frame and PNG, JPEG, GIF and WebP images use
the owner-only raster stream endpoint. Files over 2 MiB preview the first 2 MiB with the
full Download. Only the exact `/files/view` page may be framed, same-origin. Reads use the
owner-only `/api/root/files/*` routes. Checks: `tests/test_file_viewer_browser.js`
(isolated service, synthetic files) and `node tests/test_safe_links.js`.

## Tests

From this directory, with the host's `app` checkout and an interpreter that has the pinned
Operations and Context engines installed:

```sh
BAZARLYK_CORE_ROOT=/absolute/path/to/qorqut-code/app \
QORQUT_TEST_ROLE_ROOT=/absolute/path/to/agent-roles \
TEST_PYTHON=/absolute/path/to/python \
  node tests/<file>.js

python3 -m pytest -q tests/test_foreground_launcher.py
```

Browser fixtures use real assets with synthetic API responses (or the host's
`tests/service_browser_fixture.py` on isolated ports and separate test tmux sockets) and
never launch Claude. They need Playwright with Chromium: set `PLAYWRIGHT_MODULE` (or
`NODE_PATH`) and, for an existing browser, `PLAYWRIGHT_CHROMIUM_EXECUTABLE` (the Global
task check uses `CHROMIUM_PATH`). Plain Node checks need no browser, for example
`node tests/test_learning.js`, `tests/test_routines.js`, `tests/test_task_state.js`,
`tests/test_safe_links.js` and `tests/test_san_juan_time.js`. Several legacy host tests
need modules that have moved to the separate Operations engine; run only the focused
fixture that matches the change.

`tests/perf_browser_synthetic.js` is a synthetic Playwright timing check against the
isolated fixture; it never targets a running installation.
`tools/agent-icons/build_catalog.js` builds
`docs/agent-icon-catalog.html` ([icons](docs/qorqbot-agent-icons.md),
[brand](docs/qorqbot-brand.md)).

## Documents

- [Host references](docs/HOST-REFERENCES.md): the host contracts this dashboard pairs with.
- [Shared Chat view contract](docs/SHARED-CHAT-VIEW-CONTRACT.md)
- [Context read UI](docs/CONTEXT-READ-UI.md), [source UI](docs/CONTEXT-SOURCE-UI.md),
  [usage costs](docs/CONTEXT-USAGE-COSTS.md)
- [Paper workspace entry](docs/PAPER-CUTOVER.md)
- [People callers](docs/V1.2-PEOPLE-CALLERS.md)
- [Dashboard refresh coalescing](docs/PERFORMANCE_REFRESH.md)

Pending WhatsApp Signoffs support click-to-edit message text in Chat references and
the review modal. Save prepares a new owner-attributed proposal with the recipient
unchanged; approval is separate. Original references follow owner revisions, and
Details keeps a link to the previous message. Drafts survive refresh and failed saves;
uncertain saves retry their original idempotency key.
Check: `node tests/test_signoff_message_edit_browser.js`.

Email Signoffs also support clicking directly on the subject or body to edit. Private-account and project sends, replies and forwards retain their original recipients, sender, attachments and source context. Saving creates a new owner-attributed pending proposal; approval remains separate. Email text follows the existing 998-character subject and 50,000-character body limits.

Focused browser checks: `PLAYWRIGHT_MODULE=/path/to/playwright node tests/test_signoff_email_edit_browser.js` and the same command with `EMAIL_EDIT_PROJECT=1`.
