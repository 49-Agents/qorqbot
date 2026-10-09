# Architecture decisions

## D40 — Compose manual startup and continuous native history

The recorded Terminal is now an accepted real core/UI flow. [Native composition](NATIVE-COMPOSITION.md)
uses per-agent attributed core credentials and managed hook/MCP configuration,
then the existing session monitor composes discovery, capture, host archive,
projection and dated publication. No new service, host migration or automatic CLI
launch. Setup and collection build in parallel against frozen observation models;
actual config loading is checked independently. Manager reviews directly.

## D39 — Native admission, capture and projection seams

[NATIVE-CONNECTION.md](NATIVE-CONNECTION.md) records accepted callable seams and
their remaining composition boundary. These tests are not normal-server native
acceptance; D40 provides the next connection.

## D38 — Usable host Terminal and parallel UI integration

Manager decision, 2026-10-08. [Terminal server](TERMINAL-SERVER.md) composes the
accepted recorded shell into the ordinary console application for reserved
onbash, while UI implements the frozen protocol independently. Optional explicit
installation settings enable the local target; other agents still await their
VM target. The same core owns observation, viewer admission and durable human
controls. There is no native CLI launch or second coordination service.

One read operation and a bounded authenticated WebSocket expose the existing
shell. A browser reconnect/view change cannot restart it, replay input or clear
human control. This usable terminal is the first connected integration step;
native startup/inbox, actual Chat dispatch and native history remain the next
Manager-owned contracts. The two accepted seam reports provide those facts;
no further general exploration is assigned. Each completed implementation gets
a separate Astra MAX reviewer, then actual compiled UI/core verification.

## D37 — Parallel ownership, recorded-shell and adapter implementation

Manager decision, 2026-10-08. The manual-creation review does not block three
independent builders: [inbox ownership](INBOX-OWNERSHIP.md) owns host migration
6/application observation; [recorded shell](RECORDED-SHELL.md) owns the local
persistent Bash/recorder and PTY attachment; [adapter corrections](ADAPTER-REHEARSAL-FIXES.md)
own demonstrated Codex re-preparation and captured composer defects. File and
schema ownership are disjoint. All new builders/reviewers use the owner's latest
explicit override: Astra MAX, with internal self-review disabled.

Persistent terminal identity is distinct from a connected native-session owner.
The terminal can exist before/between CLIs and records those bytes continuously.
Only a verified native session obtains the single inbox claim. One combined
integration assignment follows review to wire startup hooks, authenticated
Terminal transport/UI and native delivery/capture. No component test substitutes
for that running workflow, and no automated CLI launch is reintroduced.

## D36 — User-started terminals and one logical inbox owner

Accepted by the owner, 2026-10-08. Creation prepares identity/files and a tmux
shell, never a native CLI. The user starts/stops/configures providers in Terminal;
Chat attaches to the observed session. First active inbox owner wins, same-session
reconnect succeeds, and a competing session is refused and told to notify the
user and wait. No launcher lock or deliberate-bypass defense. Transport failures
are not conflicts; brief disconnects and completed turns do not release ownership.

This supersedes D04's managed-launch framing, D08 automatic context restoration,
D29's future start intent and old runtime start/restart/switch plans. Preserve
raw/provider-independent history and core's dated transcripts. Follow
[the current scope and assignments](TERMINAL-OWNED-SESSIONS.md); earlier evidence
continues to describe only the implementation tested at its recorded revision.

## D35 — Headless jobs and same-origin console sessions

**Decision:** [CONSOLE-SERVER.md](CONSOLE-SERVER.md) extends the initial local
server with one supervised typed-job worker, bounded HttpOnly browser sessions
and optional static UI hosting. Bearer MCP/operator calls remain independent.
Background work no longer waits for a connected UI or manual queue pumping;
receipt recovery cannot release restore holds or retry uncertain terminal input.
This deliberately supersedes D26's first-increment no-job-start restriction.
It adds neither another service nor an account/permission system. Loopback-only
hosting remains the verified target until wider deployment has its own checks.

## D34 — Connect stored messages, conversation entries and dated copies

**Decision:** [CHAT-IMPLEMENTATION.md](CHAT-IMPLEMENTATION.md) owns the next
host schema/assembly change: direct messages, exact handling, stable chat
entries and automatic daily files. Reuse the existing typed jobs and streamed
file receipts to atomically replace complete daily partitions, coalescing queued
updates. This minimizes another durability mechanism at the cost of write
amplification on very busy days; preserve bounded memory and no total-day cap.
Publication progress is distinct from raw capture and terminal submission.
Native parsing, source-verified echoes and dispatch remain explicit following
integrations. The minimal UI can use real persisted offline chat immediately
without presenting that as a running assistant.

## D33 — Core automatically publishes dated per-agent chat transcripts

**Owner requirement:** whenever the owner chats with an agent, `qorqut-core`
automatically saves the conversation in that agent's `transcripts/` folder,
with dates. Saving must not require an agent tool call, UI export action or a
particular provider to remain selected.

**Implementation direction:** publish UTF-8 JSONL under
`agents/<agent-id>/transcripts/YYYY-MM-DD.jsonl`, partitioned by the durable
entry's UTC recording date. Retain original occurrence timestamps separately.
Include full user/assistant content, stable entry/conversation identity and
known run/provider/source provenance. Replay, native echoes and provider
switches use existing entry/source identities, never equal-text deduplication.

M07 owns automatic publication from durable conversation entries through core's
existing background work, with a persisted publication cursor, crash-safe
filesystem progress and visible lag/errors. Resume after restart without
duplicating entries or requiring another chat message. Core safely creates the
reserved directory inside an established owned agent folder when needed; this
does not authorize reseeding authored files. M09 includes these files in full
agent export/restore. Onbash follows the same rule.

These convenient dated copies are derived from durable history; immutable raw
native/terminal archives remain intact and retain unsupported/partial records.
Publication failure cannot discard durable chat or silently claim files are
current. This requirement joins conversation/history acceptance before a chat
workflow is considered complete. It is planned, not implemented by the already
accepted registry/archive or the separate native-prefix assignment.

## D32 — Verify file prefixes using permanent spool receipts

**Decision, 2026-10-08:** implement [NATIVE-PREFIX.md](NATIVE-PREFIX.md) before
native-reader restart/checkpoint composition. Expose bounded read-only receipt
pages from the existing local spool and compare an already-held file descriptor
against every receipt in a frozen prefix. Receipt hashes survive reclamation;
they compare supplied bytes but cannot reconstruct lost originals. Return finite
prefix/tail/stat evidence without changing capture state or authorizing input.

No schema, second byte store or service is needed. Native source attestation,
durable checkpoints, file ingestion/discontinuities and provider append contracts
remain separate design/integration work. This proof cannot replace D31 host
durability, the live recorder observation or a native readiness verdict.

## D31 — Commit host chunk files before authorizing source ACKs

**Decision, 2026-10-08:** implement the internal host storage boundary in
[HOST-ARCHIVE.md](HOST-ARCHIVE.md), using the existing host database and effect
gate. Source/epoch/byte coordinates own immutable chunk receipts; filesystem
fsync precedes the transaction advancing the host watermark, which precedes an
ACK-eligible return. This raw stream does not create a generic operation/file
job for every terminal fragment or add an upload service. The eventual collector
still uses D16's authenticated pull/ACK protocol.

Retired run lineage can drain without authorizing current execution. A host
restore epoch separately fences intake and ACKs for historical archive epochs;
this increment cannot release those holds. Conflicts preserve both accepted
and conflicting evidence and close further ACK admission for that epoch.
Native source capture is a separate dependency: trace the current adapter/spool
interfaces before designing it, without claiming supplied identities attest a
native process. No archive parser, VM or runtime readiness follows from this
storage increment.

## D30 — Query live recorder state in its existing capture loop

**Decision, 2026-10-08:** add the bounded optional local observation protocol in
[RECORDER-PROBE.md](RECORDER-PROBE.md). A nonce-bound Unix datagram response from
the verified recorder process distinguishes current terminal capture from a
historical SQLite health row. The same recorder loop answers; no heartbeat
writer, thread, extra daemon, database or network endpoint is needed.

The observation is not native/provider readiness or a liveness lease. Keep the
complete automated-input predicate closed until its other evidence exists.
This work changes only local recorder/probe behavior and owning tests, so it can
proceed separately from M01's host schema and application assembly.

## D29 — Register real agents before resource integration is available

**Decision, 2026-10-08:** the first M01 increment implements durable identity,
onbash uniqueness, status history and actual authored workspace initialization
through five shared application operations. The concrete contract is
[AGENT-REGISTRY.md](AGENT-REGISTRY.md). One assignment owns migration 3 and
application assembly; dependent lanes do not invent another registry/schema.

Creation records start intent and one real provisioning job. After preparing the
workspace, that job waits with `dependency_unavailable` while runtime/resource
integration is absent. Automatic claims skip that reason. An inspection time is
not an instruction to replay effects. Lifecycle and observations never advance
to ready/active/idle merely because files exist. Onbash setup uses the same
registration path, starts with `start=false`, and never launches a host CLI.

Publish the initial owned directory atomically without replacing an existing
destination. Marked retries preserve authored files; unmarked directories remain
unresolved. Concrete workspace/projection jobs retain their effect receipts;
the derived `agent.json` can be rebuilt from committed records. Ordinary authored
file editing remains the separate M09 single-file contract. No new service,
generic filesystem framework, permissions engine or speculative runtime tool.

## D28 — SDK diagnostics obey the transport's content boundary

Independent browser review and Manager reproduction showed that MCP 1.30.0
logs raw invalid request/notification values through the root logger. Four
named-logger filters missed those records even while HTTP errors were sanitized.
Both standalone browser and core transport must cover actual SDK validation logs.

Use a small record-factory sanitation boundary at application assembly, matching
SDK logger names and installed SDK source paths, including root calls. Preserve
safe diagnostic metadata, existing factory behavior and unrelated logging; strip
payload-bearing message/arguments and exception/stack text before handlers emit.
Keep setup idempotent and imports inert. Do not patch/vendor the SDK or add a
logging framework. An already equivalent boundary needs verification, not a
second sanitizer. Owning wire tests exercise malformed requests and notifications
with synthetic sentinels, without real secrets or browser/provider effects.

## D27 — Recorder startup signals and owned-process cleanup

Independent review and Manager real-PTY reproduction found three defects in the
first local recorder: escalation omitted its direct child when another job was
foreground, leader exit left original-group members alive, and a TERM arriving
before `login_tty` disappeared. The local recorder remains unapproved.

Keep the child unreaped while exit/cleanup evidence is still needed, using
non-reaping observation and final direct-child reaping. Failure cleanup has
finite deadlines and targets the owned direct child and every live member of
its verified original PTY session; leader exit alone is not session completion.
Do not use an unbounded final wait or signal an inherited/shared parent group.
Unconfirmed cleanup stays an explicit failure with admission closed, and parent
terminal/descriptor/signal state must still be restored. Group termination and
direct-child reaping are different claims; detached-subtree supervision remains
the later systemd/cgroup integration boundary.

A later review and Manager reproduction found that leader exit before cleanup
can erase the PTY's foreground observation. Use the session identity anchored by
the unreaped child's PID/start and matching SID, which survives terminal hangup.
The bounded `/proc` membership check includes all live processes in that exact
owned SID, including background groups. Revalidate member identities and signal
them through pidfds. This supersedes selection by original/foreground PGID only;
do not add a history of foreground groups. Before session creation, target only
the exact child, never the inherited/shared parent session. Normal interactive
signal forwarding still follows the PTY foreground semantics.

Count live states separately from zombies, refresh process-start/membership
evidence, and keep incomplete observations or elapsed deadlines explicitly
unsuccessful. Detached new sessions remain outside this local boundary. Do not
read or retain unrelated command lines, environments or process contents.

Block forwarded signals across fork and child handler/PTY setup. Restore the
parent mask on every path and the child's intended mask before exec. A pending
signal must reach the exact owned child even before its group exists; an ESRCH
cannot consume it as successfully forwarded. Preserve normal foreground signal
semantics. Focused real-PTY tests cover these races and committed-byte retention;
they do not establish host archive, native-provider or VM acceptance.

## D26 — Thin core transport over the reviewed application

The first HTTP/MCP/operator increment uses the reviewed foundation at `ef18e59`
and [CORE-API.md](CORE-API.md). One local process and shared registry preserve
application attribution, schemas and mutation receipts. Fixed private bearer
bindings establish actor identity with all-allowed access; owner browser sessions
and credential issuance remain later work. Health certifies only the registered
operations, and no unimplemented domain, stream or browser tool is advertised.

## D25 — Freeze the independent browser service's first wire contract

After review of the local browser primitives, browser owns the bounded v1
MCP/identity/command/control and optional handoff contract in its
[`docs/PROTOCOL.md`](https://github.com/qorqutbot/qorqut-browser/blob/codex/browser-mcp-plan/docs/PROTOCOL.md).
Both transports call the accepted local gate. Actor credentials supply identity;
the trusted application forwards the original actor and owns reconciliation.
This is target/attribution integrity, with no per-agent role policy. Browser
stores local effect receipts; core still owns all durable request/message and
Done/Cancel transactions. No missing relay or cross-agent implementation is
advertised as working. The independent server increment must pass actual MCP/HTTP
checks before integration, without requiring core/UI runtime imports.

For browser's fixed application-handoff URL, private reachability is a trusted
provisioning requirement. Browser validates HTTP/HTTPS URL syntax, rejects
userinfo/fragments, uses direct connections without inherited proxies/redirects
and verifies HTTPS certificates normally. It does not invent IP/DNS address-class
rules; agent tool arguments cannot override the configured destination.

## D24 — Explicit paste framing and fresh Enter evidence

Independent terminal review and Manager reproduction on tmux 3.4 showed that
`paste-buffer -p` inserts delimiters only when the application has requested
bracketed paste. Automated input must send one explicitly framed byte sequence
through a raw, non-converting buffer paste, with exactly one begin/end pair.
Keep the durable payload hash and size about the original validated body; framing
is transport-owned. Human raw frames remain separate. Do not change the native
application's terminal mode to make an input attempt pass.

Composer eligibility still requires a supported native editor that interprets
bracketed paste; transport framing alone does not prove native compatibility.
Absent or uncertain adapter evidence remains not ready. Actual native acceptance
is a later gate, distinct from the local receiver tests.

The same review reproduced a composer changing to a modal while a SQLite writer
delayed the Enter intent. After persisting that intent, refresh binding, external
readiness and exact-paste/safe-Enter evidence immediately before Enter. Do not
carry an earlier proof across a database wait. A rejected proof after possible
paste is uncertain, with no Enter and no automatic replay.

A subsequent review reproduced capture loss and machine-hold activation during
the final verifier's permitted settling wait. Refreshing its composer snapshot
does not refresh those external signals. After final paste verification returns,
sample capture readiness and the external hold again at the Enter boundary;
unavailable readiness or an active hold prevents Enter and leaves uncertainty.
Readiness callbacks are prompt observations, not polling or settling loops. Do
not insert another deliberate wait or database write before the effect. This is
an ordered local admission check, not an atomic guarantee against asynchronous
process death; machine-hold acquisition still uses the shared input gate before
acknowledging quiescence.

## D23 — Preserve messages and validate terminal paste text

The terminal builder identified that embedded ESC bracketed-paste termination
and CR can submit input before the guarded Enter. Manager verified the pinned
P05 source already rejects control characters at this transport boundary.

Keep message bodies exact. For automated terminal text, allow LF/TAB but reject
other C0, DEL and C1 characters, including CR/ESC, with `invalid_argument` and
reason `unsafe_terminal_text` before attempt creation or any input. No silent
normalization or permanently deferred retry. Messaging retains the original
body and records a failed delivery; corrected text is another message. Human
terminal key/control frames remain on their separate controlled path. This is
transport framing correctness, not a v1 permission tier.

Validate the rule with focused terminal-boundary tests including an embedded
bracketed-paste terminator followed by CR and zero observed tmux input. Do not
duplicate the character-validation suite in every transport or repository.

## D22 — Staged implementation with independent review

The owner authorized up to ten concurrent Astra MAX ACLA actors, with native
self-review disabled and a fresh Astra MAX review assignment after every
completed builder assignment. Follow [BUILD-EXECUTION.md](BUILD-EXECUTION.md) for
ownership and gates. Initial local effect primitives precede transport/integration
so unfinished dependencies do not produce invented competing contracts. The
separate browser MCP decision remains mandatory. VM selection and real-system
acceptance remain open; code/fixture success does not settle them.

Status: planning decisions, not evidence of implementation. D02 and D08 are explicit defaults awaiting the owner's optional clarification. A future implementation change should update the relevant decision rather than silently drifting from the contracts.

## D01 — Small Python application and shared operations

**Decision:** `qorqut-core` owns one Python 3.12 host application, one host SQLite database, one domain-operation layer shared by HTTP/MCP/operator clients, and ordinary files for authored material and raw archives. Source ownership follows D20. D21 adds the explicitly requested independent browser MCP service beside each browser; it does not add another application coordination authority.

Qorqut's Python transport, terminal, browser and native-format knowledge are valuable. Staying in Python makes selective extraction easier and reduces integration languages. The official MCP SDK supplies protocol behavior. Use one validation model family, preferably Pydantic v2 already required by the SDK; do not maintain independent hand-written HTTP and MCP schemas.

SQLite transactions give creation/message/status/event/outbox atomicity on a single host. Raw objects live outside database rows; FTS5 provides local text search. A database server, external queue and external search deployment add failure modes without satisfying an unmet v1 requirement. Revisit only with measured constraints or an approved multi-host requirement.

## D02 — Real VM per ordinary agent

**Status:** proposed default; owner clarification pending.

**Decision:** QEMU/KVM under libvirt with one Linux VM per ordinary agent. Onbash uses a host executor. A VM contains its CLI processes, native stores, Chrome and VNC desktop.

This follows the owner's “own VMs” wording. Containers and host network namespaces do not provide that same boundary. Libvirt is mature lifecycle machinery; writing directly against QEMU's process/QMP lifecycle would increase product code. Firecracker/microVMs are plausible later, but do not simplify a persistent interactive Linux desktop and arbitrary CLI tools in the first implementation.

The inspected machine needs a future prerequisite/install rehearsal. `/dev/kvm` existing is not acceptance. If containers are chosen instead, replace M02's concrete driver and image recipe; preserve stable agent/run identities, runner protocol, disk/profile contracts and the complete-history requirement.

## D03 — Separate authored workspace from native state disk

**Decision:** virtiofs shares the agent's authored workspace. Native CLI HOME, Chrome profile and local recorder/runner state live on a persistent guest-local ext4 disk. A separate writable root overlay survives normal reboots.

Sharing a live browser profile over a general filesystem mount introduces lock/SQLite behavior we do not need. Keeping native stores on ext4 uses the platform's ordinary assumptions. The host's agent folder still owns the VM state disk file and archived transcripts. Stopped exports provide portability without pretending a live disk copy is consistent.

Image rebuild is explicit and creates a new root overlay. It can preserve HOME/workspace while losing arbitrary old system-level modifications; the plan must show this to operators. Browser profile upgrades also limit rollback to older browser versions.

## D04 — Stable logical agent, one managed active run

**Decision:** `agent_id` and main `conversation_id` survive provider and VM changes. At most one managed live CLI owns each agent's input route. Multiple viewers may attach.

This keeps message routing, browser control and observed activity unambiguous. Concurrent independent workers should be independent agents. Native CLI subagents remain child sources within their parent's lineage and are still captured. Supporting several simultaneous top-level runs inside one logical agent is deferred until a concrete workflow requires it.

## D05 — All operations allowed, identity retained

**Decision:** core owns agent personas, descriptive roles and any future permission policy. V1 uses `allow_all`: no role grants, capability policy, signoff approval or business-scope filtering. Every registered actor can call every implemented public v1 operation against any agent. Descriptive roles in `persona.md` guide behavior without granting or restricting operations.

Transport credentials identify the actor and exact runner; validation, idempotency, ownership metadata and generation checks prevent misattribution and accidental wrong-target effects. They do not create privilege tiers. Runtime observations are accepted only from the bound execution source because an observation is evidence, not an arbitrary user assertion.

All agents share a trusted installation. VM separation avoids accidental environment collisions and preserves independent desktops; unrestricted control and host onbash mean this is not a hostile multi-tenant service. Public hosting and separate human access rights require another product decision.

## D06 — Plain CLI execution; provider-specific adapters at the edges

**Decision:** the CLI remains the model executor. Use tmux, a recorder wrapper and Claude/Codex adapters, with CLI-configured model/provider selection. Do not introduce a parallel model SDK conversation runner.

Adapters own preparation, exact native binding, composer interpretation, transcript/child-source discovery and resume. The platform owns identity, files, coordination and durability. Runtime configuration stores structured argv and adapter settings; it does not embed a permanent list of model names.

Unsupported CLIs can eventually use `terminal_only` capability, but complete semantic transcript/restoration support is advertised only for tested adapters. Terminal launch flexibility must not imply a false complete-history guarantee.

## D07 — Raw archive, normalized history and UI are different layers

**Decision:** exact native/terminal bytes are durably captured first. Parsing, conversation rendering and search are rebuildable projections. Every source remains registered independently of the current UI/native session marker.

Qorqut's current visible Chat storage is useful but deliberately incomplete. Its best-effort 8 MiB snapshots and current-source marker do not meet this product's stronger requirement. Build transactional local spooling and immutable host chunks with acknowledgment only after durable storage.

No automatic raw-history retention limit or truncation is included. Full disks cause visible admission/pause behavior. Unknown records and partial lines remain raw evidence. Never claim to preserve provider-internal data that was never emitted.

## D08 — Automatic bounded context restoration

**Status:** proposed default; owner clarification pending.

**Decision:** prefer verified native resume for the same native session. For a new client/provider, deliver current instructions, a captured memory snapshot, unresolved work and a bounded deterministic selection of prior conversation, with exact provenance and omissions recorded.

A complete archive is not an unlimited context window. Replaying every tool/system record as a new privileged instruction is also incorrect. Deterministic selection plus agent-authored memory keeps v1 lean and auditable; no hidden summarizer service or model bill is introduced.

Build packets from retained plain-file snapshots and a recorded raw/parsing checkpoint, not a potentially stale conversation projection. Verified native resume is a separate outcome without an extra automatic startup prompt. Mandatory context must fit the configured budget; optional oversized history becomes explicit references/omissions.

If the owner chooses memory-only startup, change the default packet recipe; do not remove archive/search/source lineage. Identical internal model state across providers is not a promised result.

## D09 — Durable messages and conservative input outcomes

**Decision:** message persistence, terminal submission and semantic handling are separate facts. Use durable stable attempt IDs and guest receipts. An uncertain paste/Enter is never replayed automatically.

SQLite cannot atomically commit with tmux or a remote model. Exactly-once side effects are therefore not promised. Known pre-input failures can retry, identical requests can return existing receipts, and uncertain attempts can be explicitly reconciled. These rules retain Qorqut's useful failure behavior without its role restrictions.

## D10 — One private HTTP/WS runner transport; direct noVNC

**Decision:** one libvirt NAT network, stable reservations, a fixed guest runner port and normal HTTP/WebSocket routes. VNC/CDP stay guest-local; the host proxies the desktop and invokes browser commands through the runner.

This avoids per-agent public port allocation, a custom message multiplexer and another Guacamole service. Same-guest ports can repeat across VMs. Exact agent/generation/boot identity is checked after finding an endpoint; neither IP nor a tmux session name is sufficient identity.

The host-global agent uses the same runner API locally. Its required host ports/displays are persisted and checked for collision, not rediscovered on every reconnect.

D21 adds a separate browser MCP/control endpoint on guest loopback beside Chrome. Native CLIs connect directly to it; the runner forwards host commands/control/relay through its private API. Onbash receives its own persisted browser-service port. This does not expose VNC/CDP publicly.

## D11 — Plain files and a current memory checkpoint

**Decision:** `persona.md`, notes, research, data, downloads, scratch material and memory are ordinary agent-owned files managed by core. `persona.md` is the single authored persona/role/instruction source; generated CLI entrypoints refer to it. Seed it once during creation and preserve later edits. A small folder initializer is enough; no separate agent-template repository or framework is needed. Add atomic API writes, hashes and a bounded memory convenience operation; do not import Qorqut's full document/catalog/revision subsystem.

Direct CLI tools should work normally in `/workspace`. The platform does not claim to version every external filesystem edit. Context packets and exports retain selected exact bytes and their hashes when they need historical evidence. General document versioning or an editor can be added for a demonstrated requirement later.

## D12 — Two explicit backup products

**Decision:** online immutable-archive backup and stopped full-agent/full-installation backup are different operations with different guarantees.

An SQLite snapshot plus its immutable chunk inventory can capture a consistent archive view while new chunks arrive. Browser profiles, native stores, VM disks and mutable workspace files require quiescence for the v1 full-backup guarantee. Stop and verify all included writers; preserve image dependencies and manifest hashes. Live VM migration/snapshots with application consistency are deferred.

Full bundles include runner receipts/spool and browser service command/control receipts even for host onbash. Restore reissues control identities/recovery epoch and holds unfinished external effects for reconciliation; a historical pending record cannot establish that the original system never executed it. Archive-only restore remains read-only and does not resume queues.

## D13 — Single-page chat and backend-first gates

**Decision, amended by the owner's 2026-10-07 direction:** build the frontend in `qorqutbot-ui`, consuming core's HTTP/events/WebSocket contracts. One page has a left agent list, middle conversation and collapsible right status/browser/machine inspector. Browser input requests produce full-width inline interactive cards with top-right modal expansion. File references produce compact durable cards; opening/previewing them is deferred. Use pinned terminal/noVNC assets and the shared backend operations. All application coordination and authoritative configuration remain in core.

Keep an ultra-minimal dark black/white/gray style with thin lines, without purple or futuristic decoration. Owner images will refine layout/styling. A rich editor, business workspace/task manager or large design system is unnecessary. See [UI-PLAN.md](UI-PLAN.md) for the included interactions and acceptance; its chat scope supersedes the initial decision to defer all Chat UI.

## D14 — Source extraction with local provenance

**Decision:** copy only narrowly useful source with its applicable notices and tests; rewrite where coupling costs more than preserving a small contract. Pin the inspected Qorqut and agent-dms revisions in the porting record.

The owner authorizes flexible copying or rewriting. No public distribution license has been selected for this private project. Existing third-party attribution remains attached when code is adapted. The new product must have no runtime import/path dependency on the live Qorqut installation.

## D15 — Explicit control and independently supervised execution

**Decision:** agent runtimes live in owned systemd units/cgroups independent of the host/runner service lifetime; CLI and desktop have separate stop boundaries. Runtime stop is CLI-only; archive closes admission and stops the entire agent environment after invalidating queued lifecycle intent. Detached descendants are included in termination evidence.

Terminal control uses an exact run, a monotonic control epoch and one input viewer. Disconnection leaves human mode until explicit release/takeover; no automatic control expiry is needed in v1. Browser manual control reuses the existing input-request state machine; Done and Cancel each have atomic, distinct notification outcomes. These are coordination rules shared by all actors, not permission tiers.

## D16 — Host pulls persistent runner capture

**Decision:** the host collector pulls bounded pending chunks from the runner and POSTs an ACK only after immutable files and host inventory commit. The recorder never depends on a live network connection or host process. Fetch cursors are pagination, not consumption; reconnect uses durable source watermarks.

This matches the existing private HTTP runner interface without a second inbound upload protocol. A retired authenticated source may still be drained into its original history, while current control remains generation/recovery-bound.

## D17 — Intent-only tools and durable conversation entries

**Decision:** keep `browser_request_input` with a user-action description and add `files_reference(path, ...)`. Agents learn when to request user help or present a file, not how the frontend renders it. Backend operations resolve exact targets and atomically store their domain record/event/conversation entry. The frontend renders typed entries, not a transcript regex that can retrigger effects on replay.

M07 supplies the conversation read projection/entry ledger; M06/M09 retain request/reference authority. Preserve complete raw archives independently. File references point to correctly resolved target-environment files; implicit file snapshots and renderers are not required for this increment.

## D18 — Display name and fixed icon catalog

**Decision:** replace the planned mutable `name` field with `display_name` and add `icon_key`. IDs continue to own paths/routing. One catalog of twenty monochrome icons serves user/agent creation, with a default for omitted icon choice. Inline rename and icon updates use the existing revision-checked agent operation and never restart execution. No second mutable machine name or arbitrary avatar upload is needed.

## D19 — Preserve the dedicated Chrome display alongside machine access

**Decision:** each agent has a TigerVNC display dedicated to its persistent Chrome, even when its VM also has a general graphical console. Browser inline/modal/inspector views share that exact browser surface. Machine is a distinct surface, defaulting to a usable guest desktop through private libvirt VNC; onbash gets its own separate local desktop. P0 proves both paths.

Reuse the noVNC viewer and exact surface/control epoch contracts. Machine takeover coordinates existing terminal/browser gates and releases only its own holds. No browsing profile is cloned for embedding and no existing owner desktop is reused. This adds a surface binding and local supervision, not another application service.

## D20 — Three repositories, one backend authority

**Decision, confirmed by the owner:** consolidate agent management, runtime, recording and communications in `qorqut-core`. Keep `qorqut-browser` reusable and separate. Name the frontend repository `qorqutbot-ui`. These are the three source repositories; there is no separate coordination application or contracts repository.

- **Core** owns agent identity/persona/workspaces, lifecycle, VM/host execution, tmux/CLI adapters, transcripts/continuity, messaging, durable request/history storage, configuration, HTTP/MCP/operator APIs and browser integration. These remain internal modules, with one host database and the small runner.
- **Browser** owns Chrome/profile lifecycle, agent browser-control tools, its own MCP server, takeover-request bridge, TigerVNC/Openbox setup, browser control arbitration and local remote-control transport. It takes explicit configuration and an application handoff endpoint; it imports neither core nor UI. Core's handoff operation commits request/event/job atomically and later commits Done/Cancel with its notification message.
- **UI** owns presentation, interaction, noVNC/terminal viewer assets and transient client state. It reads and changes backend configuration through core operations. It owns no lifecycle decisions, durable coordination store or runner credentials.

Core's installer pins the browser release and protocol; UI consumes core's versioned network API. Browser owns its MCP/control contract, and core owns the application API contract. The independent browser service retains local command/control receipts; core retains the only host coordination database. No Git submodules, database server, broker or generic plugin layer is needed. Core operates headlessly; the UI build can be served as optional static assets or through a same-origin proxy without a source-code dependency from core to UI.

Keep these plans together until implementation bootstrap moves each authoritative document to its owning repository and replaces old copies with links. The current planning publication does not mean the core/browser repositories or packages already exist. Use focused tests at the owning boundary and a few integrated flows; no example apps or duplicate per-repository acceptance suites.

## D21 — Browser runs its own MCP server

**Decision, amended by the owner's direction:** `qorqut-browser` is an independently running MCP server in the normal deployment. Native CLIs configure two servers: core for agents/messages/status/files/history, and browser for `browser_turn`/`browser_request_input`. Core does not import/register browser tool handlers into its MCP endpoint. The same browser server can be reused by another application.

Run one browser service beside each agent's Chrome, including a separately bound onbash instance. Use the official SDK's Streamable HTTP on a private local endpoint, independent of CLI/runner lifetime. Its local control/command receipts persist on the agent's state disk; they are execution evidence, not another message/request database. Direct MCP calls and runner-proxied operations enter the same command lock and control-epoch checks. Service restart preserves Chrome and cannot reset a human hold or replay an uncertain command.

Core supplies configuration and an authenticated HTTP handoff bridge. Browser forwards requests with stable idempotency/actor/target bindings; core atomically persists request/event/job/conversation entry and later Done/Cancel/message. Core reconciles its desired control epoch through the runner to browser's private control interface. Readiness requires browser acknowledgment. If the bridge is unavailable, the tool reports unavailability or an unresolved outcome, never successful notification without a durable receipt. In reuse without a handoff bridge, browser automation works independently and takeover is not advertised. No UI or core Python import is required by browser.

## D41 — Bounded Terminal archival and queued input batches

The recorder's terminal producer uses the existing host Archive independently
of native-session ownership. [TERMINAL-ARCHIVE.md](TERMINAL-ARCHIVE.md) specifies
the normal observer connection and durable receipt-before-ACK contract in the
existing local spool file. No new service or host schema is needed. In parallel,
the UI combines adjacent unsent input bytes behind the existing single in-flight
receipt. It preserves raw byte order, resize barriers and control epochs. These
changes do not enable automated Chat input; native workflow verification keeps
its already frozen executable pins.

## D42 — Present existing runtime observations without granting input

The UI reads selected-agent runtime_get within its existing poll and displays
core's reported logical owner, setup and native-collection states in the
inspector. Unknown/disconnected, stale/error observations and raw/history
coverage remain distinct. Connection is not working/idle or Chat readiness.
No backend schema, operation, launch control or automatic provider selection is
added. The concrete frontend plan lives in qorqutbot-ui RUNTIME-INSPECTOR.md.

## D43 — Short inbox notifications preserve exact stored message bodies

Use a fixed message-ID notification in the native composer and retrieve full
sender/thread/body through MCP. Native input observations exposed long-paste
collapse and Claude TAB expansion. No body is truncated or rewritten. Add exact
message-ID lookup and separate pure layout proof; connect actual dispatch after
the shared input gate is accepted. [Contract and scope](INBOX-NOTIFICATIONS.md).

## D44 — Reuse the recorded terminal gate for automated input

Map native run receipts to the existing recorded terminal ID, lock and human
control epoch. Keep the existing paste/Enter receipt implementation and reject a
second human-control authority. [Concrete interface](SHARED-TERMINAL-INPUT.md).

## D45 — File references record an observed path, not contents

Implement metadata-only references, an explicit conversation variant and dated
publication. Existing host workspaces support relative paths; only onbash accepts
native absolute paths until a VM resolver exists. Idempotent replay precedes
filesystem observation. [Schema and operation](FILE-REFERENCES.md).

## D46 — Dispatch through the incumbent recorded terminal

[Host message dispatch](HOST-MESSAGE-DISPATCH.md) composes D43/D44 with one
durable host attempt ledger, fresh target-specific readiness and receipt-based
recovery. One notification per accepted idle hook prevents queued-message races.
Creation/manual CLI management and the separate handling receipt stay unchanged.
Schema 9 is assigned only to this implementation; no additional service or tool.

## D47 — Present explicit file references

UI cards consume D45's typed historical metadata, preserve full paths and offer
Copy path. They infer nothing from prose and do not open or fetch file contents.
The concrete plan lives in qorqutbot-ui docs/FILE-REFERENCE-CARDS.md.

## D48 — Rescan native sources and observe raw capture per run

The accepted coverage audit exposed permanently cached discovery. The
[collection correction](NATIVE-COLLECTION-CONTINUITY.md) adds bounded periodic
rescans without replacing descriptors before their drain boundary. It also gives
D46 private per-run raw-health evidence so semantic parser/coverage warnings do
not masquerade as primary recording failure. Public gaps stay visible. Shutdown
drain, child/attachment coverage and rename deduplication remain separate work.

## D49 — Bounded burst waiting and observation recovery

The connected Codex run exposed transport refusals, UI disconnection after failed
reads, and slow four-chunk Terminal archival. The
[three correction scopes](RESPONSIVENESS-CORRECTIONS.md) retain four executing
calls with a small timed waiter bound, distinguish failed status reads from actual
terminal changes, and use a bounded time/byte/chunk archive batch. No new service,
schema, retry of possible effects or provider-selection control is introduced.

## D50 — Batch Terminal transfer bytes, preserving original capture records

D49's actual workload still missed its 30-second archival gate despite passing
bounded UI/read checks. [Terminal transfer ranges](TERMINAL-TRANSFER-RANGES.md)
combine up to 128 contiguous original chunks and 256 KiB into one existing host
archive object, with a saved constituent manifest and exact receipt replay.
The recorder and original local coordinate/hash ledger are unchanged. Only the
local transfer-intent table gains a nullable manifest for legacy compatibility;
host schemas/APIs, native archival and observer cadence do not change.

## D51 — Observe the native Codex startup identity before the delayed hook

The accepted cold-start exploration found a held UUID-named writer lock before
Codex's first input. [Startup connection](NATIVE-STARTUP-CONNECTION.md) verifies
that exact native process, lock and recorded-shell ancestry, then uses the
existing incumbent-wins owner boundary. It runs on the existing setup tick,
records a distinct binding observation and creates no native transcript or
input-readiness claim. Claude keeps its real startup hook. First-message capture
bootstrap remains a separate required design; no automatic CLI launch is added.
