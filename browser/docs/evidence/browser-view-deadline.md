# Viewer observation deadline correction — 2026-10-09

Branch `codex/browser-view-deadline`, based on
`4e2041e3e90a951d7afd8c67019674b4aebbf364`. Manager's actual core/noVNC run
recorded browser `TimeoutError` and peer closure `1011/unavailable` during
ordinary input/display traffic. The 50 ms deadline covered worker scheduling
and the full validated Store read, not just SQLite's 20 ms busy wait.

The Manager-directed correction sets `CHECK_TIMEOUT=1.0` and retains
`WATCH_INTERVAL=.05`. The latter is a polling target, not D57's original hard
100 ms shutdown promise. Per-input validation still precedes forwarding;
failed/timed-out checks close the relay. The separate bounded check capacity,
20 ms busy wait, original binding/service incarnation, human admission, shared
viewer lease and joined forwarding/socket cleanup are unchanged, as are public
schemas, local no-auth behavior and the existing 1 s/5 s cleanup/I/O deadlines.
See [the amended protocol](../PROTOCOL.md#human-viewer-relay).

The two existing `test_remote.RelayGateTest` groups passed against the installed
wheel in **1.828 s**. The existing input assertion now exercises a valid check
delayed by 120 ms and asserts the viewer remains open. The changed-epoch/mode,
incarnation, exclusive lease and close-before-control-completion assertions
remain in their owning groups.

The existing `test_remote.RealViewerTest` passed on its **single run**, in
**15.116 s**: **4,608,000** rendered pixel bytes, actual pointer/keyboard input,
`Saved human relay` in the subsequent MCP snapshot, and socket closure before
the automation receipt. Disconnect retained human control; MCP shutdown kept
Chrome alive. Normal fixture cleanup then stopped the owned desktop. A separate
read-only cleanup check found no live fixture supervisor/Chrome, retained child
entries, listening fixture ports or display socket/lock.

Private evidence root (`E`):
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/browser-view-deadline-r1`.
Logs: `installed-relay-checks.log`, `actual-rfb-flow.log`; actual bundle:
`actual-desktop-7xfq09ee/`. Wheel build/install, `pip check`, installed-module
provenance and cleanup are recorded there. The unchanged `requirements.lock`
was installed into a fresh private venv; only tests were on `PYTHONPATH`.

```sh
PYTHONPATH=tests "$E/venv/bin/python" -m unittest -v test_remote.RelayGateTest
PYTHONPATH=tests QORQUT_REAL_DESKTOP=1 QORQUT_DESKTOP_EVIDENCE="$E" \
QORQUT_TEST_HELPER="$(dirname "$E")/browser-desktop-integration/helper/node_modules/agent-browser/bin/agent-browser-linux-x64" \
"$E/venv/bin/python" -m unittest -v test_remote.RealViewerTest
```

Tested wheel: `wheels/qorqut_browser-0.1.0-py3-none-any.whl`, SHA-256
`f6c704ee7597346675cd9f23877ed098105c3e327175d767ff097be12b9aa31a`.
Build/install and dependency checks passed. Combined actual core/noVNC acceptance
remains Manager's next check; this smaller RFB flow does not certify it. No
broad suite, nested review, merge or deployment was performed by this worker.
