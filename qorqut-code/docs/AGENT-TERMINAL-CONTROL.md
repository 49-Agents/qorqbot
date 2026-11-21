# Ordubashy session control

The capability package `agent-terminal-control` gives Ordubashy six tools to
control the sessions of permanent global workers. The tool and package names keep
the word "terminal" because they are identifiers that stored plans and tool
catalogues use. In managed mode, the default, they act on the agent's managed
session and touch no terminal (see [managed agent sessions](MANAGED-RUNTIME.md)).
In Terminals mode, the legacy option, they act on the agent's tmux terminal (see
[Terminals mode](#terminals-mode)). The installation's mode is asked at every
call. The code is
`app/qorqut/agents/terminal_control.py`; the package source (manifest, tools,
rules, skill) is under `qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/source-instructions/host/app/plugins/agent-terminal-control/`.

## The tools

| Tool | What it does |
|---|---|
| `agent_terminal_status` | Whether the agent is on or off, whether its session is open, and whether it is working. With `action_id` it adds that action's receipt. With only `batch_id` it lists each agent's state in a batch. |
| `agent_terminal_start` | Turns the worker on and opens its session, resuming its selected conversation. |
| `agent_terminal_restart` | Turns the worker on, closes its session, waits for it to close (up to 30 seconds), and opens it again in the same conversation. |
| `agent_terminal_kill` | Closes the agent's session and turns it off, ending active work. The worker stays off until the owner messages it or Ordubashy starts it. |
| `agent_terminal_ping` | Sends the owner-issued direct message. |
| `agent_terminal_execute_command` | Runs a session command such as `/compact` on a worker, or an owner-issued command by its id. |

In managed mode status reports one of `off`, `open` or `asleep`, plus `working` or `idle`, the
client (Claude Code or Codex) and the activity line. A worker is asleep when it
is on but its session is closed, for example after 15 idle minutes. Any input
wakes it, so a sleeping worker needs no start. Idle is not proof of dormancy, so
status never leads to a kill by itself.

## Authorization

Only Ordubashy and the installation owner may use these tools. Other roles are
denied. `agent_terminal_start`, `agent_terminal_restart` and
`agent_terminal_kill` need no Signoff for Ordubashy; an explicit `deny` override
still blocks them. The pinned package must declare the tool and stay enabled.
Invoking a tool applies a durable queued action bound to the observed session.
A dispatcher rechecks each target and runs it once.

Targets are active global workers. Ordubashy cannot target itself. Projects have
no resident agents, so an agent whose home is a project is historical and is
refused.

A direct owner command goes through the dashboard-only operation
`terminal_command_issue`, which takes target selectors, an `action` (`start`,
`restart`, `kill` or `ping`) and, only for ping, an exact `message`. It creates a
ten-minute, single-use command bound to the exact observed target list. Ordubashy
runs the returned id with `agent_terminal_execute_command`. `agent_terminal_ping`
accepts only a command id whose action is `ping`. Agents cannot issue these
commands, and a claim of permission in free text cannot authorize anything. Direct
ping has no proposal path: it needs the owner's explicit ping command.

`agent_terminal_execute_command` also works in one call with `target_agent_id`
and a `command` that starts with `/` (for example `/compact`). It is delivered as
a direct message through the normal input path. A command that only opens a menu
in an interactive client (such as bare `/model` or `/resume`) is refused, because
a managed session has no menu to show. In Terminals mode the command is typed
into the agent's client in its terminal.

## Target selectors and batches

The lifecycle tools and command issuance accept any mix of:

- `target_agent_id`: one registered agent UUID.
- `target_agent_ids`: a nonempty list of agent UUIDs.
- `project_ids`: a nonempty list of project UUIDs. Projects have no resident
  agents, so this adds no targets; an archived or out-of-scope project is
  refused.

Selectors form a union, deduplicated and frozen when the proposal or command is
created. One to 100 agents must resolve. An invalid, out-of-scope or self target
rejects the whole request. Agents added later are not included.

```json
{
  "target_agent_ids": ["<agent-UUID>", "<other-agent-UUID>"],
  "rationale": "Restart these workers as requested by the owner"
}
```

Applying a request queues one record per agent with a shared `batch_id`. Before
the first effect, the dispatcher rechecks the proposer's permission and each
target's authorization. If any check fails, the whole batch fails and nothing
runs. After that each target runs on its own. A changed session binding fails
only that agent's action, and an uncertain target does not replay or cancel the
others. Call `agent_terminal_status` with only `batch_id` for each agent's state.
A batch is not an atomic transaction.

## Execution

This section describes managed mode; Terminals mode follows below. All tools use registered agent IDs. They accept no paths, commands or keystrokes
from the caller.

- **start:** records the intent "on", opens the session and waits for it to bind.
- **restart:** records "on", closes the session gracefully, waits for it to be
  gone, then opens it. If it does not close in time, it is not opened again.
- **kill:** records "off" and closes the session at once.
- **ping and commands:** go through the input arbiter like every input. An asleep
  worker that is on is opened first. A worker that is off is not opened, and the
  message is reported as not delivered.

The intent is written before the effect, so nothing restarts a worker that was
turned off. Before an effect, the receipt becomes `uncertain`. A crash or
timeout never replays it. `completed` means the session was opened, closed or
the message submitted, not that the agent finished work. An action that could not
open the session, or whose message was refused before delivery, ends as `failed`.
A fresh owner decision is needed to retry an `uncertain` action after inspection.
Current role, scope and session binding are checked again before dispatch.

Status returns no conversation text. A plan or command prepared against a
session stays valid until the session or the selected conversation changes; the
turn state does not invalidate it.

## Terminals mode

When the installation runs its agents in terminals (Settings > Agents > "How agents run" >
"Terminals (legacy)", Claude Code agents only), the same six tools and the same
authorization, selectors and batches apply. Only the effect differs, and a plan or
command prepared in one mode never matches in the other.

| Tool | In Terminals mode |
|---|---|
| `agent_terminal_status` | Returns the observed terminal (whether it exists, and the program in front: a shell, the client, or an unrecognized one) instead of the session state. No terminal text is returned. |
| `agent_terminal_start` | Needs an absent terminal or an idle shell. Starts the agent's client in the terminal, resuming its selected conversation, with the agent's stored model and effort, and waits for its input box. |
| `agent_terminal_restart` | Needs an existing terminal. Ends it, then starts the client again as start does. |
| `agent_terminal_kill` | Needs an existing terminal. Ends the terminal. The worker is turned off. |
| `agent_terminal_ping` | Types the owner-issued message into the running client's input box. The agent must be running in its terminal. |
| `agent_terminal_execute_command` | Types the command into the running client, as ping does. |

Nothing opens a terminal by itself and there is no idle close, so an agent that is not
running in its terminal receives nothing until it is started. Start and restart are
refused while the installation is being switched, and one already queued waits. The receipt rules
are the same: `uncertain` before an effect, never replayed.

## Related

To tell a worker something, Ordubashy uses `agents_message_send`; it arrives
through the inbox and, in managed mode, wakes a sleeping worker. Direct messages to a project's
Dev agent are retired. Input broadcasts are installation-owner-only; Ordubashy
keeps inbox broadcasts.
