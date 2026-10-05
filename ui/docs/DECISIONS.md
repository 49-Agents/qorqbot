# Architecture decisions

Status: planning decisions, not evidence of implementation. D02 and D08 are explicit defaults awaiting the owner's optional clarification. A future implementation change should update the relevant decision rather than silently drifting from the contracts.

## D01 — Small Python application and shared operations

**Decision:** one Python 3.12 host application, one SQLite database, one domain-operation layer shared by HTTP/MCP/operator clients, and ordinary files for authored material and raw archives.

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

**Decision:** no role grants, capability policy, signoff approval or business-scope filtering. Every registered actor can call every implemented public v1 operation against any agent.

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

## D11 — Plain files and a current memory checkpoint

**Decision:** notes, research, downloads, scratch material and memory are ordinary agent-owned files. Add atomic API writes, hashes and a bounded memory convenience operation; do not import Qorqut's full document/catalog/revision subsystem.

Direct CLI tools should work normally in `/workspace`. The platform does not claim to version every external filesystem edit. Context packets and exports retain selected exact bytes and their hashes when they need historical evidence. General document versioning or an editor can be added for a demonstrated requirement later.

## D12 — Two explicit backup products

**Decision:** online immutable-archive backup and stopped full-agent/full-installation backup are different operations with different guarantees.

An SQLite snapshot plus its immutable chunk inventory can capture a consistent archive view while new chunks arrive. Browser profiles, native stores, VM disks and mutable workspace files require quiescence for the v1 full-backup guarantee. Stop and verify all included writers; preserve image dependencies and manifest hashes. Live VM migration/snapshots with application consistency are deferred.

Full bundles include runner receipts/spool even for host onbash. Restore reissues control identities/recovery epoch and holds unfinished external effects for reconciliation; a historical pending record cannot establish that the original system never executed it. Archive-only restore remains read-only and does not resume queues.

## D13 — Minimal console and backend-first gates

**Decision:** build a small static console for agent rows, status, terminal, desktop and pending human input. Use pinned terminal/noVNC assets. HTTP/MCP and an operator client provide the full backend surface.

A polished Chat interface, business workspace, task manager, visual file editor and design system are not prerequisites. The backend still needs clear observed state and recovery controls; a blank API-only experience would make browser login and terminal setup difficult to verify.

## D14 — Source extraction with local provenance

**Decision:** copy only narrowly useful source with its applicable notices and tests; rewrite where coupling costs more than preserving a small contract. Pin the inspected Qorqut and agent-dms revisions in the porting record.

The owner authorizes flexible copying or rewriting. No public distribution license has been selected for this private project. Existing third-party attribution remains attached when code is adapted. The new product must have no runtime import/path dependency on the live Qorqut installation.

## D15 — Explicit control and independently supervised execution

**Decision:** agent runtimes live in owned systemd units/cgroups independent of the host/runner service lifetime; CLI and desktop have separate stop boundaries. Runtime stop is CLI-only; archive closes admission and stops the entire agent environment after invalidating queued lifecycle intent. Detached descendants are included in termination evidence.

Terminal control uses an exact run, a monotonic control epoch and one input viewer. Disconnection leaves human mode until explicit release/takeover; no automatic control expiry is needed in v1. Browser manual control reuses the existing input-request state machine; Done and Cancel each have atomic, distinct notification outcomes. These are coordination rules shared by all actors, not permission tiers.

## D16 — Host pulls persistent runner capture

**Decision:** the host collector pulls bounded pending chunks from the runner and POSTs an ACK only after immutable files and host inventory commit. The recorder never depends on a live network connection or host process. Fetch cursors are pagination, not consumption; reconnect uses durable source watermarks.

This matches the existing private HTTP runner interface without a second inbound upload protocol. A retired authenticated source may still be drained into its original history, while current control remains generation/recovery-bound.
