# Backend planning documents

The [global implementation plan](IMPLEMENTATION-PLAN.md) is the entry point and the authority for scope, dependencies and delivery gates. Module plans expand it; they do not silently override shared contracts.

| Document | Purpose |
| --- | --- |
| [Implementation plan](IMPLEMENTATION-PLAN.md) | Requirements, work packages, dependency order, milestones and definition of completion |
| [Architecture](ARCHITECTURE.md) | Processes, filesystem layout, ownership, networking and cross-module flows |
| [Contracts](CONTRACTS.md) | IDs, state machines, data records, operations, event and delivery semantics |
| [Chat workspace plan](UI-PLAN.md) | Three-column layout, dedicated Chrome/machine surfaces, intent tools, inline browser/file cards, twenty icons, rename and visual direction |
| [Decisions](DECISIONS.md) | Chosen defaults, rationale, alternatives and unresolved owner preferences |
| [Porting plan](PORTING.md) | Exact Qorqut source baselines; extract, adapt, rewrite and omit decisions |
| [Acceptance plan](ACCEPTANCE.md) | Traceable requirements, fault injection, VM/CLI verification and release gates |
| [Research baseline](RESEARCH.md) | What was actually inspected and rerun, including limitations and failures |

## Module plans

| ID | Module | Owns |
| --- | --- | --- |
| M00 | [Foundation and storage](modules/00-foundation.md) | Application operations, SQLite, events, jobs, idempotency and file receipts |
| M01 | [Agents, workspaces and status](modules/01-agents.md) | Identity, display name/icon, creation, folder ownership, directory, status and observed activity |
| M02 | [Environments and VM lifecycle](modules/02-environments.md) | libvirt/QEMU, disks, network, images and host execution selection |
| M03 | [Runner and terminal transport](modules/03-runner.md) | Guest/host control bridge, tmux, process binding and durable input outcomes |
| M04 | [CLI adapters and preparation](modules/04-cli-adapters.md) | Claude/Codex configuration, hooks, readiness, native sources and resume |
| M05 | [Messaging and delivery](modules/05-messaging.md) | Full messages, threads, delivery attempts, explicit handling and offline queues |
| M06 | [Browser, desktop and human input](modules/06-browser.md) | Persistent Chrome, automation, VNC, control ownership and input requests |
| M07 | [Transcript capture and history](modules/07-transcripts.md) | Recorder, all-source registry, durable spool/archive, parsing/search and conversation read projection |
| M08 | [Continuity and runtime switching](modules/08-continuity.md) | Saved context, native resume, provider changes and bounded restoration packets |
| M09 | [Files, memory and portable export](modules/09-files.md) | Authored workspace, file references, atomic file operations, memory and agent export format |
| M10 | [HTTP, MCP, operator client and chat UI](modules/10-api.md) | Public transports, identity binding, one-page conversation, browser/file cards, viewers and inspector |
| M11 | [Operations, backup and quality](modules/11-operations.md) | Installation, supervision, capacity, backup/restore, upgrades and release evidence |

## Status vocabulary

`Planned` means specified but unimplemented. `Implemented` requires source and focused tests. `Verified` requires the stated acceptance evidence for a named build and environment. Publication of a plan does not advance either status.

All twelve modules are **Planned** in this baseline dated 2026-10-07.
