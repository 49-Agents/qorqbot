# Compiled UI / installed Terminal integration r1

2026-10-08. The complete corrected-pair workflow passed in one fresh disposable
fixture, including the previously failing mobile display case. This is execution
evidence for Manager review, not deployment or native-provider acceptance.
Only this report is changed on `codex/verify-terminal-integration-r1`.

Manager handoff 241 pinned core `4cacde062bc36fad2ae9b560bf509a497d729217`
and UI `3f5684ffd1f0c78d3a99690ea7a11c6ff07381fb`, incorporating accepted
geometry sources `f99ac1eea42b6e3c7e270af5756e8ad5af3f0d46` and
`6fd80c494b1e4226aca486c0ef9c4d156ee1587c`. The assigned clean branch was
fast-forwarded to that exact core pin. The workflow followed
[Terminal integration](../TERMINAL-INTEGRATION.md),
[geometry](../TERMINAL-GEOMETRY.md), clarifications 212/215/217 and the
documentation-only acceptance update `7e2f56173a25070d5ab153ed42c46b5db67edc02`.
The current installation has **schema 7, 16 operations and five handlers**, as
specified by the latest handoff. No existing source, test or lock was modified;
no native-history UI work was consumed.

Private executable checks and complete evidence are retained in the assigned
`terminal-integration-r1-private` artifact directory. The corrected build and run
are under `corrected/` and `corrected/attempt-1/`; original exports and attempts
remain separately intact. Tokens, profiles, runtime databases, raw captures,
process fingerprints and actual disposable fixture paths are excluded from Git.

Exact Git archives supplied both exports. The unchanged Python lock installed
32 pinned distributions into a fresh private Python 3.12.3 venv; the unchanged
npm lock supplied the compiled UI. Build/check commands, with private paths
abbreviated, were:

```text
git archive --format=tar 4cacde062bc36fad2ae9b560bf509a497d729217
git archive --format=tar 3f5684ffd1f0c78d3a99690ea7a11c6ff07381fb
/usr/bin/python3 -m venv <private>/venv
<private>/venv/bin/python -m pip install -r <private>/core-source/requirements.lock
<private>/venv/bin/python -m pip wheel --no-deps --no-build-isolation --wheel-dir <private>/wheels <private>/core-source
<private>/venv/bin/python -m pip install --no-deps <private>/wheels/qorqut_core-0.1.0-py3-none-any.whl
npm ci --no-audit --no-fund
npm run build
<private>/venv/bin/python <private>/run.py
/usr/bin/python3 <private>/verify-imports.py attempt-1
/usr/bin/python3 <private>/verify-flow.py attempt-1
/usr/bin/python3 <private>/verify-device-query-v2.py attempt-1
<private>/venv/bin/python <private>/provenance.py
```

The npm commands ran inside the exact UI export. Node 22.22.0, npm 10.9.4,
locked Playwright 1.64.0 and installed Google Chrome 153.0.8010.36 were used;
xterm 6.0.0 / FitAddon 0.11.0 remained unchanged. The supervisor launched
`node <private>/browser.mjs <fixture>/browser-config.json`; that script imports
the export's Playwright package and launches `/usr/bin/google-chrome` headlessly
with fresh isolated contexts, private HOME/TMPDIR and disposable profiles.

Both console starts used the normal generated entrypoint:

```text
<private>/venv/bin/qorqut-core serve --installation <fixture>/data/installation.json --transport-config <fixture>/transport.json --ui-root <private>/ui-source/dist
```

Its clean environment supplied fixture HOME, `/usr/bin:/bin`, `C.UTF-8`,
`PYTHONNOUSERSITE=1` and import logging, with no source-import override.
The real application worker and observation loop provisioned the workspace and
recorded Bash; there was no manual job pumping or fabricated readiness. Local
Terminal settings explicitly selected private runtime/socket storage, installed
Python, `/usr/bin/tmux` 3.4 and `/usr/bin/bash` 5.2.21. The initial registered
workspace marker, managed HOME, generation 1 and exact recorder/Bash bindings
were verified. Initialization alone created no workspace or shell.

All 221 core and 55 UI exported files still matched their archive members after
execution. All 72 packaged files matched source, wheel and installation. Both
normal console logs showed 65 core modules imported exclusively from that
installation, with completed startup and shutdown. Private `exports.json`,
`provenance.json`, build logs and `installed-import-provenance.json` retain the
full comparison and process argv. Wheel SHA-256:
`cb06cd09378fcb61c7f259fc2341502a3d5c9488b84424f34517c9c2e3b32f5c`.
Every final distribution file was fetched from the normal host and matched on
both sides of the restart:

| Served file | SHA-256 |
| --- | --- |
| `/` (`index.html`) | `fd1681210cdb649db12152189ba559f33cb32babd5238d01f97a9e3284109b00` |
| `/assets/index-CKHw0WID.js` | `3b3bb36af819dbff176d39484a624431740c10cb81cc3658ea85f8a553e9abd1` |
| `/assets/index-DtysQLHi.css` | `e12a89548884941512fa4bd452d45711c8e85faac2556b4648dd71d3ca0b7392` |
| `/assets/terminal-notices.txt` | `adab7e720e02259bc98b5d8542d6e99d2e6438239a089579cd6cffd9a9e944e5` |

The connected workflow established these outcomes:

- Actual UI sign-in produced HttpOnly, SameSite Strict cookies. Five real
  authenticated WebSocket viewers used same-origin URLs without query tokens
  or Authorization overrides, discovered protocol 2 and initially admitted no
  input. The schema exposed all 16 operations. The UI-created ordinary agent
  returned `unsupported_target` and acquired no local terminal storage.
- Explicit acknowledged control enabled Playwright keyboard input. Lowercase,
  uppercase, Ctrl-U, Ctrl-C and `keyboard.insertText` Unicode produced exact
  observed input bytes, expected shell files and matching live/recorded output.
  `R1_LITERAL=aA é 日本` survived end to end; Ctrl-C prevented the canceled file
  command. A single **synthetic DOM ClipboardEvent** traversed the real UI,
  WebSocket, core and Bash with bracketed multiline framing. Its two-line file
  appeared only after explicit Enter. No owner clipboard was read.
- Inspector/viewport changes under control produced actual pane and mirror
  sizes 104×48, 147×48 and 117×37, corroborated by shell `stty size`. The second
  cookie viewer explicitly took control; the previous viewer then sent no input
  for attempted typing. Both physical mirrors matched the pane and retained
  `read-only,ignore-size` flags with manual window sizing. Explicit release and
  takeover advanced the durable epoch; closing a viewer preserved its hold.
- Chat/Terminal/agent navigation retained separate drafts and the stored message.
  New viewers remained view-only. The queued coordination message was published
  automatically to the dated transcript; its decoded text and publication file
  bytes were unchanged after restart. Native runtime stayed disconnected with
  `input_ready=false`, no current session owner, and provisioning's unresolved
  resource dependency remained visible.
- Before outage, the controlling viewport made the pane/mirror 150×70. Real
  shell output placed a right marker beyond column 80 and a bottom marker at
  row 68. After all browser viewers and the normal console closed, the exact
  recorded Bash, tmux, recorder and positively identified child survived. The
  unique child marker was absent at shutdown and captured during the outage.
  Restarting the identical installed console preserved terminal/source/spec,
  process fingerprints and the epoch-5 human hold. Fresh login/viewing displayed
  the outage output without replayed input, resizing or automatic takeover.
- At 390×844, the restarted read-only display retained the 150×70 native grid
  and 13px font. Actual keyboard scrolling revealed complete right and bottom
  markers. Pane and mirror stayed 150×70; the observer sent **zero input, resize
  or control frames**, and the durable hold was identical. Explicit mobile
  takeover then fit pane/mirror/display to 45×41; a 430px viewport changed them
  to 50×41. Returning through Chat to a new desktop viewer preserved that grid
  and hold without resizing. Logout disconnected the viewer and retained Bash
  and the epoch-6 hold.

There were 492 successful sequenced receipts: 481 input frames containing 597
bytes and 11 resizes. File effects and recorder bytes, not receipts alone,
establish shell execution. No WebSocket domain errors, uncaught application
errors, failed requests or CSP violations occurred. Console errors were limited
to three expected unauthenticated session-discovery 401s and two automatic
Chrome favicon 404s; the notice returned successful matching bytes. Intentional
shutdown/close events are recorded separately. Script CSP stayed restrictive;
runtime inline styles were allowed as specified.

The device-query result follows clarification 215 precisely. Four shell-emitted
queries were present in raw capture, consumed/answered by normal tmux, and absent
from the controlled browser's output. Only the deliberate command and subsequent
Ctrl-C were sent by that browser. Initial attachment queries did reach the
view-only browser without producing input. This does **not** claim those four
queries reached controlled xterm. Controlled parser separation is covered by the
separately accepted synthetic-peer Chrome groups in UI geometry evidence at
`6fd80c494b1e4226aca486c0ef9c4d156ee1587c`; all four compiled asset hashes here
match that evidence. No passthrough option, API/WS mock or injected viewer frame
was introduced.

All 14 screenshots in `corrected/attempt-1/screenshots/` were inspected directly.
Key files are `controlled-terminal-desktop.png`, `wide-controlled-markers.png`,
`restart-chat-desktop.png`, `restart-terminal-desktop.png`,
`mobile-terminal-right.png`, `mobile-terminal-bottom.png`,
`mobile-terminal-controlled.png`, `mobile-chat.png`, `mobile-agents.png` and the
three `mobile-inspector-*.png` views. Controls and text remain readable; terminal
overflow stays within its scroll viewport. `visual-inspection.json` records each
image hash and dimensions. Desktop views used 1440×960 and 1800×1300; narrow
Terminal used 390×844, and subsequent Chat/navigation/inspector used 430×844.

Owned cleanup passed: normal console lifespan shutdown, explicit exact-terminal
shutdown, fingerprint-checked process cleanup and descendant reaping left all
12 recorded process identities gone. Both Chrome processes exited successfully;
their profiles and the disposable fixture tree were removed. Raw evidence remains
private. No unrelated process, service, owner profile or native store was touched.

Original-pair evidence remains a **failed acceptance**, despite its functional
checks: the original root notice was unserved (212) and narrow read-only content
was inaccessible (217). Earlier attempts also retain their distinct harness
timing/selection/publication-check failures. None is counted as a corrected-pair
pass or hidden by a workaround. The single corrected run above repeats the full
workflow with the separately accepted fixes; existing unit/browser suites were
not rerun.

This verifies the usable recorded Bash Terminal through the compiled UI. It does
not exercise native/provider startup, hook admission, Chat dispatch, native
assistant collection, physical OS IMEs, host archive transfer, other browsers,
VM/browser-MCP, reboot, backup or restore. Local captured bytes stayed distinct
from host archival (zero host-archived bytes) and complete native history. The
native hook/capture/projection acceptance remains outside this run. No production
fix, dependency change, delegation, extra reviewer, PR, main merge or deployment
was performed; Manager's
direct review follows the completion report.
