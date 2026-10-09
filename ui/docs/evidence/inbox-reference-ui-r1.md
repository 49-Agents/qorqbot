# D53 inbox reference UI — implementation evidence

2026-10-09. Candidate for Manager review on `codex/build-inbox-reference-ui-r1`,
based on UI `67499d1b58998d0ccfe2d54289a224fca7797a30`. Frozen core contract:
`9685beefaa0ccbd72fdd4eee46d1e49eac9270dc`, read through immutable Git objects.
Scope: [INBOX-REFERENCE-UI.md](../INBOX-REFERENCE-UI.md).

`src/wire.ts` adds the optional nullable conversation read annotation.
`src/inbox-reference.ts` validates both canonical UUID strings and the native
user message kind; it neither parses pointer text nor joins history. EntryRow
renders a neutral native details/summary disclosure with occurrence time,
unchanged pointer, message ID and original recording time. Entry ID and timeline
position remain intact. Missing/null/malformed annotations retain ordinary
rendering. Native user messages without a sender actor say `Terminal input`;
existing owner/agent sender labels remain. Seven scoped CSS rules keep the
disclosure compact and wrapping. No composer, Terminal, store, cursor, file-card,
network or backend code changed.

Checks used the existing synthetic HTTP fixture on an isolated loopback port
and actual `/usr/bin/google-chrome` 153.0.8010.36 through Playwright 1.64.0.
This is **mocked API evidence**, not connected-core or native-flow acceptance.
Node v22.22.0; npm 10.9.4. No dependency changes.

- `npm ci --no-audit --no-fund`: succeeded, 26 locked packages.
- `npm run build`: TypeScript check and Vite production build passed.
- `npm test -- --grep 'synthetic inbox references'`: one focused scenario passed
  (1.3s test, 3.1s total); no page errors. Covers both native providers, a
  reference outside the loaded page, omitted/null/malformed annotations, both
  UUID fields, wrong source/role/kind, native sender labels, unchanged entry
  order/identity/text/time, one stored body, Tab/Shift+Tab/Enter/Space, and
  desktop 1440×960 / mobile 390×844 overflow. Only existing static/session/read
  requests occurred; no WebSockets or message mutations.
- `git diff --check`: passed. The existing native-text scenario's expected
  sender was updated to `Terminal input`; that broader scenario was not rerun.

Private artifacts are under
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/build-inbox-reference-ui-r1/check-8LZNtQ/`.
`build.log` and `chrome.log` contain the checks. In
`chrome/shell-synthetic-inbox-refe-1d13a-expand-by-keyboard-at-390px/`, the
three `inbox-reference-*.png` screenshots were visually inspected: compact
collapsed rows, readable expansion and keyboard focus, and unclipped mobile
text. `served-ui.html` and `synthetic-conversation-response.json` capture actual
served response bytes without session requests or credentials for paired review.

SHA-256: served HTML (identical to built `dist/index.html`)
`530e8af5abddd3e7056a3bb0296396398455843975a217694ad1c6c1b395573b`;
synthetic conversation response
`50152dd628f00865bf208f9d6274a50d4f07103dd1a9c3adb9f6c35464bdcc34`.
Built assets: `index-BT2tbzWT.css`, `index-FV6XuFIl.js`.

Core association correctness and the paired response check remain Manager's
acceptance work. No native CLI/model/provider, live service, VM, owner session
or data was used. Original evidence is preserved. No PR, merge or deployment.
The exact committed/pushed SHA and clean-state verification accompany the ACLA
completion report.
