# Ordubashy terminal control

Optional Qorqut capability package `agent-terminal-control` 1.0.0 lives in this
repository under `plugins/agent-terminal-control`. It grants only registered
terminal operations to Ordubashy; it is not a general-purpose shell/tmux plugin.
No role pin changes or live grant is implied by installing the source.

## Authorization

`agent_terminal_start`, `agent_terminal_restart`, and `agent_terminal_kill` are
allowed for Ordubashy without Signoff (owner decision 2026-09-17); an explicit
`deny` override still blocks them. Other roles are denied. The pinned plugin must
declare the tool and remain enabled. Invocation applies a durable queued action
bound to the observed session; the dispatcher rechecks each target and executes
it once.

For a direct owner command, the authenticated installation owner invokes the
**dashboard-only** `terminal_command_issue` operation with target selectors (below),
`action` (`start`, `restart`, `kill`, or `ping`) and, only for ping, exact `message`.
This creates a ten-minute, single-use command bound to the exact observed target list.
Ordubashy executes the returned ID with `agent_terminal_execute_command`.
`agent_terminal_ping` accepts only a command ID whose action is `ping`.
Agents cannot issue these commands. A free-text transcript or agent-supplied
claim of permission cannot authorize an action. The existing authenticated
operation API exposes command issuance; there is no natural-language authority
extractor or new dashboard button in this change. An owner UI/client must submit
that structured operation to express a direct command.

Direct ping has no unsolicited Signoff proposal path: it needs an explicit
owner-issued ping command. Input broadcasts are now installation-owner-only;
Ordubashy retains inbox broadcasts. Already queued agent input broadcasts are
rechecked and denied by the dispatcher. Raw shell access is unchanged, as requested.

## Starting an agent from Chat

The installation owner may invoke `agent_terminal_start` directly from the
dashboard (owner policy on the global scope allows it; the plugin requirement
applies to agent principals). Chat's **Start** button does exactly that. Nothing
starts an agent by itself: every start is the owner's, from that button or by
typing `claude` or `codex` in the agent's own terminal. The queued action is
dispatched like any other: same rechecks, same idle-shell preflight. Before
typing the launcher, the dispatcher records folder trust for the target's
workspace folder when the owner has consented (`bazarlyk/agent_trust.py`), so the
readiness wait reaches the composer instead of stopping at that folder's trust
prompt. A CLI can still ask about an enclosing git repository, which is never
trusted on the owner's behalf; the readiness wait never types into that prompt, so
the owner answers it in the agent's terminal.

For every agent except Dev, Start also requires the observed idle foreground shell
to be physically in the agent's prepared workspace root (`process_info.cwd` of the
observed foreground process, both paths resolved through symlinks), because that
shell's `claude` and `codex` wrappers apply only there. The check runs before
any trust is recorded, selected conversation resumed, Ctrl-U sent or launcher
typed, and again at the last moment before the resume or Ctrl-U with the same
pane and the same foreground process (a changed process or an unreadable folder
refuses). Another folder is refused with advice to return the terminal to the
agent's folder; Start never changes the folder or types a `cd`. Dev's
owner-project launch is exempt. A person typing into the same tmux pane outside
the terminal lock cannot be excluded.

## Target selectors and batch review

Lifecycle tools and owner command issuance accept any combination of:

- `target_agent_id`: one registered agent UUID (existing callers remain supported).
- `target_agent_ids`: a nonempty list of registered agent UUIDs.
- `business_ids`: a nonempty list of business UUIDs, selecting all active registered
  profiles whose home business is in that list, including profiles with stopped terminals.

Selectors form a union, deduplicated and frozen at proposal/command creation.
At least one selector and 1–100 resolved agents are required. An invalid, archived,
out-of-scope, or self target rejects the whole request, rather than silently skipping
it. Business selection never includes Global agents. There is no blanket “all Codex
processes” selector. To select multiple businesses, pass their explicit IDs.

Example lifecycle request:

```json
{
  "business_ids": ["<business-X-UUID>", "<business-Y-UUID>"],
  "target_agent_ids": ["<additional-agent-UUID>"],
  "rationale": "Restart these business agents as requested by the owner"
}
```

One invocation covers all selected agents and names the resolved targets. Agents
added to a business later are not included. Before application, all frozen targets
must still match; otherwise nothing is queued. Application queues one receipt per
agent with one shared `batch_id`, returned with the records. Before the batch's first
effect, the dispatcher rechecks every queued action's current proposer permission and
target authorization; if any fails (for example a saved action now reaching an owner-only terminal the
proposer may not control), the whole batch fails with no terminal effect. Afterwards each
target is dispatched independently: a changed terminal binding fails only its own action,
and a stale or uncertain target does not replay or cancel completed peers. Call `agent_terminal_status` with only `batch_id` for
per-agent states and details. An execution batch is not an atomic external transaction.

Business-wide direct ping uses the same selectors on **owner-only**
`terminal_command_issue` with `action: "ping"` and an exact message. Ordubashy passes
the returned command ID to `agent_terminal_ping`; it cannot change recipients or copy.
The entire command is consumed once, atomically with all its queued actions.

Preflight still requires compatible terminal states for every selected target:
start requires an absent terminal or idle shell; restart/kill require existing
terminals; ping requires the agent's verified foreground client (Codex or Claude Code). A business selection is
not permission to type into arbitrary shells or to start agents merely to ping them.

## Execution

All tools use registered agent IDs, not caller-provided paths, commands, sessions,
or keystrokes. `agent_terminal_status` exposes the observed terminal and optionally
an action receipt. No terminal text is returned by status.

The native-host adapter fingerprints the tmux server, session, pane and foreground
process start time. A changed profile or terminal invalidates a proposal/command.
Start accepts an absent terminal or shell, uses canonical saved-profile preparation,
clears partial shell input, enters the profile runtime's client (`codex` or `claude`),
waits for its composer, and submits the role command (`$role` plus a second Enter
for Codex, `/role` for Claude Code). Trust/login/preparation failures stop progression.
Restart kills the bound terminal then starts; kill does not infer dormancy from idle
status. Direct ping only accepts the agent's verified foreground client and rejects control
characters. Self-control is rejected.

Before an external effect, the receipt becomes `uncertain`. A crash or timeout never
replays the effect. `completed` means the terminal sequence was submitted/termination
confirmed, not that agent work finished. A fresh owner decision is needed to retry
an uncertain action after inspection. Applied means queued, not executed;
use the receipt. Current role/plugin/scope and terminal binding are checked again
before dispatch. This is not an OS security boundary or protection from simultaneous
manual typing; operators should avoid editing a target terminal during dispatch.

## Release

Merge and deploy only with explicit owner authorization. Migration 065 adds
`terminal_commands` and `terminal_actions`.
Publish the package from the versioned source map, grant its exact published pin
to the selected Ordubashy profile, and reprepare through canonical operations.
No live migration, publication, grant, agent launch, kill, ping, or restart is part
of source implementation. Existing terminal sessions must survive deployment.

Validation uses temporary databases and injected terminal adapters, never real
agent prompts. Tests cover frozen/deduplicated business batches, per-agent outcomes, direct allow policy with role boundaries, owner-only single-use commands, stale
bindings, expiry, plugin revocation, uncertain execution, and the fixed tmux sequence.
