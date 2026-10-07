# Backend planning documents

Start with [Terminal-owned sessions](TERMINAL-OWNED-SESSIONS.md) for the owner's
2026-10-08 scope correction: manual CLI startup and one logical inbox owner.

Current parallel implementation: [inbox ownership](INBOX-OWNERSHIP.md),
[recorded shells](RECORDED-SHELL.md), and
[adapter rehearsal fixes](ADAPTER-REHEARSAL-FIXES.md). D37 assigns disjoint
ownership and the following combined integration check.
The local inbox-owner increment has passed [bounded acceptance](evidence/inbox-ownership-manager-acceptance.md);
it does not yet connect a native CLI to Chat.
The local recorded-shell increment has also passed
[bounded acceptance](evidence/recorded-shell-manager-acceptance.md); its host
activation and Terminal UI connection remain pending.
Manual creation has passed [scoped review](evidence/manual-creation-manager-acceptance.md)
and its [combined core/UI check](MANUAL-CREATION-INTEGRATION.md) is now in
independent review, alongside the completed adapter corrections.

The [global implementation plan](IMPLEMENTATION-PLAN.md) is the entry point and the authority for scope, dependencies and delivery gates. Module plans expand it; they do not silently override shared contracts.

The agreed repositories are `qorqut-core` (backend/configuration/coordination), `qorqut-browser` (its own MCP server and browser tooling) and `qorqutbot-ui` (frontend). Agents connect to separate core/browser MCP endpoints. The backend plans now live here in core; browser owns its work package and UI owns its chat plan. Link files preserve navigation from the original planning checkout. Module numbers identify work packages.

| Document | Purpose |
| --- | --- |
| [Implementation plan](IMPLEMENTATION-PLAN.md) | Requirements, work packages, dependency order, milestones and definition of completion |
| [Architecture](ARCHITECTURE.md) | Three-repository boundaries, configuration ownership, processes, filesystem layout, networking and cross-module flows |
| [Contracts](CONTRACTS.md) | IDs, state machines, data records, operations, event and delivery semantics |
| [First core HTTP/MCP contract](CORE-API.md) | Concrete local transport, attribution, schemas and operator-client scope over the reviewed foundation |
| [First agent-management increment](AGENT-REGISTRY.md) | Concrete identity/onbash, directory/persona seeding, status and five-operation contract; explicit runtime dependency |
| [Durable chat and transcript files](CHAT-IMPLEMENTATION.md) | Message/inbox/handling operations, conversation cursors and automatic dated publication using existing file receipts |
| [Console server](CONSOLE-SERVER.md) | Headless job processing, receipt recovery, browser sessions and optional same-origin static UI |
| [Chat/server integration](CHAT-SERVER-INTEGRATION.md) | Combined real-server dated publication and restart recovery, without manual queue pumping or an open UI |
| [Live recorder observation](RECORDER-PROBE.md) | Bounded local query of the actual capture loop; separate from native/provider readiness |
| [Durable host transcript intake](HOST-ARCHIVE.md) | Immutable host chunk receipts, fsync-before-ACK, source lineage, conflicts and restore holds |
| [Native prefix verification](NATIVE-PREFIX.md) | Read-only receipt pages and bounded verification of an open file after local payload reclamation; prerequisite to native-reader composition |
| [Chat workspace plan](UI-PLAN.md) | Three-column layout, dedicated Chrome/machine surfaces, intent tools, inline browser/file cards, twenty icons, rename and visual direction |
| [Decisions](DECISIONS.md) | Chosen defaults, rationale, alternatives and unresolved owner preferences |
| [Porting plan](PORTING.md) | Exact Qorqut source baselines; extract, adapt, rewrite and omit decisions |
| [Acceptance plan](ACCEPTANCE.md) | Traceable requirements, fault injection, VM/CLI verification and release gates |
| [Research baseline](RESEARCH.md) | What was actually inspected and rerun, including limitations and failures |

## Module plans

Implementation is managed through [BUILD-EXECUTION.md](BUILD-EXECUTION.md), which
records staged ownership, the current owner launch policy and independent review.

The [isolated native rehearsal](NATIVE-REHEARSAL.md) is complete within its
[accepted evidence](evidence/native-rehearsal-manager-acceptance.md). D37 uses
those observations without treating them as a connected runtime.

| ID | Module | Repository | Owns |
| --- | --- | --- | --- |
| M00 | [Foundation and storage](modules/00-foundation.md) | `qorqut-core` | Configuration, application operations, SQLite, events, jobs, idempotency and file receipts |
| M01 | [Agents, workspaces and status](modules/01-agents.md) | `qorqut-core` | Identity/persona, descriptive roles and v1 policy, display name/icon, creation, folder ownership, status and activity |
| M02 | [Environments and VM lifecycle](modules/02-environments.md) | `qorqut-core` | libvirt/QEMU, disks, network, images and host execution selection |
| M03 | [Runner and terminal transport](modules/03-runner.md) | `qorqut-core` | Guest/host control bridge, tmux, process binding and durable input outcomes |
| M04 | [CLI adapters and preparation](modules/04-cli-adapters.md) | `qorqut-core` | Claude/Codex configuration, hooks, readiness, native sources and resume |
| M05 | [Messaging and delivery](modules/05-messaging.md) | `qorqut-core` | Full messages, threads, delivery attempts, explicit handling and offline queues |
| M06 | [Browser, desktop and human input](modules/06-browser.md) | `qorqut-browser` + core integration | Independent browser MCP/tools/setup; core owns handoff API, durable requests, notifications and machine-wide coordination |
| M07 | [Transcript capture and history](modules/07-transcripts.md) | `qorqut-core` | Recorder, all-source registry, spool/archive, parsing/search and conversation read projection |
| M08 | [Continuity and runtime switching](modules/08-continuity.md) | `qorqut-core` | Saved context, native resume, provider changes and bounded restoration packets |
| M09 | [Files, memory and portable export](modules/09-files.md) | `qorqut-core` | Authored workspace, file references, atomic file operations, memory and agent export format |
| M10 | [HTTP, MCP, operator client and chat UI](modules/10-api.md) | Core transports + `qorqutbot-ui` | Core API/identity/proxies; UI conversation, cards, viewers and inspector |
| M11 | [Operations, backup and quality](modules/11-operations.md) | `qorqut-core` | Installation, supervision, capacity, backup/restore, version compatibility and release evidence |

## Status vocabulary

`Planned` means specified but unimplemented. `Implemented` requires source and focused tests. `Verified` requires the stated acceptance evidence for a named build and environment. Publication of a plan does not advance either status.

All twelve modules began as **Planned** in the 2026-10-07 baseline. Current
accepted increments and their remaining work are recorded in
[BUILD-EXECUTION.md](BUILD-EXECUTION.md); bounded acceptance does not complete a
whole module or a system gate.
