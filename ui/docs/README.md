# Backend planning documents

The [global implementation plan](IMPLEMENTATION-PLAN.md) is the entry point and the authority for scope, dependencies and delivery gates. Module plans expand it; they do not silently override shared contracts.

The agreed repositories are `qorqut-core` (backend/configuration/coordination), `qorqut-browser` (browser tooling) and `qorqutbot-ui` (frontend). These plans remain together here until implementation bootstrap moves each authoritative document to its owner. The module numbers are work packages, not repositories or services.

| Document | Purpose |
| --- | --- |
| [Implementation plan](IMPLEMENTATION-PLAN.md) | Requirements, work packages, dependency order, milestones and definition of completion |
| [Architecture](ARCHITECTURE.md) | Three-repository boundaries, configuration ownership, processes, filesystem layout, networking and cross-module flows |
| [Contracts](CONTRACTS.md) | IDs, state machines, data records, operations, event and delivery semantics |
| [Chat workspace plan](UI-PLAN.md) | Three-column layout, dedicated Chrome/machine surfaces, intent tools, inline browser/file cards, twenty icons, rename and visual direction |
| [Decisions](DECISIONS.md) | Chosen defaults, rationale, alternatives and unresolved owner preferences |
| [Porting plan](PORTING.md) | Exact Qorqut source baselines; extract, adapt, rewrite and omit decisions |
| [Acceptance plan](ACCEPTANCE.md) | Traceable requirements, fault injection, VM/CLI verification and release gates |
| [Research baseline](RESEARCH.md) | What was actually inspected and rerun, including limitations and failures |

## Module plans

| ID | Module | Repository | Owns |
| --- | --- | --- | --- |
| M00 | [Foundation and storage](modules/00-foundation.md) | `qorqut-core` | Configuration, application operations, SQLite, events, jobs, idempotency and file receipts |
| M01 | [Agents, workspaces and status](modules/01-agents.md) | `qorqut-core` | Identity/persona, descriptive roles and v1 policy, display name/icon, creation, folder ownership, status and activity |
| M02 | [Environments and VM lifecycle](modules/02-environments.md) | `qorqut-core` | libvirt/QEMU, disks, network, images and host execution selection |
| M03 | [Runner and terminal transport](modules/03-runner.md) | `qorqut-core` | Guest/host control bridge, tmux, process binding and durable input outcomes |
| M04 | [CLI adapters and preparation](modules/04-cli-adapters.md) | `qorqut-core` | Claude/Codex configuration, hooks, readiness, native sources and resume |
| M05 | [Messaging and delivery](modules/05-messaging.md) | `qorqut-core` | Full messages, threads, delivery attempts, explicit handling and offline queues |
| M06 | [Browser, desktop and human input](modules/06-browser.md) | `qorqut-browser` + core integration | Browser mechanisms/tools/setup; core owns durable requests, notifications and machine-wide coordination |
| M07 | [Transcript capture and history](modules/07-transcripts.md) | `qorqut-core` | Recorder, all-source registry, spool/archive, parsing/search and conversation read projection |
| M08 | [Continuity and runtime switching](modules/08-continuity.md) | `qorqut-core` | Saved context, native resume, provider changes and bounded restoration packets |
| M09 | [Files, memory and portable export](modules/09-files.md) | `qorqut-core` | Authored workspace, file references, atomic file operations, memory and agent export format |
| M10 | [HTTP, MCP, operator client and chat UI](modules/10-api.md) | Core transports + `qorqutbot-ui` | Core API/identity/proxies; UI conversation, cards, viewers and inspector |
| M11 | [Operations, backup and quality](modules/11-operations.md) | `qorqut-core` | Installation, supervision, capacity, backup/restore, version compatibility and release evidence |

## Status vocabulary

`Planned` means specified but unimplemented. `Implemented` requires source and focused tests. `Verified` requires the stated acceptance evidence for a named build and environment. Publication of a plan does not advance either status.

All twelve modules are **Planned** in this baseline dated 2026-10-07.
