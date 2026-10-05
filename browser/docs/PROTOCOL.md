# Browser service protocol v1

Manager contract for the next bounded increment, 2026-10-07. Local effects at
`a94e537519ba19d484375a7f8fb986339149285a` passed independent and Manager review.
This specification is not evidence that its server or real Chrome/VNC integration
already exists. Core consumes this contract over HTTP without package imports.

## Composition and startup

One service beside each browser exposes official MCP SDK Streamable HTTP at
`/mcp` and the private routes below. Every transport uses the same `Store`, `Gate`
and `AgentBrowser`, retaining their receipt, identity, lock and control semantics.
Use one server worker, no reload and bounded concurrent requests. Blocking
helpers/SQLite waits run outside the async transport loop. Request cancellation
cannot cancel an effect and then label it safely retryable.

Entrypoint: `python -m qorqut_browser.server --config PATH`. Explicit private JSON
configuration contains `browser` (existing `BrowserConfig`), absolute
`helper_executable`, absolute `credentials_file` and optional `handoff`. Imports
do nothing. Bind only `127.0.0.1` at `browser.service_port`. Production uses actual
readiness checks, with no configuration switch to fake them. Tests may inject
the existing executor/readiness seams.

Startup opens already provisioned state and calls `Gate.startup()` once. It
does not initialize missing state, create profiles/mounts, launch a desktop or
reopen automation. Application reconciliation through the control route advances
the epoch and gates admission. Receipt lookup remains observation.

## Attribution and bounds

The private credentials file contains `actors` entries with a distinct secret
`token`, UUID `actor_id` and UUID `agent_id`, plus a distinct secret
`application_token`. Reject missing, duplicate or short (under 32 characters)
tokens and nonprivate file permissions. These are application-supplied identity
projections, not another registry or role policy. Never expose secrets in logs,
exceptions, representations, tool schemas or identity output.

Every route, including MCP initialization/resources, authenticates Authorization
bearer credentials. Actor tokens fix actor identity and default target agent;
reject an `actor_id` argument or `X-Qorqut-Actor` attribution override. For private
HTTP forwarding/control, the application token requires `X-Qorqut-Actor: UUID`:
the trusted application has already authenticated that originating actor. The
application token is not accepted on `/mcp`. Control reconciliation is an
application operation; model callers request it through the handoff tool. All
configured actors have the same model tools; no permission engine is introduced.

Use stateless MCP HTTP so sessions cannot retain another request's actor. Keep
SDK transport validation; reject unconfigured Host/Origin values and enable no
permissive CORS. Bound JSON requests to 1 MiB, including chunked bodies. Use
finite client timeouts and bounded response reads. Callers cannot supply helper
executables, CDP addresses, output paths, credentials or forwarding URLs.

## Local HTTP API

Existing `Identity`, `Binding`, `Command`, `Reconcile`, `ControlStatus`, `Receipt`
and `EffectResult` JSON models in `contracts.py` define the shapes. UUIDs serialize
as strings, integer constraints remain, and unknown fields are rejected. Never
substitute a current epoch for the original command binding on retry.

| Route | Body/result and behavior |
| --- | --- |
| `GET /v1/identity` | `{protocol: 1, identity: Identity, control: ControlStatus, actions: [...], handoff_enabled: bool}`; no secret/path exposure or effects |
| `POST /v1/browser/command` | `Command` to `Receipt` through `Gate.execute`, with authenticated actor |
| `GET /v1/browser/commands/{command_id}` | Saved `Receipt` or `not_found`; never execute or settle during lookup |
| `POST /v1/browser/control` | `Reconcile` to `Receipt` through `Gate.reconcile`; application token and forwarded actor required |

Actor credentials can inspect identity/receipts and issue local commands. The
authenticated application may forward them with original actor attribution.
Local commands must match the configured browser and exact control binding;
these endpoints never forward recursively.

HTTP errors use `{error: {code, message}}`, with bounded nonsecret messages:
400 invalid arguments; 401 missing/invalid attribution; 404 unknown receipt;
409 binding/idempotency/uncertain-effect conflict; 422 unsupported capability;
503 unavailable/not-ready/storage-blocked. Saved receipts return HTTP 200 even
when failed, pending or uncertain. Preserve historical outcomes. Unexpected
errors reveal no traceback, credentials, fill text or page contents.

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
- `target_agent_id`: optional UUID, defaulting to the authenticated actor's agent.
- `observed_tab`: optional object with `binding: Binding` and opaque `tab_id` of
  1–256 characters; the application validates that observation's original target.

This requests notification, not proof that the user acted or control is ready.

## Application handoff bridge

Optional configuration contains `url` (complete private application HTTP/HTTPS
endpoint, no userinfo/fragment) and absolute `token_file` (private bearer secret).
Do not inherit HTTP proxies or follow redirects. Send one authenticated POST
with `protocol: 1`, original `actor_id`, resolved `target_agent_id`, original
`idempotency_key`, exact `prompt` and `observed_tab` or null. Identical retries
produce identical payloads; do not inject a newly observed epoch or service actor.

Core authenticates the bridge, validates actor/target/observation, resolves the
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
attribution rejection/isolation, direct/proxy command deduplication through one
gate, and control closing both paths. One small application fixture proves that
response-loss retry retains actor/target/key and yields one saved request; check
mismatched receipts and absence of a bridge. Reuse existing gate/helper fixtures
without duplicating their full failure suite or adding examples.

Check startup keeps admission closed and existing uncertain receipts remain inert
across service restart. Build/install the standalone wheel with pinned tested
dependencies. No live accounts or core/UI runtime imports. These checks establish
actual MCP/HTTP behavior with synthetic browser effects. Real helper, DOM/cookies,
desktop supervision, VNC/RFB fencing, machine holds, core transactions and G0–G6
acceptance remain pending.
