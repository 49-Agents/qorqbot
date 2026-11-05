# Agent Viewbox

A Viewbox tab in every registered agent's right-hand pane (Global agents, business agents, and business agents
selected on Global). The agent places links; the owner opens them beside the session.

## Tool

`viewbox_place(link, title)`: exactly these two arguments. The agent is the authenticated principal; there is no
`agent_id` argument and nothing can place on another agent's board. Core default is `allow` (an explicit deny
override still wins). No signoff, no network fetch, no agent wake.

- `title`: 1–3 words, at most 80 characters, no control/format characters.
- `link`: trimmed `http`/`https` only, at most 4096 characters, no userinfo, no control characters or backslashes,
  valid host and port. Query and fragment are preserved. Anything else (`javascript:`, `file:`, `data:`, …) is rejected before any write.
- Root-relative links are accepted only for `/files/view` and `/tables/view` with their strict selectors:
  `/files/view?path=…` or `?document_id=<uuid>[&business_id=<uuid>&version=<n>]`; `/tables/view?table_id=<uuid>[&business_id=<uuid>]`.
  An absolute link to this application's own non-loopback origin and a viewer path is stored root-relative.
- Kind is inferred: `file`, `table`, `local` (host `qorqut` or this application's host) or `web`.

## Board and retention

Durable per-agent placements (tables `agent_viewbox_boards`, `agent_viewbox_items`, migration
`108_agent_viewbox.sql`). A link is unique per agent on its canonical form; placing it again repins the same row
(same id, new title, top of the board). A monotonic per-agent sequence orders the board. Rows are retained; the
owner read returns the latest 30.

`GET /api/root/agents/<agent_id>/viewbox` is owner-only (the dashboard's same-origin identity; agent credentials
are refused) and returns `{agent_id, revision, items[≤30: id, link, title, kind, placed_at, attention_id],
dismissed_attention_ids[]}` with a weak ETag (`If-None-Match` → 304) that also covers the saved dismissals. It is
read-only and never marks anything read.

### Dismissing queue items

`POST /api/root/agents/<agent_id>/viewbox/dismiss` with exactly `{"attention_id": "<viewbox:item:sequence>"}` removes
that one placement from the owner's Focus queue and returns `{agent_id, dismissed_attention_ids[]}`. It is the same
owner-only, Origin-checked dashboard route as the board read: any `Authorization` header (even empty) is refused. The
write is additive and idempotent, not a toggle: the ID must be a placement on the agent's current board (otherwise 409,
and unknown, foreign or already-replaced placements are never saved) or already saved for that agent, so a retry
succeeds. Malformed or extra fields are rejected (422).

A dismissal names the exact placement, so placing the same link again is a new placement with a new `attention_id` and
reappears. The board, its source file/table/link and the agent's history are never deleted or edited, `GET` still lists
every item, and the viewed acknowledgements are a separate preference: viewing does not dismiss and dismissing does not
mark viewed. Dismissals persist per owner and agent in `runtime/dashboard-preferences/<owner-hash>.viewbox-dismissed.json`
(cross-process lock, atomic replace, fsync), outside releases and the database; no migration. Reading creates nothing.
At most 200 dismissals are kept per agent; beyond that the oldest placements no longer on the board are forgotten (a
replaced placement never returns, so none can reappear).

Unread placements from active Global and business agents appear newest-first in the owner's attention queue,
bounded to each agent's latest 30 links. Opening a placement in Viewbox (or using Open in new tab) acknowledges
its `attention_id` through the existing owner queue endpoint. Listing the board does not acknowledge it.
The read state persists in owner dashboard preferences; repinning the same link creates a new attention ID,
so an old acknowledgment cannot clear the new placement. No Focus card or agent inbox event is created.
This extends existing placement storage and requires no additional migration.

## Dashboard

Tab order: general views, then Viewbox, then workspace-specific views, then Status. It never displaces Focus or
Inbox and never opens automatically. The list shows icon | title | age (`2m`, `3h`, `1d`), newest first, and
`Nothing shared yet.` when empty; exact time and link are on hover/accessible name. It is fetched on mount and then
every 12 s only while the pane is visible; an unchanged board is not re-rendered.

Opening a row shows a compact bar (Back, title, Fill pane, Open in new tab) over a frame that fills the pane. A
bottom-right corner handle (pointer, touch, or keyboard arrows / Shift+arrows) resizes both dimensions, remembered
per agent in the browser, clamped to the pane (minimum 240×180 when space allows). Fit resets. New placements
never remount an open frame; Back shows the latest list, focus and scroll restored. Switching agent cancels
pending reads and removes the frame and listeners.

### Iframe limits

- Exact same-origin `/files/view` and `/tables/view` are framed without a sandbox; the service sends
  `X-Frame-Options: SAMEORIGIN` and `frame-ancestors 'self'` for exactly `/files/view` and `/tables/view`; everything else stays `DENY`.
- Every other URL is sandboxed `allow-scripts allow-forms` (no same-origin, top navigation or popups) with
  `referrerpolicy=no-referrer`.
- Many sites forbid embedding (`X-Frame-Options`/CSP); a cross-origin frame cannot report that reliably. The pane
  only shows its fallback note on an actual load error or a deterministic case (non-framable link, `http` on an
  `https` dashboard). **Open in new tab** is always available.

## Table viewer

`/tables/view?table_id=<uuid>[&business_id=<uuid>]` is a minimal owner-only read-only page (50-row pages, Load
more) over `tables_get` / `tables_rows_list`. Global tables omit `business_id`. Unknown, duplicate or non-UUID
selectors return 400. The Tables screen has a Copy link button for the canonical URL.

## Discovery

`viewbox_place` appears in MCP discovery and bootstrap guidance text (`VIEWBOX_GUIDANCE`). Runtime guidance that
mentions `files_link` depends on the file-viewer change.

## Dependencies and deployment

- Migration `108_agent_viewbox.sql` follows migrations 104–107. Its SQL is unchanged from the earlier `106_agent_viewbox.sql`
  (sha256 `6e034f1e…4858`); only the number moved.
- The file viewer (`/files/view`) is integrated: the Viewbox frames its exact
  URL and `viewbox_place` guidance names `files_link`. `tests/test_agent_viewbox_real_browser.js` drives the real
  list into the real reader for Markdown, CSV and hostile HTML (Back, Open in new tab, corner/parent resize
  without reload, nothing reaching the parent, owner API or network).
- The board read refuses any explicit `Authorization` header (agent, human, owner-session, invalid or empty)
  before reading; only the tokenless owner dashboard reads it. Responses are `private, no-store`.
- No role publication, preparation or adoption is included; no live records are touched.

Tests: `tests/test_agent_viewbox.py`, `tests/test_host_viewbox_dismissals.py`, `tests/test_tables_view.py` (core) and
`tests/test_agent_viewbox_browser.js`, `tests/test_agent_viewbox_real_browser.js` (dashboard, with
`tests/viewbox_browser_fixture.py`).
