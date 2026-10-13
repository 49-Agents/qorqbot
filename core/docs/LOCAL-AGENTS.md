# D62 — Ordinary agents are local folders and tmux shells

Manager decision D62, 2026-10-13. Recorded verbatim from the Manager's repair
handoff (the sentences below are the decision; later sections are implementation
notes for this version).

> Owner explicitly answered: 'Local folders and tmux shells (Recommended)' for
> ordinary agents. This supersedes older VM-per-agent requirements in this
> version. Preserve reserved onbash identity; ordinary local agents share the
> owner's host OS and are NOT security-isolated VMs. Creation prepares a
> distinct workspace, private native HOME and persistent recorded tmux shell. It
> never launches Codex/Claude or chooses their model/effort. User manually
> starts CLI and its startup/inbox path connects that agent's Chat.

The older VM-per-agent wording in [the implementation plan](IMPLEMENTATION-PLAN.md),
[ARCHITECTURE.md](ARCHITECTURE.md), [D36](TERMINAL-OWNED-SESSIONS.md) and the
module plans is historical for this version. Existing evidence stays a record of
what was tested then; it is not rewritten as a pass for local agents. No real VM
executor exists in this baseline, `kind='vm'` is retained only as an unsupported
historical value, and no desktop-isolation, network-isolation or credential-
isolation claim follows from a local agent. Browser attachment, absolute file
references and other onbash-only capabilities are unchanged by this decision.

## Contract

- **Kind.** Every agent created through `agents_create` is `kind='host'`. The
  reserved onbash identity is unchanged: `installation.onbash_agent_id` is
  assigned only by `ensure_onbash`, never changes, and `reserved()` resolves
  strictly through that pointer and validates the target's kind and actor. Any
  number of other `host` agents may exist.
- **Preparation.** Provisioning publishes the owned workspace (unchanged), then
  prepares the agent's own recorded Bash shell under
  `agents/<id>/runtime/` with a private native `HOME`
  (`runtime/home`), `terminal.sqlite3`, `native.sqlite3` and a tmux socket under
  the configured local-terminal runtime root. Preparing a verified shell is the
  only extra effect. No Codex/Claude process, model, effort, login or provider
  setting is started, chosen or copied.
- **Ready is not connected.** The job completes, and `lifecycle` changes from
  `provisioning` to `active`, only after the owned workspace and a bound,
  running, recorder-verified shell are observed. That means *shell ready*; the
  agent has no connected CLI or Chat owner until the user starts a CLI in
  Terminal and its startup/inbox path connects.
- **Never ready.** A recovery hold, an unavailable executable, an ownership
  mismatch, a stopped shell or an uncertain effect leaves the job waiting with a
  typed hold and the lifecycle at `provisioning`. A stopped shell is never
  silently replaced while a native session may exist.
- **Lifecycle record.** The transition advances `lifecycle_revision`, appends an
  attributed `agent.lifecycle_changed` event bound to the provisioning job,
  changes only the untouched initial status `Initializing.` (status revision 1)
  to a concise shell-ready message with an `agent.status_changed` event, and
  queues the generated `agent.json` refresh. Authored statuses, persona and
  memory are never overwritten.
- **Runtime.** The one background service observes a finite snapshot of eligible
  local agents. Terminal state, native setup, session-owner reconciliation,
  native collection and attention are keyed by stable agent ID. One agent's
  failure never aborts another's observation. Each agent has its own runtime
  root, `HOME`, workspace, `terminal.sqlite3`, `native.sqlite3`, tmux socket and
  recorder identity. There is still exactly one logical inbox owner per agent;
  two agents have independent owners. Input and Chat target the agent's owner.
- **Schema 11.** One explicit offline migration rebuilds `agents` to drop the
  single-host unique index and admit `active`, converts this version's legacy
  ordinary `vm` registrations to `host` without touching IDs, actors,
  conversations, workspaces, messages, native bytes, receipts or events, and
  queues a generated-projection refresh for each converted agent. After the
  migration kind is immutable again. Migration runs only through the explicit
  installation workflow (`Application.initialize`, which takes the exclusive host
  lock), never when a server opens. Stop the service and take the usual stopped
  backup of `state.sqlite3` (with its WAL) first; the migration is one
  transaction and a failed run leaves schema 10 untouched.

## Limits

- **Observation budget.** One pass visits each eligible agent once, sequentially,
  and a cached terminal view older than five seconds reads as
  `observation_stale` (attach is refused until the next pass). Each visit holds
  the effect gate and fsyncs a few directories; on a development host at load
  average 25 it measured 0.5–0.8 s per agent, so about six agents is the point
  where views start to age out *on that loaded host*. A calm host is far
  cheaper. The pass cadence is start-to-start one second (a long pass is not
  followed by an idle second). A larger fleet needs the redundant steady-state
  fsyncs skipped or the freshness window widened; neither is done here.
- **Shared effect gate.** Preparing a new agent's shell holds the same effect gate
  as every other terminal effect, so an attach or input for another agent waits
  for it. Measured with a disposable core and real tmux on a host at load
  average about 19: the first onbash viewer attach after `agents_create` took
  about 3.6 s against 0.6–0.9 s afterwards (agent creation itself 0.1–0.2 s). The
  wait is bounded by one shell preparation; a per-agent gate is not built here.
- **Onbash-only capabilities.** Absolute host paths in file references
  (`files/references.py`) and browser attachment (`browser/service.py`) still
  require the reserved onbash identity; an ordinary local agent is refused
  (`unsupported_capability` for browser attachment, `target_changed` for an
  absolute file path, the latter formerly `unsupported_capability`) rather than
  mapped. Opening them to local agents is a separate decision.
- **Isolation.** Ordinary local agents share the owner's operating-system
  account, filesystem, network and desktop. A private `HOME` and workspace
  separate configuration and authored files; they do not confine a CLI's
  commands. No real provider login is exercised by this change, and synthetic
  native fixtures are labeled synthetic wherever they are used as evidence.
