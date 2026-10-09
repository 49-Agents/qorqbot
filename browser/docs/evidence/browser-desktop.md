# Local desktop implementation evidence — 2026-10-09

Branch: `codex/browser-desktop-integration`, based on
`c341377ffc6552f139ea5ad071ef146e015805f7`. This increment implements the
owner/Manager handoff and subsequent lifecycle decisions. Commit/push identity
is recorded in the ACLA completion report. No merge or deployment is included.

## Delivered boundary

- Explicit desktop `init/start/status/stop`; foreground ownership independent
  of MCP, lifetime locking, process fingerprints, actual Chrome/CDP and X/VNC
  readiness, retained start identity and fresh-instance requirement.
- Dedicated Chrome profile, TigerVNC/Openbox, loopback interfaces, VNC
  `SecurityTypes=None`, normal private X authority, and graceful CDP close
  before helper/window-manager/display shutdown.
- Public actor/agent defaults and optional actor attribution; no application
  credentials/token config, Authorization middleware or bridge tokens.
- Existing tools/routes/schemas and one gate/receipt store. Settled effects
  explicitly unlock only after receipt commit; uncertain/crash paths preserve
  inherited locking. A verified helper fingerprint supports desktop cleanup.

These are host-package mechanisms. Core/UI handoff, noVNC routing, RFB input
fencing, machine holds, VM/runtime-unit installation and real login takeover
remain outside this increment. No core or UI source files changed.

## Actual installed-wheel flow

The single owning flow `test_desktop.RealDesktopTest` passed in 15.121 seconds
against actual Chrome 153.0.8010.36, TigerVNC 1.13.1 (X server 12101011), Openbox
3.6.1 and native agent-browser 0.38.1. Python was 3.12.3, SQLite 3.45.1, MCP
1.30.0 and websockets 17.2. Runtime dependencies use `requirements.lock`.

The official MCP client initialized/discovered the actual service and executed
navigate → snapshot → fill → click → snapshot → screenshot on a disposable local
HTTP page. The screenshot visibly contains the filled value and clicked result.
The same command ID returned its saved receipt. Client detach and MCP restart
left Chrome alive. Human mode blocked new automation and survived MCP restart;
explicit reconciliation released it. Normal stop removed Chrome's Singleton
locks, terminated the verified helper, then stopped the display. Same-instance
relaunch was rejected. A fresh supplied instance launched the same profile;
the fixture observed its persistent synthetic cookie, retained historical
receipts and human hold, and rejected a stale old-instance command. Final stop
completed with all recorded desktop processes gone.

The tested disposable coordinates were display `:801`, CDP `127.0.0.1:55737`,
VNC `127.0.0.1:60823`, MCP `http://127.0.0.1:47707/mcp`. VNC's actual RFB handshake
offered only the None security type. These listeners are now stopped.

The explicit runtime was `/tmp/qb-qnj_dhr5`. Both derived helper socket paths
were **78 bytes**, below the pinned helper's 103-byte limit. Profiles, screenshots
and logs were retained separately under the assigned private evidence root.
A persistent deployment must choose a short persistent runtime path; the package
does not silently allocate another socket directory.

Private evidence root:
`/home/alp/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/browser-desktop-integration`.
The successful bundle is `actual-desktop-eu5or2wx/`, including `result.json`,
the synthetic profile, screenshot artifacts, service/supervisor/CLI logs and
`runtime-evidence/`. `actual-flow-6.log` records the test result.

Exact real-flow invocation (with `E` set to that private evidence root):

```sh
PYTHONPATH=tests QORQUT_REAL_DESKTOP=1 \
QORQUT_TEST_HELPER="$E/helper/node_modules/agent-browser/bin/agent-browser-linux-x64" \
QORQUT_DESKTOP_EVIDENCE="$E" \
"$E/venv/bin/python" -m unittest -v test_desktop.RealDesktopTest
```

`PYTHONPATH=tests` supplies only fixtures: the browser package and its subprocess
entrypoints came from the installed wheel, not `src`.

## Focused checks and build

Each selection below passed, using
`PYTHONPATH=src:tests "$E/venv/bin/python" -m unittest -v <selection>`.
These were staged checks, repeated only when affected implementation changed.

| Selection | Result / private log |
| --- | --- |
| Public service config; production attach-only startup; MCP discovery/shared HTTP receipts/control; default/forwarded attribution; handoff response-loss retry | 5 passed, `transport-tests.log` |
| `test_desktop.DesktopInitializationTest`, `test_browser.SupervisorTest` | 6 passed, `final-lifecycle-tests.log` |
| Successful descendant retains fd; concurrent gate serialization; existing `test_browser.HelperTest`; explicit init/failure cleanup | 9 passed, `helper-lifecycle-tests.log` |
| Existing service restart test, strengthened to retain an unresolved effect until explicit settlement | Passed, `lifecycle-tests.log` |
| Existing abrupt-intent exit, post-intent crash, MCP wait cancellation and service-death helper-lock tests | 4 passed, `crash-tests.log` |
| Existing argv/lock check extended to reject an occupied live listener after the TIME_WAIT fix | Passed, `port-probe-test.log` |

Build/install commands:

```sh
"$E/venv/bin/python" -m pip wheel --no-deps --no-build-isolation --wheel-dir "$E/wheels" .
"$E/venv/bin/python" -m pip install --no-deps --force-reinstall "$E/wheels/qorqut_browser-0.1.0-py3-none-any.whl"
"$E/venv/bin/python" -m pip check
```

All succeeded; `pip check` reported no broken requirements. Both installed CLI
entrypoints are available. Final tested wheel SHA-256:
`7e0122b3132189d6ecbeebb9a3b9cbf3fcece515c66867405b158d8a9029aca6`.
`git diff --check` also passed. This is not a full-suite or security-review claim.

## Failures retained and corrected

Earlier runs of the same flow are preserved as `actual-flow-1.log` through
`actual-flow-5.log` with their private fixture directories. They exposed:

1. An overlong helper socket path: Manager required explicit short runtime
   configuration without a new API or implicit socket registry.
2. A successful persistent helper retaining the inherited flock: Manager
   required explicit unlock after settled receipt commit. The failed fixture's
   positively identified daemon was stopped by verified pidfd SIGTERM; its
   desktop then shut down. The original completed receipt remains retained.
3. A fixture paragraph omitted by the existing compact snapshot: the fixture
   result became a heading, preserving an actual DOM-result assertion.
4. Signal shutdown leaving profile locks: Manager required the pinned synchronous
   WebSocket client and one CDP `Browser.close`, followed by exit/lock checks.
5. A cleanly closed CDP connection in TIME_WAIT: the availability probe now uses
   SO_REUSEADDR while still rejecting a live listener.

No failed/uncertain command was replayed. Each correction used a fresh disposable
fixture. All six fixture supervisors now report no live supervisor and zero
owned children; failure artifacts and stale profile locks remain preserved.
The earlier blocked review actor was not resumed, its suppressed material was
not retrieved, and its security/logging probes were not run. No extra review
agents, native inference, real accounts, public listeners or live owner profiles
were used.
