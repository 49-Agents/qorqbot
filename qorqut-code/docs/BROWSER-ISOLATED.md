# Isolated business browser (`isolated-desktop-v1`)

Status: **source and staged assets only.** Nothing here is installed, migrated,
activated or verified against the live host, live Guacamole or a live login.

## What it is

One persistent, headed Chrome per explicitly assigned business, with its own
TigerVNC display and **operating-system-enforced proxy-only outbound access**. It is
opt-in per business (`businesses.config_json.browser.mode = "isolated-desktop-v1"`).
Businesses without a mode keep the legacy headless browser, its validation and its
paths. The legacy service rejects a new-mode config; the new mode never runs as the
old host Chrome and never falls back to direct networking.

The business's own agents share it through the existing prepared
`.bazarlyk/browser` attach/check/detach path and `browser_turns`/`control.lock`/
`lease.json`. The configured global Ordubashy or an active Global peer agent holding
`coordinate_businesses` may select it explicitly (see
[Explicit Ordubashy selection](#explicit-ordubashy-selection)). The dashboard owner
views and controls it over VNC from those agents' panes.

## Configuration and files

The saved block carries `mode`, `expected_exit_ip` (IPv4 the proxy must exit from),
`proxy_server` (a numeric HTTP `http://IPv4:port`; hostnames and aliases are refused),
the assigned CDP port and generation. Everything else is generated and validated by
`configured_target`: arbitrary path, port or generation edits and mixed-mode
identities are rejected.

| Item | Location |
|---|---|
| Chrome profile, downloads | `<hq>/browser/profile`, `<hq>/browser/downloads` (private, 0700) |
| Credential-free projection | `<hq>/browser/setup.json` (generated; never a second configuration writer) |
| Proxy credential | `<root>/.bazarlyk/secrets/browser-proxy/<UUID>-isolated.json` (0600); the legacy `<UUID>.json` is preserved |
| Locks, lease, readiness | `<root>/.bazarlyk/browsers/<UUID>/` (`control.lock`, `lease.json`, `ready.json`, `service.lock`) so old and new launchers contend for the same locks |
| Host deployment | `/etc/qorqut-browser-isolated/<UUID>.json` (display, VNC address, runtime dir, tools; no commands, proxies, credentials or profile paths) |

`hq/browser` is excluded from Git/history/export scans, storage backups and
migration snapshots. A mode change is the only transition of generated paths and the
credential reference, and requires the browser to be stopped and unleased.

### IP eligibility for a new business

Assign an IP only when **neither a persona nor a business is assigned to it**.
An HN account alone does not block allocation: an IP with HN history but no
persona and no business is free, subject to the existing audit and lifecycle
checks. An empty persona or an HN-only persona still owns its IP and blocks a
new assignment. Disabled and archived businesses also retain their reservations.
Never release an existing assignment implicitly to make an IP available.

This is a rule for new allocations. Existing assignments are preserved; an identical provisioning retry does not allocate the IP again.
Account history and assignment are separate facts. In particular, an old Global
IP table label such as `Free - HN only` or `Free - empty persona` is insufficient:
check the current persona registry and canonical business reservations, then
refresh the audit. The Global table is a manually reconciled view, not an allocator.

### One proxy IP, one business

Preparation and the application-time writer validation (including
`owner_action_propose`/`browser_update`) reject a proxy IP already assigned to another
business, comparing normalized IPs across every browser block, including disabled
legacy ones. A legacy literal-IP proxy without `expected_exit_ip` reserves its host
IP. There is no automatic allocation.

`python -I -m bazarlyk.browser.inventory --root <root> --snapshot <audit.json> <ip>…`
reports `available`, `assigned`, `occupied` or `unknown` from canonical assignments
plus an **explicitly supplied** sanitized audit snapshot (see
`tools/browser/isolated/browser-proxy-inventory.json.example`; `--from-csv` sanitizes
an audit CSV to a snapshot, dropping persona names, paths and ports but retaining
`persona_assigned`). A nonempty CSV `persona` cell means assigned, even for an
empty or HN-only persona; an explicitly empty cell means unassigned. Missing
persona evidence remains unknown. Duplicate IP rows must be reconciled first.
The snapshot is evidence, not an assignment store: unknown, stale (over 7 days),
malformed or inconclusive is never free. New provisioning requires an explicit
boolean `persona_assigned: false` as well as a clean non-HN profile check and no
business reservation. Older snapshots without this field remain readable but
cannot establish availability. HN account history alone is ignored; persona
ownership is not. The example retains the pilot's persona assignment and is
only a schema example, not a free-IP recommendation. Keep the current
snapshot at `<businesses-root>/.bazarlyk/browser-proxy-inventory.json` (private, outside
Git) and pass it with `--snapshot`/`--inventory`; this source change does not write that
live file, and a snapshot is regenerated from a fresh audit at each activation.
The inventory/provisioning commands enforce the persona audit check; direct
operator/API configuration changes must follow the same audit prerequisite.
The configuration writer independently enforces business-IP uniqueness at
preparation and application. There is no live persona-registry synchronization.

## Network boundary

A system unit with `PrivateNetwork=yes` gives the browser a namespace with loopback
only, no interface and no route. Chrome keeps its sandbox. A host **gateway** unit is
the only path between the namespace and the host, as three fixed byte bridges under a
per-business runtime directory (0700, created by systemd) with 0600 Unix sockets:

1. `upstream.sock` → the frozen numeric proxy IP/port (never sees credentials);
2. host `127.0.0.1:<CDP>` → the namespace's `cdp.sock`;
3. host `<Tailscale address>:<VNC>` → the namespace's `vnc.sock`.

Inside, a loopback TCP-to-Unix relay reaches `upstream.sock`, and the pinned
proxy-chain relay receives the credentials on stdin with its upstream rewritten to
that relay. Xtigervnc listens on a private Unix socket with a private VNC password
file. Chrome's proxy-bypass, DNS, WebRTC and QUIC flags remain as defense in depth.

Startup refuses outside a loopback-only namespace (probed from actual interface and
route state), requires the live gateway, and checks the proxy exit IP before writing
readiness. There is no direct fallback. A failed recheck withdraws readiness; a
different exit IP stops the browser (exit 78); a gateway/child death or a
config/generation change stops the affected browser. Every process is tracked by PID
plus boot/start identity and owned namespace, socket, port, profile and generation;
nothing is killed by name or port, and foreign locks are never deleted. Shutdown
removes only owned runtime state and keeps the profile.

Exit codes: 0 clean stop (also a config/generation change; the operator restarts
explicitly), 1 transient failure, 75 busy (an occupied display, port or already-running
owner; nothing of the occupant is touched), 78 configuration, namespace or exit-IP fault
needing operator repair (`RestartPreventExitStatus=78`). systemd restarts 1 and 75 within
its start limit.

## Operator commands

Run with the deployed core's interpreter as an authorized operator on the host.
Nothing starts a unit, edits the database or rotates existing credentials.

```sh
# Provision (credential from stdin only; never argv/Git/logs). --dry-run writes nothing.
<protected-proxy-source> | <core>/.venv/bin/python -I -m bazarlyk.browser.provision \
  --root <root> --workspace <workspace> --business <slug> --mode isolated-desktop-v1 \
  --expected-exit-ip <ip> --deployment /etc/qorqut-browser-isolated/<UUID>.json \
  --inventory <sanitized-audit.json> --record-prior <private-prior.json> [--dry-run]
# Read-only completeness/host-readiness report
<core>/.venv/bin/python -I -m bazarlyk.browser.provision --root <root> --workspace <workspace> \
  --business <slug> --mode isolated-desktop-v1 --check --deployment <deployment.json> [--sibling <other.json>]
# Host checks: collisions/listeners/secrets/tools; end-to-end verify; explicit exit recheck
<core>/.venv/bin/python -I -m bazarlyk.browser.isolated.check preflight --deployment <deployment.json> [--sibling …]
<core>/.venv/bin/python -I -m bazarlyk.browser.isolated.check verify --deployment <deployment.json>
<core>/.venv/bin/python -I -m bazarlyk.browser.isolated.check recheck --deployment <deployment.json>
# Owner viewer: the business's one browser (adds/removes only its own surface; --dry-run; keeps the first prior file)
<core>/.venv/bin/python -I -m bazarlyk.browser.isolated.surfaces --instance-root <instance> --business <slug> \
  --surface-id <id> --label <label> --viewer-url <guacamole-url> [--dry-run|--remove]
```

Provisioning writes only private files (credential, VNC password, the prior browser
block, the projection) and asks the owner API for the one canonical change at the
current revision with a deterministic idempotency key. A real run holds one
per-business provisioning lock (`<root>/.bazarlyk/secrets/browser-proxy/<UUID>.lock`,
shared with the legacy command) from the authoritative state read through the
credential and VNC files, prior record, canonical application and projection; a
concurrent run is refused as `busy` and the identical command is safe to retry.
`--dry-run` and `--check` take no lock and create no directory or file. Secrets are
linked into place complete or not at all, so a racing writer can never replace an
existing credential or VNC password; identical reruns are no-ops, a different existing
credential is refused and nothing else changes, and an already isolated business is
never re-pointed to another proxy by provisioning. The `--record-prior` file is the exact
old block and revision kept as evidence; it is not a request the settings API accepts
back (see the recovery workflow in `tools/browser/isolated/`).
Unit templates and the deployment example
are in `tools/browser/isolated/` (`qorqut-browser-gateway@.service.example`,
`qorqut-browser-isolated@.service.example`, `deployment.json.example`); replace the
`/APPROVED-RELEASE` placeholder and install them through the reviewed release
lifecycle. Both must be **system** units with `User=`; a user-manager unit silently
gets no network isolation and the session refuses to start.

### Reuse for another business

Nothing is specific to one business: another business needs its own UUID-named
deployment file, display, VNC port, CDP port, credential and proxy IP, checked by
`preflight --sibling` and the one-IP rule.

## Explicit Ordubashy selection

Only the one configured global Ordubashy or an active Global peer agent may pass
an authorized business UUID on `browser_turn` `acquire` (`protocol: "handles"`). For
Ordubashy the caller's principal must equal the installation's
`management_coordinator_id` (read in the same transaction, on every handle operation),
so an unset coordinator, another Ordubashy with the same role and skill, or a
reassigned coordinator is refused and a prior coordinator's handle stops working.
A peer agent needs an ecosystem scope that names the business, the explicit
`coordinate_businesses` capability, current business membership and the browser-control
skill; revoking the capability stops its existing handle on the next call. The two
peers contend for one business browser through the same exclusive queue and neither
can take or release the other's lease. Omitting `business_id` keeps its own configured,
lease-free browser. The business browser is leased through the same
exclusive `browser_turns` queue (`browser_key = business:<id>`), the same
`control.lock`/`lease.json`, and `browser_handles`. It is refused as busy, never taken,
while a business agent or durable worker owns it, and it refuses the business agents
while it is held. A released slot is reserved for a waiting durable worker.

Coordinator identity, active membership, current scope, the browser-control skill and
the browser generation/instance are rechecked on every call, and commands, status,
renew, checkpoint, artifact, reconcile and release resolve only from the stored handle.
A superseded, expired, replaced or stale handle is refused; an uncertain action is
never retried blindly and blocks the lease until reconciled by the same handle, which
also ends the turn and clears the shared lease record. Archived businesses and
sandbox-assigned businesses are refused.

## Owner viewer

Businesses have no resident agents, so a business browser belongs to the business, not
to an agent. `operations/.runtime/browser-surfaces.json` (schema 2) maps each business
UUID under `businesses` to exactly one browser surface; that surface cannot belong to a
second business or appear in a Global agent's own `agents` list. The stager writes only
that business record and needs no registered agent; global own browser and Desktop
bindings are untouched. `tools/browser/isolated/browser-surfaces.project.json.example` maps an
example project to the dedicated browser beside Ordubashy's own browser and Desktop. The
stager rejects invalid viewer URLs, non-private files, conflicting or cross-bound surface
IDs, a second surface for the same business and any older registry (convert it with the
explicit v1.2 migration first) without touching the file. A Guacamole VNC connection to
the host VNC address is added by the operator; it grants the owner keyboard/mouse control
of that browser only.

The installation owner reads it through `GET /api/biz/{slug}/browser-surfaces`:
`{business_id, configured, browsers, desktop: null}` with zero or one browser, private
and `no-store`. A host or isolated browser returns exactly its registered surface. A
sandboxed business returns only its ready sandbox owner viewer (synthetic ID
`business-sandbox-browser`, the business name and kind `browser`); when that viewer is not
ready it returns no browser and never falls back to a registry or host surface.
`configured` reports viewer setup for the selected target. The read requires an active
business and the owner's current membership, not a resident agent, and it starts no
browser, service or agent, takes no lease and writes no configuration.

## Private bundled Node runtime

`tools/browser` is built into a self-contained runtime; no host Node or npm is ever
consulted, installed, or changed. `tools/browser/build_runtime.py`
(`/usr/bin/python3 -I tools/browser/build_runtime.py`, standard library only) is the
frozen core build command and only constructs the candidate artifact:

1. It refuses anything but Linux x86-64, then downloads the one pinned official
   distribution (`node-v24.21.0-linux-x64.tar.xz`, npm 11.19.0, over HTTPS only,
   including redirects) and verifies its pinned SHA-256 **before** anything is extracted
   or executed.
2. It extracts only the expected `node-v24.21.0-linux-x64/` tree into a private
   temporary directory beside the outputs, refusing traversal, absolute or escaping
   links (`link/..` tricks included), hard links, devices, duplicate entries and
   oversized archives.
3. It runs the locked `npm ci` (agent-browser 0.38.1, proxy-chain 3.0.1) with that
   Node and its own npm by absolute path and a child-only environment whose PATH starts
   with the bundled `bin/`, then checks the installed versions against the pins.
4. It publishes `tools/browser/node/` and `tools/browser/node_modules/`, replacing only
   a previous output it can prove it generated (a `node/` carrying its marker file, an
   npm-generated `node_modules/`). Any other occupant, a symlink included, is refused, and
   a failure at any step leaves the previous outputs untouched.

Both directories are declared `build_outputs` (copied into the staged artifact and
hashed) and ignored by Git.

`tools/browser/run-agent-browser` (a checked-in POSIX wrapper) finds its own runtime
directory, prepends only that runtime's `node/bin` to its child PATH and `exec`s the
absolute bundled `node/bin/node` with the pinned `agent-browser.js` and unchanged argv.
`agent-browser`'s own `#!/usr/bin/env node` launcher is therefore never used.
`runtime_executable` selects the wrapper for a runtime directory that contains it and
fails closed, rather than using the host Node, when that runtime is incomplete (wrapper
not executable, bundled Node missing or not executable, agent-browser package
missing). A runtime directory without the wrapper keeps its previous selection
(`node_modules/.bin/agent-browser`), so existing global browser descriptors, profiles and
prepared integrations are untouched. The deployment file's `node` selects only the
session's relay Node and must be the same bundled binary; `check preflight` reports both
the bundled Node and that relation for a runtime that ships the wrapper.

## Evidence and limits

- Focused core tests: `tests/test_browser_isolated_config.py`,
  `test_browser_isolated_provision.py`, `test_browser_isolated_runtime.py`,
  `test_browser_isolated_surfaces.py`, `test_browser_coordinator.py`,
  `test_browser_inventory.py` (plus the added cases in `test_owner_actions.py` and
  `test_migration_snapshots.py`).
- Real smoke, `tests/test_browser_isolated_smoke.py` (opt-in `QORQUT_ISOLATED_SMOKE=1`,
  passwordless sudo, Chrome, Xtigervnc, openbox, a built runtime directory
  (`QORQUT_ISOLATED_SMOKE_RUNTIME`: bundled Node 24, locked packages, relay, wrapper), private credentials):
  temporary transient **system** units and namespace, blank profile, unused display and
  ports, the selected proxy. It observes the kernel's answers: Chrome exits through the
  proxy, the namespace has no other path, a routed namespace or a missing gateway starts
  nothing, broken authentication and a different exit IP fail closed, an upstream break
  withdraws readiness with no host-IP fallback, collisions are refused without touching
  the occupant, restart keeps the profile and leaves no orphans, child/gateway death
  stops the browser, and a config change stops both units. A scoped launcher
  attach/tab-list/goto/eval/detach runs through the wrapper against the disposable browser
  with a decoy host `node` first on PATH that must never run, and the relay process is the
  bundled Node.
- The provider sometimes answers one of Chrome's two speculative `CONNECT`s with a 404,
  which surfaces as `ERR_TUNNEL_CONNECTION_FAILED` on a first navigation. The smoke test
  retries only that error; production agents may see the same first-load error.
- **Not tested:** a live Guacamole VNC connection or login. The dashboard tests use stub
  iframes and prove pane routing only. No real Claude/model/fleet acceptance probe was run.
