# Explicit Chrome control

Global agents whose role selects the `browser-control` procedure (Ordubashy, Learning
Applier and the permanent workers) use it; browser use itself needs an authorized `browser_turn`
operation, which a permanent worker has only when Ordubashy grants it. Its short native
skill description is discoverable in Claude Code and Codex; the body is read only
for an explicit owner request to use Chrome/the qocaat browser. A URL, ordinary
research or a single web request is insufficient. This is a semantic instruction,
not a keyword hook or a guarantee enforced by code. It supports natural-language
requests without requiring a slash command. Nothing attaches during bootstrap.

The normal integration retains only the existing Bazarlyk MCP server. Browser
instructions, page snapshots and CLI help are absent from mandatory context.
The official [Playwright CLI](https://github.com/microsoft/playwright-cli) supplies
the actions. Qorqut supplies target selection and conservative lifecycle handling.
Role artifacts remain procedure-only; executable code lives in core, consistent
with the catalog's prohibition on executable skill files.

## Runtime contract

The pinned CLI includes every open tab's title when formatting command results.
An unresponsive renderer can make its timeout-free `page.title()` stall even
`tab-list`. Qorqut's packaged `browser_init.cjs`, loaded through Playwright's
`browser.initPage` configuration, bounds this optional lookup to 750 ms and marks
an unavailable title. Pending requests are shared rather than accumulated, and
normal titles return after the renderer recovers. It does not navigate, reload,
close, or otherwise repair the affected tab. Actions on that tab may still fail;
other tabs remain usable. Reattach after installing an updated initialization hook.

Preparation creates `.bazarlyk/browser` only when the selected Ordubashy artifact
contains `browser-control`. Invoke it as `sh .bazarlyk/browser <command>` from the
registered agent workspace. It uses the installation's prepared Python runtime,
so no shell activation or global CLI installation is needed. Re-preparation after
removing the skill/role removes the managed launcher and both native skill copies.

For Ordubashy only, the operator supplies `.bazarlyk/browser.json` in that exact agent workspace:

```json
{
  "schema_version": 1,
  "cdp_endpoint": "http://127.0.0.1:9225",
  "runtime_dir": "/absolute/path/to/deployed/biz-core/tools/browser"
}
```

There is no default endpoint and no fallback browser. Only explicit loopback HTTP
endpoints are accepted. `runtime_dir` contains the checked-in npm package and
lockfile; install it with `npm ci --ignore-scripts --no-audit --no-fund` in that
directory. Node 20+ is required by the pinned dependencies. CLI 0.1.19 pins
Playwright 1.63.0-alpha-2026-08-31; the lockfile also records integrity hashes.
The launcher checks the CLI version and never downloads packages at invocation.

This file is deployment configuration, not an operational table or role artifact.
Preparation leaves it untouched. Keep it operator-controlled under the existing
protected `.bazarlyk/` folder. It contains no profile or proxy credentials.

Session identity is derived from the absolute workspace and endpoint. Commands
run in `work/browser/<session>/`; use absolute paths for file arguments. Artifacts
can contain private page content and remain in that agent's work folder. Ambient
`PLAYWRIGHT_*` environment variables are removed. Attachment uses an explicit
generated CLI config with bounded connection/action/navigation timeouts. Commands
are serialized with a nonblocking per-session lock and a 45-second outer timeout.
An action timeout is an uncertain outcome; inspect before retrying a submission.

The launcher rejects target/config/session overrides, browser launching, global
shutdown and profile clearing. `detach` preserves the attached browser. Ordinary
page commands, including `eval` and `run-code`, remain available for requested work.
These restrictions prevent common accidents; arbitrary code and processes under
the same OS account can bypass them. They are not an authorization sandbox or
Qorqut's prepared-change/signoff mechanism. The skill respects authorization
already supplied by the owner and requests missing authority before external acts.
Concurrent owner/agent workflows still need coordination; command serialization
does not reserve a browser for a whole conversation.

## Targeted observations

After attaching, `observe` projects the current accessibility snapshot into a
small JSON result while retaining the complete Playwright snapshot as a private
artifact. Use `observe overview`, `observe find "text"`, `observe region
"label"`, `observe values` or `observe table "label"`. `overview` defaults to a
4,000-character response budget; `--budget=<characters>` accepts 1,000 through
24,000 characters, and `--depth=<n>` adjusts its structural scope. The result
keeps actionable accessibility refs, names, values and control state. Dialogs,
alerts, statuses and invalid controls remain visible when a scope is narrow.

The result includes an `observation` handle, `items`, `omitted`, `excluded`,
`next`, `evidence` and a `budget` record. Items are kept whole; when the budget
does not fit another item, `omitted_reason` points to the evidence artifact
instead of silently cutting a result. Continue with the same immutable handle,
for example `observe overview --observation=<handle> --offset=<next>`. Pagination
reads the saved artifact and does not issue another browser command. The
`evidence` path is relative to the workspace's `work/browser/<session>/`
directory; it is mode 0600 and is available when the exact snapshot is needed.
If the first item cannot fit even by itself, `pagination_stalled` and
`oversized_item` explain that there is no advancing `next`; inspect `evidence`
or retry with a larger budget.

Observation failures return a bounded JSON error with an `exit_code` and an
`error_artifact` path containing the complete captured CLI output. The wrapper
rejects arbitrary observation paths and refuses symlinks, non-regular files,
changed evidence and oversized artifacts. This read-only projection does not
add deltas or batching, and uses the same session lock, target selection,
business lease and owner activity handling as the existing commands.

As a synthetic size check, a 200-row accessibility snapshot measured 26,584
characters in the existing full snapshot and 3,961 characters in the default
overview response (about 85% fewer characters). These are local character
measurements, not provider-token billing or live-browser benchmarks; small
pages can be larger because the compact JSON carries refs and evidence handles.

## qocaat finding and rollout

On 2026-09-11 the existing `remote-browser.service` was active and exposed Chrome
153.0.8010.36 at loopback port 9225. Raw CDP responded, but an existing page had
an empty URL. Playwright's initial-page initialization waits for that page's first
navigation, explaining the earlier attachment timeout. The launcher checks CDP
target metadata before attaching and reports this exact condition without
navigating, closing or resetting owner tabs. `about:blank` is valid; an empty
string or `:` is not. The owner can navigate the affected tab to `about:blank`
before retrying. Do not restart Chrome or discard its profile as an automatic fix.

Unlike a bare `connectOverCDP()` probe, CLI 0.1.19's CDP factory passes
`noDefaults: true`. It preserves Chrome's default download/display settings.
No patch to Playwright or the machine's `/opt/remote-browser` runtime is needed.

After the owner approves merging/releasing both repositories:

1. Install the locked CLI dependencies in the deployed core release.
2. Publish Ordubashy 1.15.1, explicitly repin the intended Ordubashy profile while
   preserving plugins, grants, scope and assignments, and re-prepare it with the
   deployed core interpreter. Publishing source does not update live pins.
3. Install the target JSON above only in the intended Ordubashy workspace, using
   the actual deployed runtime path. Do not copy it to other agents.
4. Verify the prepared launcher with `sh .bazarlyk/browser --help`. In an
   owner-requested Chrome task, attach, inspect tabs, and detach. Resolve the
   empty-URL tab through the owner if it still exists. Do not use an owner's real
   agent conversation as an automated model acceptance test.

Rollback: detach the selected CLI session, restore the previous role pin and
re-prepare. The managed launcher/skill disappear. Retain operator target config,
work artifacts and Chrome profile for recovery; no browser data migration exists.

## Business browsers

Ordubashy may directly update saved `enabled`/credential-free `proxy_server` changes
with `business_config_update`; see [engineering authority](ORDUBASHY-ENGINEERING.md).
The optional owner_action_propose browser action still requires exact human Signoff and,
for the isolated mode, also accepts `mode` and `expected_exit_ip`; a proxy IP another
business already holds is refused at preparation and again at application.
Both routes reuse owner validation
and generated per-business identity; it does not provision credentials, restart
Chrome, or authorize browser use. Other agents' browser-config writes remain blocked.

### Initial provisioning and business creation

Both bootstrap and `business_create` intentionally create businesses without
a browser assignment. Absence of `config.browser` means unavailable, never direct
browsing or the owner's Chrome. During setup, the owner/operator explicitly
assigns a proxy using the deployed runtime:

```sh
<protected-proxy-source> | <deployed-core>/.venv/bin/python -I -m bazarlyk.browser.provision \
  --root <businesses-root> --workspace <deployed-workspace> --business <slug>
```

The source emits exactly one proxy URL, including percent-encoded authentication
if needed. Never put the URL in command arguments, inline shell commands, Git or
task text. This operator command uses the registered owner's canonical
`business_config_update` operation with the current revision, preserving all
unrelated config. The shared service must already be upgraded to the browser
release and migration 042 must be applied. The command does not migrate, publish
roles, start Chrome, or modify `.env`/Settings. Run only as an authorized operator
on the installation host; it is not an agent tool or an OS security boundary.

Identical reruns preserve the browser identity. A different existing assignment
or credential is refused; rotation requires the explicit recovery procedure below.
If config application fails, the protected credential file is retained for an
identical retry, not silently removed or overwritten. Verify saved config after
an interruption. Secrets do not enter operational previews or history.

An explicit owner-run alternative, `--mode isolated-desktop-v1 --expected-exit-ip <ip>`,
assigns the isolated desktop browser instead: one persistent headed Chrome with its own
TigerVNC and an OS-enforced proxy-only network namespace, separate credential and
`hq/browser` profile, and a one-business-per-proxy-IP rule. It is opt-in per business and
leaves every unmoded business on this legacy headless path. See
[isolated business browser](BROWSER-ISOLATED.md).

After configuration, install/start the exact business's systemd instance, verify
its exit IP through Chrome, publish the selected roles, repin only the intended
business agent profiles while preserving all other settings, and re-prepare
their folders. An enabled config is not proof that Chrome or its proxy works.
Keep proxy source attribution and verification results in a private operator
inventory, not business core or mandatory agent context. Reusing an active Fleet
persona's proxy also shares that persona's network identity; never copy its Chrome
profile, cookies, login bundle, ports or stealth seed. Do not alter the Fleet
persona mapping or scheduler when assigning an authorized proxy copy here.

`environment_get` remains a separately granted, scope-local Settings read. These
proxy credentials live outside Settings and are used only by the server-side
relay. Browser access does not grant `environment_get`, and granting it does not
expose the proxy credential file.

### Configuration and runtime

Requires migration 042 and the companion role artifacts. Source publication never
repins live profiles.

No business has a resident browser agent or launcher; the former private
`/api/agent/browser` route and its business-agent launcher are retired. A Global
agent reaches a business browser only through the `browser_turn` MCP operation:
`acquire` with `protocol: "handles"` and an explicit `business_id` (an optional
`consumer_id` UUID only separates the caller's own tasks; it is not verified as a
native conversation). The authority is the role's ordinary `browser_turn`
permission plus the selected browser-control skill, for any active business and
without membership or a delegation capability. The returned handle is bound to the
authenticated credential and machine registration, business, turn, lease, browser
generation and instance; every status, renew, checkpoint and command re-reads the
credential and registration, active identity and role, skill, operation permission
and business state. The lease is exclusive with the other Global agents, the
owner's viewer and durable workers, and a busy browser is refused, never taken.
A queued agent turn becomes the owner only while its parent identity is still an
active Global agent (`qorqut_operations.global_agents.global_profile`), never
judged from the requester label. Otherwise that row is blocked, with no lease or
wake, and the next queued entry advances. The same check admits every new browser
action. A recorded action is only replayed, and an unsettled one keeps its turn
until it is reconciled. Owner and assignment turns keep their own checks.
An uncertain command needs `reconcile` before anything is repeated.

The eligibility check was qualified only with synthetic stores, a fake target and
a recording runner, with no real browser, service or data. These passed against
the accepted Operations `6f731e9`:
`tests/test_browser_turn_global_eligibility.py` (7),
`test_browser_turn_queue.py` (3) and `test_browser_coordinator.py` (14).
Context `291b9ce`, template `7e2f45e`, dashboard `f33eeb3` and roles `0a1807d`
were used as exports. These browser test modules still import the removed
`bazarlyk.contracts` or `bazarlyk.application` modules, so they do not collect
and were not run:

- `test_browser_short_handles.py`
- `test_browser_configured_lease_free.py`
- `test_browser_real_worker_path.py`
- `test_browser_worker_runtime_path.py`
- `test_browser_turn_integration.py`
- `test_live_delegation_repairs.py` A host business
uses its configured service target; a sandbox business keeps Chrome in its browser
container: each leased command runs the host browser runtime against a 127.0.0.1
listener created for that command only, which reaches the container's loopback
Chrome through the exact Docker ownership, generation and instance and re-proves
the lease on every CDP request and WebSocket check before it is closed. No browser
endpoint, Docker or URL parameter is accepted from or returned to the caller, the
sandbox private agent listener is not used, and nothing starts Chrome or an agent.
No browser endpoint or instructions enter bootstrap context.

Before a new IP assignment, confirm the IP belongs to neither a persona nor a
business, including disabled/archived business reservations. HN account history
alone is allowed; an HN-only or empty persona assignment is not. Use the fresh
audit procedure in [IP eligibility](BROWSER-ISOLATED.md#ip-eligibility-for-a-new-business),
including for direct configuration changes. Unknown is not free.

The owning business's SQLite `config_json.browser` contains `enabled` and
`proxy_server`. The owner updates these through `business_config_update` (the
existing `/api/biz/<slug>/operations/business_config_update` endpoint), with the
current revision and idempotency key. Keep other ordinary config fields when
editing: this operation remains replacement-style except for the reserved browser
block. Example input block, with no inline credentials:

```json
{"browser":{"enabled":true,"proxy_server":"http://proxy.example:8080"}}
```

Core automatically assigns `cdp_port` (19400–20399), UUID-based `user_data_dir`
and `downloads_dir`, `credentials_ref`, and a `generation`. A unique SQLite index
arbitrates concurrent prepared allocations. A conflicting prepared allocation
must be re-prepared against current state; it never silently shares the port.
Persisted ports do not change on restart or when occupied by an unrelated process.
Renaming the business preserves its profile, downloads and port. Disabling retains
its data. Caller-selected generated fields are rejected. Ordinary config edits
that omit `browser` preserve the entire block; agents cannot modify it.

Credentials live only in `<businesses-root>/.bazarlyk/secrets/browser-proxy/<business-UUID>.json`,
mode 0600, with `username` and `password` strings. Empty strings for both explicitly
select an IP-authorized proxy; missing files or partial credentials fail closed.
The reference cannot select an arbitrary file or another business's secret.
Never put credentials in business config, Git, command arguments or role context.

`python -I -m bazarlyk.browser.service --root <businesses-root> --business-id <UUID>
--runtime <deployed-core>/tools/browser` runs one **headless** Chrome plus one
loopback relay. This does not add visible Chrome windows or a manual-login/VNC
dashboard. Install with the example systemd template in `tools/browser/`; substitute
the approved release's interpreter/runtime and actual root. The service is never
started by bootstrap or by an agent browser command. No live units are installed
by tests or source changes.

The relay uses pinned `proxy-chain` 3.0.1 (Node >=20.11). It has a mandatory fixed
upstream, no direct branch, no verbose request logging, and receives credentials
over stdin. HTTP/HTTPS and SOCKS5 upstream URLs are accepted; authenticated HTTP
and HTTPS CONNECT are covered by local fixtures. Verify the actual provider and
SOCKS5 behavior before using them live. Chrome gets only the local relay address.
We do not reuse Bazarlyk's CDP authentication handler or stealth launcher.

Chrome retains its sandbox, disables QUIC/nonproxied WebRTC UDP, and removes
implicit localhost/link-local bypass with `<-loopback>`. Browser DNS resolution
is disabled except for the local relay; the relay resolves upstream connections.
There is no JS/fingerprint injection or extension loader. These are independent
browser identities and proxy-configured browsing, **not** a network sandbox,
protection from same-UID shell agents, or a guarantee of website unlinkability.
Separate proxy URLs may share or rotate exit IPs. Static/sticky exit assignment and
an actual in-browser exit check are deployment requirements if distinct stable
network identities are wanted. See [Chromium proxy bypass behavior](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md)
and [proxy-chain's upstream contract](https://github.com/apify/proxy-chain).

Like Playwright's own Chrome launcher, headless business Chrome explicitly uses
`--password-store=basic` to avoid waiting for the owner's desktop keyring while
opening its cookie store. Profiles therefore rely on private filesystem permissions,
not desktop-keyring encryption; treat their contents and backups as credentials.
The supervisor requests orderly CDP `Browser.close` before bounded signal fallback
so persistent cookies flush on normal restart. This is operator lifecycle handling,
never the agent's `detach` behavior. Forced termination can still lose recent state.
The systemd template uses `KillMode=mixed`: initial SIGTERM goes only to the
supervisor, with a final cgroup-wide kill after 60 seconds. Do not use
`KillMode=control-group` here: its initial signal would kill Chrome's children
before orderly profile shutdown can run.

The supervisor checks its own Chrome's listening socket and publishes local
readiness bound to config generation, profile and process start identities for
itself, Chrome and relay. This proves local readiness, not valid proxy credentials
or a verified exit IP. Startup failure is failure, never “ready with a warning”.
An occupied port is refused without killing its owner. A service failure stops
only its own children. Profile data is retained. Changing proxy/enabled config
invalidates commands immediately and stops the old service on its next check;
the operator explicitly restarts with the new settings, rather than hot-switching
an active task. Updating the secret also requires an explicit restart.

Business ownership is acquired at attach, checked before each command, and released
after successful detach. Callers in the same business contend for
one browser; different businesses proceed independently. Tokens survive ambiguous
HTTP responses. No timeout automatically steals a lease or retries a page action.
The lease is operational state under that business's protected browser directory;
its lock inode is never deleted. CLI artifacts remain in the owning agent's work
folder, and detach preserves Chrome and its relay.

Recovery: ask the owning task to detach first. If it cannot (crashed session,
revoked role, changed home business, or failed CLI detach), the operator may restart
**that business's** service after checking for in-flight work. Restart invalidates
all prior attachments/ownership with a fresh service instance and preserves the
profile. Do not delete profiles, steal by elapsed time, or kill Chrome by port.
Old page commands fail closed; a command already executing when authority changes
cannot be retroactively undone. This is not MCP Signoff enforcement.

## Validation

`tests/test_browser_control.py` checks fail-closed configuration, override rejection,
session binding, environment isolation, timeout handling, empty-tab diagnosis,
selected-role projection, absence from startup/MCP configuration, and removal on
role change while preserving operator configuration.

`tests/test_business_browser.py` covers config preservation, generated identity,
allocation, private credentials, current-role ownership and stale readiness. Its
opt-in real test starts two disposable Chrome profiles with two authenticated local
proxy fixtures, verifies separate cookies and persistence across restart, and never
contacts a real upstream provider. `node --test tools/browser/relay.test.cjs` tests
upstream routing, authentication rejection, origin-auth separation, HTTPS CONNECT,
and no direct forwarding when the upstream fails.

The opt-in real test launches and terminates only a disposable headless Chrome
profile on an ephemeral port. It attaches through the locked CLI, creates a test
page, interacts with it, preserves an existing tab and verifies Chrome survives
detachment:

```sh
QORQUT_TEST_WORKSPACE_ROOT=/absolute/path/to/workspace \
QORQUT_TEST_CHROME=/usr/bin/google-chrome-stable \
  /absolute/path/to/venv/bin/python -I -m pytest -q tests/test_browser_control.py
```

Owner conversational checks: “research this URL” must not attach or load the full
procedure; “use Chrome on qocaat to inspect this page” should load it and select
the configured target. Follow with unrelated web research to check that browser
permission is not carried forward. Automated source tests do not prove model
instruction-following behavior.

## Service release compatibility

Point the operator-installed service at an approved deployed core release that
matches the operational schema, including its locked browser runtime. Review
active business ownership before restarting: archived businesses stay stopped,
and sandbox-assigned businesses must not also run a host browser. Preserve each
business's registered profile, port, proxy and credential files.

The example unit limits failures to three attempts in ten minutes, with a
30-second delay. Permanent schema/configuration faults exit 78 and require
operator repair. Startup validates the application ID, exact migration history
and required tables without scanning every operational record. Explicit snapshot
validation, backup/restore and migrations retain full integrity/reference checks.
