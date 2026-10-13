# D57 — Connect the owned browser to agents and the desktop UI

[D63](LOCAL-AGENT-CAPABILITIES.md) allows every prepared local host agent to
attach its own explicitly configured browser service with the same identity
checks. It also removes the unused Machine/Desktop placeholder from Computer;
the actual Browser and Terminal views remain. No service/profile is borrowed or
automatically launched for an ordinary agent.

2026-10-09. Owner: continue until browser integration is wired, tested and
deployed. Preserve manual native CLI startup, no application authentication,
desktop-only UI and the existing three repositories. This increment connects
prepared browser services; VM provisioning and the separate machine desktop
remain separate. Do not restart or drive the owner's native CLI for testing.

## Scope and ownership

Core owns durable per-agent service attachments, native MCP configuration,
requests, control intents, conversation cards, completion messages and the
same-origin stream route. Browser owns the WebSocket-to-TigerVNC relay and
enforces its existing command/control gate. UI uses pinned noVNC 1.7.0 and the
core API. There is no extra server, broker, auth layer or browser Python import
in core. The existing onbash desktop is adopted with its profile intact.

Three implementation lanes use this exact contract: core integration, browser
streaming, and UI. Manager handles the combined test and deployment. No nested
agents, self-review or additional reviewer rounds.

## Browser relay contract

Add WebSocket `/v1/browser/view` on the browser service. Core connects to the
configured loopback service, with public X-Qorqut-Actor attribution. No browser
client supplies a destination. First client frame is JSON:
`{protocol: 1, binding: Binding}`. Require the existing exact identity and epoch,
ready human mode, current service incarnation and actual desktop readiness.
Return `{type: "ready", protocol: 1, binding: Binding}` before any RFB bytes.
All following frames are binary RFB; the target is the configured VNC port.
Support the optional `binary` WebSocket subprotocol. No credentials.

Use a human-only viewer instead of implementing a second read-only RFB parser.
One viewer holds the existing process-shared command lock for its entire
connection. Take it without indefinitely waiting. Recheck the binding/human
state before client-to-VNC writes. The watcher targets a 50 ms polling interval;
each complete read-only observation has a one-second deadline, including thread
scheduling and resource validation. Failed or timed-out observations close the
relay, as do changes to epoch, ready flag, service identity or mode. This is the
Manager's 2026-10-09 timing amendment after actual noVNC traffic exposed false
disconnects with a 50 ms observation deadline; it replaces the original hard
100 ms watcher bound and does not promise real-time shutdown.
Reconcile already closes admission before awaiting this lock: close both
sockets and join all relay tasks before releasing it. Consequently a completed
reconciliation cannot leave the prior relay forwarding input. Disconnect keeps
human mode; it does not complete a request or enable automation. Backpressure,
bounded chunks, handshake/write timeouts and shutdown cleanup are required.
Reject another simultaneous viewer cleanly; the UI moves one viewer between
inline, inspector and expanded placement. Do not reset or close Chrome.

The existing control HTTP schema, browser_turn, browser_request_input and
browser://identity remain unchanged. This is ordinary local functionality, not
a retry of the earlier provider-blocked review. Leave refused actions stopped.

## Core attachment and operations

Add schema 10 for browser surfaces, input requests and saved control intents.
Keep foreign keys, stable UUIDs, unique per-agent attachment, one unresolved
request per surface and immutable historical request/control facts. Durable
jobs execute network effects outside SQL. Reuse operation idempotency, Jobs,
events, Messaging and Conversations; no parallel messaging or retry framework.

Public operations (normal OperationResult envelope):

| Name | Arguments | data |
| --- | --- | --- |
| browsers_attach | agent_id, service_url, idempotency_key | BrowserView |
| browser_get | agent_id | BrowserView |
| input_requests_create | target_agent_id, prompt, idempotency_key, observed_tab=null | InputRequestView |
| input_requests_list | agent_id, limit=50, before_created_at=null | `{items: InputRequestView[], has_more: bool}` |
| input_requests_complete | request_id, expected_revision, idempotency_key, note=null | InputRequestView |
| input_requests_cancel | same as complete | InputRequestView |
| input_requests_resume | request_id, expected_revision, idempotency_key | InputRequestView |

Prompts/nonblank/key limits and observed_tab follow browser's existing protocol.
Notes are nullable and at most 4096 characters. List order is descending creation
time plus request UUID. before_created_at is unpadded URL-safe base64 of the
UTF-8 JSON array [created_at, request_id], from the last returned item; validate
both fields and bound cursor length. UI primarily reads cards and browser_get.
Owner/manual creation supplies target explicitly. The private handoff endpoint
POST `/api/v1/browser/handoff` accepts the existing protocol-1 payload including
actor_id and calls input_requests_create through the shared application boundary.
It returns the existing browser HandoffReceipt shape, without OperationResult.
Preserve original actor, target, key, prompt and observed_tab. Duplicate requests
never create another card/job. Advertise browser_request_input only on browser
MCP; core's coordination operations have the names above, not browser tool aliases.

`service_url` is a configured HTTP loopback origin with explicit port, no path,
query/userinfo/fragment. Fetch `/v1/identity` outside SQL, validate protocol,
agent_id, installation recovery epoch and current bound environment/boot, then
save its immutable identity with one stable surface UUID. Same attachment is
idempotent; changing an active attachment is refused. Never adopt another profile
or silently attach a new instance. Invalid or missing services are visible.
An initial reconciliation, if necessary, uses an explicit saved control intent
and empty settled_commands. Existing human holds or uncertain browser effects
are never silently cleared. Core never edits browser's receipt database.

BrowserView is exactly:
`{agent_id, recovery_epoch, surface: BrowserSurface|null,
active_request: InputRequestView|null}`.

BrowserSurface is exactly:
`{surface_id, agent_id, kind:"browser", status:"available"|"unavailable",
reason:string|null, binding:Binding|null,
control:{mode:"automation"|"yielding"|"human"|"reconciling",ready:boolean}|null}`.
This is a fresh bounded observation, not authority from a stale cache; unavailable
is a valid view. It never exposes service ports or raw configuration to the UI.

InputRequestView is exactly:
`{request_id, agent_id, conversation_id, surface_id, requester_actor_id,
prompt, state:"requested"|"ready"|"completed"|"cancelled"|"interrupted",
revision, created_at, updated_at, note:string|null,
browser_binding:Binding|null, control_pending:boolean,
error:{code:string,message:string}|null}`.

Readiness comes only from a completed saved browser human-control receipt for
the original exact binding. The stream route additionally checks a fresh browser
handshake. An old ready request alone never grants control after service restart.
Resume is an explicit retry/rebind for the same unresolved request and same
browser identity, using a new saved intent only after observation establishes
why the earlier intent cannot be retried. Preserve unresolved effects: never
populate settled_commands from guesses. Changed browser identity remains an
interrupted request requiring explicit reattachment, not automatic takeover.

## Requests, control and transcript persistence

Create commits request, immutable conversation card, event and a browser_control
job atomically. Only one requested/ready/interrupted request per surface;
another distinct request conflicts with a clear existing-request error. A job
retains one original Reconcile command UUID, expected/desired binding, actor and
payload across network-response loss. Read receipts or retry the identical
control command; do not create a new effect on ambiguous failure.

Take advances the control epoch and selects human mode through that job. Pending
and interrupted states stay visible. A failure must not claim the user can act.
Complete/cancel have one winning terminal transition. In one SQL transaction
record terminal state/note, desired release epoch/control job, event, and exactly
one normal durable notification through Messaging.send. Repeated Done/Cancel
must not send twice, including a different key after the terminal transition.
An opposite terminal action conflicts. Notification goes to the requesting
agent, or target agent for a manually created owner request. Attribution is the
completion/cancellation actor. Text distinguishes completion from cancellation
and includes request ID, prompt and optional note. Release may still be pending;
the browser gate prevents automation until acknowledged. Notifications never
assert a successful login. A closed viewer never changes request state.

Extend the existing immutable Entry with kind/source type `browser_request`.
Source is `{type:"browser_request", request_id:UUID}`; text is the exact prompt.
Store a request FK and immutable creation record, mirroring the existing file
reference migration/insertion pattern. ConversationItem adds nullable
`browser_request:InputRequestView` for current presentation. Daily transcripts
contain the immutable request entry automatically; mutable status stays in the
read view and durable events. Refreshing a page updates the card without adding
another conversation entry. Set needs_user_input from unresolved requests in the
agent directory in addition to existing authored attention.

Core shutdown closes its viewers but not browser Chrome/TigerVNC or the native
CLI. Core restart keeps active requests and resumes saved jobs by original
receipt identity. Restore activation holds browser jobs/attachments under the
old recovery epoch; do not forward commands to a pre-restore surface.

## Native configuration and stream route

Extend existing managed native preparation to install a separate qorqut-browser
MCP entry for an attached browser, with the agent actor header. Preserve custom
native settings, core MCP, hooks and persona. Track only the owned browser entry
and instruction block in a separate or compatible existing manifest. No global
CLI settings, model/effort selection or automatic CLI restart. Native setup's
cache key includes the current attachment; native sessions may need their own
MCP refresh or a user-managed next launch to load a newly installed server.
Agent instructions explain intent: use browser tools for the owned browser and
request input when the user must act; inspect the browser again after the
completion/cancellation notification. Do not explain UI layout to the model.

WebSocket `/api/v1/browser/{request_id}/stream` is the only UI viewer address.
Resolve the request and target, require saved ready unresolved state and current
recovery identity, open the browser relay and send its binding handshake. Accept
the UI's raw binary RFB connection only when the browser replies ready. Forward
with bounded buffers; close both sides on completion/cancel, identity change,
disconnect or core shutdown. There is no destination URL in the UI request.
Normal core local attribution applies, without login, password or tokens.
Do not expose a general TCP tunnel or browser CDP proxy.

## UI behavior

Pin @novnc/novnc 1.7.0. Use its RFB class with scaleViewport=true,
resizeSession=false, shared=true, viewOnly=false only on the ready stream.
No iframe is needed for the component; it provides the requested embedded
interactive browser surface without another web app. Keep one live RFB instance
and move its container when expanding, retaining input focus sensibly.

Validate the new wire shapes and target identity as existing views do. Follow
existing per-agent store polling, cancellation/generation and mutation retry
semantics. Mutations retain the original idempotency key on uncertain outcomes.
Feature-gate with advertised operations; old servers keep the unavailable card.

Render browser_request as a full-width neutral card in chat: exact prompt,
requested/ready/error/terminal status, inline live surface when ready, icon-only
expand/collapse, Done and Cancel. Completed/cancelled cards show their final
state and note, without reconnecting. A failure has explicit Retry/Resume.
On socket loss show Reconnect, never blindly replay input. Closing a modal
returns to inline; switching agent/view disposes the viewer and retains request.

Computer's Browser card reads browser_get. With an attached service, Open browser
creates the same manual input request (prompt "Manual browser control") or opens
the existing unresolved request. Its live viewer, inline and expanded placements
share one current active viewer; inactive placements offer Open here. Done/Cancel
return control. Keep Machine/Desktop explicitly unavailable and separate.
No mobile redesign, authentication, extra headers or delivery-status clutter.
Preserve current 16px message spacing, chat drafts, copy/hover, Terminal and files.

## Focused acceptance and deployment

Browser lane: existing gate tests plus a compact real TigerVNC/WebSocket flow;
human-only admission, competing viewer, epoch change closes the stream before
control completion, disconnect retains hold, and shutdown leaves Chrome alive.
Core lane: attach/target validation; exact-key handoff retry; one card and dated
publication; Done/Cancel race and one message; pending receipt recovery; startup
config preserves native settings; actual HTTP relay handshake with isolated peer.
UI lane: existing Chrome fixtures extended for inline/inspector/expansion,
completed cards, API/socket failure and mutation retries. No broad copied suite.

Manager runs one combined disposable onbash flow with installed core/browser,
compiled UI, actual Chrome/TigerVNC and an ordinary local form: MCP tool request,
inline pixels and real pointer/keyboard input, Done notification, resumed agent
browser snapshot, Cancel, exact-key retry, core restart and retained synthetic
profile data. Check actual native MCP config loading in a disposable manually
started CLI where supported; never drive the owner's live native terminal.
Commit/push the three candidates, merge and deploy under standing authorization,
preserving the live owner terminal and browser profile. Report tested scope and
any required native MCP refresh honestly; don't call VM/machine support done.

## Manager clarifications implemented in the core candidate

2026-10-09, decisions 385 and 389. The seven operations and public shapes above
remain frozen. A fresh-key `browsers_attach` explicitly replaces an attachment
only after all its requests are terminal. Same-identity endpoint changes require
all old control intents settled. A freshly validated different Identity may
replace an unavailable service with unconfirmed intents: retain every original
endpoint, identity, payload and observed receipt; retire outstanding dispatch
with `retired_attachment`, preserve an error explaining that release/effect was
unconfirmed, and send no additional notification. Keep the stable surface UUID.
The old service is not claimed to have released control or stopped. Old jobs
cannot dispatch through, or grant readiness to, the replacement attachment.

A fresh-key same-endpoint/same-identity attach is also the explicit idle reconnect
after service restart, but only with a fresh `reconciling`, not-ready observation,
no unresolved request, and all prior intents completed. Save one new initialization
command from that observed binding, increment its epoch, select automation, and
keep `settled_commands` empty. Original-key replay is unchanged; ready automation
is a no-op; human/yielding holds and unsettled effects remain fenced. Requests use
`input_requests_resume` instead. Reads never reopen admission. First attachment
and replacement likewise never clear an existing human hold or uncertain effect.
