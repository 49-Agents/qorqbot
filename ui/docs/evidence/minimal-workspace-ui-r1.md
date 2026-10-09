# Minimal workspace UI r1 evidence

2026-10-09. Branch `codex/build-minimal-workspace-ui-r1`, base
`319bedf2c00eb4a49de61baccc2c23d178857acf`. Implements Manager decision D56 in
`UI-SIMPLIFICATION.md` and the reference amendment in
`UI-REFERENCE-DIRECTION.md`. Manager documentation commit
`7c80e9743d4e885aa78d53dd1c463909b37b3d92` was cherry-picked as `82f0b70`.
Owner correction in review message 370 explicitly defers mobile/touch acceptance;
the accepted viewport scope is 937×779 and a wide desktop.

## Delivered behavior

- Removed workspace branding/conversation headers, row rename menu, manual
  Terminal control toolbar and complete Terminal footer. Create is at the
  navigation bottom; persistent floating view icons retain accessible names,
  tooltips and active state.
- Neutral left/right chat bubbles preserve literal text, provenance, inbox
  disclosures, file references and delivery errors. Recording timestamps use a
  disclosure. A one-row rounded composer grows to a bounded height and retains
  existing input, byte-limit and pending-request behavior.
- Inspector name/icon are direct controls. Rename trims and saves once on
  blur/Enter, ignores composing Enter, cancels on Escape and preserves existing
  revision/idempotency/conflict behavior. Details/Library/Computer tabs retain
  mutation feedback; Library deduplicates validated references from loaded chat
  pages and loads earlier pages only on request. Computer shows distinct,
  unavailable noninteractive Browser and Machine/Desktop surfaces.
- One take follows each validated socket hello; matching acknowledgement gates
  native input and resize. Takeovers/conflicts require deliberate reconnect;
  uncertain/queued input is never replayed. Disposal closes the socket. The
  synthetic peer models D56 owned-disconnect release without clearing another
  viewer's hold. Real core cleanup remains core-owned.
- Conversation/composer wrap long content without horizontal scrolling. Actual
  xterm 6 custom scrollbar DOM reports 1px rail and thumb, transparent track,
  gray `#414141` thumb, no arrows and no horizontal rail. Existing fit/resize,
  wheel and keyboard scrolling remain in use. No touch handler was added.

The store, public operations, protocol version, agent icon catalog, dependencies
and lockfile are unchanged. Chat/Terminal vectors are the supplied Qorqut v1
`globalChatViewToggle` paths from dashboard
`cfbbd53c77758d50b521a32d1d4ce19b06ae5a54/code/js/global-chat.js`; provenance is
retained in `src/view-icons.tsx`. That pinned source has no additional vector
license notice. Existing xterm notices are retained.

## Checks and private evidence

Checks use fresh installed-Chrome contexts, synthetic HTTP/WebSocket peers and
owned ephemeral ports. No real inference, owner terminal, native store, profile
or running Qorqut data was used. Environment: Chrome 153.0.8010.36, Node 22.22.0,
npm 10.9.4, locked Playwright 1.64.0, xterm 6.0.0 and FitAddon 0.11.0.

`npm ci --ignore-scripts --no-audit --no-fund`, `npm run build` (TypeScript/Vite)
and `git diff --check` passed. All browser checks remain in the existing
`tests/shell.spec.ts`; there is no duplicate suite. The full desktop run passed
11 of 13 grouped checks. Its two failures identified collapsed-details/tab focus
handling and a reload race in the inspector test helper. Chrome retains layout
rectangles for hidden closed-details descendants, so drawer focus now selects
visible tabbable controls. The helper waits for the loaded toggle state and the
responsive render after viewport changes.

Final results across the existing 13 grouped checks are passing. The focused
correction run passed creation/rename, layout/focus and file-reference/Library
checks; it exposed a second helper race at the drawer-to-wide transition. After
that test-only correction, runtime observations/recovery and Terminal
views/drawers both passed (2 checks, 13.9 seconds). No production edits followed
the successful final build. The prior full run's Terminal protocol check passed
held control acknowledgement, actual keyboard/Unicode/IME/paste input,
backpressure, takeover/conflict, binding changes and reconnect without replay.

Final build log: `build-desktop-final.log`. Browser logs:
`chrome-desktop-reference-r1.log` (11 pass, 2 corrected failures),
`chrome-desktop-corrections.log` (3 pass, 1 corrected helper failure), and
`chrome-desktop-runtime-final.log` (2 pass). These are focused corrections, not
an additional acceptance suite or a claim that the initial run passed intact.

Initial diagnostics are retained, including an over-strict terminal grid-rounding
assertion (xterm canvas rounding leaves up to one cell plus 1 CSS pixel) and the
touch check superseded by the owner's explicit desktop-only direction. Native
input assertions use real DOM keys, clipboard and Chrome composition input;
input-before-ack uses a held control reply, without injecting input into the peer.

Private logs, measurements and screenshots are under
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-minimal-workspace-ui-r1/`.
Desktop Chat, Details, Library, Computer and Terminal screenshots were inspected.
Original failures and screenshots remain private; only this concise evidence is
committed. The completion report records the final immutable SHA and push status.

## Limits

These checks establish the compiled UI boundary against synthetic peers. They do
not prove real core disconnect cleanup, a native CLI, recording durability or
inbox ownership. Manager owns the combined disposable-core acceptance and any
publication. Mobile/touch, physical operating-system IMEs and other browsers are
outside this pass. Existing responsive styles remain. No merge or deployment
was performed by this actor.
