# Terminal-owned sessions and one inbox owner

Manager decision D36, agreed with the owner on 2026-10-08. This is the current
v1 product contract. It supersedes earlier automatic CLI start/restart/switch,
creation-time provider/model choices, launcher exclusivity and automatic
cross-provider context-restoration requirements. Accepted code/evidence remains
bounded by its recorded checks; this plan does not declare a connected runtime.

D62 ([local agents](LOCAL-AGENTS.md)) extends the recorded shell, native
collection and the one-logical-owner rule from the reserved onbash agent to every
local agent, each with its own runtime directory, tmux socket, recorder identity
and owner. A different live session on the same agent still conflicts; sessions
on different agents are independent. The `unsupported_target` terminal state now
applies only to non-host kinds.

## User flow

Creation records the permanent agent, prepares its folder/persona/files and
establishes a persistent tmux shell in its workspace when its execution target
is available. It never starts a native agent CLI. The user opens Terminal and
starts/resumes/stops/configures the CLI there, including provider, model, effort,
login and native trust. Chat and Terminal are two views of that same execution;
switching views or reconnecting a viewer never starts or stops it.

Core observes the exact native session, connects inbox/Chat, tracks activity and
captures its native history continuously. CLI exit disconnects Chat delivery;
history and dated transcripts remain. Chat never delivers to an idle shell.
Creating an agent from another agent follows the same manual-start rule.

## One logical inbox owner

Startup integration asks core to connect one native session to the agent inbox.
An atomic per-agent claim admits the first session; the same running session can
reconnect idempotently. Another session is refused while the existing session
owns the inbox. Never replace a live owner merely because a newer startup hook
arrived. Use the same owner for inbox consumption/delivery and Chat routing.
Native session identity plus its process lifetime distinguishes a reconnect
from two processes resuming the same native conversation. Child tools/processes
are not independently connected root sessions.

A conflict has a distinct result and message: another session is connected;
tell the user and wait until it is closed. Surface this in Terminal/UI as well
as hook context. The second CLI may remain open, but receives no ownership and
must not take over inbox or Chat. Do not kill either process or silently switch.
Transport failure or uncertain ownership is a separate unavailable/unknown
condition, never evidence of a conflicting session.

Ownership is logical, not a count of HTTP/MCP sockets or browser tabs. A brief
connection interruption preserves it. A finished model response does not
release it. Graceful CLI exit releases its own binding; crashes need bounded
reconciliation, with stale releases/reconnects unable to revoke a replacement.
Exact hook/observation and recovery wire details are the next Manager design,
informed by the native rehearsal. No lock wrapper or adversarial terminal
bypass prevention is required: this is a cooperative single-user system.

Keep recording independent of UI selection and ownership errors; conflict must
not relabel a second native session's captured bytes as the owner's history.
Existing raw archive and automatic per-agent dated copies remain required.
Pending messages stay durable while no session is connected. A saved/queued
message never proves native processing.

## Provider changes and environments

The user exits the old CLI, then starts/resumes the desired one in Terminal.
Agent identity, persona/notes/files, browser profile, messages and captured
history persist. The new provider can read those files/history; automatic
context packets, provider/model selectors and platform start/restart/switch
operations are outside v1. Native parsers, hooks, readiness and safe input still
require exact compatibility evidence.

A persistent VM per ordinary agent remains the desired isolation boundary;
the concrete VM setup remains open. Use onbash and isolated disposable local
fixtures to connect and test the common flow without treating ordinary agents
as host agents. Environment provisioning may create/boot machines and shells;
it never starts a model. CLI exit need not stop its VM or Chrome. Browser's own
MCP, persistent profile, VNC and user-input request integration are unchanged.

## Immediate implementation: creation contract

Keep the existing 14 operations, five handlers, schema version 5 and durable
operation/event/job implementation. No migration or new service is needed for
this correction. Change AgentsCreate so cli is optional with default None and
start defaults to strict false. Mark both fields deprecated in its schema:
new UI callers omit them. Optional legacy cli values are accepted only as the
existing registration metadata snapshot; absence uses current defaults within
the registration transaction. These values do not configure or select a CLI.

For a new mutation, start=true returns invalid_argument with a clear instruction
to start the CLI in Terminal, with no agent/job/event write. Keep validation of
the historical argument shape so an already committed old start=true operation
can replay its immutable result through Application.execute before the handler.
The replay check uses the original explicit/effective start=true arguments.
Changed defaults can make an old omitted-default request conflict; preserve its
original receipt and report the conflict, never mint a new key or repeat creation.
A replay never launches anything. start=false/omitted registration always stores
false; remove the variable start path from _register so onbash and ordinary
creation cannot enqueue a model launch intent. No silent ignoring of a fresh
explicit start=true request.

Retain old rows, native/CLI default metadata, operation receipts and event bytes.
Existing start_requested/cli view/projection fields are legacy registration
metadata, not running-session observations or future launch authorization.
Do not rewrite historical receipts or fake them as current state. Retirement
of those fields can accompany a later versioned cleanup; they must never drive
automatic launch. UI stops presenting them as runtime configuration.

Folder provisioning still truthfully waits for the unimplemented environment/
terminal integration. Preserve its current dependency hold, resource fields and
workspace_initialized_at. This correction does not mark a shell or VM ready.

Owning files: agents/models.py, agents/registry.py, focused tests under
tests/agents and mechanical call-site adjustments only where this contract
changes expectations. Other suites' fixtures may continue sending legacy cli
metadata. No app/schema/recorder/browser/runner production edits are necessary.

Checks: identity-only create from owner and agent; explicit start=true refusal
with no rows/effects; strict malformed start rejection; omitted/false start and
onbash always store false; existing committed old-shaped receipt replay remains
identical after reopen; durable folder/identity/status/idempotency behavior
remains. Extend existing grouped tests and one actual shared HTTP/MCP flow; no
new test matrix or usage examples. Run owning agent tests and affected transport
checks, build/install and one installed representative flow. Record exact scope
and limitations in docs/evidence/manual-creation-r1.md. A fresh independent
review follows before Manager integrates.

## Immediate implementation: UI creation

Against the above core contract, creation contains only display name and the
existing twenty-icon picker. Send display_name/icon_key/idempotency_key; do not
send hidden provider/model/start defaults. Remove model/provider/start selectors
and validations, preserve all pending mutation/ambiguity/draft behavior, and
explain that the user starts/configures the CLI in Terminal. Preserve unavailable
Terminal presentation until its actual API exists; no fake toggle or stream.

Remove the inspector's configured provider/model/start-request fields. It may
show the actual current_run observation with explicit unknown/unavailable
wording and a short manual-start explanation. Legacy wire fields can stay as
optional/unrendered data where needed; no large unrelated client rewrite.

Extend the existing grouped creation browser check to assert the controls are
absent and the request omits cli/start. Preserve six grouped browser checks,
strict build and desktop/mobile visual inspection using installed Chrome and
fresh disposable profiles. Synthetic API checks are not live core integration;
Manager runs the compiled UI against the accepted changed core after both
independent reviews. Record docs/evidence/manual-creation-ui-r1.md.

## Following connected-runtime work

Manager next freezes the startup registration/inbox ownership and shell/viewer
contracts using main Qorqut's terminal-owned flow plus the reviewed rehearsal.
Implement and review the core owner/hook bridge and terminal transport; UI then
adds the real Chat/Terminal switch and conflict/reconnect presentation. Integrate
native-source collection/normalization with the existing dated publisher and
actual inbox dispatch into the same accepted owner. Use onbash first, then the
chosen VM execution target. Each completed change receives fresh independent
review and one combined running-system check; do not turn this into more repos
or separate coordination services.

## Acceptance changes

A01.4 now proves creation starts no CLI, then user-driven terminal setup reports
missing login/capability honestly. A01.2 lifecycle races concern connected owner
and resource archival, not queued automatic starts. Add a compact connected-flow
check for simultaneous startup claims, same-session reconnect, different-process
resume conflict, temporary disconnect, graceful exit/crash and stale release.
A08.1/A08.3/A08.4 and G5 verify manual provider exit/start/resume with preserved
identity/files/browser/history/pending messages, no extra injected startup turn
and no automatic replay. A08.2 automatic packet construction is deferred outside
v1. Existing capture and delivery durability checks still apply.
