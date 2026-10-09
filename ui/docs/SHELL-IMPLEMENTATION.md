D57: [Browser UI implementation](BROWSER-IMPLEMENTATION.md) implements the
[Manager-frozen browser integration](https://github.com/qorqutbot/qorqut-core/blob/c96952f/docs/BROWSER-APP-INTEGRATION.md).
It supersedes earlier browser-viewer deferrals; Machine/Desktop stays unavailable.

# First connected chat interface

[D55 local access](https://github.com/qorqutbot/qorqut-core/blob/main/docs/LOCAL-ACCESS.md)
supersedes the browser authentication requirements below: open directly through
GET identity, schema and workspace discovery; connection errors offer Retry
connection while preserving drafts and pending request identity.

The owner agreed D36 on 2026-10-08: manual CLI startup/configuration in Terminal,
with one logical inbox-owning session and Chat as another view. Read the
[owning core scope](https://github.com/qorqutbot/qorqut-core/blob/main/docs/TERMINAL-OWNED-SESSIONS.md)
and [the current UI correction](MANUAL-CREATION.md). Earlier provider/start and
automatic-switch requirements below are superseded; terminal/browser integration
still needs its actual backend contract.

Manager design, 2026-10-08. Implements UI.03 using the real core API and the
visual/interaction direction in UI-PLAN.md. This is a bounded assignment, not
a deferral of the final browser/machine/file-card scope. Those runtime/card
domains are not ready; do not invent API contracts or simulate an assistant.

## Build and source boundaries

Use TypeScript, Preact, Vite and ordinary CSS. Pin direct dependencies and commit
one npm lockfile. Use local SVG paths for core's exact twenty icon keys, without
a large icon package, remote assets, uploads or extra state/router frameworks.
Produce `dist/index.html` and `dist/assets/` for core's optional static mount.
There is no backend in this repo.

Read core's `CORE-API.md`, `AGENT-REGISTRY.md`, `CHAT-IMPLEMENTATION.md` and
`CONSOLE-SERVER.md` at the exact Manager plan commit supplied in the handoff.
Core's owning checkout is `/home/alp/qorqut-core`; do not use another builder's
moving checkout. Define small typed frontend wire representations. Never
import Python, SQLite, browser-service code or runner credentials.

## Connection and API client

Everything uses the current same origin. Probe `GET /api/v1/session` on open;
401 shows a small sign-in form with a password field for the console token.
Send it only in JSON to `POST /api/v1/session`, then erase it. No token in
browser storage, URLs, logs or bundled config. Send HttpOnly cookies through
same-origin fetch. DELETE signs out and clears private UI state. During an
expired-session sign-in preserve this page's unsent drafts; never persist chat
bodies/drafts across reloads. Only presentation preferences such as selected
agent/inspector visibility may use local storage, as revalidated hints.

Load `/api/v1/schema`, require `api_version=1`, and discover supported
operations. After sign-in read config and the paginated agent directory with
its recovery epoch. Use actual core fields (`id`, `display_name`, `icon_key`,
nested `status`, `cli`, etc.), not guessed examples. Unsupported versions show
a clear compatibility error; absent operations have no live action controls.

Handle both application envelopes and transport refusals. Show concise errors
without losing drafts. Generate one `crypto.randomUUID()` idempotency key per
user mutation. Preserve exact arguments/key after an ambiguous response;
explicit Retry uses that same request. Never automatically issue a new
message/key after timeout. Distinguish stored/queued, submitted, uncertain and
handled; request activity is not a native typing indicator.

Use bounded `events_list` polling: one active request, about two seconds while
visible, capped backoff on failure. No invented SSE/WS routes. Invalidate and
reread affected records. A changed recovery epoch clears server rows/cursors
and rereads while keeping drafts. Fetch conversation updates with its cursor
contract, deduplicate by entry ID, and reject late responses from another
agent selection/session. Refresh current delivery/handling views even without
new entry insertion. Abort obsolete reads; canceled mutations remain possibly
committed. Request errors never silently claim a send was safe to repeat.

## One page

Use UI-PLAN's near-black flat surfaces, restrained white/gray text and thin
dividers. No purple, neon, gradients or decorative dashboard widgets. Begin
with 240px navigation, fluid chat, 320–360px inspector. On small screens use
accessible dismissible drawers, preserving usable chat/composer width without
horizontal page scrolling.

Navigation shows icons/names and actual status/activity/attention. Loading,
empty, offline, unknown and failed states have readable labels. Selecting an
agent opens its permanent conversation and preserves its in-memory draft/scroll.

Creation dialog: display name and twenty-icon radio picker only. Send no
provider/model/start selections; the user configures and starts the CLI in
Terminal. Accepted creation shows provisioning, never running. Follow
[MANUAL-CREATION.md](MANUAL-CREATION.md) for the D36 correction.

Double-click a name to rename; provide a keyboard/touch Rename action. Enter
submits revision-checked `agents_update`; Escape and blur cancel. A conflict
reloads the current name and preserves attempted text in the error/retry flow.
Icon changes use the same revision contract. Duplicate names remain valid;
IDs appear in inspector details/disambiguation, not primary labels.

Chat renders escaped plain text with preserved whitespace, sender and timestamps
(show both when occurrence and recording differ). No unsafe HTML or transcript
regex that triggers UI effects. This domain emits message entries. Unknown
future entry kinds get an unsupported-content notice, not a made-up card. Chat
is never labeled a complete raw archive.

Load latest history initially; Load earlier uses `before_seq` and retains the
scroll anchor. Append updates without pulling a reader away from older entries;
offer a new-message affordance. Composer supports Shift+Enter newline, Enter
send, accessible Send button and IME-safe input. Respect the 65,536-byte UTF-8
limit without truncation. An ambiguous send retains its original request for
Retry. A confirmed saved send clears only the submitted draft, preserving text
typed afterward. Agent switches cannot redirect a late mutation result.

Inspector collapses without navigation. Show actual work/status, freshness,
observed run availability and `conversation_list.publication` lag/error. Browser, Machine and
Terminal sections may show a plain unavailable explanation while their APIs
are absent. No clickable fake controls or hardcoded stream addresses. Native replies, terminal view and browser/file cards remain required later work.
Provider changes are user actions in Terminal.

## Direct browser checks

Add a compact Playwright suite using installed Chrome with a fresh temporary
profile. Pin the test package; no system browser install or owner profile.
A local test-only synthetic API may implement the exact frozen schemas; it
must never be bundled as a fallback. Use temporary ports/data and clean only
owned child processes. Keep screenshots/browser artifacts outside Git and
commit a concise evidence report with private paths and exact checks.

Exercise sign-in/logout and incompatible API; creation/icons/rename keyboard
and conflicts; selection/drafts/late responses; send/ambiguous same-key retry;
paging/recovery/equal text; desktop/mobile drawers, focus and escaping. Group
related cases instead of testing every component or icon. Run build/type checks
and inspect desktop/mobile screenshots.

Use direct testing, as the owner allowed own-browser or ACLA-agent UI tests.
No external QA service or owner login is needed. Under the latest owner instruction, Manager reviews and tests the completed
SHA directly, then checks the integrated UI against the actual backend. Fixtures do not count as VM/provider/noVNC acceptance.

Commit implementation, lockfile, focused tests and evidence on the assigned
branch. No usage examples, fake demos, global installs, deployment or live
sessions. Ask Manager for missing contracts/design before dependent work.

## Native history compatibility

Manager decision, 2026-10-08: adopt the native Entry variant accepted in
qorqut-core `35b6d82cd702fe6e82562d7a3c6533ac04050baa` (schema 7), without
changing Terminal geometry or adding backend logic. Conversation source is a
discriminated message/native union. Native source carries provider, run/source/
epoch IDs, raw offset/length/SHA, parser version, native ID, part index and
timestamp origin; it has no message_id. Native user sender_actor_id, delivery
and handling are nullable. Existing message entries keep their current shape.

Render both variants as the same escaped conversation text, attributed to Owner
for native user and the agent display name for native assistant. Keep occurred
and recorded timestamps and permanent entry-ID/cursor behavior. Do not infer
queue/delivery/handling badges or cards from native rows, merge equal text, or
expose raw protocol coordinates in the chat. Retain refresh and draft behavior.
The composer note should describe the manual Terminal workflow and saved
delivery, without the blanket assertion that native replies are unavailable
or a claim that dispatch/capture is ready.

Update only conversation wire declarations, that note and the existing
synthetic conversation fixture/browser check. Extend one existing nonterminal
group with native user/assistant, null delivery, escaped text and equal-text
distinct identities across refresh. No new framework, suite, dependency,
example or styling work. This lane runs independently of the Terminal geometry
lane, which owns the Terminal declarations and its two browser groups in the
same isolated-worktree merge workflow. Direct UI checks remain synthetic;
normal server collection and actual CLI acceptance are separate gates.
