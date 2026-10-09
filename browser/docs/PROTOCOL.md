# Browser service protocol v1

Local owner-only contract, amended by owner/Manager handoff on 2026-10-09. Local effects at
`a94e537519ba19d484375a7f8fb986339149285a` passed independent and Manager review.
Core consumes this contract over HTTP without package imports. Functional evidence
is recorded separately; this contract does not certify core/UI/VM integration.
The human-only relay follows Manager's
[D57](https://github.com/49-Agents/qorqbot/blob/fabb57151bb7f842a0e01c02b031798bf7081160/core/docs/BROWSER-APP-INTEGRATION.md),
which supersedes the earlier passive-viewer/RFB-filter plan for this increment.

## Composition and startup

One service beside each browser exposes official MCP SDK Streamable HTTP at
`/mcp` and the private routes below. Every transport uses the same `Store`, `Gate`
and `AgentBrowser`, retaining their receipt, identity, lock and control semantics.
Use one server worker, no reload and bounded concurrent requests. Blocking
helpers/SQLite waits run outside the async transport loop. Request cancellation
cannot cancel an effect and then label it safely retryable.

Entrypoint: `python -m qorqut_browser.server --config PATH`. Explicit private JSON
configuration contains `browser` (existing `BrowserConfig`), absolute
`helper_executable`, public UUID defaults `actor_id` and `agent_id`, and optional
`desktop` and `handoff`. `agent_id` must equal `browser.binding.identity.agent_id`. Imports
do nothing. Bind only `127.0.0.1` at `browser.service_port`. Production uses actual
readiness checks, with no configuration switch to fake them. Tests may inject
the existing executor/readiness seams.

Startup opens already provisioned state and calls `Gate.startup()` once. It
does not initialize missing state, create profiles/mounts, launch a desktop or
reopen automation. Application reconciliation through the control route advances
the epoch and gates admission. Receipt lookup remains observation.

## Attribution and bounds

This standalone service has no login, password, token file, Authorization header
or authentication middleware. It runs on loopback for the local owner. Every
request uses the configured public `actor_id` and `agent_id` by default. Optional
`X-Qorqut-Actor: UUID` supplies attribution for a local forwarded request, including
MCP; it never grants permission and never changes browser ownership. Malformed or
duplicate attribution headers return `invalid_argument`. There is no actor
registry. An `actor_id` body argument remains outside the command/tool schemas.

Control is an explicit local HTTP operation; the model-facing tools do not expose
reconciliation. Binding, control epoch and actor-sensitive idempotency checks
remain mandatory to prevent accidental wrong-target or duplicate effects.

Use stateless MCP HTTP so sessions cannot retain another request's actor. Keep
SDK transport validation; reject unconfigured Host/Origin values and enable no
permissive CORS. Bound JSON requests to 1 MiB, including chunked bodies. Use
finite client timeouts and bounded response reads. Callers cannot supply helper
executables, CDP addresses, output paths or forwarding URLs.

## Local HTTP API

Existing `Identity`, `Binding`, `Command`, `Reconcile`, `ControlStatus`, `Receipt`
and `EffectResult` JSON models in `contracts.py` define the shapes. UUIDs serialize
as strings, integer constraints remain, and unknown fields are rejected. Never
substitute a current epoch for the original command binding on retry.

| Route | Body/result and behavior |
| --- | --- |
| `GET /v1/identity` | `{protocol: 1, identity: Identity, control: ControlStatus, actions: [...], handoff_enabled: bool}`; no secret/path exposure or effects |
| `POST /v1/browser/command` | `Command` to `Receipt` through `Gate.execute`, with local actor attribution |
| `GET /v1/browser/commands/{command_id}` | Saved `Receipt` or `not_found`; never execute or settle during lookup |
| `POST /v1/browser/control` | `Reconcile` to `Receipt` through `Gate.reconcile`; default or forwarded local actor attribution |
| `WS /v1/browser/view` | Exact protocol-1 binding handshake, then binary RFB for one ready human viewer |

Any local caller can inspect identity/receipts, issue commands or reconcile
control. Local commands must match the configured browser and exact control
binding; these endpoints never forward recursively.

The command lock is inherited by helper subprocesses. After a completed effect
or proven pre-dispatch failure, the gate explicitly unlocks only once the settled
receipt commits; this permits a persistent idle helper daemon to retain its file
descriptor without blocking later commands. Uncertain outcomes, failed commits
and service crashes retain close-only cleanup. Receipt lookup never unlocks
another invocation's file description or settles an effect.

HTTP errors use `{error: {code, message}}`, with bounded nonsecret messages:
400 invalid arguments/attribution; 404 unknown receipt;
409 binding/idempotency/uncertain-effect conflict; 422 unsupported capability;
503 unavailable/not-ready/storage-blocked. Saved receipts return HTTP 200 even
when failed, pending or uncertain. Preserve historical outcomes. Unexpected
errors reveal no traceback, fill text or page contents.

## Human viewer relay

Core connects to `/v1/browser/view` with the same optional public
`X-Qorqut-Actor` attribution and configured Host/Origin rules as HTTP. No query
arguments, credentials or caller-supplied destinations are accepted. The only
target is `127.0.0.1` at this browser's configured `vnc_port`. The optional
`binary` WebSocket subprotocol is selected when requested; it is not required.

The first client message must be text JSON with exactly
`{protocol: 1, binding: Binding}`. The exact identity and control epoch must
match this service, its current incarnation must still own admission, and
control must be ready in human mode. Admission verifies actual desktop
readiness using the existing supervisor check. The response is exactly
`{type: "ready", protocol: 1, binding: Binding}` before any RFB bytes. Every
subsequent message in either direction is binary RFB. This is one interactive
human viewer; there is no passive viewer or RFB filtering mode.

The viewer acquires the existing process-shared command lock without waiting
and holds it for the connection's lifetime, including cleanup. A competing
viewer is rejected. Automation continues to use the same gate and cannot run
while the viewer owns that lock. Before every client-to-VNC write, the relay
checks the original binding, ready human mode and service incarnation again.
A watcher waits 50 ms between observations. This is a normal polling target,
not a hard real-time shutdown promise under thread scheduling or host load.
Each entire read-only observation has a one-second deadline, including worker
scheduling and `Store.connection` path/read validation. The SQLite busy wait
remains 20 ms, and read worker capacity stays separate from commands waiting on
the lock. Changed binding, mode, readiness or incarnation, failed checks and
timed-out checks still close the relay; input never proceeds on stale state.
This cadence/deadline distinction is Manager's 2026-10-09 correction to D57's
original at-most-100 ms watcher bound; see the
[deadline correction evidence](evidence/browser-view-deadline.md).

Reconciliation publishes the advanced epoch and closed yielding/reconciling
state before awaiting this same lock. The relay joins its forwarding tasks and
closes both sockets before releasing the lock. A completed control receipt
therefore cannot coexist with old forwarding. Disconnect and service shutdown
do not release the durable human hold, complete a request or reset Chrome.
ASGI lifespan shutdown cancels and joins all active viewer handlers; Chrome,
TigerVNC and their profile remain independently owned by the desktop supervisor.

Handshake text is limited to 4 KiB. Binary messages and VNC read chunks are at
most 64 KiB; the installed server's WebSocket receive queue is four messages,
with compression disabled. Each direction awaits writes for backpressure,
with five-second handshake/connect/write deadlines and bounded close cleanup.
After upgrade, admission/handshake failures close with code 1008 and a stable
error code as reason; text in the RFB phase closes with 1003, oversized RFB with
1009, and unavailable I/O/state with 1011. Header/query failures use the ordinary
HTTP error envelope before upgrade when ASGI denial responses are supported.
No error frame is inserted into the RFB stream, and input is never replayed.

Core owns the same-origin request stream and durable request lifecycle. UI's
pinned noVNC 1.7.0 connects through that core route and moves one live viewer
between placements. Browser's existing HTTP control schema, MCP tools and
`browser://identity` response are unchanged.

## MCP tools and resource

Expose read-only `browser://identity` with the identity response above. Its
control observation may become stale; the gate still checks every command.

`browser_turn(command_id, binding, action)` uses the `Command` shape and returns
the structured `Receipt`. This increment supports snapshot, navigate, click,
fill and screenshot only. Descriptions explain these actions, retaining command
IDs on retry and uncertain outcomes. Large output retains existing owned artifact
references; do not add generic file serving. Tabs/upload/download/evaluation and
cross-agent execution routing remain subsequent integration.

Advertise `browser_request_input` only with valid handoff configuration. Describe
the intent to request a user action, retaining the key on retry and inspecting
the browser again after completion. Do not describe UI mechanics. Arguments:

- `idempotency_key`: 1–200 characters.
- `prompt`: nonblank, 1–4096 characters, preserved exactly.
- `target_agent_id`: optional UUID, defaulting to configured `agent_id`.
- `observed_tab`: optional object with `binding: Binding` and opaque `tab_id` of
  1–256 characters; the application validates that observation's original target.

This requests notification, not proof that the user acted or control is ready.

## Application handoff bridge

Optional configuration contains `url` (complete private application HTTP/HTTPS
endpoint, no userinfo/fragment), with no token configuration or headers.
Do not inherit HTTP proxies or follow redirects. Send one local POST
with `protocol: 1`, original `actor_id`, resolved `target_agent_id`, original
`idempotency_key`, exact `prompt` and `observed_tab` or null. Identical retries
produce identical payloads; do not inject a newly observed epoch or service actor.

Core consumes the public actor attribution, validates target/observation, resolves the
conversation/surface/browser and commits its request/event/conversation/job
transaction. Another application can implement this same endpoint. Browser adds
no local request/message store and imports no core code.

Success is a JSON object with `protocol: 1`, echoed `actor_id`, `target_agent_id`
and `idempotency_key`; UUID `request_id`, `conversation_id`, `surface_id`; `state`
(requested, ready, completed, cancelled or interrupted); and nullable
`browser_binding`. A ready state requires a binding. Validate the shape and
echoed actor/target/key before returning it. This is the application's committed
receipt, not independent proof of current control readiness. The application
owns historical idempotency results, state queries, Done/Cancel and their single
notification messages.

Limit responses to 64 KiB. Application errors use the HTTP error envelope above.
A definite connect failure may report unavailable. Response loss, timeout after
possible send, redirect, malformed/mismatched success or ambiguous server failure
reports `uncertain_effect` and retains the original key. Never fabricate a request
ID or successful notification. No automatic retry in this increment: a caller
can explicitly repeat the exact request/key to retrieve its saved result.
Handoff does not reconcile control itself; core's transition job does that.

## Focused acceptance

Use official MCP client initialization, discovery, resource reads and tool calls
against an actual isolated server. Check conditional handoff discovery,
default/forwarded attribution, direct/proxy command deduplication through one
gate, and control closing both paths. One small application fixture proves that
response-loss retry retains actor/target/key and yields one saved request; check
mismatched receipts and absence of a bridge. Reuse existing gate/helper fixtures
without duplicating their full failure suite or adding examples.

Check startup keeps admission closed and existing uncertain receipts remain inert
across service restart. Build/install the standalone wheel with pinned tested
dependencies. No live accounts or core/UI runtime imports. The disposable desktop
acceptance adds actual Chrome/TigerVNC, pinned-helper DOM
actions, persistent synthetic cookies and ordered stop/restart. Relay acceptance
reuses that disposable fixture for an actual TigerVNC/WebSocket handshake,
rendered pixels, pointer/keyboard input, exclusive human control, epoch closure,
retained hold on disconnect and Chrome survival across MCP shutdown. See
[relay evidence](evidence/browser-human-stream.md). Machine holds, combined core
transactions/UI/noVNC and real-guest G0–G6 acceptance remain separate gates.
See [operations](OPERATIONS.md) and the recorded evidence.
