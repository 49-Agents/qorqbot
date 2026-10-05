# Architecture decisions

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
