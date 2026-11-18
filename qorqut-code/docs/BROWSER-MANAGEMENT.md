# Worker browser management

Ordubashy can set up, check, repair, restart and stop any active permanent worker's own browser with one MCP operation,
**`agent_browser_manage`**, without asking the owner for approval. It is routine maintenance of the worker's own configured
browser, not a new privilege: it grants no authority to spend, submit forms, act as the owner or touch another service.

Workers never run it. A worker whose browser is missing or broken gets a structured error
(`browser_not_configured` or `browser_configuration_invalid`) telling them to ask Ordubashy; they report it once and keep using
`browser_turn` afterwards.

## Operation

```json
{"agent_id": "<worker UUID>", "action": "status"}
{"agent_id": "<worker UUID>", "action": "ensure",  "idempotency_key": "setup-atlas-1"}
{"agent_id": "<worker UUID>", "action": "repair",  "idempotency_key": "fix-atlas-1"}
{"agent_id": "<worker UUID>", "action": "restart", "idempotency_key": "restart-atlas-1"}
{"agent_id": "<worker UUID>", "action": "stop",    "idempotency_key": "stop-atlas-1"}
{"agent_id": "<worker UUID>", "action": "use_shared", "idempotency_key": "share-atlas-1"}
{"agent_id": "<worker UUID>", "action": "use_own",    "idempotency_key": "own-atlas-1"}
```

There is no path, executable, endpoint, flag, port, service name or shell argument: the host derives all of them. Mutations
need an `idempotency_key` (1–200 printable characters); `status` needs none and changes nothing.

| Action | Effect |
| --- | --- |
| `status` | Reports `missing`, `healthy`, `stopped`, `busy`, `target_missing`, `conflict`, `invalid` or `unsupported`, the ownership mode and the next action. |
| `ensure` | Creates a dedicated managed browser when the worker has none; starts a stopped owned one; leaves a healthy browser untouched. Repeating it never duplicates a browser. |
| `repair` | Diagnoses; restores a missing generated `browser.json` from saved ownership; starts a stopped owned browser; restarts an owned browser that does not answer. A healthy browser is a no-op. |
| `restart` | Restarts exactly the owned browser. Refused with `browser_in_use` while a browser command is running. |
| `stop` | Stops exactly the owned browser. Same refusal while a command runs. |
| `use_shared` | Moves the worker to the installation's one Shared browser (see [The Shared browser](#the-shared-browser)). |
| `use_own` | Moves the worker back to its own browser. |

A receipt names the target, action, whether anything changed, the resulting state, that the profile was preserved, a bounded
diagnostic and the verification result. A managed browser is reported `healthy` only when a real, bounded CDP health check
succeeds *and* the answering process is the exact supervised Chrome that owns the port (recorded boot-bound identities plus
listener ownership); another process answering CDP on that port is reported as such (`stopped`), never adopted, stopped or
called healthy. A failed or timed-out start is reported as a failure, never as ready. No cookies, page content, credentials or arbitrary logs
are returned. A repeated key replays the saved receipt (`"replayed": true`); a restart or stop whose earlier attempt did not
report completion is reported `"outcome": "uncertain"` with the current state and is not repeated blindly.

Only the configured Ordubashy (and the installation owner, outside MCP) may manage, and only an *active permanent global
worker*, including workers created later. Ordubashy himself, the Learning Applier, inactive workers and business agents are
refused; a worker with an `allow` override is still refused, and an explicit `deny` override on Ordubashy is honored.

## Managed headless browsers

`ensure` on a worker without a browser creates a **dedicated headless managed browser**:

* one persistent headless Chrome (normal sandbox — never `--no-sandbox`), loopback CDP only, starting on `about:blank`.
  It is [Qorqbot's own Chrome](#qorqbots-own-chrome) when that is installed and starts on this computer, otherwise the
  installed Google Chrome (`/usr/bin/google-chrome-stable`). The choice is made when the browser is created and saved in
  its ownership record;
* a separate profile per worker at `operations/.runtime/agent-browsers/<agent UUID>/profile`, kept private (`0700`);
* a CDP port from the reserved range **20400–21399** (business browsers use 19400–20399). The port is chosen *and* the
  worker's ownership record written as one step under a global allocation lock (released before Chrome starts), so two workers
  can never pick the same free port; a port saved for another worker, configured in any workspace or deployment file, or with
  a live listener is skipped. A reservation interrupted before the browser ran is picked up again by the next `ensure`/`repair`;
* a host-owned supervisor (`python -m qorqut.browser.agent_supervisor --state <dir>`) run as a **transient systemd user
  service** (`systemd-run --user --unit=qorqut-agent-browser-<installation hash>-<worker UUID>.service --collect`, fixed argv and
  environment, no shell). It therefore lives outside the application service's cgroup and **survives application restarts and
  releases**. A unit that is already running is reused, never duplicated. The supervisor holds a per-worker process lock, records
  boot-bound identities for itself and Chrome, and publishes `ready` only after the CDP endpoint answers *and* that exact Chrome
  owns the listener;
* the user manager does not share the application service's mount namespace. When the service sees its instance through a
  private bind mount (`BindPaths=`), the supervisor is started with the directory's path on the underlying filesystem and is
  told the path the ownership record was written with (`--recorded-state <dir>`), so both sides read and lock the same files;
* Chrome's temporary directory is `<dir>/tmp` when its singleton socket path fits a Unix socket address, and otherwise a
  private `qorqut-browser-<hash>` directory under `/run/user/<uid>` (Chrome aborts on a longer socket path);
* a generated `.qorqut/browser.json` (unchanged schema: `schema_version`, `cdp_endpoint`, `runtime_dir`) written only after
  the browser is verified, using the installation's pinned browser runtime.

Ownership (`meta.json`) is written before launch, so a timeout can never lead to a second browser. A random listener on the
reserved port is never adopted; processes are signalled only by their recorded identity, never by port, name or unverified PID.
Chrome's profile lock files are never removed. A worker's own browser commands re-check the managed identity under the use lock,
so a worker never talks to a foreign process that took over a dead browser's port; it gets `browser_not_ready` pointing to
Ordubashy. After a host reboot (or any other loss) `status` reports `stopped` and `repair`/`ensure` starts the same profile
again. `stop` ends exactly that supervisor (and, only if it ignored the stop, exactly its derived unit).

## The Shared browser

An agent uses its own browser (the default) or the installation's one **Shared browser**. There are no named profiles.

* The Shared browser is one more managed headless Chrome with the same supervisor, port range, ownership record and health
  check. Its state is `operations/.runtime/agent-browsers/da69c2a4-cd9c-5aa7-9a94-176338b13618/`, a fixed identifier that
  is not an agent and cannot be managed by that identifier.
* Every agent set to it shares its cookies and website sign-ins. Each agent's commands run pinned to that agent's own tab
  (`agent-browser --pin-tab`): its first command opens a new tab, and a closed tab is reported as `tab_gone`, never replaced
  by another agent's tab. The tabs are not hidden: `tab-list` shows them all.
* The choice is the record `agent-browsers/<agent UUID>/shared.json`. The agent's generated `.qorqut/browser.json` then
  holds the Shared endpoint and `"shared": true`. A command is refused (`browser_configuration_invalid`) when the record and
  the target file disagree.
* `use_shared` starts the Shared browser when needed, stops the agent's own browser and leaves its profile and sign-ins on
  disk. `use_own` removes the choice, starts the agent's own browser again (creating one if it never had one) and stops the
  Shared browser when no agent uses it any more. Its profile stays.
* For an agent on the Shared browser, `status`, `ensure`, `repair`, `restart` and `stop` act on the Shared browser, so they
  affect every agent on it. Results carry `"browser": "shared"` (otherwise `"own"`).
* **No fallback.** When the Shared browser is stopped or broken, the agent's commands fail with `browser_not_ready`. Nothing
  points the agent at its own profile or any other browser.
* A hand-installed `browser.json` and a legacy-bound browser are never replaced: `use_shared` answers
  `browser_target_conflict`.
* Owner sign-in on the Shared profile stops the Shared Chrome, so every agent on it waits until the window is closed.
* The owner sets the choice per agent in Settings ("Which browser each agent uses"), which posts
  `{"action": "choose", "agent_id": ..., "browser": "own" | "shared"}` to `/api/root/browser-setup`.

## The owner's live view

The dashboard's Browser tab of a global agent shows that agent's managed browser live when no viewer is configured for it
(`qorqut/browser/live.py`, `biz-dashboard/js/agent-browser-live.js`). Owner's dashboard only; not an operation.

| Route under `/api/root/agents/<agent UUID>/browser-live` | Does |
| --- | --- |
| `GET` | Lists the browser's open pages (`id`, `title`, `url`). |
| `GET /stream?tab=<id>` | Server-sent events: `open` (the view's id), `tabs`, `frame` (a JPEG and the page's size in CSS pixels, at most 20 a second), `closed`. |
| `POST /input` | `{"view": ..., "events": [...]}`: pointer, wheel, key and inserted-text events for that view's tab. |

* It uses Chrome's DevTools port directly, never `agent-browser`: it opens no tab, holds no lock and does not pause the
  agent. The agent and the owner can act on the same page at the same time.
* Chrome draws only the front tab, so opening a view brings its tab to the front. Other tabs keep working but run as
  background tabs.
* For an agent on the Shared browser the list shows every tab of the Shared browser. The view does not know which tab
  belongs to which agent.
* A stream ends when its tab closes or the connection drops; the dashboard does not reconnect by itself.
* The visible sign-in window stays for what a picture cannot do: file pickers, passkeys, downloads.

## Qorqbot's own Chrome

Qorqbot downloads one pinned [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) build, so an
installed Google Chrome is not required (`qorqut/browser/managed_chrome.py`).

* **Where.** `operations/.runtime/chrome/<version>-<platform>/` holds a build (the archive's own folder plus
  `installed.json`); `operations/.runtime/chrome/current` is a symbolic link to the build in use. Platforms: `linux64`,
  `mac-arm64`, `mac-x64`, the ones Qorqbot is packaged for.
* **When.** The automatic browser setup loop (`setup.ensure_all`, every two minutes, and the owner's Retry setup) starts
  the download in a background thread and does not wait for it. A failure is retried with the same growing wait as an
  agent's browser (4 minutes doubling to one hour); Retry setup tries at once.
* **Checks.** The archive's SHA-256 must equal the pin before anything is extracted. Extraction writes only folders,
  regular files and links that stay inside the build, each file created new and never through a link. The finished build
  is published by one rename, so an interrupted download or extraction leaves only a `.staging-*` folder that the next
  run removes. The build is then started once, headless with its normal sandbox and a throwaway profile, and `current`
  is set only when that works. Running again with the pinned build in use does nothing.
* **Which Chrome a browser uses.** A browser created from then on records `.../chrome/current/...`. A browser that
  already exists keeps the Chrome in its ownership record: a profile last opened by a newer Chrome can lose data in an
  older one (measured: local storage written by Chrome 158 was gone after Chrome 153 opened the profile; cookies
  survived), and the installed Chrome updates itself past the pin. `QORQUT_CHROME_EXECUTABLE` and a runtime that
  bundles Chrome (`QORQUT_NATIVE_ROOT`) are explicit choices: nothing is downloaded and nothing replaces them.
* **When it cannot start.** A build that does not start here stays on disk unselected and the installed Chrome stays in
  use. Settings then says why in plain words when no Chrome is installed either. Two known causes on Linux: missing
  system libraries, and Ubuntu 23.10 or later, where AppArmor denies user namespaces to programs outside the system
  folders, so the downloaded build reports `No usable sandbox!` while `/usr/bin/google-chrome-stable` works.
  `--no-sandbox` is never added.
* **No automatic update.** Chrome for Testing does not update itself. The version changes only with the pin; a browser
  moves to a new pin the next time it starts, and a superseded build is deleted once no process runs from it.
* **Differences from Google Chrome.** In a visible window (owner sign-in) it shows a banner that it is for automated
  testing. It honours `--load-extension` (with `--disable-extensions-except`), which Google Chrome 137 and later ignore.

Settings reports `managed_chrome` in `/api/root/browser-setup`: `state` is `ready`, `downloading`, `waiting`, `failed`
(download or install; retried), `unusable` (does not start here), `unsupported` (no build for this computer) or
`not_used` (explicit Chrome), with an `explanation` for the owner and a `diagnostic`.

### Changing the pinned version

1. Pick the Stable version from the Chrome for Testing page (it must exist for all three platforms).
2. `python -m qorqut.browser.managed_chrome hashes <version>` downloads the three archives and prints the new
   `ARCHIVES` entries. Chrome for Testing publishes no checksums, so these hashes are "what was downloaded that day":
   compare the sizes and, when in doubt, download on a second network.
3. Set `VERSION` and `ARCHIVES` in `managed_chrome.py`, run `tests.test_managed_chrome`, and check the pinned
   `agent-browser` against the new build (open, snapshot, click).
4. After the release each installation downloads the new build by itself; the old one stays in use until then.

## Idle tabs and cookie banners

Each managed browser's supervisor also looks after its pages (`qorqut/browser/idle_tabs.py`, `cookie_banners.py`); agents
need no instruction for either.

* **Idle tabs.** A web tab that no session used and whose address did not change for 30 minutes is unloaded: the same tab
  goes to a small "Idle" page, so its identifier and the agent's binding to it stay. The agent's next command loads the
  page again (one history step back). Within about 10 minutes the page returns exactly as it was; later it reloads.
* **Cookie banners.** A banner is refused when it offers a refusal; it is never accepted, and a page without a refusal
  control is left alone.
* A browser gets both when it next starts.

## Preservation

Profiles, cookies, logins, `browser.json` and ownership metadata survive `restart`, `stop` and `repair`. There is no delete
action: nothing wipes a profile, replaces a configured target or silently assigns a different port or profile. A target file
that differs from the saved record is reported `conflict` and left untouched.

## Ordubashy using a worker's browser

`agent_browser_manage` only maintains a worker's browser. To use it, for example to sign a website in there on a computer
without a screen, the configured Ordubashy selects it with `browser_turn`: `list` names each active permanent worker's own
browser under the worker's agent ID, and `acquire` with `protocol: "handles"` and that `browser_id` returns a handle bound to
it. Every call with that handle proves afresh that the caller is the configured Ordubashy and that the worker is still an
active permanent worker. Commands hold the worker's use lock like the worker's own, so a restart or stop is refused while one
runs, and artifacts stay in Ordubashy's own folder. No other agent can select another agent's browser. The worker's own access
is not paused, so the worker waits while Ordubashy works there.

## Existing browsers and the legacy binding

A healthy existing `.qorqut/browser.json` is never replaced, migrated, cloned or restarted by `ensure`. The file alone does
not prove which OS service runs the browser, so `status` and a healthy `ensure` work, but `repair`, `restart` and `stop` of an
**unbound** existing browser answer `browser_management_unconfigured`. That is an installation step, not a missing permission.

The operator binds the exact unit once, from a shell on the host, as the application's OS user:

```sh
python -m qorqut.browser.agent_binding bind --instance-root <instance> [--component NAME=PATH ...] \
    --agent-id <worker UUID> --scope user|system --unit <name>.service \
    --profile <absolute profile directory> --endpoint http://127.0.0.1:<port>
```

It refuses anything it cannot prove: the workspace `browser.json` must name exactly that endpoint; the unit must exist and its
static definition (unit file, drop-ins, referenced JSON) must name both the profile and the endpoint port; the unit, profile and
endpoint must not belong to another worker. It stores a fingerprint of the unit definition and copies no credential or profile.
Registration takes the same per-worker lifecycle and global allocation locks as a manager action, so it cannot race one or a
second registration (`browser_management_busy` if one is running).
Afterwards `repair`, `restart` and `stop` act only on that exact unit through a fixed `systemctl` argv, re-verifying the binding
and fingerprint first (a changed unit is a conflict), and a missing `browser.json` is restored from the saved record.

**Before promising a restart of an already running agent browser, bind it.** Until then only `status` and a healthy
`ensure` can be offered.

## Installation requirements

* A Chrome: [Qorqbot's own](#qorqbots-own-chrome), downloaded by itself, or Google Chrome installed at
  `/usr/bin/google-chrome-stable`; and the installation's pinned browser runtime prepared. Without them `status` reports
  `unsupported` and `ensure` returns `browser_capability_missing`. On Ubuntu 23.10 or later Qorqbot's own Chrome cannot
  start with its sandbox, so an installed Google Chrome is still needed there.
* User namespaces available to the application user so Chrome's normal sandbox works.
* A running **systemd user manager for the application user**, reachable from the application service: lingering enabled
  (`loginctl enable-linger <application-user>`) and `/run/user/<uid>` available. Otherwise managed `ensure`/`repair` report
  `browser_service_unavailable` naming the user manager; nothing is started inside the application service as a fallback.
* Nothing root-owned is needed for new managed browsers: the application service runs as the configured application user and creates them itself, and the
  application unit itself is unchanged.
* A legacy unit with `--scope user` is controlled with `systemctl --user` in the application user's session. A `--scope system`
  unit needs a noninteractive sudoers rule for exactly `systemctl start|restart|stop <that unit>`; without it the operation
  returns `browser_privilege_unavailable`.
