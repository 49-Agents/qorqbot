# Managed agent sessions

Every agent, Ordubashy and each worker, runs as a managed session. Qorqbot
starts the session itself. There is no terminal, no tmux, no agent shell and no
folder-trust step, and nobody types `claude` or `codex` anywhere. The owner talks
to an agent in Chat.

Code: `app/qorqut/managed_runtime/`. Paths below are relative to
`qorqut-code/app/qorqut/` unless they say otherwise.

## Process model

One detached host process runs per open session
(`python -m qorqut.managed_runtime.host --state <folder>`, `managed_runtime/host.py`).
The host owns three things: a driver that runs a headless client, a control
socket, and an event log. Because the host is detached, a service restart or a
release does not end a running turn.

- The service never drives a client directly. `managed_runtime/manager.py`
  (`ManagedRuntime`) finds hosts, checks them and sends them operations.
- Every input still goes through the input arbiter (`native_chat/arbiter.py`).
  The managed adapter in `native_chat/adapters.py` hands the text to the host.
- `managed_runtime/launcher.py` starts the host. On a Linux service install it
  uses a transient `systemd-run --user` unit (named `qorqut-managed-...`), so the
  host is outside the service's cgroup. A folder install and macOS use a plain
  detached process (`start_new_session`).
- The host gets a small allow-listed environment. The agent's own environment,
  with its secrets, travels only in a private `launch.json` that the host reads
  and deletes at once. It never appears in a command line.

### State folder

Each agent has `<instance>/operations/.runtime/managed-agents/<agent_id>/`
(mode 0700):

| File | Meaning |
|---|---|
| `host.lock` | Held with `flock` for the host's whole life. A second host exits with 75. |
| `host.json` | The host's public record: pid, identity, state, session id, transcript path, client pid, client, exit reason. |
| `token`, `control.sock` | Control token and socket (both 0600). A short path elsewhere is used when this one would be too long. |
| `events.jsonl` | Bounded event log with a counter. |
| `sent.jsonl` | Ledger of message ids the host accepted, and ids it refused for good ("fenced"). |
| `host.log` | Host diagnostics. It holds no token, environment value or message text. |
| `launch.json` | Exists only for an instant at start. |
| `options.json` | The agent's client, model and effort (see Launch options). |

### Finding a host again

After a service restart the manager spawns nothing. It scans the folders and
counts a host as running only when all of these hold: its lock is held, its
process identity and command line match `host.json`, the socket peer is that
process, and `hello` with the token returns the same agent and installation.
Anything else is "absent" or "unverified". Nothing is ever killed by name or
port.

### Control protocol

Newline-delimited JSON over the Unix socket (`managed_runtime/protocol.py`).
Every request carries the token, and the host checks the peer's user id.
Operations: `hello`, `status`, `send`, `lookup`, `interrupt`, `answer`,
`command` (`compact`, `model`), `close` (`graceful` or `now`) and `events`.
`status` is state: phase (`starting`, `idle`, `working`, `blocked`, `closing`),
activity, pending questions, the last turn and `usage`, what the context holds
(`{tokens, window, at}`, see [Compaction](COMPACTION.md)). A client that lost an answer to
`send` asks `lookup` with the message id. If the host never saw it, the id is
fenced so a late copy is not typed.

## Drivers

The host picks a driver from the launch spec. Both offer the same interface
(`start`, `send`, `interrupt`, `answer`, `close`) and the same events.

- **Claude Code:** `managed_runtime/claude_driver.py` runs `claude -p` with
  stream-json input and output. Every message carries a `human` origin and an id,
  so mid-turn messages show up in the transcript. A prompt that a hook blocks is
  reported as `prompt_blocked`.
- **Codex:** `managed_runtime/codex_driver.py` runs `codex app-server` over stdio
  (JSON-RPC). It starts or resumes one thread. A message sent mid-turn steers
  the running turn. `compact` and `model` are real app-server calls
  (`thread/compact/start`, `thread/settings/update`), not typed text.
  The driver passes the prepared `.codex/config.toml` and hooks itself, and
  refuses to run if Codex does not enforce the prepared permissions.

`managed_runtime/clients.py` is the one table of what differs between the two:
driver, first message (`/role` for Claude Code, `$role` for Codex), settings
file, and how effort levels are written.

Pinned client versions (`app/tools/native_clis/package.json`): Claude Code
2.1.327 and Codex 0.170.0.

## On, off and asleep

- **Asleep:** no host runs for the agent (never started, idle close, service
  restart, crash).
- **Off:** `native_session_owners.desired_state` is `stopped`. The owner ended the
  session in Chat, or Ordubashy killed the worker.
- **On:** anything else.

A host closes its session after 15 idle minutes (`idle_seconds` 900). Idle means
no turn running, no pending question and no activity for that long. A closed
session is asleep, not off.

### What opens a session

`native_chat/opener.py` is the one opener. Any input to an agent that is asleep
and on opens it: an owner Chat message or upload, a Telegram message, a
delegated task, a report, an agent message, a reminder, an inbox nudge, a
broadcast, an attention delivery and a direct ping. The owner's message is its
own first message. Every other source first sends a short wake notice
(`<qorqut-wake>`), then delivers its input through its own record, so an input is
delivered once.

An agent that is off is opened only by the owner writing to it (Chat or
Telegram, which also turns it on) or, for a worker, by Ordubashy's start or
restart. Idle close, a service restart and a crashed host never turn an agent
off.

At most four sessions start at the same time (`open_busy` beyond that; the
open is tried again). There is no limit on how many are open. One open runs per
agent at a time. Non-owner wakes are paced, and a failing open backs off. When
opens keep failing with `cli_unavailable`, `resume_failed`, `resume_unavailable`
or `driver_unavailable`, `agents/unreachable.py` tells Ordubashy.

Qorqbot's own compaction replaces Ordubashy's conversation through the same
opener (`replace`): close, then a fresh conversation, as one open. It is not an
input source and is not paced or counted. See [Compaction](COMPACTION.md).

### Resume

A new session resumes the agent's selected conversation. The first message of a
fresh session is the role command. If the conversation file is gone and
Qorqbot's archive holds what it said, the open fails with `resume_unavailable`;
the owner can choose "new conversation". A failed resume (`resume_failed`) never
silently starts a new conversation.

## Launch options

`<state folder>/options.json` holds `client` (`claude` or `codex`), `model` and
`effort` (`minimal`, `low`, `medium`, `high`, `xhigh`, `max`), each optional.
The owner sets them in Settings > Agents. The routes are
`GET/POST /api/root/agents/<id>/chat/options` (owner only).

- Without a stored client, the agent uses the client its profile names when that
  client is installed and signed in, else the other one. After the first
  successful open, the client used is stored, so the agent does not change
  client later.
- A client change is refused with `session_open` while the session is open. Model
  and effort apply at the next start.
- Claude Code receives `--model` and `--effort`. Codex receives model and effort
  together; a model without an effort gets `medium`.
- The executable is Qorqbot's own downloaded copy when selected, else the
  owner's copy (`app/qorqut/agent_cli_install.py`).

## Permissions

Permission requests never reach the owner. The host answers each one itself.
It allows when the agent's prepared settings say bypass
(`.qorqut/claude-launch-settings.json` with `defaultMode` `bypassPermissions`, or
Codex `default_permissions` `:danger-full-access`) and denies otherwise. Claude
Code also gets the prepared `permissions.allow` rules as `--allowedTools`,
because an untrusted folder would otherwise ignore them.

Questions an agent asks on purpose (Claude Code `AskUserQuestion`, Codex
`item/tool/requestUserInput`) still reach the owner as question cards. MCP
elicitation requests are declined. The pending list in the host status holds
questions only.

## Chat routes

All are owner only (`native_chat/http.py`): `messages` (opens the session when
none is open), `interrupt` (stop the running turn), `answer` (answer a
question), `close` (end the session and turn the agent off) and `options`.

## Shutdown and updates

- A service stop does not touch hosts.
- `python -m qorqut.managed_runtime.shutdown --instance-root <instance>` closes
  every host of one installation, ends one that will not close (by pid, after
  checking it is the recorded host) and removes stale files. The updater and the
  extension installer run it through `tools/distribution/native_launch.py`
  (`stop_managed_hosts`). Closed sessions open again at the next input.
- The updater refuses while an old tmux server of the installation still
  answers (`require_no_terminal_agents`). Run the cutover first.

## Cutover of an older installation

An installation whose agents still run in tmux moves over with the service
stopped:

```
python -m qorqut.migration.managed_cutover inspect --instance-root <path>
python -m qorqut.migration.managed_cutover apply   --instance-root <path>
```

`inspect` changes nothing. For each agent with a client running in the
installation's own tmux server, `apply` records the client (decided from the
running process), conversation, model and effort in `options.json`, ends the
client with SIGTERM, checks it is gone, ends the agent's tmux session, and
removes the old shell files. `--only <agent id>` limits it to one agent,
`--wait-seconds` sets how long a running turn may finish, `--force` ends a
client that does not stop, and `--dry-run` reports without changing. Database
rows stay as history; the next input resumes the recorded conversation.
The tool goes agent by agent and is not all-or-nothing: if one agent is refused (for example a turn still running after
`--wait-seconds`), the agents handled before it stay ended, and the output says so; run it again to finish. Helpers a client
leaves behind (its MCP bridge, tools) are ended with it, by pid and start time. Codex's own background server (the app-server
daemon) is shared by every Codex window of the same Codex home, so the tool only reports it in `inspect` and in the apply record
and never ends it; it can be left running. Run it
with the same `CLAUDE_CONFIG_DIR`, `HOME` and `CODEX_HOME` the old clients had,
or the conversations are not found. It only touches the installation's own tmux
socket. This is the one place tmux is still named.

### Packages and a start command carried over

After the instruction update and `python -m qorqut.migration.upgrade apply`, and before the service starts, run
`python -m qorqut.migration.package_adoption apply` with the service's own `--workspace`, `--instance-root`, `--component` and
`--extensions-config` arguments (`inspect` first shows the plan). An installation made by an earlier release holds the old role and
plugin packages in its catalog and pinned in its agent profiles; the instruction update replaces the authored text, which agents read
by package id, but not those pins. Without this step Ordubashy has no agent-control tools (`agent_terminal_status`, `_start`,
`_restart`, `_kill`, `_ping`, `_execute_command`) and the role version in an agent's identity is the old one. The step publishes what
the installation lacks, moves each pin forward only, and gives Ordubashy the control plugin; running it again changes nothing.

A start command written for the previous version may still pass `--terminal-workspace`. The service accepts it, ignores it and logs one
warning; remove it from the command at the next convenient restart. The `QORQUT_TERMINAL_WORKSPACE` environment variable is ignored.

## Known limits

- A Codex agent with scoped (not full-access) permissions starts only when
  Codex's own sandbox helper can run. If it cannot, the session fails to start
  with Codex's message. Ubuntu 24.04 restricts the user namespaces that helper
  needs by default, so scoped Codex agents can fail there until the host allows
  them.
- On Linux the client is started so the kernel ends it with its host. macOS has
  no such facility; there only the identity-checked clean-up of the stale record
  ends a client left behind by a killed host.
- A recovery or backup window refuses while any host runs; the owner ends the
  sessions in Chat and tries again.

For the tools Ordubashy uses to control sessions, see
[the session control reference](AGENT-TERMINAL-CONTROL.md).
