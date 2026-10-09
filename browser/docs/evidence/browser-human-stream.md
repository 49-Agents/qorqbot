# Human browser relay evidence — 2026-10-09

Branch `codex/browser-human-stream`, based on
`19c4577795f20ffd4261ca41464afb63767cb046`. Implements Manager's frozen D57
browser lane; the completion report records the exact candidate commit/push.
No merge, deployment or other repository changes are included.

## Delivered contract

`WS /v1/browser/view` accepts the exact protocol-1 binding handshake, responds
ready before binary RFB, and targets only the configured loopback TigerVNC.
It uses existing local attribution, actual desktop readiness and the shared
Gate/Store lock. One ready human viewer holds that lock through socket cleanup.
Checks before input and a bounded watcher fence changed epochs, mode, readiness
and service incarnation. Reconcile still publishes closed admission before
waiting for the lock. ASGI shutdown joins viewers without stopping Chrome.
The HTTP control schema, MCP tools, identity response and database schema are
unchanged. No passive RFB parser, general tunnel, credentials or core/UI imports
were added. [PROTOCOL.md](../PROTOCOL.md) specifies the limits and wire behavior.

## Focused installed-package checks

Eight checks passed in **12.641 s**, recorded in
`installed-gate-relay-checks.log`: two grouped relay checks and six existing
gate/MCP checks covering process serialization, yielding/timeout, stale service
and binding, shared HTTP/MCP receipts and cancellation during an effect. They
prove the viewer closes before reconciliation reaches its readiness/receipt
boundary, holds the real flock, rejects competitors, retains human mode on
disconnect, observes state/incarnation changes, and closes on lifespan shutdown.

The one real `test_remote.RealViewerTest` passed in **14.898 s**
(`actual-flow-2.log`). It reuses `DesktopFixture`, extracted from the existing
`RealDesktopTest`, and the prior pinned agent-browser helper tooling. Fresh
Chrome/TigerVNC/profile/runtime state was allocated for every attempt.

The actual WebSocket flow negotiated RFB 3.8/None security, read **4,608,000**
rendered pixel bytes and clicked/typed/saved the ordinary local form. After
explicit release, an MCP snapshot and screenshot showed **Saved human relay**.
Automation was blocked while the viewer held the gate; a second viewer was
rejected. The old socket closed before the ready automation receipt and could
not forward further input. Both optional `binary` negotiation and no subprotocol
worked. Disconnect retained ready human mode. MCP shutdown closed the viewer
while the exact Chrome fingerprint remained alive; service restart retained
human mode with admission closed. Normal fixture cleanup then stopped the owned
desktop. Both attempted fixtures have zero children and no live supervisor.

Private evidence root (`E`):
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/browser-human-stream`.
Successful bundle: `actual-desktop-ing8t7_w/`, including `result.json`, screenshot,
synthetic profile, receipt/runtime copy and service/supervisor logs. Configured
runtime `/tmp/qb-oott_52w`; display `:801`, CDP `40259`, VNC `57879`, service
`33371`, all loopback listeners now stopped. `cleanup-confirmation.json` records
both fixture outcomes.

Reproduce the real flow with the installed wheel (only test fixtures on PYTHONPATH):

```sh
PYTHONPATH=tests QORQUT_REAL_DESKTOP=1 QORQUT_DESKTOP_EVIDENCE="$E" \
QORQUT_TEST_HELPER="$(dirname "$E")/browser-desktop-integration/helper/node_modules/agent-browser/bin/agent-browser-linux-x64" \
"$E/venv/bin/python" -m unittest -v test_remote.RealViewerTest
```

Chrome 153.0.8010.36, TigerVNC 1.13.1, Openbox 3.6.1, agent-browser 0.38.1;
Python 3.12.3 and the unchanged `requirements.lock`, including MCP 1.30.0,
AnyIO 4.15.1, uvicorn 0.54.0 and websockets 17.2. Wheel build, force-install,
`pip check` and `git diff --check` passed. Tested wheel SHA-256:
`4dbd5fdb80950a9f4c9c0d8abd88b8451b186a0553fc2bd2df86a6b08bef25ce`.
Build/install logs and the wheel are under `E`.

The first real attempt (`actual-flow-1.log`, `actual-desktop-scxinymn/`) exposed
transient SQLite contention between input/watcher reads with a zero busy wait.
A bounded 20 ms database wait fixed that while preserving the 50 ms observation
deadline and at-most-100 ms watcher budget. No uncertain input was replayed.
An initial in-process test fixture also needed its lifespan enter/exit in the
same task as required by AnyIO; that fixture correction preceded these checks.

Combined core/UI/noVNC, VM/machine surfaces and deployment remain Manager's
separate acceptance. This is no broad failure-suite or security-review claim.
No reviewers, native inference, owner profiles/processes/account data or earlier
blocked review probes were used.
