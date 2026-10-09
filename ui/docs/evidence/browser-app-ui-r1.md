# D57 browser UI evidence

2026-10-09; branch `codex/browser-app-ui`, based on
`a9f72a3cd0a34db9cc724e6ee15741e605098cef`. Implements the Manager's frozen
core `c96952f` contract without changing public operation names, arguments or
wire shapes. Only the UI repository changed. See [implementation](../BROWSER-IMPLEMENTATION.md).

Locked `npm ci --ignore-scripts`, `npm run build` (including TypeScript) and
`git diff --check` passed. Toolchain: Node 22.22.0, npm 10.9.4, Chrome
153.0.8010.36, noVNC exactly 1.7.0. Vite reports a 617.85 kB JavaScript chunk
(169.92 kB gzip); its size warning is nonfatal. Both browser and existing Terminal
notices are present in the built assets; the Terminal notice is unchanged.

Three grouped D57 Chrome checks passed against the compiled UI and a disposable
synthetic API plus real local WebSocket/RFB peer. The installed noVNC client
rendered verified pixels and sent real pointer coordinates and keyboard events.
Checks cover one retained canvas/socket across inline, Computer and expanded
placements at 1440×960 and 937×779; nested inspector expansion/collapse and focus;
Done/Cancel notes and closed historical cards; manual creation; no automatic
socket replay; explicit expanded Reconnect and interrupted Resume; view/agent
switch teardown; exact-key create/cancel retries after saved but lost responses;
pending states, invalid shapes/targets, HTTP failure and late read cancellation;
old-server fallback and separate unavailable Machine/Desktop.

Five existing owning Chrome regression groups passed in `checks-r4`: creation,
rename/icons/conflicts; late selection/mutations/drafts; message hover/copy,
composer and drawers; file cards/Library; and live Terminal/Chat geometry and
control. Subsequent changes were confined to browser lifecycle/presentation,
new browser checks and documentation. Initial browser test failures exposed
Playwright's incompatible WebSocket mock and overly broad/transient selectors;
the real socket fixture and precise assertions corrected these. The final three
browser groups all pass (16.7 seconds); no broad duplicate suite was added.

Private artifacts:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/browser-app-ui/`.
`final/` contains npm/build/Chrome logs, toolchain and distribution SHA256
manifests, compiled `dist/`, and `checks/` screenshots. Earlier fixture/debug runs
remain separately under this directory. Wide/937 inline, Computer and expanded
screenshots plus interrupted/cancelled states were inspected.

This proves frontend behavior against synthetic requests and RFB pixels, not
actual TigerVNC, installed-core handoff/notifications/restart, native MCP loading
or VM/machine support. Those remain the Manager's combined functional test.
No owner processes, profiles, native account stores or live data were used.
No nested agents or reviewer batch was started. Candidate only: no merge or
deployment performed by this lane.
