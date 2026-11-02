---
name: bazarlyk-role-operations-delegation
description: Create and supervise an explicitly selected legacy delegated assignment, a saved plan run by a durable Qorqut worker. Ordinary requests to hand work to a worker go to the permanent team; native Codex subagents require an explicit owner request for that runtime.
---

# Legacy delegated assignments

## Scope and authority

This procedure applies only when a legacy delegated assignment is explicitly selected,
or to supervise one that already exists: a saved plan run by a durable Qorqut worker
created with `delegated_assignments_create`. Ordinary requests to delegate, use a
worker or hand work over go to the permanent team in the Ordubashy brief; nothing here
redefines them. The parent agent is this main session: Ordubashy. The parent agent
delegates only its own work and owns its conversation with the owner, strategy,
priorities, plan approval, consequential decisions and escalations. It defines each objective, exact project or Global scope,
plan, permitted operations and records, internal recipients, constraints, resource
bounds, completion evidence and escalation conditions. Delegated workers execute within
that plan and may make routine decisions and recoverable retries inside those limits.
They do not get authority from being assigned a task or receiving a message.

## Selected assignments

A saved assignment may cover a short isolated chore as well as continuing work. Batch
related microsteps into one outcome; use one writer for overlapping records or Focus
ordering. Selecting one does not change anyone's responsibility or expand authority for
a task.

Use a native Codex subagent only when the owner explicitly requests a
"native Codex subagent" for that task. Availability of native tools or an older
example is not that request. See the explicit
native exception below; never silently fall back to it.

Browser work remains direct by default under browser-control. Generic delegation
guidance alone does not change the direct browser default or authorize a new
browser task; a native Codex subagent needs the owner's explicit request.

If durable dispatch or a required worker tool is unavailable, preserve the
canonical task/plan and queued assignment, report the blocker once, and wait
for a decision or recovery notification. Do not substitute native helpers,
launch a shell worker, broaden grants, or execute the delegated task yourself.

Durable dispatch is available only when all runtime gates are satisfied:
Ordubashy role version 1.34.0 or later, the explicit
`manage_delegated_assignments` capability, a prepared registration with durable
dispatch enabled, and a compatible Codex CLI 0.155.x runtime attached to
the parent agent's trusted inbox watcher. A source branch or queued assignment does not
prove these gates are active; inspect the returned assignment state and runtime
reason. Unsupported or unprepared runtimes leave work queued. The child gets a
separate generation-bound registration and only the required Bazarlyk MCP
connection; it does not start a watcher, own the parent agent's inbox, or spawn nested
helpers.

For saved assignments, `allowed_operations` lists only project operations; it
may be empty for a self-contained assignment. Worker get/checkpoint/event tools
and `browser_turn` are supplied automatically (the complete inbox lifecycle
requires Ordubashy 1.36.3 or later). To authorize browser work, list
`browser_use` and bind the exact `browser_key` in scope; do not list the automatic
control tools. Omitted `business_id` means Global.

## Browser delegation

For a selected legacy browser assignment, use one durable assignment with the
authorized browser scope, `browser_use` grant,
browser-control instructions, target URL/tab, permitted changes, completion
criteria and stop conditions. `browser_turn` is supplied automatically. Use the
assignment-bound browser path below; no native spawning/discovery is needed.
The worker retains its checkpoint and queues for exclusive access. The parent agent does
not acquire Chrome on its behalf or poll while it waits.

Without browser delegation, execute directly via authenticated `browser_turn`
using browser-control. Carry forward existing owner authorization; do not ask
again for actions already permitted. A URL supplied as context alone does not
authorize Chrome control.

## Explicit native Codex subagent exception

Only after the owner explicitly requests a native Codex subagent, inspect the
native tools exposed in the current Codex
turn and their schemas for an available helper-start capability. Native
delegation tools belong to the Codex runtime; they are not Qorqut MCP operations.
MCP `tools/list` and `operations_describe` discover authorized Qorqut operations
such as `browser_turn`, but cannot establish whether native helper spawning is
available. The prepared Codex `[agents]` settings enable native helper defaults; settings
alone do not prove that spawning is exposed or working. Do not guess a tool name
or search the parent agent's or home instructions to discover a missing capability.

For an explicitly requested native browser subagent, read browser-control and pass its full
instructions or its accessible prepared path, the parent agent's registered workspace,
project scope, target URL/tab, permitted changes, completion evidence and stop
conditions. Carry forward the owner's exact request and existing authorization
without asking again. Require the subagent to check its own result before reporting.

For this explicit native exception, if the native tool surface cannot be inspected, report that native delegation
availability could not be verified. If inspection confirms no helper-start
capability, give one concise blocker naming native delegation as the missing
prerequisite and distinguish it from MCP/browser permission or target status.
Do not poll, hunt for instructions, launch a shell helper, use owner HTTP, or
silently replace an explicitly requested helper with direct execution. The external coding-agent
`control-via-API.md` guide describes a different caller identity; it is not
registered-agent setup or a fallback. Keep prepared identity and authenticated
MCP authoritative.

Native browser helpers use the authenticated `browser_turn` MCP tool, calling
`acquire` with `protocol: "handles"`. `consumer_id` is optional: it is only a
caller-chosen UUID that keeps separate browser tasks apart, is never verified and
does not prove a native session identity. Use the value the parent agent supplies
in the handoff, if any; never look up or hunt for a session UUID to fill it. Use the returned
short `handle` for commands and other follow-up calls; it never expires. Do not use the shell launcher or attempt to read its
credential. Missing MCP access or an unavailable configured target is a blocker.

For native helpers, project context is NOT a browser selector: omit `business_id`
and `browser_key` from every `browser_turn` call, even when the task concerns one
project. The server selects the parent agent's configured Chrome. Pass this
rule explicitly in the handoff alongside browser-control. For example:
`{"action":"acquire","protocol":"handles","consumer_id":"<optional-caller-UUID>","idempotency_key":"<unique-key>"}`.
Project operations still use their required project scope. A project-specific
browser requires a separately scoped durable assignment, not an added selector
on a native call. A selector validation error is not evidence of missing grants:
correct the arguments for the intended configured browser; do not repair
credentials, switch targets, or send status/checkpoint calls without a valid turn.

## Durable browser execution and shared boundaries

Schedule one browser operator at a time per configured browser, including the parent agent.
The parent agent's own commands are lease-free and not queued, so do not drive Chrome
while a durable worker runs. Durable workers own a persisted queue with a bounded lease: an
assignment keeps its turn for the whole browser task, waiting releases it, and
the runtime wakes the next eligible assignment. Independent task/record work
continues in parallel. Batch page steps for one outcome; the delegated worker
handles navigation, fields and routine recovery within the plan without the parent agent
approving each click. Follow browser-control for attach, current page references,
uncertain submissions and detach. Return the resulting URL/state, verification,
changed record IDs where relevant and unresolved work. A click alone is not
completion evidence.

Durable workers have only assignment-scoped Bazarlyk operations plus the
assignment-bound `browser_turn` MCP operation: no shell, web search, Apps, other
MCP servers, writable local files or nested helpers. The operation accepts
`acquire`, `status`, `command`, `checkpoint`, `renew`, `artifact`, `reconcile`
and `release`. Acquire with `protocol: "handles"` returns a short `handle`; pass it on follow-up calls, with
no lease IDs or tokens. A waiting acquire returns no usable handle. After resume,
acquire a fresh handle; never reuse one for commands from an earlier generation.
The same assignment may use an old handle only to reconcile its uncertain action.
The server resolves only the exact project/configured browser key saved
in the assignment. Browser steps run directly when the worker's turn arrives,
without a handoff from the parent agent. A queued acquire persists a browser wait, releases
execution capacity, retains FIFO position and is resumed by the dispatcher after
release; the parent agent does not poll it. Every command records intent before execution,
returns structured output, and requires reconciliation after uncertainty before
retrying. Read current state/revisions first; never restart cancelled/revoked
work or assume a note automatically wakes a worker. The parent agent accepts the completion
report and does not reread records or reproduce browser checks.

Native tool visibility does not expand assignment authority. Workers and helpers
may not consume the parent agent's inbox, contact the owner, decide Signoffs, change grants,
start watchers, spawn helpers or take unrelated external actions. Pass needed
task context rather than private owner conversation or credentials.

## Create and supervise a saved assignment

Before creation, refresh the relevant agent and project configuration and read
canonical records. Confirm current scope and grants. Do not copy credentials,
assume a missing record is permission, or put project facts in orchestration
state. Keep Task completion and facts in their existing canonical records.

Create one assignment around a meaningful outcome. Its saved plan should name
ordered steps, allowed routine choices, exact record/source boundaries,
internal recipients if supported, constraints, explicit stop/wake conditions,
success evidence, retry/action/turn/time budgets, and escalation rules. Grant
only installed operations necessary for the plan. Do not include batch,
Signoff, owner-only, platform-action, grant-management, or external-send
operations. Avoid overlapping writers; serialize Focus ordering and shared
record updates.

If native spawning reports `Unknown model`, report the stale startup model catalog
and request its refresh; do not substitute another model.

Qorqut's worker configuration sets every delegated worker's model and reasoning
effort, with more effort for browser work. Do not choose, request or change them,
including after a failure.

After creation, inspect the returned assignment, state, `dispatch_reason`, plan
revision, project/scope and operation boundaries. A queued item is not proof of
dispatch. If a reason is present, keep it queued and report that reason once. If
it is queued without a reason, say it is awaiting execution until the runtime
reports a live worker. Do not poll repeatedly or duplicate the assignment.

After the creation response, continue other authorized work or end the turn
quietly with inbox delivery active. Completion and blocker notifications bring
the result back to the parent agent. No periodic status reads, sleep/check loops,
native wait loops, or unchanged progress messages. Inspect compact status only
when the owner asks, an actionable event arrives, or a concrete recovery issue
requires it. Accept the worker completion report without repeating its work.
Revise only with the expected plan revision;
a changed objective or authority boundary requires the parent agent to approve a new plan.
Pause or cancel when directed. A paused, cancelled, revoked, archived or
superseded assignment must not be restarted by a worker or recovery process.

## Delegated responsibilities

Every responsibility below, run as a selected legacy assignment, uses a durable
Qorqut worker, including a single small task adjustment. "Tell X to do Z" uses a
bounded assignment to resolve the
authorized recipient, send one internal message with a stable action key, and
report its delivery receipt. Existing task ownership stays intact.

**Project briefings.** Collect current authorized source records, compare
progress with the requested period/outcome, and prepare an evidence-linked
briefing with uncertainties and proposed next steps. Keep source facts,
interpretation and proposals distinct. Use existing authorized note/document/
plan mechanisms. Never rewrite platform instructions, role briefs or project
strategy. The parent agent owns strategic interpretation and changed direction.

**Task adjustments.** Check for duplicates, read task configuration and current
assignees, then make only the permitted create/assignment/status/date/note
changes. Preserve unrelated fields, owner holds, review obligations and
assignees unless the plan explicitly changes them. Keep Global tasks in Global
and project tasks in their owning project. Verify task IDs and revisions. Filing
is not proof of work completion. Record a technical blocker's concrete error and
evidence on the existing task, with who acts next.

**Focus maintenance.** Read each card and its canonical source. Apply only the
approved names, action text, status and ordering. Archive only under explicit
criteria when no owner action or review remains. One writer owns ordering.
Focus visibility never completes a task or decides a Signoff. Return ambiguous
priorities to the parent agent.

**Notifications.** The parent agent remains the sole consumer of its own agent inbox and owns the
delivery ledger. When the compatible dispatcher is active, it routes only an
exact, unambiguous event linked to the assignment. The durable worker receives an
assignment-scoped view, claims that event, saves its handling outcome, and
settles the claim with the parent agent's acknowledgement atomically. A live claim
suppresses a duplicate reminder to the parent agent; ambiguous, unlinked, private-owner,
strategic and approval events remain with the parent agent. A read, summary or enqueue is
not acknowledgement. Never invent a consumer or clear an unhandled event. If
dispatch is unavailable, preserve the event and follow the visible reason.

**Setup and other initiatives.** Execute a checklist the parent agent approved:
coordinate with the agent or owner responsible for that work, verify
dependencies and completion criteria, and change only authorized configuration.
Do not take over their canonical responsibility. Outbound content, sends,
replies and protected actions keep their existing approval flow. Messages and
replies in assignment-linked threads retain trusted worker/assignment
attribution. With active dispatch, an exact linked reply can wake the waiting
assignment; without that runtime gate, it remains the parent agent's notification.
Do not infer delivery or continuation from message attribution alone.

## Boundaries and recovery

Delegated workers do not contact the owner, approve/reject Signoffs, bypass outbound
approval, change grants, use owner HTTP, start another registered global peer,
run a root watcher, spawn nested helpers, overwrite the parent agent's current-work or
working-memory declaration, or implement software/design. Follow the worker's
assignment rather than bootstrapping the complete role. The operating system
identity is shared, so rely on server-side application binding for data
permissions; never describe process-level isolation that the runtime does not
provide.

Within the plan, workers may choose routine next steps and retry transient
failures using the stable action key and bounded retry policy. Before retrying
an uncertain write, reconcile its receipt and current canonical record. After a
restart, resume from saved checkpoint and receipts; never treat a thread ID by
itself as proof of recovered progress. A generation change, cancellation,
revoked grant, scope mismatch, or changed revision fences stale writes.

Escalate changed objectives, contradictory evidence, new authority or Signoff
requirements, material deadline/budget/scope changes, exhausted recovery, or
human-only input. Include evidence/source IDs, attempted recovery, the decision
needed and a recommendation. Use the parent agent's existing owner-input path for
owner decisions. Save a bounded result or escalation with changed record IDs,
revisions, verification, remaining work and next wake condition. The parent agent accepts the worker's completion report and gives one consolidated owner
update without repeating its checks or investigation. The worker checks its own
work and reports uncertainty; the parent agent intervenes only for exceptions.

If dispatch, routing or recovery is unavailable, preserve queued/blocked work
and surface the exact visible reason once. Never hide a runtime gap by dropping
events, duplicating effects, spawning an unrequested native subagent, or moving
delegated execution onto the parent agent.

## Boundaries across agents

Delegation gives a worker no account, mailbox or configuration authority,
no standing email or external-send authority, no peer administration and no further
delegation; the worker only intersects the parent agent's current policy. Email
work stays with Ordubashy unless he grants Email Management to a worker. Only the parent agent that created a delegated assignment reads
or controls it, and it neither assigns work to another Global nor starts, stops or
reconfigures another agent's session without the owner's instruction.
