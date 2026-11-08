# Ordubashy durable delegation

This document tracks source implementation of durable, opt-in operational
assignments for ecosystem Ordubashy. It does not activate dispatch, publish or
adopt a role, prepare a live agent, restart a session, or authorize external
sends.

Browser MCP execution uses [short server-bound handles](BROWSER-HANDLES.md);
workers do not carry browser lease IDs/tokens between calls.

## Runtime contract

"Delegate", "worker", "outsource", "helper", "subagent" and "use Luna" mean a
durable Qorqut assignment, including short chores, task updates, briefings and
explicitly delegated browser work. Browser work remains direct when the owner
has not requested delegation. Only an explicit owner request for a "native
Codex subagent" selects Codex's built-in helper runtime; native availability and
Luna configuration never select it automatically. An unavailable dispatcher or
missing worker operation is a blocker, not permission for native/parent fallback.

The parent reads the creation response, then does other work or yields with
inbox delivery active. Completion and blocker events return through that inbox.
No model-driven status polling or empty native wait loops are needed; background
supervisor polling is ordinary code. This does not start a closed parent session.

Workers run on the CLI of the parent Ordubashy session that hosts the
dispatcher: Codex workers under a Codex session, Claude Code workers under a
Claude Code session. For each durable assignment, use a separate root session:

1. Start one worker turn and persist the returned session ID against the
   assignment's current execution generation. Codex: `codex exec --json`, whose
   `thread.started` event carries `thread_id`. Claude Code: `claude -p
   --output-format stream-json --verbose`, whose `system`/`init` event carries
   `session_id`.
2. After that process exits, deliver the next approved assignment step or
   routed event by resuming that session: `codex exec resume --json <thread_id>`
   or `claude -p --resume <session_id>`. A Claude Code resume of a missing session
   fails before `init` and is recreated from the checkpoint like a missing Codex
   thread.
3. Bound each process and turn. Waiting assignments retain their saved thread
   but do not occupy a running process slot. A missing or incompatible thread
   must be recreated from the canonical assignment/checkpoint, not from the
   parent transcript.

Do not use `codex queue` for worker follow-ups: it remains the existing
parent-session inbox delivery path. Durable worker sessions have no parent
inbox watcher, do not run the registered-agent startup hook, and do not inherit
the parent's consumer UUID or current-work declaration.

The runner must use an assignment-only working directory and an explicit CLI
configuration that ignores user configuration and exposes only the
assignment-bound Qorqut MCP connection. Claude Code workers use
`--setting-sources project,local` (no user settings, hooks or memory; the private
folder has no project settings), `--tools ""` (no built-in tools),
`--strict-mcp-config` with one HTTP `qorqut` server authenticated by the same
`mcp_headers` helper as Codex workers, and `--permission-mode dontAsk` with
`--allowedTools` naming only the assignment's operations. They keep the real home
folder for their sign-in and resumable sessions. Claude usage is recorded as all
input (uncached, cache read and cache write) with its cache-read part as cached
input. A Codex worker model is not passed to Claude Code, which then uses the
account's default model; the effort level is passed through. The worker connection is authenticated
with a server-issued, revocable binding; a worker may not select a parent token
or an alternate unrestricted MCP server. Runtime instructions contain only the
saved assignment, allowed scope, current checkpoint, and necessary source
references. The service remains authoritative on every tool call.

## Isolated contract evidence

### Release executable and version pin

Release preflight records the resolved `codex` executable path and complete version output
(verified against `codex-cli 0.159.3`).
The adapter accepts the previously verified `0.155.x` series and exactly
`0.159.3`; other versions remain rejected. The loopback-only delegated-runtime
contract passed on 0.159.3, covering initial execution, saved-session resume,
JSON completion events and scoped MCP without loading the parent connection.
This verification uses synthetic responses and credentials, with no live model
or business operations. It does not claim an owner live acceptance run.

`tests/test_codex_delegated_runtime_contract.py` launches the installed
Codex CLI against loopback-only model and MCP fixtures. In the observed
`codex-cli 0.155.1` runtime, it verifies:

- `exec` returns a persistent thread ID and a completed turn;
- after the first process exits, `exec resume` accepts a follow-up and returns
  the same thread ID;
- both turns use `gpt-5.6-luna` with low reasoning effort, with no Astra model
  request in the fixture;
- user-level configuration contains a deliberately different MCP server, but
  the worker ignores it and discovers only the synthetic worker MCP tool using
  the worker-only credential.

The test requires `QORQUT_TEST_CODEX_DELEGATED_RUNTIME=1`. It is deliberately
not included in the ordinary suite. The isolated test proves the CLI/session
and connection-configuration contract only; it does not prove Qorqut's
assignment authorization, effect receipts, crash reconciliation, event claims,
cancellation fencing, process supervision, role behavior, or cost savings.

The implementation keeps durable dispatch disabled unless the prepared local
registration and the adopted Ordubashy role/capability are both opted in. The
local runner currently accepts only Codex CLI `0.155.x`; preparation can still
report its broader ordinary Codex compatibility baseline, but that is not proof
that durable dispatch is available. Unsupported CLI versions or registrations
do not start a worker and leave saved work queued with an explicit reason. A
saved CLI thread ID is runtime state, not the canonical progress record.

## Core implementation checkpoint

Core migration `078_operational_delegation.sql` now stores revisioned
assignments, exact business/record boundaries, approved plans, worker
checkpoints, wake conditions, runtime generations/leases, hashed subordinate
bindings, and stable action receipts. Parent MCP operations can create, inspect,
revise, pause, resume, or cancel assignments. A bound worker can read only its
assignment and checkpoint/report its own progress. Assignment tools require
the Ordubashy 1.34.0+ pin and the explicit
`manage_delegated_assignments` profile capability; old pins remain gated off.
Migrations `079_delegation_action_attribution.sql` and
`080_delegated_message_attribution.sql` record trusted parent/binding/plan
attribution on committed effects and assignment-linked internal messages.
Migration `081_delegated_event_claims.sql` adds assignment-scoped claims for
exact linked parent inbox events. A bound worker can claim visible events for
its own live generation and atomically save a handling outcome with the parent
delivery acknowledgement; active claims fence parent acknowledgements, and
ambiguous/unlinked events remain in the parent inbox.
Migration `082_delegated_runtime_accounting.sql` accounts active execution time
separately from durable waiting and stores bounded Codex turn/token-usage audit
events.
Migration `083_delegation_failure_retries.sql` preserves claim counts for audit
while bounding only failed or unexpectedly expired executions; normal reply- or
timer-driven wait/resume continuations do not spend the retry budget.

Worker authentication resolves a distinct bearer digest to one live assignment
generation and registration, and rechecks expiry, profile/registration
revision, cancellation, and lease when operations are prepared and applied.
Domain reads require explicit in-boundary record selectors; mutations are
checked again against exact prepared targets. Worker batches, parent state,
owner-only operations, and externally consequential platform actions are
blocked. Transactional record effects mirror the ordinary application
idempotency receipt in the assignment action ledger in the same SQLite
transaction. File effects are not available through this path.

The local adapter is implemented in `qorqut/bootstrap/delegated_runtime.py`
and runs only beside the trusted parent's inbox watcher. The parent remains the
single inbox consumer; the dispatcher claims only assignments whose exact
linked events are visible to that parent, while ambiguous and unlinked events
stay parent-visible. A worker runs at most three Codex CLI `0.155.x`
`exec`/`resume` processes at once, with a private state directory outside the
project tree, a generation-scoped credential, and a minimal configuration
that ignores user/project settings and disables Apps, plugins, shell, browser,
web search, code mode, native subagents, and network-capable filesystem
permissions. It exposes only the required Qorqut MCP endpoint. The child
does not start a watcher or acquire the parent's inbox lease.

Waiting workers exit and release local capacity; a due wake time or an exact
linked event can make the assignment claimable again. The supervisor persists
the Codex thread ID before the first tool call, records bounded turn/token
usage, renews leases, and enforces active-runtime and turn budgets. If a saved
thread cannot be reopened before a new turn starts, it clears that pointer and
recreates a child from the canonical assignment/checkpoint. Process failures
fence the generation and preserve the checkpoint for bounded retry. Stopping
or replacing the parent watcher fences active children so the next trusted
watcher can recover them; this is not a standalone daemon when no parent
watcher is alive. The local state is separated from the project configuration,
but this is not a security boundary against another process running as the
same operating-system user.

`tests/test_delegated_runtime_driver.py` exercises the local adapter using
synthetic Codex processes and fake service clients, including worker
configuration, isolated state, dispatch gating, thread persistence/recreation,
turn accounting, and runtime-budget behavior.

`tests/test_delegated_dispatch_regressions.py` covers scoped configuration and
Focus reads, repeated expected wait/resume cycles versus failure retries, and
the runtime-limit blocked-state/notification path. The new
`tests/test_delegated_assignment_acceptance.py` runs actual application
operations against a temporary SQLite database: a worker reads task
configuration and its assigned task, updates the task, sends an attributed
message, and is interrupted before checkpointing. After bounded recovery it
replays the same task/message action keys without duplicate effects, handles two
routed peer replies across repeated wait/resume generations, updates the task
again, settles both events, and completes. The test asserts three claims but
only one failure retry, plus no parent application operations during worker
execution. It uses deterministic synthetic actions and no model provider; it
does not count provider turns or establish model behavior.

These cases are backend plumbing/recovery evidence, not a live provider or
end-to-end role acceptance run. The implementation has not been activated or
live-trialed; real multi-step role behavior, provider-turn counts, measured
end-to-end cost and live-service recovery remain unverified and need a separately
authorized limited live trial before broad activation.

The remaining native-helper lifecycle test at
`tests/test_codex_subagent_contract.py` already includes the production
activity-hook registration, observes the child completing before the parent
resumes, and asserts that the parent session and current-work declaration stay
unchanged. That is bounded native-helper evidence only, not durable assignment
recovery evidence. On the supported `codex-cli 0.155.1` runtime, its exact
check is `test_native_luna_child_inherits_mcp_without_claiming_inbox` (with the
low/default and medium/explicit-effort cases): a completed Luna child is
observed before `wait_agent` returns, the parent resumes afterward, and the
parent activity/current-work record remains intact. Completion is therefore
not treated as an active helper execution; retained session history is separate
from active execution capacity. This evidence must not be substituted with
the inspected `0.153.4` installation, and it does not authorize a live trial.

The prior focused persistence/application run reported 8 passing tests in
`tests/test_operational_delegation_store.py`; those passing cases were not
rerun. A new node-id-selected case verifies that action receipts retain the
worker binding and plan revision. The store contract covers
scope validation, claim/reclaim generations, lease backoff, waiting capacity,
cancellation, one-time binding authentication, record boundaries, atomic
action receipts, and parent/worker operations. The prior accepted suites and
native lifecycle tests were not rerun. New adapter tests and selected routing
checks were run once. The owner instructs that cases already reported passing
must not be rerun.

## Delivery boundary

Source changes remain on the dedicated task branches until separately merged.
The eventual rollout is opt-in: deploy compatible core and additive schema,
publish/adopt the immutable role pin, prepare only the intended Ordubashy
profile, verify effective parent/worker configuration, then enable dispatch
and run a separately approved limited trial. Rollback pauses dispatch and
revokes worker bindings while retaining assignments, receipts, and unhandled
events. No live activation or rollout is part of this source task.

## Luna 6 migration

Current native-helper and durable-assignment defaults use `gpt-6-luna`.
The live Codex `model/list` catalog verified that identifier and low/medium
support on 2026-09-22. Earlier trial results above retain their original model.
Durable assignments no longer take a model or effort from Ordubashy: the worker
configuration in `qorqut/agents/worker_models.py` always selects `gpt-6-luna`,
with high effort when the assignment grants browser use and medium otherwise;
revisions recompute the effort.

New requests explicitly selecting retired `gpt-5.6-luna` are rejected by the
operation schema and store validation. Existing saved assignments upgrade to
Luna 6 atomically when the dispatcher next claims an eligible execution
(queued, due waiting, or safely recovered expired execution). A `model_upgraded`
event records the transition; IDs, checkpoints, thread IDs, scopes and limits
are preserved. CLI resume receives the new model explicitly. Paused/blocked
work remains paused/blocked until its normal authorized resume. Completed and
cancelled history is untouched. A still-running legacy journal worker can
finish with its existing binding; that compatibility does not authorize new
legacy assignments or remove any document, scope or lease checks.

Rollout: deploy compatible core, publish/adopt Ordubashy 1.36.3 for selected
agents, refresh their prepared Codex config and instructions, and verify native
and durable model selection. Existing native sessions may require a controlled
refresh to reload config; changing source alone does not update them. Pending
prepared requests explicitly naming Luna 5.6 must be re-prepared. No bulk SQL
rewrite, automatic session restart, or schema migration is required.

Native Codex subagents validate names against their startup model catalog.
Refresh the authenticated catalog and restart/refresh selected parents before
activation: Codex 0.155.1's bundled offline catalog can still omit Luna 6 and
return `Unknown model` even though authenticated `model/list` offers it.
The isolated custom-provider native CLI test accepts
`QORQUT_TEST_CODEX_MODEL_CATALOG` pointing to a discovered models JSON fixture,
because its synthetic provider cannot refresh the authenticated catalog. This
is a test input, not a production catalog override or proof of provider inference.


Validation for this migration: 46 store/driver/preparation/routing tests, eight
journal tests, one isolated real-CLI durable resume contract, two isolated
real-CLI native-subagent contracts (low/medium), and ten companion role tests
passed. CLI contracts use synthetic model/MCP servers; authenticated model
availability was checked separately through `model/list`. Journal tests use
an interpreter with the current context-base dependency and the companion
agent-roles worktree. No live business task or agent session was started.
