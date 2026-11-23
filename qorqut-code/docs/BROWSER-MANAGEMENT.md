# Agent browser management

Every agent uses the installation's one **Shared browser**: one Chrome whose website sign-ins all agents share, each agent
working in its own tabs. There is no browser per agent and nothing to choose.

Ordubashy has full control of it: he sets it up, checks, repairs, restarts and stops it with one MCP operation,
**`agent_browser_manage`**, without asking the owner for approval and without sending the owner to Settings. It is routine
maintenance, not a new privilege: it grants no authority to spend, submit forms, act as the owner or touch another service.

Workers never run it. A worker that is not connected to the browser, or whose connection is broken, gets a structured error
(`browser_not_configured` or `browser_configuration_invalid`) telling them to ask Ordubashy; they report it once and keep using
`browser_turn` afterwards. Ordubashy, given the same error for his own tabs, fixes it himself.

## Operation

```json
{"action": "status"}
{"action": "ensure",  "idempotency_key": "setup-1"}
{"action": "repair",  "idempotency_key": "fix-1"}
{"action": "restart", "idempotency_key": "restart-1"}
{"action": "stop",    "idempotency_key": "stop-1"}
```

The operation takes only an action: there is one browser, so there is no agent to name. There is no path, executable,
endpoint, flag, port, service name or shell argument either: the host derives all of them. Mutations need an
`idempotency_key` (1–200 printable characters); `status` needs none and changes nothing.

| Action | Effect |
| --- | --- |
| `status` | Reports `missing`, `healthy`, `stopped`, `busy`, `target_missing`, `conflict`, `invalid` or `unsupported`, the next action, each permanent agent's connection, and what this computer has for the browser (below). |
| `ensure` | What the Settings card's **Retry setup** does. Starts the download of [Qorqbot's own Chrome](#qorqbots-own-chrome) when it is missing; creates the Shared browser when there is none; starts it when it is stopped; connects every permanent agent to it (its record and its generated `browser.json`); leaves a healthy browser untouched. Repeating it never duplicates a browser. |
| `repair` | As `ensure`; also writes the browser's own target file again when it is missing, unreadable or differs from the ownership record, reserves the browser again when the ownership record cannot be read (the profile and its sign-ins are kept), and restarts the browser when it does not answer. A healthy browser is a no-op. |
| `restart` | Restarts the Shared browser, for every agent. Refused with `browser_in_use` while a browser command is running. |
| `stop` | Stops the Shared browser, for every agent. Same refusal while a command runs. Automatic setup leaves a browser stopped this way off until `ensure`. |

`agents` in every answer lists Ordubashy and each active permanent worker with `connected`. An agent that cannot be
connected has a `problem` instead (its folder is not there until the agent has been started once; nothing is written
through a symbolic link). One agent's problem never fails the action for the others. `ensure`, `repair` and `restart`
connect an agent by replacing whatever `browser.json` its workspace had, so an agent that was given a browser by hand
is moved to the Shared browser too; that other browser is not stopped and its profile is not touched.

Through MCP the answer also carries `setup`, the facts the Settings card shows: `tool_installed`, `chrome_installed`,
`own_chrome` (`state` and what it means: `ready`, `downloading`, `waiting`, `failed`, `unusable`, `unsupported` or
`not_used`), `last_failure` when the last setup failed, and `sign_in_window_open` while the owner's sign-in window has the
browser paused (a mutation is then refused with `conflict`). Ordubashy asks the owner only for what these say needs them:
installing Google Chrome where Qorqbot's own cannot run, or restarting the computer when an unidentifiable browser
process holds the profile (`browser_state_unverified`).

A receipt names the action, whether anything changed, the resulting state, that the profile was preserved, a bounded
diagnostic and the verification result. The browser is reported `healthy` only when a real, bounded CDP health check
succeeds *and* the answering process is the exact supervised Chrome that owns the port (recorded boot-bound identities plus
listener ownership); another process answering CDP on that port is reported as such (`stopped`), never adopted, stopped or
called healthy. A failed or timed-out start is reported as a failure, never as ready. No cookies, page content, credentials or arbitrary logs
are returned. A repeated key replays the saved receipt (`"replayed": true`); a restart or stop whose earlier attempt did not
report completion is reported `"outcome": "uncertain"` with the current state and is not repeated blindly.

Only the configured Ordubashy (and the installation owner, outside MCP) may manage. Every other agent is refused; a worker
with an `allow` override is still refused, and an explicit `deny` override on Ordubashy is honored. The agents that are
connected are Ordubashy and every *active permanent global worker*, including workers created later; the Learning Applier,
inactive workers and business agents are not.

## The managed headless browser

The first `ensure` creates the Shared browser, a **headless managed browser**:

* one persistent headless Chrome (normal sandbox — never `--no-sandbox`), loopback CDP only, starting on `about:blank`.
  It is [Qorqbot's own Chrome](#qorqbots-own-chrome) when that is installed and starts on this computer, otherwise the
  installed Google Chrome (`/usr/bin/google-chrome-stable`). The choice is made when the browser is created and saved in
  its ownership record;
* one profile at `operations/.runtime/agent-browsers/da69c2a4-cd9c-5aa7-9a94-176338b13618/profile`, kept private (`0700`).
  That fixed identifier is not an agent and cannot be managed by that identifier;
* a CDP port from the reserved range **20400–21399** (business browsers use 19400–20399). The port is chosen *and* the
  browser's ownership record written as one step under a global allocation lock (released before Chrome starts); a port
  saved in another ownership record, configured in any workspace or deployment file, or with a live listener is skipped. A reservation interrupted before the browser ran is picked up again by the next `ensure`/`repair`;
* a host-owned supervisor (`python -m qorqut.browser.agent_supervisor --state <dir>`) run as a **transient systemd user
  service** (`systemd-run --user --unit=qorqut-agent-browser-<installation hash>-<worker UUID>.service --collect`, fixed argv and
  environment, no shell). It therefore lives outside the application service's cgroup and **survives application restarts and
  releases**. A unit that is already running is reused, never duplicated. The supervisor holds a per-browser process lock, records
  boot-bound identities for itself and Chrome, and publishes `ready` only after the CDP endpoint answers *and* that exact Chrome
  owns the listener;
* the user manager does not share the application service's mount namespace. When the service sees its instance through a
  private bind mount (`BindPaths=`), the supervisor is started with the directory's path on the underlying filesystem and is
  told the path the ownership record was written with (`--recorded-state <dir>`), so both sides read and lock the same files.
  An agent's `upload` names its files to Chrome by the same translation (see Uploads in
  [BROWSER-CONTROL.md](BROWSER-CONTROL.md));
* Chrome's temporary directory is `<dir>/tmp` when its singleton socket path fits a Unix socket address, and otherwise a
  private `qorqut-browser-<hash>` directory under `/run/user/<uid>` (Chrome aborts on a longer socket path);
* a generated `.qorqut/browser.json` (unchanged schema: `schema_version`, `cdp_endpoint`, `runtime_dir`) written only after
  the browser is verified, using the installation's pinned browser runtime.

Ownership (`meta.json`) is written before launch, so a timeout can never lead to a second browser. A random listener on the
reserved port is never adopted; processes are signalled only by their recorded identity, never by port, name or unverified PID.
The supervisor (and the owner's sign-in window) asks Chrome to close itself over DevTools before terminating it, but only when the Chrome it launched owns the port's listener; anything else answering there is left alone. Chrome's profile lock files are never removed. An agent's browser commands re-check the managed identity under the use lock,
so a worker never talks to a foreign process that took over a dead browser's port; it gets `browser_not_ready` pointing to
Ordubashy. After a host reboot (or any other loss) `status` reports `stopped` and `repair`/`ensure` starts the same profile
again. `stop` ends exactly that supervisor (and, only if it ignored the stop, exactly its derived unit).

## How agents use it

* Every agent shares the browser's cookies and website sign-ins. Each agent's commands run pinned to that agent's own tab
  (`agent-browser --pin-tab`): its first command opens a new tab, and a closed tab is reported as `tab_gone`, never replaced
  by another agent's tab. The reply then tells the agent to open a new tab with `tab-new <url>` and continue there
  (`agent_browser.explain`). The tabs are not hidden: `tab-list` shows them all.
* An agent is connected by the record `agent-browsers/<agent UUID>/shared.json` and its generated `.qorqut/browser.json`,
  which holds the Shared endpoint and `"shared": true`. `ensure` writes both. A command is refused
  (`browser_configuration_invalid`) when the record and the target file disagree.
* `ensure` and `repair` end in the same state from whatever step an interrupted run stopped at. A damaged `shared.json` is
  replaced, not trusted. A `shared.json` that is a symbolic link is refused and never written through.
* Automatic setup (at start and in the background) connects every active permanent agent, so a new agent needs no step.
* Results carry `"browser": "shared"`.
* **No fallback.** When the Shared browser is stopped or broken, the agents' commands fail with `browser_not_ready`. Nothing
  points an agent at any other browser.
* A browser the operator once installed by hand for one agent is not kept: see
  [Browsers installed by hand](#browsers-installed-by-hand).
* Owner sign-in in a window stops the Shared Chrome, so every agent waits until the window is closed. A sign-in through
  the live view (see [Website sign-in](#website-sign-in)) stops nothing.
* Settings shows whether the browser is ready and offers Retry setup; there is nothing to choose. Ordubashy does the same
  with `ensure`, so the owner does not have to open Settings.

## The owner's live view

The dashboard's Browser tab of a global agent shows that agent's managed browser live when no viewer is configured for it
(`qorqut/browser/live.py`, `biz-dashboard/js/agent-browser-live.js`). Owner's dashboard only; not an operation.

| Route under `/api/root/agents/<agent UUID>/browser-live` | Does |
| --- | --- |
| `GET` | Lists the browser's open pages (`id`, `title`, `url`). |
| `GET /stream?tab=<id>&width=<pixels>` | Server-sent events: `open` (the view's id), `tabs`, `frame` (a JPEG, its number `n` and the page's size in CSS pixels, at most 20 a second), `closed`, or `refused` (`code`, `message`) when the view could not be opened. `width` (optional) is how wide the dashboard draws the picture, in its screen's pixels: the JPEG is no wider. |
| `POST /input` | `{"view": ..., "events": [...], "order": n}`: pointer, wheel, key and inserted-text events for that view's tab. `order` (optional) is the batch's place among the view's batches, from 1. |
| `POST /received` | `{"view": ..., "picture": n, "width": pixels}`: the dashboard has received picture `n`; `width` (optional) is a new width to draw at. |

* It uses Chrome's DevTools port directly, never `agent-browser`: it opens no tab, holds no lock and does not pause the
  agent. The agent and the owner can act on the same page at the same time.
* Chrome draws only the front tab, so opening a view brings its tab to the front. Other tabs keep working but run as
  background tabs.
* The list shows every tab of the Shared browser, under every agent's Browser tab. The view does not know which tab
  belongs to which agent.
* A stream ends when its tab closes or the connection drops; the dashboard does not reconnect by itself.
* The port is handed out only while the browser's supervisor reports `ready` (the exact supervised Chrome owns it); otherwise the
  route answers `browser_not_ready`. All four routes refuse a request that is not from this dashboard (`Sec-Fetch-Site` other
  than `same-origin`), as the change feed does.
* At most 4 views of one agent and 16 in all are open at once; another is refused with `busy` ("Too many browser views are
  open…") and nothing already open is closed. The view is opened when the stream starts, and opening and closing are one unit, so
  a request cancelled early leaves nothing behind.
* Pictures arriving faster than 20 a second are acknowledged and only the newest is kept; it is shown when the interval has
  passed, so a page that stands still never leaves a stale picture.
* Pictures go at the pace of the owner's connection. A page that keeps moving (a spinner is enough) offers a full picture 20
  times a second; over a slow or distant connection they queued on the way and the owner saw the page seconds late, their own
  typing included. So the dashboard reports each picture it receives, and at most 2 are on the way at once: meanwhile the
  newest waits on the server and replaces the ones before it. A picture not reported within 3 seconds stops holding the next
  one back. A dashboard page that never reports (loaded before this existed) is sent every picture, as before.
* The picture is sent no wider than the dashboard draws it (Chrome scales it; it is never enlarged), and the dashboard lays it
  out at the page's size whatever size it came at. When the stage changes width the dashboard says so and the pictures start
  again at the new width.
* Input does not wait for answers: each batch is sent as it happens, numbered, with at most 4 on the way, and the view applies
  them in their numbered order whatever order the requests arrive in. A batch waits 1 second for a missing earlier one, then
  goes on; a late one is still applied. Pointer moves alone wait until no request is on the way and collapse into the latest
  position meanwhile.
* In the dashboard: a double or triple click is counted by the dashboard (browsers report no count on pointer events); AltGr
  characters are typed rather than sent as Ctrl+Alt; the wheel is forwarded only over the picture once it is live; Escape pressed
  twice within 0.6 s leaves the picture so Tab moves on (the first Escape still reaches the page; the hint is in the note above
  the picture). If finding the agent's configured viewer fails, the error is shown, not the live view.
* Watching is use: a tab with an open view is not [unloaded as idle](#idle-tabs-and-cookie-banners), and opening a view
  on an unloaded tab loads its page again without waiting for it, so the owner sees it load.
* The visible sign-in window stays for what a picture cannot do: file pickers, passkeys, downloads. On a computer without a
  screen it cannot open, and the live view is how the owner signs in.

## Website sign-in

`website_sign_in` (Ordubashy only, MCP) connects the owner's account on a website in the Shared browser, so every agent
has the sign-in. `open` and `sign_out` take a `site` (a domain or its https sign-in address) and nothing else. `status` lists
the sites recorded for the browser.

* **With a screen**, `open` shows a Chrome window on the browser's profile. The owner signs in and closes it; the browser, and so
  every agent, is paused until then.
* **Without a screen** (Linux with neither `DISPLAY` nor `WAYLAND_DISPLAY`, as on a server) `open` opens the sign-in address in a
  new tab of the Shared browser and answers
  `{"opened": "slack.com", "where": "live_view", "agent": "Ordubashy", "next": "..."}`. `agent` is the caller, whose **Browser** tab
  the owner opens in the dashboard ([the live view](#the-owners-live-view)); the site's page is among that tab's pages, and the
  panel shows the first listed page when it opens. `next` carries the instruction: wait until the owner says they are done, then
  open the site and check; ask for a password or code only if the owner cannot use the live view. It adds that every agent
  on the Shared browser shows the same pages.
* **How the tab is opened.** Over DevTools on the browser itself (`Target.createTarget`), as the live view reaches it, and not
  with an `agent-browser` command. Nothing is stopped, restarted or paused, no lock is taken, and `setup` keeps looking after
  the browser. A command would navigate the tab of some session, which may be the page an agent is
  working in. A tab made over DevTools is bound to no session (`idle_tabs`: a session is bound to its tab's id), so the tab an
  agent works in is not touched. As with any open view, a tab nobody uses is unloaded after 30
  minutes and loads again when the owner opens it. The port is used only while the browser's supervisor reports the exact
  Chrome ready; otherwise the answer is `browser_not_ready`, as for the live view.
* **`status` and what "signed in" means.** Qorqbot cannot see that the owner has signed in. It records the site when its page
  opens, so the site is listed with `signing_in` empty (there is no window) and `session_saved` as before: only whether the
  browser holds cookies of that site, which many sites set before login. Settings shows it under "Websites your agents can use
  as you" as "No session" until such cookies appear. The agent is told to rely on the owner saying they are done and on its own
  look at the page. `sign_out` works unchanged on a site opened this way.
* **Session cookies.** The owner signs in in the agent's own running Chrome, so no cookies are copied. The window flow copies
  cookies that end with the browser session into the restarted browser (`lasting_copies`) because it restarts the browser; this
  flow never learns when the owner is done and cannot. A login that only has such cookies may therefore not survive a restart of
  that browser, as when an agent signs in itself on a server; the agent signs in again then. Not tested.
* A repeated `open` for the same site opens another tab and lists the site once. Only Ordubashy may call the tool, as before.

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
* **A build is never removed while a record points into it.** Replacing a damaged build of the pinned version and removing a
  superseded one both leave any build that a browser's ownership record points into (`current` counts as the build it links to);
  an unreadable record leaves everything. A damaged build in that situation is replaced once no record points into it.
* **A computer that cannot start it is not asked every hour.** When the start check fails because of a denied sandbox or missing
  system libraries, `chrome/start-failed.json` records it for this pin and the automatic loop repeats the explanation without
  starting Chrome. Retry setup and a new pin check again; a failure that may pass by itself (not starting for another reason) is
  not recorded.
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

* **Idle tabs.** A web tab that no session used, that the owner did not watch and whose address did not change for 30
  minutes is unloaded: the same tab goes to a small "Idle" page, so its identifier and the agent's binding to it stay.
  The agent's next command loads the page again (one history step back). Within about 10 minutes the page returns
  exactly as it was; later it reloads.
* **What counts as use.** An agent's command: it sets the modification time of its `agent-browser` session file
  (`<session>.target`). The supervisor's home is its browser's state directory, so the application hands each
  supervisor it starts the directories its own commands keep those files in (`QORQUT_BROWSER_SESSION_DIRECTORIES` in the
  unit's environment). A command run from a shell whose runtime or home directory is neither the application's nor
  `/run/user/<uid>` is not seen. And the owner's live view: while a view is open it renews an empty file named by the
  tab in `agent-browsers/<browser>/watched/` about once a minute; the supervisor removes a file older than the limit.
* **A page that wants to be asked before it is left** (a `beforeunload` handler: unsaved work) is not unloaded and not
  asked; it is looked at again after another 30 minutes. Should the question come up all the same (a frame inside the
  page asks), it is answered "stay" so that it cannot block the page, unless `agent-browser`, which answers every
  dialog of a browser it is connected to with "leave", was first; then the tab is unloaded like any other.
* **Many unloaded tabs.** A command of a session that has no tab yet (an operator-installed browser only) loads every unloaded tab again,
  and waits 15 seconds at most for all of them together.
* **Cookie banners.** A banner is refused when it offers a refusal; it is never accepted, and a page without a refusal
  control is left alone. The refusal must sit in a box named after a known consent tool (small), or in a box that floats
  (fixed, sticky, dialog), is small and talks about cookies or consent; a name such as `cookie-settings` or `gdpr` is not
  evidence on its own, so an admin page's "Reject" button is not clicked. A refusal drawn as a link to another address is
  not followed.
* A browser gets both when it next starts. The supervisor's log (the unit's journal; `supervisor.log` in a folder
  installation) says once that both run, or once why they do not (its Python cannot import `websockets`), and the first
  failure of each afterwards.

## Preservation

Profiles, cookies and logins survive `restart`, `stop` and `repair`. There is no delete action: nothing wipes a profile.
The browser's own target file and ownership record are the manager's alone: `ensure` leaves them untouched and reports
`conflict` or `invalid` when they disagree or cannot be read, and only `repair` writes them again. A repaired ownership
record may name a different port; the profile directory is always the same one.

## Ordubashy using a worker's tabs

`agent_browser_manage` only maintains the browser. To work in a worker's tabs, for example to check there that the owner has signed in to
a website (see [Website sign-in](#website-sign-in)), the configured Ordubashy selects it with `browser_turn`: `list` names each active permanent worker's
tabs under the worker's agent ID, and `acquire` with `protocol: "handles"` and that `browser_id` returns a handle bound to
it. Every call with that handle proves afresh that the caller is the configured Ordubashy and that the worker is still an
active permanent worker. Commands hold the browser's use lock like the worker's own, so a restart or stop is refused while one
runs, and artifacts stay in Ordubashy's own folder. No other agent can select another agent's tabs. This is the one place an agent ID still selects anything. The worker's own access
is not paused, so the worker waits while Ordubashy works there.

## Browsers installed by hand

There is no other browser. Earlier versions left alone a browser the operator had installed for one agent (a hand-written
`.qorqut/browser.json`, or a systemd unit registered with `python -m qorqut.browser.agent_binding`). That is removed: such
an agent is connected to the Shared browser by the next `ensure` (automatic setup runs one within two minutes of start),
its target file is replaced, and its old ownership record is ignored. The old browser's service and profile are not
stopped or deleted; the operator disables that service. Sign-ins made in the old profile are not carried over: the owner
signs in again through Ordubashy.

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
