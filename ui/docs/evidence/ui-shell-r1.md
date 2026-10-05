# UI shell r1 evidence

2026-10-08. Branch: `codex/ui-shell-r1`. UI base:
`2c54899c7e0ea24112aab1e76cb3da9e787fcea9`. Core contracts and existing wire
models were read with `git show` at
`055cbd48071d28c03cde3daf1a08521875c130a3` in `/home/alp/qorqut-core`.
No moving builder checkout was used. This evidence is committed together with
the implementation; the completion envelope records the full delivery commit,
post-commit clean status and confirmed push result.

Revision 2 responds to `MANAGER_REVIEW` message 126 against candidate
`62dd56b373317ac833e5c134e1f02e96ba9b96ba`. The three confirmed defects are
corrected within the existing shell:

- Both recovery entry paths now share a generation-guarded discovery failure
  handler. Failure enters `error` / `Offline` with Retry connection and stops
  normal polling. Same-page retry preserves drafts and original mutation keys.
- Event cursor advancement waits for the required config and directory reads
  to succeed in the same generation with an unaborted request. A failed read
  retains its event for the bounded poll/backoff to retry without another event.
- Unselected agent rows show their own pending edit, conflict, attempted text
  and reapply actions. Selected-agent feedback stays in the chat. Wrapped row
  actions remain usable in the mobile drawer without changing the active chat.

Implemented the Manager's `SHELL-IMPLEMENTATION.md` scope: same-origin session
and discovered API client; paginated agent directory; twenty original local
SVG icons; creation and revision-checked presentation edits; responsive three
column chat with mobile drawers; escaped message history, cursor paging,
in-memory drafts/scroll, bounded event polling and recovery fencing; exact-key
uncertain-request retries; delivery/handling and publication observations.
Only revalidated selection and inspector presentation hints use local storage.
The test-only API is outside the application import graph. Core/browser and
global plans were not changed.

## Checks

Environment: Node `v22.22.0`, npm `10.9.4`, `/usr/bin/google-chrome`
`153.0.8010.36`. Direct dependency versions: Preact `11.0.0`, Vite `8.3.3`,
TypeScript `5.9.3`, Playwright test `1.64.0`, Node types `22.19.0`.
One npm lockfile; SHA-256:
`1bb41c1a285e07e63ed856ef05f12ecc3351cae41d88f04e66863d52ce22b0cd`.

Commands run in the assigned worktree:

```sh
npm install --ignore-scripts --no-audit --no-fund
npm ci --ignore-scripts --no-audit --no-fund
npm ls --depth=0
npm run build
QORQUT_UI_TEST_ARTIFACTS=/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/ui-shell-r1/revision-2-browser TMPDIR=/tmp npm test
git diff --check
```

- Clean lock installation and dependency inventory passed. `npm run build`
  includes strict `tsc --noEmit` over source/tests and a production Vite build.
  Final revision build passed: `dist/index.html`, one CSS asset (10.59 kB), one JS asset
  (48.09 kB; 16.48 kB gzip). No synthetic token/model/API fixture strings were
  found in the production bundle.
- Final strict build and grouped Chrome suite each ran once after the revision:
  **6 passed in 39.0 seconds**. Regressions are folded into the existing session,
  creation and history cases; no component test matrix was added.
- Review regressions: a one-time schema refusal after epoch change keeps Retry
  visible through the old backoff interval and issues no further operations
  until manual reconnect; the original draft and exact uncertain mutation
  arguments/key survive. A one-time event-driven directory refusal retries
  from the uncommitted cursor and refreshes the authoritative name without a
  new event. An unselected rename conflict immediately shows its attempted text
  and reapply action on desktop and mobile; keyboard reapply uses a fresh key
  and current revision while preserving the selected chat and draft.
- Sessions: sign-in, cookie attributes, expiry with retained drafts, explicit
  logout clearing private state, reload without drafts, version refusal,
  missing controls for absent capabilities, failed recovery and manual reconnect.
- Agent operations: paginated directory, authoritative creation validation,
  default CLI/start choice, twenty-icon picker, duplicate names, provisioning
  labels, keyboard/double-click rename, Escape/blur cancellation, preserved
  attempted name after revision conflict, new revision/key on explicit reapply,
  and revision-checked icon changes.
- Interaction races: obsolete conversation reads, target-bound late sends,
  text typed during a send, late results after logout/new sign-in, ambiguous
  saved send without automatic replay, byte-for-byte same-key explicit retry,
  transport refusal, IME Enter, Shift+Enter, exact 65,536-byte UTF-8 send and
  rejection of 65,538 bytes without truncation.
- History: latest page, anchored earlier page, separate equal-text identities,
  preserved scroll on selection, new-message affordance without scroll theft,
  submitted/handled views refreshed without insertion, recovery clearing old
  rows/cursors while preserving drafts, escaped HTML and unknown-kind notice.
- Presentation: desktop inspector collapse, mobile drawers, keyboard focus
  containment/restoration, no horizontal document overflow, occurrence and
  recording times, publication lag/error. Browser page exceptions were absent.

The first Chrome attempt used an overlong private temporary path and failed
before loading the app; using `/tmp` for Playwright's fresh disposable Chrome
profile avoided the Unix socket path limit. Initial browser checks exposed
drawer Tab escape (fixed), a fixture disconnect before headers that Chrome
could transparently replay (fixture now disconnects after partial response
bytes), and a fixture UTF-8 chunk-decoding bug (fixed with byte accumulation
before decoding). The affected checks were rerun successfully. No system
browser installation, owner profile or live installation was used.

## Private artifacts and inspection

Artifact root:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/ui-shell-r1/`.
Final revision logs are `revision-2-build.log` and `revision-2-browser.log`.
Initial candidate logs (`build-final.log`, `browser-verified.log` and
`browser-recovery-check.log`) and earlier failures remain in the same private
root, outside Git. The original candidate had six passing grouped tests plus a
passing narrow recovery test; independent review identified the additional
cases now covered by the revision regressions.

Visually inspected `desktop.png` (1440×960), `mobile.png` (390×844), and
`mobile-inspector.png` under
`browser-verified/shell-escaped-content-and--f52f2-awers-focus-and-screenshots/`
(the inspector view was also inspected in `browser-r3`). Flat dark surfaces,
legible messages, usable composer, thin dividers and contained drawers matched
the supplied direction. Replaced the mobile menu font glyph with a local SVG
after screenshot inspection. Screenshots contain synthetic records only.

Revision inspection: `desktop-row-conflict.png` (1440×960) and
`mobile-row-conflict.png` (390×844), in
`revision-2-browser/shell-creation-and-fixed-i-adfdb-ation-and-revision-conflict/`,
show the unselected row's complete attempted text and wrapped reapply/dismiss
controls, without horizontal overflow or a conversation switch. Existing
desktop/mobile shell and inspector screenshots were also regenerated by the
same final suite under `revision-2-browser/`.

Final build SHA-256 values:

| Artifact | SHA-256 |
| --- | --- |
| `dist/index.html` | `d01e622e9a8919f968b3e8608b54f0a1cc65d350f3f76ab561c54951b34c165c` |
| `dist/assets/index-1eXot0bS.js` | `f0b4a52949c2894399259442f14b37b9776ca8d5c90a4614dc7237c37a02ce57` |
| `dist/assets/index-UG1v5Rja.css` | `05ce6fec2a63ed77f26e7cd0404b8993277409385f4ca37a4c24abe4317ce773` |

## Coverage boundary

Source type checking and the locally installed locked npm dependencies were
verified. Chrome exercised actual compiled `dist` assets over ephemeral local
HTTP ports against a synthetic API implementing the frozen UI-used operations.
The fixture's schema response supplies the discovery fields the client reads;
it is not a second schema validator or a claim of backend acceptance.
Each test uses a fresh browser context; Playwright owns and closes its fresh
temporary browser profile/process. The fixture closes only its own server and
connections. No production data, native CLI stores or owner sessions were used.

Real core session/chat/publisher integration awaits Manager's integration run.
No core source/wheel, VM, provider turn, native ingestion, terminal delivery,
browser/machine stream, noVNC or browser/file-card acceptance is claimed.
Provider switching and those runtime/card domains remain the explicitly
separate required work. No merge, deployment or global service change occurred.
`reviewLoop=false`: no internal reviewer subagents were launched; independent
review and final approval belong to Manager.
