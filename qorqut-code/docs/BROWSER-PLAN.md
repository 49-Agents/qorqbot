# Browser plan: shared profile, live panel, captcha tool

Status: **plan and work log.** Decisions below were made by the owner on 2026-11-18. Each work item records what was
built and what was verified; an item without a "Verified" line is not done.

Shipped (2026-11-20): work items 1 to 5 are merged into `dev` with PR #110. Sign-in through the live view (under item 2)
is merged with PR #119. "What exists today" below is the state before this plan. The "Not verified" lines are unchanged:
merging did not verify them.

## Owner decisions

1. **Browser first, app later.** The standalone app work is a separate, later project.
2. **Keep our own stack.** The agents keep real Chrome driven by `agent-browser`. Nothing here replaces it.
3. **Ideas only from OpenBot.** OpenBot (nightly-labs/openbot) is licensed PolyForm Noncommercial; Qorqbot sells commercial
   licenses. None of its code is copied, translated or pasted into this repository or into an agent prompt. Behaviour
   described in its public documentation may be used as inspiration and is written here in our own words.
4. **No tab lock.** The agent is not paused or refused while the owner uses the live panel. Consequence accepted: the agent
   keeps running while the owner types, and can act on or read the same page.
5. **Shared profile: yes. Named profiles: no.** An agent's browser is either its own (today's behaviour) or the one Shared
   profile. There is no list of named profiles.
6. **Live browser panel: yes.** The dashboard shows the agent's page live, and the owner can click and type in it.
7. **Managed Chrome download: yes.** Qorqbot downloads and pins its own Chrome instead of requiring an installed one.
8. **Idle tabs unload.**
9. **Cookie banners are refused automatically.**
10. **Captchas are solved for the owner.** The agent's instruction stays one line: if you see a captcha, call the captcha
    tool; if it fails, ask the owner. The tool clicks a checkbox captcha, uses the paid solver (Anti-Captcha) for the rest,
    and the owner is asked only when both fail.
11. **Site terms are the user's business.** How a user points Qorqbot at a website is the user's decision and is not a
    planning constraint here.
12. **Interruptions are acceptable.** The owner is the only user. A feature may pause, stop or restart agents and their
    browsers; avoiding that is not a design goal.
13. **Captcha tool later.** Items 1, 2, 3 and 5 are built first. The solver key is saved in Global Settings as
    `CAPTCHA_SOLVER_API_KEY` (2026-11-18).
14. **Quality over speed.** Nothing here is urgent.
15. **No root step for the downloaded Chrome (2026-11-19).** On Ubuntu 23.10 and later Qorqbot keeps using the installed
    Google Chrome; its own downloaded Chrome is used only where it starts with its sandbox by itself.
16. **Idle limit stays 30 minutes (2026-11-19).**
17. **Captcha tool starts now (2026-11-19).** The Mac check was run the same day; see "Checked on a Mac".

## What exists today

- One headless Chrome per agent, supervised outside the application, with a profile that belongs to that agent and a
  loopback CDP port (`qorqut/browser/agent_supervisor.py`, `agent_browsers.py`; see [management](BROWSER-MANAGEMENT.md)).
- Agents act through `browser_turn` and the pinned `agent-browser` 0.38.1 adapter ([backend](AGENT-BROWSER-BACKEND.md),
  [handles](BROWSER-HANDLES.md)).
- Owner sign-in stops the headless Chrome, opens a visible Chrome on the same profile, and restarts the headless one
  afterwards (`qorqut/browser/sign_in.py`), because Chrome allows one process per profile.
- The dashboard has a browser pane that frames a configured viewer URL (`biz-dashboard/js/agent-browser.js`,
  `GET /api/root/agents/<uuid>/browser-surfaces`); the ordinary managed headless browser has no viewer.
- A captcha solver script exists in an agent's scratch folder (hCaptcha, reCAPTCHA v2/v3 Enterprise; Anti-Captcha by
  default). It was tested only against a local mock. The key is expected in the Settings variable
  `CAPTCHA_SOLVER_API_KEY`, provider in `CAPTCHA_SOLVER_PROVIDER`.

## Work items, in order

### 0. Three tests before building

| Test | Question | Result (2026-11-18, Linux, headless Chrome 153, agent-browser 0.38.1) |
| --- | --- | --- |
| Many agents, one Chrome | Can several `agent-browser` sessions attach to one Chrome and each keep to its own tabs? | **Yes, only with `--pin-tab`.** Without it, a tab that one session opens becomes the active tab of every other session, so the next command of another agent lands on the wrong page. With both sessions pinned: each stayed on its own tab through new tabs by the other and 10 rounds of concurrent click and fill; a session whose tab is closed gets `tab_gone`, not another agent's tab. |
| Stream | Is a CDP screencast of headless Chrome smooth enough to watch and use? | **Yes.** 60 frames a second on an animated 1280x800 page, largest gap 21 ms, about 6 KB a JPEG frame. |
| Input | Do clicks, typing and paste sent over CDP behave like real input? | **Yes.** Mouse click, key-by-key typing, inserted text with non-ASCII characters and Backspace all reached the page, while an `agent-browser` session stayed attached to the same Chrome. |

Not tested: a page-opened popup (the test's `window.open` was blocked), macOS, file pickers, the clipboard of the owner's
computer, and input method editors.

Consequence for item 1: every agent on the Shared browser runs pinned to its own tab.

Further tests for item 2 (2026-11-18, same setup):

| Question | Result |
| --- | --- |
| Can the server ask an agent's `agent-browser` session which tab is its own? | `tab list --json` reports each tab's CDP target id and the session's active tab. **But** asking from a session that has no tab yet opens a stray blank tab, so the panel does not ask. |
| Does a tab that is not in front stream? | **No.** A screencast of a background tab gave 1 frame in 2 seconds; a single screenshot of it works (47 ms). After `Page.bringToFront` the same tab gave 121 frames in 2 seconds. |
| Does bringing one tab forward break another agent's work? | **No.** A click and a snapshot by another pinned session on its now-hidden tab still worked. The hidden page reports `visibilityState` "hidden", so its animations and timers slow down, as in any background tab. |
| Is `/json/list` in most-recently-used order? | **No**, so it cannot say which tab the agent used last. |

Consequence for item 2: the panel talks to Chrome over CDP directly, lists the browser's tabs for the owner to pick from, and
brings the watched tab to the front. It does not know which tab belongs to which agent.

### 1. Shared profile

- One Chrome process runs the Shared profile (Chrome's one-process-per-profile rule); every agent set to Shared attaches to
  it and works in its own tabs. Sign-ins are shared because the tabs share cookies.
- Per agent the choice is "own browser" (default, unchanged) or "Shared".
- An agent on Shared never falls back to another profile: if the Shared browser is missing or broken, the agent gets an
  error, not a different browser.
- Known trade-offs, accepted: agents on one account can interfere with each other; one crash affects every agent on Shared;
  every agent on Shared can reach every site signed in there.

#### Design

How it fits what exists (`qorqut/browser/agent_browsers.py`, `agent_supervisor.py`, `access.py`, `bootstrap/agent_browser.py`):

- **One more supervised Chrome.** The Shared browser is a managed browser like an agent's own: the same supervisor, a port
  from the reserved range 20400-21399, the same ownership record and health check. Its state directory and service unit are
  named by one fixed identifier that is not an agent, so the existing checks that tie a browser to one UUID
  (`load_meta`, `meta_valid`, `unit_name`) are unchanged.
- **The choice is a record, set through `agent_browser_manage`.** Two new actions, `use_shared` and `use_own`, with the same
  authorization as the existing ones (the configured Ordubashy and the owner). `use_shared` starts the Shared browser when
  needed, stops the agent's own browser, keeps its profile and sign-ins on disk untouched, and writes the agent's generated
  `.qorqut/browser.json` with the Shared endpoint. `use_own` reverses it. A hand-installed `browser.json` is never replaced,
  as today.
- **Pinned tabs.** A command of an agent on Shared runs with `--pin-tab`. The agent's first command opens a tab of its own;
  it never adopts a tab that is already open. `tab_gone` reaches the agent as it is, so it opens a new tab.
- **Use lock.** A command of any agent on Shared holds the Shared browser's use lock (`command_use`), so a restart or stop
  of the Shared browser is refused while any of them runs, as for an own browser today.
- **No fallback.** An agent set to Shared whose Shared browser is stopped or broken gets `browser_not_ready`. Nothing
  points it at its own profile or any other browser.
- **Owner sign-in.** Until the live panel exists, the visible sign-in window on the Shared profile stops the Shared Chrome,
  so every agent on Shared waits while the owner signs in.
- **Not hidden from each other.** `tab-list` shows every tab of the Shared browser, and an agent can select another agent's
  tab. Accepted with decision 5.

#### Built (2026-11-18)

- `bootstrap/agent_browser.py`, `bootstrap/browser.py`: `--pin-tab` for a target marked `"shared": true`.
- `browser/agent_browsers.py`: the Shared browser, the per-agent choice, `use_shared` / `use_own`, and routing of the other
  actions and of the command use lock to the Shared browser.
- `browser/access.py`: a command is pinned only when the saved choice and the target file agree.
- `browser/sign_in.py`: sign-in for an agent on Shared opens the Shared profile and pauses every agent on it.
- `browser/operations.py`, tool text: the two new actions. `browser/setup.py`, `transport/http.py`, dashboard
  `js/connectors.js`: the owner's switch in Settings.
- Documented in [management](BROWSER-MANAGEMENT.md#the-shared-browser).

Verified: unit tests with a temporary runtime folder and a stand-in launcher (`tests/test_browser_shared_profile.py`,
`test_browser_shared_target.py`, `test_browser_setup.py`, plus the existing browser tests), and the Settings switch in
headless Chromium (`biz-dashboard/tests/connections-browser.test.js`).

Not verified yet: the whole path on a running installation (real systemd unit, real Chrome, two agents sending
`browser_turn` commands). The real-Chrome evidence so far is the item 0 test, which used `agent-browser` directly.

#### Verified on real Chrome (2026-11-18)

Linux, headless Chrome 153, agent-browser 0.38.1, a throwaway instance with real systemd user units: the assembled service,
`AgentBrowserManager` with its supervisor, `BrowserAccess` and the adapter.

- `use_shared` for two agents starts one Shared Chrome, stops their own browsers and keeps their profiles; both target
  files name the Shared endpoint with `"shared": true`. `stop`, `repair` and `restart` by a member act on the Shared
  browser. `use_own` brings back the agent's own browser with its cookies; the Shared browser stops when the last agent
  leaves.
- Two agents ran 10 rounds of concurrent click and fill (40 commands) and each stayed in its own tab while other tabs
  were opened. A cookie set by one was read by the other. A closed tab gives `tab_gone`. With the Shared browser stopped
  a command gives `browser_not_ready` and no other browser is used.
- Faults found and fixed: Chrome lost cookies set shortly before a stop (it exits on SIGTERM without saving them; it is
  now asked to close over DevTools first); a failed `observe` crashed; the `tab_gone` advice named commands the agent
  does not have.
- Known behaviour: after the Shared browser restarts, an agent's first command gets `tab_gone` and it opens a new tab.

Not verified: macOS; the visible sign-in window (no display); `browser_turn` through MCP authentication (commands were
entered at `BrowserAccess`); the private bind-mount service layout; socket paths over 103 bytes (agent-browser refuses
them; the product relies on a short `XDG_RUNTIME_DIR` or `HOME`).

### 2. Live panel with takeover

- An owner-only stream of the agent's current tab into the dashboard browser pane, with pointer and key input sent back to
  the same Chrome. No second window, no restart, no cookie copying.
- No lock (decision 4).
- Today's visible-window sign-in stays as the fallback for what a streamed picture cannot do: file pickers, passkeys, some
  captchas.

#### Built (2026-11-18)

- `browser/live.py` and the routes under `/api/root/agents/<id>/browser-live` (`transport/http.py`): the tab list, the
  picture stream and the input route. See [management](BROWSER-MANAGEMENT.md#the-owners-live-view).
- `biz-dashboard/js/agent-browser-live.js`: the panel. The Browser tab of a global agent shows it when no viewer is
  configured, and the tab is offered for every agent with a built-in browser.

Verified: unit tests for the module and the route (`tests/test_browser_live.py`); the module against a real headless
Chrome by script (51 pictures in 3 seconds from a tab that started in the background, a click, typing, paste, Backspace);
the panel in headless Chromium with a stand-in stream (`biz-dashboard/tests/agent-browser-live.test.js`: pointer position
mapping, keys, paste, input-method text, wheel, tab switch, closed tab, broken stream).

#### Verified on real Chrome (2026-11-18)

Same throwaway instance, the real route with the owner's sign-in and the dashboard's own JavaScript in headless Chromium:
50 pictures in 3 seconds; a click on the picture clicked the real button; typing, Backspace, Home and select-all arrived;
tab switch, closed tab and Reconnect behaved; leaving the panel or the dashboard closes the server-side view; an agent
command ran while the view was open; for an agent on Shared the panel lists every Shared tab. The route refuses a bearer
token, a missing or foreign Origin, an unknown agent and an unknown tab.

Faults found and fixed: live views stayed open on the server when the dashboard left (the stream's cleanup was cancelled);
the Focus launcher did not offer Browser for an agent with only the built-in browser.

Not verified: macOS; a real keyboard layout, input method and the owner's real clipboard (the non-ASCII key and paste
events were made by script); touch input; file pickers; page-opened popups; how it feels to use.

#### Sign-in through the live view (2026-11-19)

Why: on a server `website_sign_in open` failed with `browser_no_screen` (29 times in 10 days on the owner's installation),
and Ordubashy then asked the owner for the password and typed it into the page itself. With the live view the owner can sign
in in the agent's real page, so no password has to pass through an agent.

Built (branch `feature/browser-sign-in-live-view`, stacked on this one; item 7 of the browser efficiency plan; merged into
`dev` with PR #119):

- Without a screen, `open` opens the site's sign-in address in a new tab of the browser the agent uses (the Shared browser for a
  Shared agent) over DevTools, stops and pauses nothing, and answers `{"opened", "where": "live_view", "agent", "next"}`. With a
  screen the window is as before. Details and reasons: [management](BROWSER-MANAGEMENT.md#website-sign-in).
- `status` lists the site (recorded when its page opens) with `session_saved` meaning cookies only, as before. Qorqbot cannot see
  that the owner has signed in, and the reply tells Ordubashy to wait for the owner to say so and then look at the page.
- The tool description, the managed browser skill's "Website sign-in" paragraph (replaced, not added to: the file is 5
  characters shorter), the connect-tools step of set-up and the owner's guide say: tell the owner to sign in at that agent's
  Browser tab; password or code only if the owner cannot use the live view. `defaults-provenance.json` records the imports, not
  later edits (the skill's hash there already differed before this change), so it is unchanged.

Verified: unit tests (`tests/test_browser_sign_in_live_view.py`, `tests/test_browser_sign_in.py`): no screen gives the reply and
a tab at the sign-in address on the right browser's port (own, Shared, a worker chosen by Ordubashy), no window, no stop, no
`ensure`, nothing paused; a screen keeps the window; another caller, an unknown agent, a browser that is not ready and a tab that
cannot be opened are refused with nothing recorded; `status` and `sign_out` after such an open; the four texts name `live_view`.
The tab opener against a stand-in DevTools socket. Not run against a real Chrome.

Not verified: `Target.createTarget` on a real Chrome (that the tab appears, comes to the front and outlives the connection that
made it); which tab the panel shows when the owner opens it (it takes the first listed page); the whole path from Ordubashy's
call to the owner signing in at the dashboard; whether a login that has only session cookies survives a restart of the browser;
that an agent with no tab of its own yet does not adopt the sign-in tab (`agent-browser` chooses among open tabs). The new
instructions reach an installation only with an instruction update.

### 3. Idle tabs and cookie banners

- A tab idle past a limit is unloaded; its next use loads the page again.
- A page's cookie banner is refused (never accepted) when a reject control is found.

#### Tests before building (2026-11-18, Linux, headless Google Chrome 153, agent-browser 0.38.1)

| Question | Result |
| --- | --- |
| Does headless Chrome treat every tab as visible? | **No, only the front tab.** Front animated tab: 60 redraws a second, 33-44% of a core. Behind another tab: 0 redraws, timers once a second, about 1% of a core, memory unchanged. |
| Freeze the page (`Page.setWebLifecycleState`) | Processor 32% to 0%, memory unchanged, needs an explicit unfreeze. Not an unload. Rejected. |
| Chrome's own tab discarding | No DevTools command; refused while a DevTools client is attached (agent-browser attaches to every tab); the one discard that worked gave the tab a new target id. Rejected. |
| Close the tab and reopen it | New target id, so the pinned session gets `tab_gone`. Rejected. |
| Navigate the same tab to a small page, later go one history step back | **Chosen.** Target id kept; the pinned session's next command works. Unload 18 ms. Processor 33-44% to 1-4%. Memory is freed when Chrome's back/forward cache drops the page (306 MB to 211 MB at 10 minutes). Back within 10 minutes restores the page exactly; later it reloads with typed form values restored and in-page state lost. |
| How to know a tab is idle without asking agent-browser? | agent-browser keeps a `<session>.target` file with the bound tab's id. Its modification time, set before every command, is the "last used" mark. |
| An extension for cookie banners | Google Chrome 153 ignores `--load-extension` in every variant. Chrome for Testing loads it but cannot start sandboxed on Ubuntu 24.04 (item 5). Works on no browser in use here. Rejected. |
| A script registered for every new document | Unreliable: missed about 1 new tab in 4, and the first document on real sites. Rejected. |
| Run the script whenever Chrome reports a document | **Chosen.** 0 misses in 40 local opens; installed on 23 of 23 real sites. |
| Local banner pages (31) | All as expected: reject-all, manage then reject-all, banner in a cross-site frame, open shadow tree, late banner, 14 language variants. |
| False positives | 0 accept clicks and 0 unrelated clicks: accept-only banners, 16 accept-like labels, a Decline beside a cookie-policy link, Reject buttons in an app, a newsletter Decline in an article about cookies were all left alone. |
| Real sites from this server | 6 of 23 showed a banner. Gone with the feature: gov.uk, commission.europa.eu, onetrust.com, cookiebot.com. Left: lemonde.fr and spiegel.de (accept-or-pay walls with no reject control). |

#### Built (2026-11-18)

- Both features run inside the browser's supervisor (`agent_supervisor.attend`), so own and Shared browsers get them and
  they outlive application restarts.
- `browser/idle_tabs.py`: every 60 seconds a web tab that no session used and whose address did not change for 30 minutes
  is navigated to a small "Idle" page that names the address and offers a link to load it again. Before each command
  (`bootstrap/browser.py`) the session's tab is marked used and, if unloaded, loaded again.
- `browser/cookie_banners.py` and `cookie_banners.js`: one DevTools connection per browser follows tabs and frames and
  runs the script in each document. It clicks only a visible control whose whole label is a refusal, inside a box named
  like a consent tool or a small floating box that explains cookies. It opens settings at most once and never saves or
  confirms. No rule list or extension was consulted or copied.
- `scripts/check_browser_tabs.py`: the real-Chrome check for both.

Verified: unit tests (`tests/test_browser_idle_tabs.py`, `test_browser_cookie_banners.py`); the check script against a
fresh headless Chrome 153 with real commands through `bootstrap.browser.run` (idle tab unloaded and keeps its id,
processor 57% to 15% of a core, the pinned session's next command finds its page again with the typed value).

Verified under real supervisor units (2026-11-19, systemd user units, installed Chrome, real adapter, real 30-minute
limit): each unit's log says once that both watchers run; the units are handed the application's session directories;
a reject button is clicked and an accept-only banner left alone on pages opened by agent commands, in an own browser,
the Shared browser and the Shared browser after a member restarted it (also inside a cross-site frame); an idle own tab
was unloaded after 30.3 minutes and an idle pinned Shared tab after 30.8, both kept and both back on the next command
in 0.1-0.2 s with their typed text; a Shared session used at minute 20 and a tab the owner watched the whole time were
not unloaded; a page with a beforeunload handler and typed text was not unloaded and never asked; the owner opening the
live view on an unloaded tab loads it again.

Fixed after an independent review (2026-11-19): the supervisor could not see where the application keeps agent
sessions, so a tab in use would have been unloaded (the launcher now hands the directories over); a page that asks
before leaving froze behind its question (such a page is now left loaded); the live view counts as use of a tab
(a mark in the browser's state folder); a missing dependency in the supervisor is logged once instead of silently
disabling both features.

Not verified: macOS and the folder installation's supervisor; a watched tab in an own browser; a wake after more than a
few minutes unloaded; the live installation's private-mount layout; Qorqbot's own downloaded Chrome; sites as seen from
an EU address. Running browsers get neither feature until they restart.

Known limits: a closed shadow tree, settings that open in another frame, two-step refusal outside en, de, fr, es, it, nl,
and accept-or-pay walls are not handled. A small floating box that explains cookies and holds an unrelated "Decline"
would be clicked. A page reached by a form POST may not come back after 10 minutes. `tab-list` shows an unloaded tab as
"Idle: <title>". The features depend on agent-browser 0.38.1's `.target` file. A command sent from a shell with another
runtime or home directory than the application's does not count as use. A page that always registers `beforeunload`
(many editors and web mail) is never unloaded. A dashboard left open on the live view keeps its tab loaded and in front.

### 4. Captcha tool (later, decision 13)

- One command the agent calls for the current tab. The server finds the captcha, clicks a checkbox type, otherwise calls
  the solver, inserts the answer and reports the result. The key is read on the server.
- Each solve is logged (site, type, outcome).

#### Built (2026-11-19)

`captcha` command inside `browser_turn` (`["captcha"]`, no arguments). Server-side: finds reCAPTCHA v2 (checkbox,
invisible, Enterprise), reCAPTCHA v3, hCaptcha, Cloudflare Turnstile and Cloudflare's own challenge page on the caller's
tab, cross-site frames included; clicks a checkbox with a real mouse click; otherwise asks Anti-Captcha for a token and
puts it into the page; answers solved / not_found / again / failed with a plain reason ("again" is for reCAPTCHA v3,
which needs the form sent once more). The whole command stops after 150 seconds. Cloudflare's own page gets the click
and a wait only. Key read on the server from Settings (`CAPTCHA_SOLVER_API_KEY`); never in a reply, log or error. Every
attempt is logged to `operations/.runtime/browser-captcha.jsonl`. One instruction line in both browser skills: "If you
see a captcha, run `captcha` and do what its answer says; if it fails, ask the owner." Code: `qorqut/browser/captcha.py`,
`captcha.js`, `captcha_solver.py`; described in [control](BROWSER-CONTROL.md).

Verified: 79 unit tests (`tests/test_browser_captcha.py`, `test_browser_captcha_solver.py`) and 34 checks on a real
headless Chrome 153 with local stand-in widget pages and a local mock solver (`scripts/check_browser_captcha.py`).

Real demo run (2026-11-19, owner approved, headless installed Chrome 153, the real Anti-Captcha account): Google's
reCAPTCHA v2 demo was solved by the solver in 22 s for $0.002 and the page answered "Verification Success"; the hCaptcha
demo failed: the click did not pass and Anti-Captcha refused the task (`ERROR_INCORRECT_SESSION_DATA`), so hCaptcha needs a
second provider; on a Cloudflare Turnstile demo with a real site key the click passed in 0.6 s with no solver call (the
page's own server check was not confirmed); Cloudflare's always-pass test key needs nothing.

Not verified before that run: no real widget and not the real solver. Unknown: whether a real checkbox passes on a click, whether sites
accept the inserted token, Anti-Captcha's task names (hCaptcha may no longer be offered there), a real Cloudflare
full-page challenge, headless detection. Needs one supervised run on public demo pages with the real key (three solves,
under two cents). The skill line reaches existing agents only with an instruction update.

Open: a key saved in the shared Global Settings file is also delivered to every agent terminal; the tool reads a
private, server-only setting first. With about seven or more agent sessions attached to one Chrome, pages with
cross-site frames timed out in the check (matters for the Shared browser; not investigated).

### 5. Managed Chrome download

- Download a pinned Chrome for Testing with a hash check into the runtime folder; prefer it over the system Chrome.

#### Built (2026-11-18)

- `browser/managed_chrome.py`: Chrome for Testing 158.0.8127.36 pinned by SHA-256 for linux64, mac-arm64 and mac-x64;
  download over HTTPS, hash check before extraction, safe extraction, publication by one rename into
  `operations/.runtime/chrome/<version>-<platform>/`, a start check with the normal sandbox, then the `current` link.
- `browser/setup.py`: the setup loop and Retry setup start the download in the background with the existing backoff;
  Settings gets `managed_chrome` (ready, downloading, waiting, failed, unusable, unsupported, not_used).
- `browser/agent_browsers.py`: a new browser records Qorqbot's own Chrome when it is installed and starts here, else the
  installed one. An existing browser keeps the Chrome in its ownership record.
- `owner_settings.py`, `transport/http.py`, dashboard `js/connectors.js`: the card shows the download and its failures
  instead of "install Chrome".
- Documented in [management](BROWSER-MANAGEMENT.md#qorqbots-own-chrome), including how to change the pin.

Verified: unit tests with stand-in archives and downloads (`tests/test_managed_chrome.py`: wrong hash, traversal and
link escapes, interrupted install, no-op rerun, start-check failures, pin change) and the Settings card in headless
Chromium. On Ubuntu 24.04: real download, hash check and install in 86 s (208 MB archive, 413 MB on disk), rerun 0.2 s,
a download killed midway left nothing usable. agent-browser 0.38.1 drove the build with the sandbox off for that test
only. A profile written by 158 lost its local storage when 153 opened it (cookies survived); 153 to 158 kept both.

Not verified: the build running with its sandbox anywhere. On Ubuntu 24.04 it does not start ("No usable sandbox!":
AppArmor allows user namespaces only for `/opt/google/chrome/chrome`), so it is installed but not selected and the
installed Chrome stays in use; `--no-sandbox` was not added. Also not verified: macOS (start, Gatekeeper), website
sign-in in Chrome for Testing, a real supervisor on the managed build, a pin change on a running installation.

Decided: no AppArmor step (decision 15). Still open: how often the pin is bumped (there is no automatic update); whether
browsers restart at a pin change.

## Review fixes (2026-11-19)

An independent review of the live view, idle tabs, cookie banners, Shared browser and managed Chrome found defects; all
are fixed with unit tests that fail without the fix.

- The live view hands out a browser's port only while its supervisor reports ready, refuses requests that are not from
  this dashboard, caps open views at 4 per agent and 16 in all with a plain refusal, opens each view inside its own
  stream so a cancelled request leaks nothing, and refuses malformed input with a validation error.
- The live view keeps the newest frame of a burst and shows it once the 20-a-second interval has passed; the dashboard
  counts double and triple clicks itself, types AltGr characters, forwards the wheel only once live, and lets Escape
  pressed twice leave the picture.
- The supervisor and the sign-in window ask Chrome to close over DevTools only when the Chrome they launched owns the
  listener.
- Cookie banners are refused only inside a small box named after a known consent tool, or a small floating box that
  talks about consent; a generic name such as cookie-settings is not evidence, and a refusal link that leaves the page
  is not followed.
- `use_own` and `use_shared` reach the same consistent state from any interrupted move and replace a damaged
  `shared.json`; a website sign-in stays with the browser it started on; a browser command reads its target under the
  use lock; Settings ignores a status read that returns while a choice is saved.
- Qorqbot's own Chrome is never replaced or removed while a browser's ownership record points into it, and a build that
  cannot start here because of a denied sandbox or missing libraries is not retried by the automatic loop until the pin
  changes or the owner presses Retry setup.

## Tried on the owner's installation (2026-11-19)

Commit `f0aaeb8a` of this branch runs on the owner's local installation since 07:57 UTC. All eight agent browsers were
restarted onto the new supervisor; each log says both watchers run. Observed there: the live view lists an agent's
tabs and streams a picture, a cross-site request is refused, Qorqbot's own Chrome was downloaded and correctly found
unusable on this Ubuntu (the installed Chrome keeps being used). A release switch does not restart agent browsers by
itself; they keep their old supervisor code until each unit is stopped once. Not yet observed there: an idle tab
unloaded after 30 minutes, a cookie banner refused on a real site, the captcha command on a real captcha.

## Checked on a Mac (2026-11-19)

Run on a rented Mac mini (Apple chip, macOS 27.2, no Chrome and no developer tools installed) with the Mac app built from this
branch (`1.4.1-browser.2`), started with the account check off, in its own folder beside another test installation.

- **Qorqbot's own Chrome.** Chrome for Testing 158.0.8127.36 (`mac-arm64`) downloaded, passed its hash check and start check, and
  ran every agent browser. Settings showed it as ready with nothing for the owner to do.
- **Stop and start.** A stopped browser left no process behind after 1 second, Qorqbot started it again within its two-minute
  round, and a cookie and local storage set before the stop were still there.
- **Cookie banners.** A banner with a refusal button was refused, a banner with only an accept button was left alone, and a banner
  inside a frame from another site was refused.
- **Idle tabs.** Two tabs left unused were unloaded 30.5 minutes after their last use. The next command on one loaded its page
  again; the other stayed unloaded.
- **Shared browser.** Two workers moved to the Shared browser: their own browsers stopped, one Shared browser started, each had
  its own tab and both saw one cookie. It kept running when the first worker left and stopped when the second did.
- **Live view.** Shown in the Mac app's window; clicks, typing and paste reached the page, with a real mouse and keyboard over
  screen sharing as well.
- **Captcha command.** Run by a real agent (Claude Code as Ordubashy) on Google's reCAPTCHA demo page with no solver key saved:
  it found the captcha, clicked it, and reported that the click did not pass and no key is saved.

**Fixed.** Command-key shortcuts did nothing in the live view on a Mac (select all, undo, redo, cut, copy, jump to the start or
end of a line or document, delete to the start of the line). Chrome on macOS runs them only when the key event names the
editing command, so the live view now names it when the browser runs on a Mac (`live.MAC_COMMANDS`). Plain keys and Option
shortcuts needed nothing. As on other systems, copy and cut inside the page do not reach the owner's clipboard.

**Tests.** The browser tests failed on a Mac because macOS reaches its temporary folder through a symbolic link, which the
browser code refuses; the tests now build their folders from resolved paths. The whole Mac test run passes.

**Seen and left as it is.**

- Agent browsers keep running after the Mac app quits, as agent terminals do, and the reopened app takes them over.
- macOS shows a "Google Chrome for Testing Notifications" notice the first time the downloaded Chrome starts.

## Not in this plan

Tab lock, named profiles, proxies, an embedded (in-app) browser, the standalone app, managed CLI install and the headless
agent runtime.
