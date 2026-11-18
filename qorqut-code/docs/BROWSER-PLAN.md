# Browser plan: shared profile, live panel, captcha tool

Status: **plan and work log.** Decisions below were made by the owner on 2026-11-18. Each work item records what was
built and what was verified; an item without a "Verified" line is not done.

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

Consequence for item 1: every agent on the Shared browser runs pinned to its own tab, and the server records each agent's
tab by CDP target id.

### 1. Shared profile

- One Chrome process runs the Shared profile (Chrome's one-process-per-profile rule); every agent set to Shared attaches to
  it and works in its own tabs. Sign-ins are shared because the tabs share cookies.
- Per agent the choice is "own browser" (default, unchanged) or "Shared".
- An agent on Shared never falls back to another profile: if the Shared browser is missing or broken, the agent gets an
  error, not a different browser.
- Known trade-offs, accepted: agents on one account can interfere with each other; one crash affects every agent on Shared;
  every agent on Shared can reach every site signed in there.

#### Design (from reading the current code; not built yet)

How it fits what exists (`qorqut/browser/agent_browsers.py`, `agent_supervisor.py`, `access.py`, `bootstrap/agent_browser.py`):

- **One more supervised Chrome.** The Shared browser is a managed browser like an agent's own: the same supervisor, a port
  from the reserved range 20400-21399, the same ownership record and health check. Its state directory and service unit are
  named for the installation's Shared browser, not for an agent. Today every check ties a browser to one agent UUID
  (`load_meta`, `meta_valid`, `unit_name`), so those checks gain exactly one more accepted owner.
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

### 2. Live panel with takeover

- An owner-only stream of the agent's current tab into the dashboard browser pane, with pointer and key input sent back to
  the same Chrome. No second window, no restart, no cookie copying.
- No lock (decision 4).
- Today's visible-window sign-in stays as the fallback for what a streamed picture cannot do: file pickers, passkeys, some
  captchas.

### 3. Idle tabs and cookie banners

- A tab idle past a limit is unloaded; its next use loads the page again.
- A page's cookie banner is refused (never accepted) when a reject control is found.

### 4. Captcha tool

- One operation the agent calls for the current tab. The server finds the captcha, clicks a checkbox type, otherwise calls
  the solver, inserts the answer and reports the result. The key is read on the server and never reaches the agent.
- Each solve is logged (site, type, outcome).
- Cloudflare's check is not covered by the existing script and is new work.

### 5. Managed Chrome download

- Download a pinned Chrome for Testing with a hash check into the runtime folder; prefer it over the system Chrome.

## Not in this plan

Tab lock, named profiles, proxies, an embedded (in-app) browser, the standalone app, managed CLI install and the headless
agent runtime.
