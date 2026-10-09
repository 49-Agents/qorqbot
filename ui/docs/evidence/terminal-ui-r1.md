# Recorded Terminal UI r1 evidence

2026-10-08. Branch `codex/build-terminal-ui-r1`, exact base
`b3d3a32169dbb8025ec406f4bc2e8fd7681f372b`. D38 core contract is pinned at
`c3d5b633aedcc1fdd056a02136259eb26f56adb1`. The Manager's D38 amendment 198,
delivered in ACLA message 199, is authoritative for human input; its plan commit
is `a353ed23761fd92ab3264aa6af501157caffe48c`. The original microtask guard was
reproduced rejecting real Chrome keys and was replaced only after that decision.
The completion report records the immutable delivery SHA and push status.

## Implemented boundary

- Per-agent, memory-only Chat/Terminal selection in the existing header. Drafts,
  history cursors and scrolling remain in the existing store. Inspector keeps
  Browser, Machine/Desktop, native run and transcript publication separate.
- Capability-discovered `terminal_get` observation polling, including while
  Chat is selected, with selection/session/recovery cancellation. Older servers
  and disabled, waiting, unsupported, stopped or unavailable targets remain
  truthful. Recording displays separate local and host offsets.
- Same-origin cookie WebSocket, strict protocol-1 messages and terminal/recovery
  binding, byte-preserving output, explicit take/release, monotonic control and
  sequence observations, and read-only reconnect without replay. Disposing a
  view closes only its viewer and never sends release/start/stop.
- Public xterm `onKey`, standalone DOM `insertText`, binding-bound composition
  and synchronous public `paste` collection implement amendment 198. Ordinary
  `onData` and all `onBinary` are excluded from pane input. Clipboard contents
  are read only from the user's paste event. No escape filtering or admission
  timing window is used.
- One pending input/resize receipt, 64 KiB queued UTF-8 input, whole-paste raw
  and framed limits, coalesced resize, 2,000 scrollback lines and a 1 MiB pending
  renderer limit. Uncertain/rejected frames and connection loss clear unsent
  input, warn that earlier input may have arrived, and require explicit action.

## Checks and provenance

All runtime checks used this worktree, synthetic HTTP data on ephemeral ports,
Playwright WebSocket routing and fresh installed-Chrome contexts. One browser
worker ran throughout. Commands:

```sh
npm ci --ignore-scripts
npm run build
QORQUT_UI_TEST_ARTIFACTS=<private-run>/terminal-ui-r1-validation/final-browser npm test -- --workers=1
git diff --check
```

Locked installation and strict TypeScript/Vite build passed. **Eight grouped
browser checks passed in 48.4 seconds**: the six existing creation, mutation,
session, Chat/history and responsive checks, plus these two owning flows:

1. Availability, capability fallback, stale selected-agent reads, view switching,
   retained drafts/scrolling, no observer input/control/resize, per-agent choice,
   desktop inspector resizing and 390×844 mobile navigation/inspector.
2. Explicit control receipt, lower/uppercase keys, Ctrl-C/arrows/Enter, Unicode
   insertion, CDP composition followed by a key/cancel, normal/bracketed multiline
   paste and raw/framed rejection, split UTF-8 output, device-query replies
   excluded from input, queue/output limits, resize coalescing, takeover, stale
   state/receipts, old composition rejection, durable hold across views,
   stale-take refresh, uncertain/rejected frames, loss/reconnect without replay,
   changed terminal binding, malformed/extra fields and session expiry.

Paste used Chrome's clipboard with synthetic text and real Ctrl+Shift+V; Ctrl+V
is a literal terminal control key. Composition used Chrome's input protocol,
not a physical operating-system IME. One late composition-end event was
DOM-dispatched as an explicitly synthetic old-binding check. Output pressure
paused parser timers while routed WebSocket output accumulated.

Environment: Node 22.22.0, npm 10.9.4, Playwright 1.64.0, Google Chrome
153.0.8010.36, TypeScript 5.9.3 and Vite 8.3.3. Exactly `@xterm/xterm` 6.0.0 and
`@xterm/addon-fit` 0.11.0 were added; their lock integrities match the Manager's
official-registry metadata. Other locked dependencies are unchanged. Package
resolution points into this worktree's `node_modules`, and Vite bundles the
libraries and xterm CSS locally. Their unmodified MIT notices ship in
`public/terminal-notices.txt`. Application code is new; no Qorqut runtime code,
live installation data or unfinished peer changes were imported. Only the pinned
core contract and runner model/dimension definitions were inspected.

Private evidence is under the run's `terminal-ui-r1-validation/`: build/install
logs, package resolution/integrity and bundle SHA-256 metadata, browser results,
screenshots and the initial DOM admission reproducer. Final desktop/mobile
Terminal, mobile inspector, owned-control and uncertain-input screenshots were
inspected directly. Layout fits the viewport; controls and warnings are readable.
The view flow passed again (4.7 seconds) after scrolling the mobile inspector to
capture its recording fields. Notices were copied exactly by the build; bundle
hashes remained identical to those exercised by the eight-check run. Profiles,
logs and synthetic terminal contents are outside Git.

## Limits

This proves the compiled UI boundary against synthetic peers. It does not prove
an installed core accepts the frames, a real shell runs, recording is durable,
native CLI/inbox/Chat/history is connected, or a VM/browser surface exists.
Physical IMEs and browsers other than the named Chrome were not tested. Unicode
bytes and text content are verified; glyph coverage depends on installed fonts
(the fixture's smiley renders as a missing glyph with the host's monospace font). The
Manager's fresh independent source review and actual compiled-UI/installed-core
acceptance remain required. No main/other-branch merge, PR or deployment occurred.
