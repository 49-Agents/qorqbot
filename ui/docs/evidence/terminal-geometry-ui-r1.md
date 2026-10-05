# Terminal geometry UI r1 evidence

2026-10-08. Branch `codex/fix-terminal-geometry-ui-r1`, exact base
`28f922bda2699deb4609ae1f44bdc469b3f1b67a`. Implements the UI lane of Manager's
finding-217 geometry contract, pinned in core's plan commit
`6149b1bd1e82f637e62d9e7e1f5d51ea29b8a8a2`. No moving core implementation was
consumed. The completion report supplies the immutable delivery commit and push
result. Manager decision 235 supplies the public timer-gate fixture and retains
direct Manager review/testing; no additional reviewer actor was launched.

## Implemented boundary

- Strict protocol-2 hello, nullable public dimensions and ordered size messages.
  Available observations require integer dimensions within 20–500 columns and
  5–250 rows. Unsupported hello and malformed geometry close the viewer with an
  unavailable/reconnect warning before input admission.
- The existing terminal region is a bounded, keyboard-focusable scroll viewport.
  Its native grid retains the 13px monospace font and 2,000-line scrollback.
  Horizontal and vertical overflow stays inside that region; header, control
  buttons and note remain visible. HTTP/state observations never resize it.
- A serial display FIFO applies hello/size and binary frames in arrival order.
  One public `terminal.write` advances only from its callback; public `resize`
  runs between byte entries. Adjacent sizes coalesce. Both the 1 MiB pending-byte
  limit and 1,024-entry limit include the in-flight write. Overflow closes only
  the viewer; stale callbacks cannot resume its queue after closure/disposal.
- Acknowledged control requests the viewport proposal through public
  `FitAddon.proposeDimensions`, with clamping, deduplication and existing ordered
  input/resize sequencing. Only actual container size changes while owned make
  further proposals. Incoming geometry, scrolling and read-only viewing send no
  resize. Release retains the authoritative grid for scrolling.

The human-input source block is byte-identical to the base: public `onKey`,
standalone DOM text input, binding-bound composition and the synchronous public
paste collector. Parser/device replies remain excluded. Existing control epochs,
uncertainty, no replay and one pending receipt behavior remain in the same client.
There are no private xterm APIs or production test hooks.

## Checks

The existing eight-group Chrome suite remains the only browser suite. Its first
six groups are byte-identical to the base. All browser work used the compiled UI,
synthetic loopback HTTP/WebSocket peers, synthetic task/clipboard contents and
fresh disposable profiles. Commands were run from the assigned worktree:

```sh
npm ci --ignore-scripts --cache <private-root>/npm-cache
npm run build
QORQUT_UI_TEST_ARTIFACTS=<private-root>/browser-verified TMPDIR=<private-tmp> npm test -- --workers=1
npm run typecheck
QORQUT_UI_TEST_ARTIFACTS=<private-root>/browser-scheduler-gate TMPDIR=<private-tmp> npm test -- --workers=1 --grep 'synthetic Terminal protocol'
QORQUT_UI_TEST_ARTIFACTS=<private-root>/browser-scheduler-repeat TMPDIR=<private-tmp> npm test -- --workers=1 --grep 'synthetic Terminal protocol' --repeat-each=3
git diff --check
```

Locked installation and TypeScript/Vite build passed. The full run recorded seven
passes: the six unchanged groups and the native-grid terminal group (4.8s).
Its protocol group exposed an unreliable fake-clock pressure fixture, rather
than establishing the required stalled-renderer precondition. Earlier pressure
attempts had both passes and failures; they are retained as failed verification,
not accepted as proof of the bound. Question 232 escalated that blocker.

Following Manager decision 235, the existing group uses a test-only gate around
public `setTimeout`/`clearTimeout`, installed before application startup. It
holds zero-delay callbacks, including callbacks already scheduled when closed,
preserves callback arguments and cancellation, and drains retained callbacks in
scheduling order. WebSocket delivery, microtasks, fetch and Node-side polling
continue. No `page.clock` remains. Fresh read-only viewers first render a ready
sentinel; retained canaries establish the stall. Eighteen valid 65,536-byte frames
prove byte overflow; 1,024 alternating byte/size entries remain connected and
the next entry proves the separate entry bound with fewer than 1 KiB of bytes.
Each case observes its exact peer's closure and no outbound frames, then drains
callbacks and checks that closure persists without replacement or input.

The corrected protocol group passed once in **12.8s total**, then **3/3 in 30.4s**
in the bounded repeat. Final typecheck and diff whitespace checks passed.
Production source and compiled hashes match the successful build, so the build
and unrelated successful groups were not rerun after this test-only correction,
as directed in decision 235.

The two extended terminal groups verify:

- A 132×60 read-only grid at desktop and 390×844 mobile sizes. Actual keyboard
  scrolling reveals `RIGHT-132-EDGE`, `BOTTOM-60-END` and
  `LOWER-RIGHT-132-60`; glyph-range visibility is measured against the viewport.
  Font/grid dimensions stay fixed, the page does not overflow, controls remain
  visible, and the peer receives no input/control/resize or control-state change.
- Delayed binary/size/binary ordering, more than 1,024 adjacent sizes coalescing,
  late HTTP/state dimensions ignored, controlled fitting and clamping at both
  bounds, resize deduplication/coalescing, takeover/release and geometry without
  feedback, invalid/unsupported geometry, and disposed callback isolation across
  replacement viewers.
- Retained real keyboard lower/uppercase/control/arrow/Enter input, Unicode
  insertion, CDP composition and cancellation/following key, real Chrome clipboard
  paste with synthetic text, normal/bracketed multiline paste and whole-paste
  limits, split UTF-8, device-query separation, input queue limits, stale control,
  uncertainty, rejection, session loss and read-only reconnect without replay.
  The late old-binding composition-end fixture is explicitly DOM-dispatched.

## Provenance and screenshots

Private root:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/fix-terminal-geometry-ui-r1-private`.
It retains install/build/typecheck/browser logs, screenshots, `provenance.json`,
`source-boundaries.json` and an exact `compiled-ui/` copy. Chrome's socket path
requires a short disposable TMPDIR; the first launch with the long private-root
path failed before application checks. The replacement `/tmp/qgui-QszXHi` was
used only for owned browser temporary files and removed after checks.

Installed environment: Node 22.22.0, npm 10.9.4, Google Chrome 153.0.8010.36,
Playwright 1.64.0, TypeScript 5.9.3, Vite 8.3.3, Preact 11.0.0, xterm 6.0.0 and
FitAddon 0.11.0. Package resolution points into this worktree's `node_modules`;
the private manifest records locked integrities and installed entry hashes.

| Compiled file | Bytes | SHA-256 |
| --- | ---: | --- |
| `assets/index-CKHw0WID.js` | 399059 | `3b3bb36af819dbff176d39484a624431740c10cb81cc3658ea85f8a553e9abd1` |
| `assets/index-DtysQLHi.css` | 16141 | `e12a89548884941512fa4bd452d45711c8e85faac2556b4648dd71d3ca0b7392` |
| `assets/terminal-notices.txt` | 2396 | `adab7e720e02259bc98b5d8542d6e99d2e6438239a089579cd6cffd9a9e944e5` |
| `index.html` | 454 | `fd1681210cdb649db12152189ba559f33cb32babd5238d01f97a9e3284109b00` |

Desktop, mobile initial/right/bottom/bottom-right and inspector screenshots in
`browser-verified/shell-synthetic-Terminal-v-d00a8-ive-grids-on-desktop-mobile/`
were inspected directly. Controlled desktop/mobile and uncertainty screenshots
in the protocol-group directories were also inspected, including final
`browser-scheduler-gate/shell-synthetic-Terminal-p-f1ef2-d-reconnects-without-replay/`
desktop/mobile captures. Text remains readable and the scrolled edge markers are
visible within the bounded viewport. The host font still lacks the fixture's
smiley glyph; Unicode byte and DOM-text assertions pass.

`index.html`, dependency inputs/lock, `src/store.ts`, Playwright configuration and
`public/assets/terminal-notices.txt` are byte-identical to the base. Notice bytes
and accepted asset location remain intact. Only the assigned UI source, existing
synthetic tests and this evidence document are part of the delivery.

## Limits

This is compiled-UI acceptance against explicitly synthetic peers. No installed
core, real tmux pane/mirror, recorder, native CLI/provider, VM or browser-MCP was
used. Physical operating-system IMEs and other browsers were not tested. The
combined real Chrome/compiled-UI/installed-core flow, including real-host notice
serving, still belongs to the integration actor after Manager supplies accepted
pins. There was no merge, PR, deployment, extra reviewer actor or self-approval.

## Manager acceptance, 2026-10-08

Accepted candidate `6fd80c494b1e4226aca486c0ef9c4d156ee1587c` after direct
review of the complete production change and owning browser assertions.
Manager personally ran both existing Terminal groups against the exact compiled
UI: 2 passed (18.6 seconds reported by Playwright). Verified compiled JS/CSS and
notice hashes against this report, and inspected the resulting desktop
controlled-terminal and mobile bottom-right screenshots. The mobile marker is
visible inside the bounded viewport, with the toolbar and note still present.
Private checks/screenshots are under `manager-review238/` in the build state
directory. No additional reviewer actor or repeated build was needed.

Source/tests were merged unchanged. This accepts the UI geometry boundary
against synthetic peers. The matching real-core/tmux/recorder combined flow
is now ready for the existing integration assignment, not already accepted.
No deployment or native CLI/VM/browser-MCP acceptance is implied.
