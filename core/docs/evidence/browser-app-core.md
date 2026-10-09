# D57 core browser integration candidate

2026-10-09, branch `codex/browser-app-core`. Implements the core lane of D57 and
Manager's explicit reattachment and idle-reconnect clarifications. No browser/UI
repository changes, merge, deployment, native inference, or owner session/profile
access were performed. Combined real Chrome/TigerVNC/UI acceptance belongs to
Manager.

The candidate has schema 10, 24 public operations and six fixed job handlers.
The seven D57 operations and exact BrowserView, BrowserSurface and
InputRequestView shapes are implemented; existing browser tool names remain
exclusive to browser MCP. Core advertises browser protocol 1. ConversationItem
adds nullable `browser_request`, and immutable Entry adds the request source.
Existing migration checksums and notices are retained. Inventory assertions were
updated mechanically, including null checks for the appended ledger column.

Core stores immutable attachment identities, original request facts and saved
Reconcile payloads/receipt observations. The browser_control job performs HTTP
outside SQL and reuses the original command after pending or lost responses.
Cards, publication jobs and events commit with creation. Done/Cancel commits one
winning terminal state, release intent and ordinary Messaging notification.
Retirement preserves unconfirmed effects as errors and stops dispatch; it never
claims the old browser released control. Reads use bounded observations, and the
viewer accepts raw binary RFB only after the exact browser-ready handshake.

Native setup owns a separate browser MCP entry/instruction manifest, retains
core/hook blocks and authored native preferences, and includes attachment identity
in its cache key. It never launches or restarts a native CLI. New configuration
may require the user's MCP refresh or next manual CLI launch.

## Verification

Private artifacts:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/browser-app-core/`.

- `installed-checks.log`: 16 focused installed-package checks passed. They cover
  initial/manual control, original-key replay, one card and dated publication,
  Done/Cancel race and atomic rollback, response-loss/restart receipt recovery,
  explicit resume, idle reconnect, identity validation, cursor handling,
  replacement retirement, immutable facts and recovery holds, actual HTTP/WS
  relay binding and shutdown, native setting preservation, schema 9 upgrade,
  HTTP/MCP schema parity and bounded dispatcher lifetime.
- The subsequent retirement correction keeps cancelled job views within the
  existing JobRecord vocabulary: `progress.phase=retired_attachment`, with
  `uncertain_effect` and preserved request error. The rebuilt installed wheel
  passed the retirement/operations_get, HTTP/WS restart flow, and passive
  import/packaged-notice rechecks in `installed-final-checks.log` (3 checks).
- `native-migration-r1.log` includes the passing existing populated-v7 migration
  regression, preserving native/message/file records and publication bytes.
  Initial fixture timing/old-schema inspection issues were corrected; the
  owning schema-9 and native preservation checks subsequently passed.
- `build-final.log`, `install-final.log` and `installed-provenance.json` record
  wheel build/install and imports from the private venv's site-packages. Test
  PYTHONPATH contained only this worktree's tests; core child servers used the
  installed console entrypoint. `git diff --check` passed.

Final wheel SHA-256:
`c7fc1a5cbbaa1235222a2531d31250b753d64dcd2b16ae8898b47f58bde9f61d`.

The browser peer was a deterministic disposable protocol implementation. It is
not real framebuffer or browser-lifecycle acceptance. Native programs were
synthetic executables that would record any unintended launch; none launched.
VM provisioning, machine control, browser process supervision and real native
MCP loading remain outside this lane. Pending/uncertain browser effects are never
settled by inference. The earlier provider-blocked review remains stopped.
