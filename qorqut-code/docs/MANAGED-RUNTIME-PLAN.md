# Managed CLIs, browser sign-in and headless agent runtime: plan

Status: **plan only, nothing built** (written 2026-11-18).

Goal: a new owner never opens a terminal. Qorqbot fetches Claude Code and Codex itself, signs the owner in
through the browser, and runs agents through the CLIs' programmatic interfaces instead of the typed TUI in
tmux. The model is OpenBot (nightly-labs/openbot, read at commit `be0223d`).

Out of scope here: the agent browser, the walkthrough rewrite, free or paid tiers, the no-browser
(device-code / paste-code) sign-in variants.

## Step 0: checks before building

Each can change a design choice below.

1. **Download source.** Which npm tarball or release holds the native `claude` and `codex` binaries for our
   pins (`app/tools/native_clis/package.json`: Claude Code 2.1.269, Codex 0.154.0), and that their licences
   allow us to download them for the owner. OpenBot takes Claude from
   `@anthropic-ai/claude-agent-sdk-<platform>` on npm and Codex from its GitHub release.
2. **Headless Claude in a prepared agent folder.** Do our hooks fire, does the MCP bridge load, is the
   transcript written to the same path and in the same record shapes `native_chat/projection.py` parses,
   does `/role` work as a first message, are the launch settings and permission mode honoured, and can a
   message be sent mid-turn?
3. **Headless Codex on macOS.** The same questions for `codex app-server --listen stdio://`, plus the
   `account/login/start` sign-in. `native_chat/codex_control.py` is Linux-only today.
4. **History.** `docs/v1.2/NATIVE_CHAT_CHECKPOINT.md` records that the old SDK chat (`qorqut/global_chat/`,
   the `claude-agent-sdk` pin) was "an independent runner and own queue" and was removed. It does not say
   why; read the git history before building.

### Step 0 results (2026-11-18, Linux only; nothing run on a Mac)

1. **Download source: settled.** Both pins exist on npm with native binaries for darwin-arm64, darwin-x64 and
   linux-x64 (`@anthropic-ai/claude-code-<platform>@2.1.269`, `@openai/codex@0.154.0-<platform>`). Codex is
   Apache-2.0 and is a folder of files, not one binary. Claude Code is "all rights reserved", subject to
   Anthropic's legal page: unmodified binary, each user signs in with their own account.
2. **Headless Claude: passes** (11 small real turns, on 2.1.327 and on our 2.1.269 pin). Workspace
   preparation can stay unchanged: all eight hook events fire with the same input, the Stop-hook block
   continues the turn, hook context reaches the model, `.mcp.json` and the permission rules are honoured,
   `--settings` works, `/role` works as a first message, there is no trust prompt, and the transcript has
   the same path and record shapes (the real `projection.py` parsed it). Step 3 must:
   - stamp `origin: {kind: "human"}` and a `uuid` on every message sent, or `projection.py` drops mid-turn
     messages;
   - treat a prompt blocked by a hook as its own outcome (the result still says success);
   - set the SDK system prompt to the `claude_code` preset (the SDK default removes it) and point it at our
     binary with `cli_path`;
   - re-check MCP approval: headless loads `.mcp.json` servers that are not listed as enabled.
   Not exercised: our real hooks and MCP bridge, long-lived sessions, a larger model than Haiku.
3. **Headless Codex: partly checked.** No model turn ran (the account's allowance is used up until
   2026-12-15), and the check ran on Codex 0.167.1, not our 0.154.0 pin. Confirmed without a model: the
   protocol (schema generated from the binary, including `account/login/completed`), project config and MCP
   loading, rollout path and resume, steer and interrupt on a shell turn. Step 3 must:
   - pass folder trust and each hook's `trusted_hash` as config (the TUI's hook-trust bypass flag does not
     work with app-server); hashes change whenever `hooks.json` is regenerated;
   - preflight the scoped permission profile (the sandbox helper failed on the test host);
   - put a timeout on `turn/interrupt` (it never answers for a finished turn);
   - raise the Codex pin and re-check on it.
   Not seen: assistant-reply and MCP-call records, the Stop and tool hooks, approvals.
4. **History: no stated reason found.** The removed runner's source is not in this repo's history. Surviving
   docs show it was a second path (own queue, own message store; an agent was runner-owned or
   terminal-owned). Recorded owner decisions that this plan supersedes for managed agents:
   `docs/agent-platform/00-DECISIONS.md` ("Agents are Claude Code or Codex sessions in mirrored terminals…
   the ability to start plain `claude`") and commit `4bd19dae` ("Never start an agent by itself"). Kept from
   them: the terminal path stays available per agent, and opening Chat starts nothing. Chat needs
   permission and question cards again.

### Build status

Steps 1 and 2 are written in the worktree, uncommitted, tested against fakes only (2026-11-18). An
independent review raised 15 findings; all were fixed or shown not to apply, and the focused tests pass
(220 app tests in a scratch venv, 71 launcher tests, 44 dashboard tests).

- Installer: `app/qorqut/agent_cli_install.py`. The launcher (`native_launch.py`) no longer downloads; it
  only finds an installed copy. The owner's own CLI counts only at or above the pinned major.minor.
- Sign-in and status: `app/qorqut/agent_cli_sign_in.py`, `/root/agent-clients` (route tested through the
  real dispatch), dashboard rows in `biz-dashboard/js/agent-clients.js`.
- Launch sites: `app/qorqut/agent_cli_launch.py` puts the selected copy first on PATH for new agent
  terminal sessions, the Codex queue, delegated workers, the Context runner and the version probes, and
  turns the updater off only for our copy.

Not verified: anything on a Mac (Keychain read, Mac binaries, process names); a real sign-in for either
provider; a real download followed by a real new terminal session; the zsh branch of the shell wrappers;
the Claude "browser did not open" fallback (the CLI's wording is a guess); release audit allow-list for
the new modules. A terminal session that was already open keeps its old PATH until it is reopened.

Step 3, Claude side, is written in the worktree, uncommitted (2026-11-18). Design and work packages:
`docs/MANAGED-RUNTIME-STEP3-DESIGN.md`; the code is the contract where the two differ.

- `app/qorqut/managed_runtime/`: `claude_driver.py` (drives `claude -p` over stream-json, no Agent SDK),
  `host.py` (one detached host per open session), `launcher.py`, `client.py`, `protocol.py`, `state.py`,
  `manager.py`, `mode.py` (per-agent `terminal` or `managed`, default `terminal`).
- `ManagedAdapter` in `native_chat/adapters.py`; binding, liveness, schema migration
  (`migration/native_transport_managed.py`); Chat routes for interrupt, answer, close and runtime; dashboard
  Stop, activity line, permission and question cards, runtime switch.
- Open flow: only an owner Chat message opens a session. Claude Code announces its session only after it
  takes up a first message, so the open flow hands the first message to the host and binds afterwards.
- Two independent reviews and one real run (host + driver + `claude` 2.1.327, five small Haiku messages)
  were done; their findings were fixed. Focused tests pass together: 779 app tests, the launcher tests, 96
  dashboard tests.

- Real service run (2026-11-18, Linux, throwaway instance on its own port, `claude` 2.1.327, Haiku, 16
  turns): all eight checks passed after one fix. Boot and migration on a fresh store; runtime switch; first
  message opening a session with the real hooks, binding as `managed`, reply in Chat 11.6 s after the
  POST; MCP bridge and `/role`; idle message, mid-turn message, Stop on a tool-held and a hook-held turn,
  a real question card; broadcast and inbox nudge while open and after close; close, resume without
  resending the role, service restart with a running host, idle close; switch back to `terminal`. The fix:
  the "terminal present" check always refused for managed agents (`agent_terminal_control.terminal_selected`).

Not verified for step 3: a permission card and a hook-blocked prompt on the real service (both seen only
at host level); Telegram and Signoffs on a managed agent; `/compact`; anything on a Mac; the systemd launch
path; the dashboard in a browser; a model larger than Haiku. To look at: the managed `claude` inherits the
machine user's whole environment through the existing resolver; `claude` exits with code 1 on idle close. Known gaps: Claude's own helper processes can outlive a
hard-killed host; a terminal opened at the same instant as a switch to `managed` is only caught afterwards;
reverting the migration is refused once a managed session has history. Codex side not started.

## Step 1: managed CLIs

- `ensure_claude` and `ensure_codex` beside `ensure_agent_browser` in `tools/distribution/native_launch.py`:
  pinned URL, hash check, size limit, native binary only. Files go to `<root>/tools/claude-<version>/` and
  `<root>/tools/codex-<version>/`.
- Resolution order: an explicit override variable, then our copy, then the owner's own CLI if it meets a
  minimum version. The owner's install is never modified.
- Our copy never updates itself (the CLI's updater is turned off for it). Its version moves only with a
  Qorqbot release, which keeps `bootstrap/features.py` and the composer recognizers valid.
- The download reports bytes so the dashboard can show progress, and a failure gives a plain reason and a
  retry.
- Done when: a clean Mac with no CLI installed reaches "installed" with no terminal.

## Step 2: sign-in and status

- **Claude sign-in.** The server spawns `claude auth login --claudeai` with no terminal attached. The CLI
  opens Anthropic's page in the owner's browser, receives the result on its own local listener, stores the
  credential and exits. We wait up to 10 minutes; Cancel stops the process. One sign-in per provider at a
  time; a second start replaces the first.
- **Codex sign-in.** Start `codex app-server`, send `initialize`, then `account/login/start` with type
  `chatgpt`. Open the returned URL in the browser, wait for the login-completed event, confirm with
  `account/read` (account type `chatgpt`). Cancel sends `account/login/cancel`.
- **Status.**
  - Claude: read the stored credential directly: on Mac the Keychain item `Claude Code-credentials`
    (via `/usr/bin/security`), elsewhere `~/.claude/.credentials.json`; signed in means a refresh token that
    has not expired. Email and plan come from `~/.claude.json`. Fall back to `claude auth status` only when
    an API-key or backend variable is set or the store cannot be read. OpenBot reports that `auth status`
    can spend the refresh token and leave the owner signed out (anthropics/claude-code#95822, #105645;
    unverified by us).
  - Codex: `account/read`.
  - This replaces the `claude auth status` call in `app/qorqut/owner_settings.py` (`agent_clients_view`).
- **Endpoints.** Owner-only, dashboard-identity-only, modelled on `/root/browser-setup`: one read
  (installed, version, source, signed in, account, download progress, pending sign-in) and one action
  (`download`, `connect`, `cancel`).
- **Dashboard.** One row per provider. The button moves through Download, Connect, Ready. When blocked, the
  row says why. Shown after account sign-in and in Settings.
- Done when: sign-in completes from the dashboard on Mac, and status is still right after a restart.

## Step 3: headless runtime

What stays: workspace preparation (`bootstrap/preparation.py`). Settings, hooks, MCP config, skills and
instructions are files the CLI reads from its folder, so a headless session in the same folder should pick
them up (check 2 confirms). The operations API, Signoffs, inbox, transcript archive, projection, overseer
and learnings are unchanged.

- **Adapter in the input arbiter.** A third adapter, `managed`, beside `TerminalAdapter` and
  `CodexQueueAdapter` in `native_chat/adapters.py`. Chat, Telegram, nudges, broadcasts, ping and the
  recovery wake keep going through `InputArbiter` unchanged. This avoids the independent-runner shape that
  was removed before.
- **Claude driver.** The Python Claude Agent SDK pointed at our managed binary, streaming input so a
  message can arrive mid-turn. Fallback: the raw `claude -p --output-format stream-json` form already used
  in `bootstrap/delegated_runtime.py`.
- **Codex driver.** Extend the app-server client in `qorqut-context/qorqut_context/runner.py` with
  `thread/start`, `thread/resume`, `turn/start`, `turn/steer` and interrupt.
- **Session lifecycle.** A session opens on the first message, closes when idle, and resumes by session id.
  The role bootstrap (`/role`, `$role`) is sent as the first message. No folder-trust prompt is involved;
  `agent_trust.py` stays only for the terminal runtime.
- **Surviving restarts.** tmux keeps agents alive across a server restart today. Each managed session runs
  under a small detached host process, the pattern `browser/agent_supervisor.py` already uses, so a release
  does not kill a running turn.
- **Session identity.** `native_chat/probe.py`, `sessions.py` and `liveness.py` verify sessions through
  tmux pane ancestry today. They get a `managed` case where Qorqbot owns the process.
- **Delivery results.** A real turn id and status replace paste-and-verify and most of its `uncertain`
  outcomes. Telegram's and the nudges' reason codes (`terminal_absent`, `terminal_unverified`, …) need
  managed equivalents.
- **Chat.** A Stop button (interrupt), and an activity line replacing what the terminal showed. Slash
  commands from `native_chat/commands.py` are mapped per driver.
- **Rollout.** A per-agent switch, `terminal` or `managed`. One worker first, then Ordubashy. The terminal
  code (`pty_broker.py`, `agent_terminal_control.py`, `terminal_composer.py`) is retired only after that.
- Done when: a new owner signs in, says hello and gets a reply, with transcripts, Signoffs and Telegram
  working on a managed agent.

## Open decisions

1. **When an agent is "on".** Proposed: on from the owner's first message until the owner presses Stop;
   while on, nudges and Telegram may reopen an idle session. Alternative: only an owner message reopens it.
2. **Provider terms.** Anthropic's legal page (read 2026-11-18) says products using the Agent SDK "should
   use API key authentication" and that third parties may not "offer Claude.ai login into their own
   applications". OpenBot rates its own subscription path "not clear". OpenAI's wording on app-server
   sign-in for commercial products was not confirmed. Keep the sign-in method per provider switchable
   (subscription or API key).

## Order

1. Step 0 checks (parallel).
2. Step 1 and Step 2. They work with today's terminal runtime and can ship alone.
3. Step 3, once checks 2 to 4 pass.
