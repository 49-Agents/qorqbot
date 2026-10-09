# Local browser desktop operations

This package runs one dedicated headed Chrome, TigerVNC display and Openbox
under a foreground supervisor. MCP/HTTP runs separately and attaches to that
prepared desktop. The operator supplies all identities, paths and coordinates;
core will generate this configuration during later integration.

## Prerequisites

Observed on the disposable Linux host acceptance environment:

| Component | Version / executable |
| --- | --- |
| Python | 3.12.3 |
| Chrome | 153.0.8010.36; `/opt/google/chrome/chrome` |
| TigerVNC | 1.13.1, underlying X server 12101011; `/usr/bin/Xtigervnc` |
| Openbox | 3.6.1; `/usr/bin/openbox` |
| agent-browser | **0.38.1**, native `bin/agent-browser-linux-x64` from the pinned npm package |
| Python runtime dependencies | Exact resolution in [`requirements.lock`](../requirements.lock); MCP 1.30.0, websockets 17.2 |

Use a non-root Linux user with working Chrome sandbox support. The package does
not install Chrome, X servers, VM images or host services. The helper is a native
binary; the npm JavaScript launcher and Node are not required at runtime.
The npm package declares Node 24+ for its tooling; the native binary itself is
what this service executes. No helper-managed browser download is needed.

Run the following from `browser/` in the monorepo, using a separate virtual environment:

```sh
python3 -m venv /absolute/venv
/absolute/venv/bin/python -m pip install -r requirements.lock
/absolute/venv/bin/python -m pip wheel --no-deps --no-build-isolation -w /absolute/wheels .
/absolute/venv/bin/python -m pip install --no-deps /absolute/wheels/qorqut_browser-0.1.0-py3-none-any.whl
```

## Configuration

Supply an absolute JSON config path, owned by the executing user with mode 0600
and no symlinks. Unknown fields are rejected. There are no application tokens,
credentials files, passwords or login operations.

| Field | Meaning |
| --- | --- |
| `actor_id`, `agent_id` | Public UUID defaults for request attribution; `agent_id` equals browser ownership |
| `helper_executable` | Absolute executable path to the pinned native agent-browser binary |
| `desktop` | Required by desktop `start`; `chrome`, `xvnc`, `window_manager` are absolute executable paths; optional `width`/`height` default to 1280/900 |
| `handoff` | Optional `{ "url": "<complete application HTTP(S) endpoint>" }`; no token fields. Absent means no `browser_request_input` tool |
| `browser.binding.identity` | `agent_id`, `recovery_epoch`, `boot_id`, `browser_instance` UUIDs; positive `environment_generation`; `protocol: 1` |
| `browser.binding.control_epoch` | Nonnegative initial epoch; retained database state wins on subsequent starts |
| `browser.uid` | Executing Linux uid |
| `browser.home`, `profile`, `downloads`, `artifacts` | Explicit distinct owned absolute directories |
| `browser.database` | Absolute `browser.sqlite3` path in a distinct owned runtime directory |
| `browser.state_file` | Readiness JSON beside the database, distinct from database and `desktop.json`, `desktop.lock`, `desktop.xauthority`, `helper-process.json` |
| `browser.mounts` | Nonempty array of exact Linux mount identities: absolute `path`, integer `mount_id`, `device` (`major:minor`), `filesystem`; must cover the actual longest-prefix mount for each directory |
| `browser.cdp_host` | Exactly `127.0.0.1` |
| `browser.cdp_port`, `vnc_port`, `service_port` | Three distinct explicit ports in 1024–65535 |
| `browser.display` | Dedicated unoccupied X display number, 1–999 |

`boot_id` must match `/proc/sys/kernel/random/boot_id`. Provisioning can read
`/proc/self/mountinfo` or the package's `state.mount_inventory()` to obtain exact
mount values. Reboot/remount requires explicitly refreshed input. Keep native
Chrome profile state on a local filesystem. Do not use an owner's existing
profile or display as the configured target.

Choose a **short runtime directory**: agent-browser 0.38.1 requires its full Unix
socket pathname to fit 103 bytes, including the derived helper/session suffix.
The adapter retains its fixed session name and uses
`<runtime>/helper-tmp/agent-browser/<session>.sock`. Long profile and artifact
paths are independent of this constraint. No implicit alternate socket directory
is allocated. A persistent installation must choose a short persistent runtime
path; the acceptance fixture explicitly uses a private short `/tmp/qb-*` runtime
only for disposable testing. The evidence records the observed socket length.

## Initialize, run, observe and stop

Prepare the parent directories first, owned by the executing uid and without
group/other write access. `init` creates only the explicitly configured
directories and local state files. It accepts empty directories or this agent's
previously claimed directories; it refuses nonempty unclaimed directories.
Repeated initialization retains receipts, control state and profile data.

```sh
/absolute/venv/bin/qorqut-browser-desktop init --config /absolute/config.json
/absolute/venv/bin/qorqut-browser-desktop start --config /absolute/config.json
```

`start` stays in the foreground, holds the lifetime lock and retains its actual
child handles. Run it independently of the MCP process. There is no daemon fork,
auto-restart loop or installed systemd unit. Future deployment may place this
foreground command in its own runtime unit, outside MCP/core/runner units.

From separate processes:

```sh
/absolute/venv/bin/qorqut-browser --config /absolute/config.json
/absolute/venv/bin/qorqut-browser-desktop status --config /absolute/config.json
/absolute/venv/bin/qorqut-browser-desktop stop --config /absolute/config.json
```

Equivalent module entrypoints are `python -m qorqut_browser.desktop` and
`python -m qorqut_browser.server`. Imports do not provision or launch anything.
MCP startup opens existing state and closes automated admission. The caller
must explicitly reconcile through `POST /v1/browser/control` before commands.
`status` verifies saved process fingerprints and actual listener ownership; a
ready desktop does not imply the separate automation control gate is open.

Chrome/CDP listens at `127.0.0.1:cdp_port`, TigerVNC at
`127.0.0.1:vnc_port`, and MCP at `http://127.0.0.1:service_port/mcp`.
The HTTP identity endpoint is `/v1/identity` on the MCP port. TigerVNC offers
`SecurityTypes=None` for a local viewer without a password. X connections retain
normal Unix socket and generated private MIT-MAGIC-COOKIE-1 handling; TCP X11 is
disabled. CDP, VNC and MCP are local owner interfaces, not application login gates.

Closing an MCP client or stopping/restarting MCP leaves Chrome running. Desktop
stop signals only the fingerprint-verified supervisor through a Linux pidfd.
The supervisor removes readiness, closes admission, waits for the managed
command lock, sends one CDP `Browser.close` on the verified browser endpoint,
and waits for the owned Chrome process group to exit and its Singleton paths to
disappear. It then terminates the verified helper daemon and stops Openbox and
TigerVNC. The CDP WebSocket disables proxies/redirects and has bounded messages
and finite deadlines. A reply or disconnected socket alone does not establish
profile flush. No process-name kill or forced Chrome shutdown is used. If
shutdown cannot be confirmed, it retains its lock,
child handles, state and logs; an explicit stop can retry. It never deletes
Chrome's Singleton locks or the profile. Unknown locks/processes require
operator inspection, not automatic adoption or lock removal. Startup-failure
cleanup may signal its newly launched children but never claims a clean profile
when locks remain; it is distinct from normal CDP shutdown.

## Restart and retained state

Each actual Chrome start attempt consumes its explicit `browser_instance` UUID.
`desktop.json` records the attempt before children launch and retains it after
stop/failure. `init` alone does not consume an instance. A duplicate live start
cannot launch a second desktop, and a stopped/failed instance cannot be reused.

For a new Chrome lifetime, supply a fresh `browser_instance` in the config while
retaining agent, profile, resources and receipt database. Restart MCP using that
config. Reconcile with `expected` equal to the retained database binding and
`desired` equal to the new configured identity plus an advanced control epoch.
The caller must explicitly resolve any uncertain commands and human hold.
Replacement does not settle effects or release human control. An MCP-only
restart keeps the existing Chrome instance identity but still requires explicit
control reconciliation. Historical receipt lookup and exact command retries
retain the old result; stale commands cannot execute against replacement Chrome.

Retain `home/`, profile, downloads, artifacts and the runtime directory together.
Runtime contains `browser.sqlite3` and its WAL/SHM, command lock, `desktop.lock`,
`desktop.json`, X authority, readiness and per-instance/role logs. Helper output
and screenshots live beneath `artifacts/<command UUID>/`. These are operational
data outside Git. Startup failures retain logs and clean only that invocation's
owned children; a missing executable is reported without installing it.

`helper-process.json` binds the pinned helper daemon to browser identity,
resources and its OS fingerprint. Only desktop stop cleans it up, after acquiring
the command lock; MCP/client detach leaves it and Chrome alive. Completed
commands explicitly unlock their inherited file description after receipt
commit. Uncertain effects and interrupted execution keep the default close-only
lock behavior and require reconciliation; no forced unlock or automatic replay
is provided.

## Application boundary and current limits

The [protocol](PROTOCOL.md) defines unchanged command/control/receipt schemas.
Optional `X-Qorqut-Actor` is public attribution only. Every HTTP/MCP effect uses
one gate and durable store. Human mode blocks managed automation; explicit
reconciliation releases it. The bridge sends one request using the original
actor/target/key, with no automatic retry or local duplicate request store.

Core must supply identities/configuration, explicit control transitions, durable
request/conversation/message transactions and Done/Cancel semantics. UI/core
handoff, noVNC routing, VNC input fencing, machine holds, real guests and actual
login takeover remain unimplemented integration work. This host flow is not
G4/G6 or VM acceptance. No native inference or real accounts are involved.

The opt-in owning test is `test_desktop.RealDesktopTest`, using
`QORQUT_REAL_DESKTOP=1`, an absolute `QORQUT_TEST_HELPER` and optional
`QORQUT_DESKTOP_EVIDENCE`/`QORQUT_TEST_CHROME`. It preserves disposable evidence
and exercises one local HTTP fixture through the official MCP client.
