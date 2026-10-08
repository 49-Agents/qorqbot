# Shared backend contracts

Current v1 scope: [D36, terminal-owned sessions](TERMINAL-OWNED-SESSIONS.md)
supersedes earlier automatic CLI start/restart/provider-switch and context-packet
requirements below. Creation establishes an agent and shell; the user starts the
CLI. One logical inbox owner connects Chat. See that plan for the exact current
creation contract and revised acceptance. VM setup remains open.

D37 freezes the next independent interfaces in [inbox ownership](INBOX-OWNERSHIP.md)
and [recorded shell](RECORDED-SHELL.md). A persistent `terminal_id` outlives
individual native session owners; the owner UUID is the native execution's
`run_id`. Local raw terminal recording uses its terminal UUID as the capture
producer run_id, not as proof of a native session. Integration preserves those
separate identities and links the source rather than relabeling old bytes.
The new `runtime_get` read is implemented by the ownership lane; connect/release
remain trusted internal callables until the startup bridge is implemented.

D38's corrected [Terminal geometry contract](TERMINAL-GEOMETRY.md) adds verified
nullable dimensions to shell/Terminal observations and uses Terminal WebSocket
protocol 2 with ordered server size announcements. Read-only mirror geometry
and UI scrolling never acquire control or resize the real pane. The existing
human input epoch/sequence gate remains authoritative for pane resize/input.
This correction needs paired core/UI acceptance; it changes no host migration,
operation count or native-session readiness contract.

Status: normative planning baseline for `qorqut-core`. Implementation must keep these names and semantics aligned across modules, HTTP, both MCP servers, tests and generated agent instructions. `qorqutbot-ui` consumes core's network contract; `qorqut-browser` owns its independent MCP/control contract and core integrates it over versioned APIs. Changes require updating this document and the affected module plans.

## 1. Identity and coordinates

Use random UUIDs for public durable identities. Paths and display names are not identifiers. Timestamps are UTC RFC 3339; monotonic counters order local events independently of wall-clock changes.

| Field | Meaning |
| --- | --- |
| `installation_id` | Stable identity of this installation and its data root |
| `recovery_epoch` | Fresh UUID on restore/import activation; fences current control from prior recovery branches, without rewriting historical provenance |
| `actor_id` | Registered owner or agent responsible for a command; derived from transport identity |
| `agent_id` | Permanent logical agent identity and folder name |
| `conversation_id` | Stable main conversation; one per agent in v1, independent of native sessions |
| `environment_generation` | Monotonic incarnation of the agent's VM/host execution environment; ordinary host API restart does not advance it |
| `boot_id` | Observed OS boot identity; changes on guest/host reboot |
| `run_id` | One managed native CLI launch or resume invocation |
| `run_generation` | Monotonic launch generation for an agent; used with run ID to fence current control |
| `native_session_id` | CLI-owned session identifier, nullable until verified and reusable during native resume |
| `source_id`, `source_epoch` | Permanent raw-source identity and append/rewrite lineage |
| `offset`, `length`, `sha256` | Exact raw byte range and content identity, never Unicode character offsets |
| `message_id`, `thread_id` | Durable coordination message and conversation thread |
| `attempt_id` | One terminal-delivery attempt, stable through transport retries |
| `request_id` | Durable browser/human input request |
| `reference_id` | Durable user-facing file reference, independent of transient toast or future preview |
| `surface_id` | Agent-owned browser or machine desktop, separate from its current process instance |
| `entry_id`, `conversation_seq` | Stable conversation presentation entry and per-conversation insertion cursor |
| `operation_id` | Public operation receipt, including asynchronous progress |
| `event_seq` | Host-wide monotonically increasing journal cursor |

Agent imports preserve original event IDs/attribution and installation/sequence as provenance while assigning destination-local `event_seq` values. Restoring a full installation rotates `recovery_epoch`; event-stream clients must reconnect without carrying a cursor from another recovery epoch.

Native child sessions belong to their parent logical agent and link to the parent run/source. A platform-created independent agent receives a new `agent_id`. Reusing a native session through another managed run requires an explicit run/source link; it must not cause duplicate raw ingestion.

## 2. Agent record and layout

An agent has `id`, `display_name`, `icon_key`, `kind` (`vm` or `host`), `conversation_id`, workspace path, config revision, lifecycle and timestamps. `display_name` is the single editable name, replacing the initial plan's `name` field. It contains 1–120 nonblank characters after trimming; duplicates are allowed and directory responses always include IDs. The ordinary UI displays names/icons; IDs appear in details/disambiguation. Neither names nor icons appear in execution addresses.

`icon_key` selects one of twenty fixed keys owned and validated by core. The UI owns the monochrome artwork. Ordinary creation defaults to `circle`; onbash initializes with display name `onbash` and icon `terminal`. Revision-checked `agents_update` changes either presentation field and records an attributed event without restarting resources. Renaming onbash never changes its reserved identity. Store keys, not arbitrary markup/image URLs.

| Key | Label | Key | Label |
| --- | --- | --- | --- |
| `circle` | Circle | `square` | Square |
| `triangle` | Triangle | `diamond` | Diamond |
| `star` | Star | `compass` | Compass |
| `globe` | Globe | `leaf` | Leaf |
| `feather` | Feather | `book` | Book |
| `notebook` | Notebook | `flask` | Flask |
| `microscope` | Microscope | `telescope` | Telescope |
| `code` | Code | `terminal` | Terminal |
| `wrench` | Wrench | `gear` | Gear |
| `lightbulb` | Lightbulb | `anchor` | Anchor |

The authored workspace contains `persona.md`, `memory.md` and `notes/`, `research/`, `data/`, `files/`, `downloads/`, `scratch/`. Core seeds missing authored files only at creation; re-preparation preserves them. `persona.md` is the authoritative editable persona, descriptive role and agent instruction source. Generated `AGENTS.md`/`CLAUDE.md` refer to it; there is no competing authored `instructions.md` or template subsystem. Plain-file edits use M09 semantics and affect the next preparation/context snapshot; no live client reload is implied. Permission-policy ownership remains in core with effective mode `allow_all` in v1; descriptive roles do not filter operations.

There is at most one reserved host-global agent, initialized as `onbash`. The installation stores its ID. It cannot be silently replaced by another host identity. Archival preserves it and its history; restoring it uses the same ID.

`agents_create` requires `display_name`, with optional `icon_key`, persona and
resources. Creation prepares identity/workspace and eventually a persistent tmux
shell; the user starts the CLI. The D36 correction makes legacy `cli` metadata
optional and `start` default false; fresh `start=true` is refused. Previously
committed requests still replay their immutable result, with no execution.
Registration and workspace completion never claim a VM, terminal or CLI is ready.

No hard-delete operation is part of v1. Archive is a durable lifecycle transition that stops execution and retains files, messages and history. Restore is explicit. Creating an agent, archiving one, or restarting one is allowed to every registered actor under the same contract.

## 3. Distinct state machines

Do not compress these independent states into one overloaded `status` field.

| Record | States | Meaning |
| --- | --- | --- |
| Agent lifecycle | `provisioning`, `active`, `archiving`, `archived`, `error` | Durable registration/resource lifecycle |
| Environment desired | `running`, `stopped` | Requested environment condition |
| Environment observed | `provisioning`, `ready`, `stopped`, `unreachable`, `error` | Last verified VM/runner condition |
| CLI run | `preparing`, `starting`, `ready`, `stopping`, `exited`, `failed`, `unknown` | Process/session lifecycle; `ready` may be busy or idle |
| Activity | `working`, `idle`, `waiting_user`, `offline`, `unknown` | Fresh evidence about native work, separate from process existence |
| Capture | `starting`, `healthy`, `backlogged`, `blocked`, `incomplete`, `unknown` | Archival health with independent local/host watermarks |
| Browser control | `automation`, `yielding`, `human`, `reconciling` | Cooperative ownership of managed browser input |
| Terminal control | `automation`, `human` | Desired serialized input ownership with control epoch and runner-confirmed readiness; observing changes nothing |
| Input request | `requested`, `ready`, `completed`, `cancelled`, `interrupted` | Human handoff lifecycle |

Each state transition has allowed predecessors checked by the domain owner and a journal event. A connection loss is `unreachable`/`unknown`, not verified process death. Confirmed stopped execution is `offline`. `waiting_user` follows explicit native waiting evidence; a pending browser request also appears as `needs_user_input=true` without claiming the agent cannot do unrelated work.

Exactly one managed live run may own an agent's input route. An `unknown` run still occupies that ownership until reconciled. Do not launch a competing run simply because the old runner stopped answering.

The optional local [recorder probe](RECORDER-PROBE.md) reports a nonce-bound
observation from the actual terminal capture loop and exact expected process.
Historical spool health cannot replace that observation. A positive probe is
only terminal capture evidence; it neither establishes native/provider readiness
nor releases an unknown run's ownership. Timeout is not verified process death.

### Lifecycle precedence

Resource provisioning and archival retain a lifecycle revision. Creation may
establish a workspace, VM and tmux shell, never a native CLI. The user controls
CLI lifetime in Terminal. Inbox/Chat registration is admitted only for a current,
non-archived agent; D36's existing active owner wins. Unknown ownership remains
unresolved rather than being replaced by a new startup hook.

Archival closes new connection/delivery admission and settles/stops its owned
resources before claiming completion; restore explicitly reopens the same
identity through provisioning/verification and never starts a CLI. CLI exit
alone preserves VM/browser availability. Archive and stopped maintenance may
stop the included environment; onbash never shuts down the host OS.

## 4. Status and presence contract

`agents_status_set` updates a human-readable summary. Normalize surrounding/repeated whitespace with Unicode `split`/single-space join. Require 1–100 whitespace-separated words and at most 4,096 Unicode characters. Reject overlength text; never truncate silently. The initial create status is “Initializing.”

Every actual text change atomically updates the current row and appends an event containing old/new text, actor, status revision, recorded timestamp and optional run ID. Confirming unchanged text returns the current revision and refreshes an explicit review timestamp without inventing a status change. Heartbeats do not rewrite the status timestamp.

Runner hooks persist activity changes locally before forwarding them. The host stores every valid observed change, including late historical changes, but updates the current projection only from the current run and increasing source sequence. Freshness timeout appends one transition to `unknown`; repeated stale polls create no extra events. Recovery appends a new verified transition.

Agent directory rows include ID, display name, icon key, kind, lifecycle, status text/revision/update time, activity and observation time, last contact, current run/CLI/model observation, capture health and `needs_user_input`. Unknown observations remain unknown. API reads never start an agent or claim work has progressed.

## 5. Operation, event and job contract

Every public application mutation supplies `idempotency_key`, bounded to 200 characters. Its scope is `(actor_id, operation_name, key)`. Persist a canonical validated input hash and the committed result. Identical retries return that result; a different input under the same key is `idempotency_conflict`. A job's evolving outcome is read through `operations_get(operation_id)` rather than changing the historical initial receipt. Ephemeral manual-input/resize frames instead carry connection sequence and current control/run binding; reconnect never automatically replays them.

Mutable configuration changes supply an expected revision. Resource/session checks happen again immediately before an external effect; an earlier valid request cannot select a later process accidentally.

Conceptual result envelope:

```json
{
  "ok": true,
  "operation_id": "uuid",
  "outcome": "accepted",
  "data": {"agent_id": "uuid"},
  "event_seq": 123,
  "error": null
}
```

`outcome` is `completed` for a completed synchronous application change, or `accepted` for durable asynchronous work. Job states are `queued`, `running`, `waiting`, `completed`, `failed`, `uncertain`, `cancelled`. Waiting includes typed reason and next retry/reconciliation time. An accepted message/launch is not completed external work.

Errors use stable codes such as `invalid_argument`, `not_found`, `revision_conflict`, `idempotency_conflict`, `invalid_transition`, `not_ready`, `target_changed`, `unsupported_capability`, `storage_blocked`, `uncertain_effect`, `unavailable`. Never classify errors by matching arbitrary provider prose.

The journal stores event ID/sequence, kind, actor, agent/related IDs, occurrence time, recording time, origin source/sequence and a versioned bounded payload. Status bodies and full messages live durably; large raw transcript bytes live in chunks, referenced by events. Unique source/event keys deduplicate reconnects. Event history is append-only; corrections are new events. No automatic TTL applies to creation, status/state or message history.

Use one typed jobs/outbox table for fixed internal handlers. It is not an arbitrary-code scheduler. Domain code owns each job's recovery logic. No SQLite transaction remains open during network calls, libvirt actions, tmux input or file streaming.

During staged implementation, a real provisioning job may finish its workspace
step and remain `waiting` with `dependency_unavailable` while resource/runtime
integration is absent. This reason is excluded from automatic claim/retry; its
next-attempt timestamp permits explicit inspection only. Preserve lifecycle and
start intent, and expose workspace progress separately. Never certify readiness
or register a fake runtime tool to hide a missing dependency. See D29 and
[AGENT-REGISTRY.md](AGENT-REGISTRY.md).

Backup restore/import differs from a normal restart: hold all restored unfinished external-effect jobs/deliveries with `recovery_review` reason until reconciled. A snapshot's pending state cannot prove the original system did not act later. Preserve prior evidence; possibly performed input stays uncertain. `operations_reconcile` delegates inspect/retry/cancel to the existing typed handler and records the chosen resolution; `deliveries_resolve` owns message-attempt resolution. Every actor may invoke these under v1 policy. New control uses the fresh recovery epoch; historical source ingestion retains its original lineage.

## 6. Message and delivery contract

[CHAT-IMPLEMENTATION.md](CHAT-IMPLEMENTATION.md) freezes the first concrete
send/list/acknowledge/delivery and conversation schemas, transcript file format
and publication-progress contract. Runtime dispatch is a recorded dependency
hold until its own implementation joins this storage path.

`messages_send` accepts recipient agent ID, complete body, optional thread/reply IDs and an idempotency key. Body limit is 64 KiB UTF-8; longer material is an authored artifact with a reference, never silent truncation. Store exact supplied body plus sender/recipient IDs and timestamps. Summaries, if later added, are derived fields.

Messages are immutable. A correction is another message. A reply preserves thread relationships; it does not acknowledge its parent. All registered actors can inspect messages under v1's all-allowed policy; filters are navigation and efficiency, not access grants.

Message storage accepts and preserves the exact valid UTF-8 body within the size
limit. Automated terminal text has a narrower transport constraint: allow LF
(`U+000A`) and TAB (`U+0009`), but reject all other C0 characters (`U+0000`–`U+001F`),
DEL (`U+007F`) and C1 (`U+0080`–`U+009F`). This includes CR and ESC. Reject the
whole terminal request with `invalid_argument` and reason `unsafe_terminal_text`
before creating a dispatch attempt or sending any tmux input. Do not normalize,
strip, escape or truncate the message into a different payload. The host retains
the original message and records a failed delivery with that reason; it does not
automatically retry a permanent payload error. Explicitly corrected text is a
new message. This constraint protects bracketed-paste framing and the separate
guarded Enter step; it does not restrict raw human terminal key/control frames.
See D23.

Delivery states: `pending`, `deferred`, `dispatching`, `submitted`, `uncertain`, `failed`, `cancelled`. A separate `handled_at`/handling receipt records semantic acknowledgment. One message can have successive explicit attempts, but an uncertain attempt must be reconciled or explicitly retried before another is created. Explicit retry after uncertainty records its rationale and duplicate-processing risk.

The host and runner persist the stable attempt ID, exact payload hash and expected run binding. Before first input, the runner durably records dispatch intent. Repeating that attempt returns its saved outcome. Known pre-input failure may defer/retry the same intention. Possible paste/Enter without confirmation is uncertain; never retry blindly.

Terminal text includes message ID, recorded sender identity and thread reference so the recipient can retrieve/acknowledge exact messages. Native echo can corroborate submission but cannot prove the requested work was completed. Unread/handled tracking is independent of transport state.

## 7. Runner protocol

Protocol v1 uses private HTTP and WebSockets. Each command carries operation/attempt ID where relevant, payload hash, agent ID, recovery epoch, environment generation, boot ID and exact run/browser binding. Identity derives from transport credentials; callers do not self-declare another sender.

| Runner route | Required behavior |
| --- | --- |
| `GET /v1/identity` | Agent/environment/boot/protocol/image identity, mounts, versions, capabilities and capture health |
| `GET /v1/runs` | Inspect actual managed tmux/recorder/CLI bindings without launching |
| `POST /v1/runs/ensure` | Idempotently launch or inspect the exact recorded run |
| `POST /v1/runs/stop` | Stop only the expected binding; distinguish signal sent from confirmed exit |
| `POST /v1/terminal/input` | Guarded, serialized input attempt with durable deferred/submitted/uncertain outcome |
| `GET /v1/terminal/attempts/{id}` | Read a saved input outcome |
| `POST /v1/terminal/control` | Reconcile exact run/control epoch and input viewer under the same lock as paste |
| `WS /v1/terminal/{run_id}` | Attach/resize/manual input for the exact run; disconnect preserves execution |
| `POST /v1/browser/command` | Forward to the browser service's single command path with exact instance/control epoch; return artifact references and existing receipt on retry |
| `POST /v1/browser/control` | Reconcile with the independent browser service; its command gate arbitrates direct MCP and proxied actions together |
| `WS /v1/desktop/{surface_id}` | Relay the exact locally hosted browser/desktop VNC under its current control epoch; VM machine-console proxy is host/libvirt-owned |
| `POST /v1/files/inspect` | Inspect a regular file path in the bound agent environment for a reference; structured arguments, no host-path reinterpretation |
| `GET /v1/events?after_seq=...` | Paginated durable hook, receipt and source-registration observations |
| `POST /v1/events/ack` | Acknowledge event cursor after host commit |
| `GET /v1/capture/chunks?after=...` | Host pulls bounded pending raw chunks; pagination is not acknowledgment |
| `POST /v1/capture/ack` | Persist host-durable contiguous source watermarks |
| `POST /v1/quiesce` | Stop new work, settle inputs, drain capture and flush local state |

A minimal version handshake allows only explicitly compatible protocol versions. Unknown mandatory capabilities block the dependent action. Do not accept a runner from a mismatched generation because it reports a familiar IP or tmux name.

Browser's separate local service exposes `/mcp` plus a private versioned identity/command/control/relay interface for the runner. Its identity includes agent, recovery epoch, environment/boot/browser binding and protocol. Browser alone serializes its effects and persists command intent/outcomes and control epochs in its local receipt store. Direct MCP and forwarded calls use the same handlers/lock; a retry retains the command ID/payload hash. Restart reconciles current control with core before admitting automation; unresolved commands remain uncertain. A core outage cannot turn an unresolved request or stale control observation into confirmed completion/readiness.

Browser owns the concrete v1 JSON shapes and attribution/receipt/error behavior
in [its protocol contract](https://github.com/qorqutbot/qorqut-browser/blob/codex/browser-mcp-plan/docs/PROTOCOL.md).
The initial server increment covers MCP, identity/command/control and the optional
handoff client; relay/input fencing and cross-agent execution routing remain
subsequent integration. Core's handoff endpoint must implement that versioned
request/receipt shape while calling core's shared durable operation.

`terminal_control(action=take|release)` requires expected run and control epoch. Take binds one input viewer and advances the epoch; release advances it and revokes the old viewer. Host admission pauses during reconciliation; runner confirmation under the input lock is required before claiming the new mode is ready. All input frames must match the current run/epoch/viewer. Disconnect/restart retains human mode without automatic expiry; explicit takeover rebinds a new viewer, and explicit release permits automation only after a fresh composer check. Neither action clears typed text. An observer cannot send input.

## 8. Archive and history contract

[HOST-ARCHIVE.md](HOST-ARCHIVE.md) defines the first internal host intake
callables and D31's file/SQL/ACK order. It adds no public operation or alternate
upload endpoint. Historical state reads do not authorize an ACK; restored
archive epochs remain held until a later explicit audit/release implementation.

[NATIVE-PREFIX.md](NATIVE-PREFIX.md) defines a read-only local receipt view and
file-prefix proof. Its frozen target is distinct from the latest capture cursor;
neither a receipt page nor a file proof advances state, establishes native
ownership/readiness, persists a checkpoint or authorizes a host ACK.

Native bytes and terminal bytes are separate streams. Supported adapters register exact owned sources, including child sessions and known attachments. Paths/cwd are metadata, not ownership. Discovery is bounded to the agent's own managed native stores; never scan unrelated personal CLI history.

Local capture atomically commits chunk bytes and advanced cursor to the spool. The host collector pulls pending chunks from the runner, writes/fsyncs immutable bytes and their directory, then commits the chunk receipt and contiguous watermark. Only after both does it POST an acknowledgment. Fetch/page cursors do not discard data: reconnect derives progress from committed per-source host watermarks. ACKs are monotonic, source/epoch-bound and cannot exceed locally captured ranges. Acknowledged means one durable host copy; independent backup durability is separately reported.

Coordinates `(source_id, source_epoch, offset, length, sha256)` identify retries. Identical repeats succeed. A conflicting accepted coordinate is preserved as a conflict and rejected for that lineage. The collector may drain authenticated retired generations' unfinished sources into their original lineage; they cannot overwrite another generation's sources or current run state.

No total-transcript size ceiling. Preserve partial lines and huge records as raw bytes. Parsing/search/display can be bounded and lag behind capture. Parse outcomes include `parsed`, `unknown_type`, `invalid_record`, `incomplete_record`, `unindexed_large_record`; all retain raw coordinates. Repeated identical text is never a deduplication key.

Expose `local_captured_offset`, `host_archived_offset`, finalized checkpoint, parser version/offset, attachment gaps and last error for each source. A native session can append again after resume; finalization states “complete through N at checkpoint C,” not permanently closed for all time.

Core automatically publishes owner/agent conversation entries to
`agents/<agent-id>/transcripts/YYYY-MM-DD.jsonl` (D33), using their durable UTC
recording date for the partition and retaining original occurrence times.
Records contain full user/assistant content, stable entry/conversation IDs and
known run/provider/source provenance. Saving requires no tool call or UI export.
Provider changes preserve the same agent folder. Publication is resumable and
idempotent by entry identity, including restart, delayed native capture and
verified user-message echoes. Unrelated equal-text turns remain separate.
Expose publication lag/failure; never treat raw archival or a successful chat
write as proof that a dated copy has already been fsynced. Immutable raw history
remains authoritative; the dated files do not replace it or invent missing data.

## 9. Manual provider continuity

D36 supersedes automatic context restoration. One stable conversation and folder
belong to the agent. The user exits the old CLI and starts/resumes another through
Terminal. Core preserves identity, persona/memory/files, browser profile, queued
messages and all captured history, and binds the new connected session without
injecting an extra startup turn. Native resume is observed and recorded where
supported. Automatic cross-provider context packets are deferred outside v1;
shared files and queryable history remain available to the new CLI.

## 10. Human input contract

`browser_request_input` is described to agents as “Request an action from the user in your browser.” The tool is exposed only by browser's own MCP server and forwards over authenticated HTTP to core's shared handoff operation. It requires a prompt, with optional observed tab reference and target agent (defaults to the authenticated calling agent). Preserve the originating actor, target and idempotency key through the bridge; caller identity must not become a generic service account. Core resolves conversation/surface/exact browser binding; a supplied observation is validated against its original instance. Commit the durable request, conversation entry/event and transition job in core's transaction. Browser stores no competing request/message history. `ready` requires browser's control acknowledgment through the runner after managed input has settled. If the bridge times out, retrieve/retry the same idempotent request; never report successful notification without core's durable receipt.

The MCP tool description/result conveys needed user action and request state, not iframe/modal/layout mechanics. The frontend renders one inline live browser card per committed request ID and can expand that same surface. Transcript parsing, retries or reconnect cannot create another request/card or repeat its side effects. Closing/minimizing a viewer changes presentation only. Completed cards do not reopen live input. Detailed presentation is in [UI-PLAN.md](UI-PLAN.md).

`input_requests_complete` accepts exact request ID, optional note and idempotency key. Completion, control-epoch increment and one completion message are committed together. Runner synchronization gates subsequent browser commands. Repeating Done returns the prior result. A closed viewer does not complete a request. Restart preserves pending requests and rebinds presentation explicitly; stale tab IDs/epochs never silently regain control.

Manual browser Take control creates the same input-request record with a manual-control prompt; release uses complete or cancel. `input_requests_cancel` accepts unresolved requested/ready/interrupted requests, records cancellation and exactly one cancellation message (not successful completion), and advances the control epoch atomically. Complete/cancel races have one winning terminal transition. Revoke obsolete input viewers immediately and reconcile with the runner; cancellation during yielding cannot resume automation until the earlier command is known settled. A disconnected runner leaves control reconciling. Canceling a request does not undo external browser actions.

Human browser handoff pauses managed browser automation, not arbitrary shell access. Unrestricted agents are trusted participants. Input completion is not a platform permission grant or proof that login succeeded.

### Browser and machine surfaces

`surfaces_list(agent_id)` returns exact surface ID/kind (`browser` or `machine`), current instance/environment/boot binding, dimensions, availability and control state. Browser always means the dedicated TigerVNC Chrome display. Machine means the separate general guest desktop/VM console, or onbash's separate owned local desktop. The browser card, modal and Browser inspector share one browser surface; no surface silently substitutes for another.

Machine observation is read-only. `desktop_control(action=take|release)` targets an exact machine surface/epoch and first records an agent/environment-wide input-admission hold, then serializes acquisition of existing terminal/browser gates. It uses internal holds, not a manufactured browser-request card. A verified-absent CLI/browser counts as quiescent so the machine is usable during setup or after CLI stop; an unknown participant remains reconciling. New managed starts/restarts/switch replacement launches and automated input wait while this hold exists. Stopping a run does not remove it. After an environment/boot change, old viewers are invalid and the hold must be explicitly rebound/released before automated admission resumes.

Enable machine input only after all present participants are confirmed held; partial acquisition remains reconciling and records owned holds for release. An unrelated human terminal/browser request conflicts rather than being silently overridden. Release returns only its own holds; disconnect retains the hold until explicit resolution. Any actor may invoke it. Machine control is cooperative and does not claim arbitrary shell work has stopped.

## 11. File references and conversation presentation

`files_reference` requires a path; optional target agent, label and note. Default target is the caller's agent/main conversation. Relative paths resolve inside that workspace. Absolute paths resolve inside the target environment: `/workspace/...` maps to its workspace, other VM paths are inspected through its runner, and onbash paths resolve locally. Preserve source location/agent/environment metadata; never interpret a guest absolute path as a host path. Missing/unreachable/non-regular sources fail clearly before a successful reference is committed. Labels and notes are bounded plain text (200/4,000 characters); paths use a bounded 4,096-character request field and ordinary target OS validation.

Commit reference ID, actor, agent/conversation, normalized location/path, observed metadata, label/note, event and conversation entry together. Same-key retry returns the existing reference. Store a reference, not an implicit content snapshot; a later missing/changed source must be explicit. Keep records through provider changes/export/import with original provenance. The initial UI shows a durable compact file card and copy-path action; direct opening/preview is deferred. Tool descriptions say when to reference a file and accept a path, without toast/layout instructions. Returns report saved reference state, not whether the user saw/opened it.

`conversation_list` is a bounded read projection of normalized native conversation, owner messages, browser requests and file references. The entry ledger stores stable ID, conversation cursor, kind/source ID and occurrence/recording times. A platform operation allocates its entry atomically; first native normalization allocates one with exact source provenance. Late imports append at a new cursor and retain original timestamps. Preserve ledger coordinates across rebuilds; parser-derived content remains rebuildable. Import to another installation preserves original entry provenance and allocates destination cursors as with the event journal.

Render one card from its domain ID; native tool records may link through verified metadata but cannot create cards or re-execute tools. Owner chat submits via `messages_send`; exact verified native echo may link to its durable message, never equal-text heuristics. User-visible Chat remains a projection, not the complete raw archive. M07 owns the read model; M05/M06/M09 retain their domain records and M10 renders them.

## 12. Public operation inventory

Core application operations share their HTTP/MCP implementation where exposed. Browser owns the separate MCP tools `browser_turn` and `browser_request_input`; core MCP does not duplicate them. Their core HTTP bridges remain for routing, operator/UI calls and durable handoff persistence. Core retains `input_requests_list/complete/cancel` as application operations. Internal observations are separate provenance-bound routes, not model-writable claims about process state.

| Group | Operations |
| --- | --- |
| Configuration | `config_get`, `config_update` |
| Directory/lifecycle | `agents_list`, `agents_get`, `agents_create`, `agents_update`, `agents_archive`, `agents_restore` |
| Status/history | `agents_status_set`, `events_list`, `operations_get`, `operations_reconcile` |
| Runtime | `runtime_get`, `terminal_control`; user starts/stops/configures the CLI in Terminal |
| Desktop surfaces | `surfaces_list`, `desktop_control` |
| Messaging | `messages_send`, `messages_list`, `messages_acknowledge`, `deliveries_get`, `deliveries_resolve` |
| Browser | `browser_turn`, `browser_request_input`, `input_requests_list`, `input_requests_complete`, `input_requests_cancel` |
| Captured history/conversation | `history_list`, `history_search`, `history_get`, `history_raw_get`, `conversation_list` |
| Workspace/presentation | `files_list`, `files_read`, `files_write`, `files_reference`, `memory_get`, `memory_update` |
| Export/operations | `agents_export`, `backups_create`, `backups_get` |

Start with only the operations implemented by the current phase; do not advertise inert tools. Use cursors and bounded response sizes for lists/search. Raw downloads/artifact references preserve complete access without putting an entire transcript into a tool result. Endpoint naming uses `/api/v1/operations/<name>` and `/mcp`; streaming/viewer routes are documented in M10.

The first concrete core transport contract is [CORE-API.md](CORE-API.md). It binds
the reviewed foundation operations to HTTP/MCP/operator calls without changing
their domain rules; later inventory and stream activation require reviewed code.

[CONSOLE-SERVER.md](CONSOLE-SERVER.md) extends that local transport with
same-origin browser sessions, an explicit headless job worker and optional UI
assets. Browser sessions do not replace MCP bearer attribution. Restore holds
and uncertain terminal effects remain outside automatic receipt reconciliation.

## 13. Table ownership

Final migrations should implement these logical records without a generic JSON document store. Small versioned extension payloads are allowed for adapter-specific metadata.

| Owner | Logical records |
| --- | --- |
| M00 | installation metadata and mutable defaults/revision, actors/registration identities, operations/idempotency, events, typed jobs/outbox, file-effect receipts |
| M01 | agents, current status, current activity/observation cursors |
| M02 | environment definitions, generations and resource/image pins |
| M03/M04 | runs, exact native/terminal bindings and terminal control epochs; guest-local command receipts |
| M05 | threads, immutable messages, deliveries, attempts, handling receipts |
| Core M06 integration with M02 | browser/machine surfaces, desired control epochs, human input requests and machine-control holds; the independent browser service owns its separate local command/control receipt store |
| M07 | raw sources, run/source links, chunks, source checkpoints, parser records, history events, FTS, conversation entry ledger/read projection; guest-local capture spool |
| M08 | context packets and switching operation checkpoints |
| M09/M11 | file reference records; export and backup manifests/receipts |

Use foreign keys, unique constraints for idempotency/source coordinates and explicit indexes for common cursors. Current rows are updated; historical events/messages/chunk coordinates cannot be mutated into different facts. Schema migration and recovery are explicit operator workflows, with the same unrestricted actor policy rather than a new signoff system.

## 14. Configuration and repository boundary

Core is the only authority for application configuration and coordination. Its installation file owns static paths/listen/network settings; SQLite owns mutable new-agent defaults and revisioned agent settings. Generated browser/runner configuration is an input projection, not another mutable authority. UI stores only presentation state and obtains effective configuration through the API.

`config_get` returns non-secret effective defaults, their revision, supported API/protocol versions, icon keys and effective `allow_all` policy. `config_update` accepts an expected revision and idempotency key, and validates supported new-agent CLI and CPU/memory/disk defaults through shared models. Commit defaults/revision/event/receipt together. Changes affect future agent creation; existing agents require their normal explicit operations. Static installation settings use the core operator maintenance path and are not writable through this defaults operation. Never expose raw configuration files, credentials or browser profile contents to UI clients.

Core pins the browser service/image version and protocol. Browser receives explicit paths, process/instance/control bindings and an application handoff endpoint; it imports no core application or UI code. Browser owns its tool/control schemas, and core supplies a small wire client/HTTP bridge. Core owns durable request/Done/Cancel/message transactions; browser owns local Chrome/control effects and receipts. Other applications can supply their own handoff API without adopting this UI or core's schema.

Native CLIs configure separate `qorqut-core` and `qorqut-browser` MCP servers. Browser uses a private local Streamable HTTP endpoint and survives CLI/runner restarts. All its direct and forwarded commands share control fencing. Without a configured application handoff/completion integration it does not advertise human takeover capability. Browser owns no agent directory, message system or installation-wide configuration store. Cross-agent selection resolves through core to the target browser service, preserving the original identity/idempotency binding; a local execution endpoint cannot silently operate another browser or recursively forward commands.

Core owns `/api/v1` schemas and event payload versions. The UI checks supported API capabilities before dependent actions; unsupported versions produce a clear compatibility error. Core can start and complete HTTP/MCP/operator flows without UI assets, and UI releases do not migrate backend state. Serve frontend and API at one browser origin through optional static mounting or a conventional proxy; this does not add another backend service.

## D40 additive runtime observations (implementation assigned)

[D40](NATIVE-COMPOSITION.md) adds nullable native_setup and native_capture views
to runtime_get; their exact models are frozen in runtime/native_models.py. They
report managed configuration and capture/parse observations, and never grant
input readiness or imply all native sources are covered. Existing operation
names/count, host schema and job handlers stay unchanged.
