# qorqutbot-ui

A chat UI for persistent agents that work through native CLI sessions, keep their own files and browser, and communicate through Qorqut MCP. The backend lives in the planned `qorqut-core` repository and browser tooling in `qorqut-browser`.

**Current state: planning baseline only.** This repository currently holds the shared implementation plans, including the backend and module specifications. The three-repository split is agreed; core/browser packages and the UI are not implemented. Plans will move to their owning repositories during implementation bootstrap.

The intended v1 has one persistent folder and Linux VM per ordinary agent, one host-based global agent named `onbash`, tmux-backed CLI execution, persistent Chrome profiles and desktops, direct messaging, complete status history, and durable transcript collection. The first supported CLI adapters are Claude Code and Codex; platform identity and history do not depend on either client or model provider. Core owns editable personas and descriptive roles; all registered actors can use all v1 operations under `allow_all`.

Start with the [global implementation plan](docs/IMPLEMENTATION-PLAN.md). It owns sequencing and completion gates. The [documentation index](docs/README.md) links the architecture, contracts, source-porting decisions, acceptance plan, and twelve module plans.

The [chat workspace plan](docs/UI-PLAN.md) records the owner's UI direction: agents on the left, chat in the center, a collapsible activity/browser/machine inspector on the right, and a minimal monochrome dark style. Browser-action tools produce inline live Chrome cards with expansion; file-reference tools produce durable compact cards, with opening/preview deferred. Agents have separate display names and one of twenty icons, plus inline rename.

Two defaults remain explicit design assumptions until the owner answers them: real VMs rather than containers, and automatic restoration of bounded saved context when changing clients. See [decisions D02 and D08](docs/DECISIONS.md). They do not change the requirement to retain complete captured history.

## Design direction

- Three repositories: `qorqut-core` for backend/configuration/coordination, `qorqut-browser` for reusable browser tooling, and `qorqutbot-ui` for the frontend. See [the ownership boundary](docs/ARCHITECTURE.md#2-repository-and-package-boundaries).
- One Python backend application, one host SQLite database, ordinary files, and a small shared runner for VM and host execution. Core uses the browser package and works without a UI.
- Stable agent identity; replaceable CLI runs, VM generations and browser processes.
- Reuse narrowly extracted Qorqut mechanisms and focused behavioral tests. Keep business workflows and permission enforcement out of v1; no example apps or duplicate test suites.
- Preserve raw transcript bytes separately from searchable conversation projections and context supplied to a new model.
- Make crashes, uncertain delivery, disconnected guests, and full disks explicit states with tested recovery.
- Keep the chat UI focused on conversation, inline browser handoffs, file references and inspection, with each agent's dedicated TigerVNC Chrome surface separate from machine control.

## Repository and runtime data

This is the UI source destination and current planning home. Agent workspaces, browser profiles, credentials, VM disks and transcripts belong in core's separately configured runtime data directory, never in Git.

The initial planning baseline is on `main`. Future implementation uses scoped branches and follows [AGENTS.md](AGENTS.md). No implementation or deployment is implied by publication of these plans.
