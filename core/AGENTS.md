# Working on qorqut-core

Read `docs/IMPLEMENTATION-PLAN.md`, `docs/CONTRACTS.md`, and the applicable module plan before implementation. This is a backend-first project. The planning baseline is not evidence that a feature exists or has passed acceptance.

## Engineering rules

- Preserve the owner's requirements: stable agent identity and folder, per-agent execution/browser, host-global onbash, unrestricted v1 operations, durable communication/status/creation history, and provider-independent captured history.
- Keep one core host application and a small runner, plus the owner's explicitly requested independent browser MCP service beside each browser. Other module boundaries do not justify another service, database server, broker, framework, or generic plugin system.
- Follow the monorepo module boundary: `core/` (`qorqut-core`) owns backend coordination/configuration and agents/runtime/messaging/history; `browser/` (`qorqut-browser`) owns browser tooling and its separate MCP server; `ui/` (`qorqutbot-ui`) owns the frontend. Core runs headlessly. Keep the UI free of backend business rules and browser free of core/UI imports. Register browser tools only on browser's MCP endpoint.
- Reuse Qorqut selectively using `docs/PORTING.md`; do not import its live installation, database, business roles, private records, credentials, or configuration into this repository.
- Keep raw archives and derived views separate. Never call a visible Chat projection a complete transcript archive. Never call pasted input processed work or silently retry an uncertain external effect.
- Use the shared application operations from HTTP, MCP and the operator client. Transport adapters do not implement separate business rules.
- Inspect Git status first. Preserve other work. After the initial repository bootstrap, implement on dedicated branches and isolated worktrees under `../worktrees/<task>`. Do not merge or deploy without authorization covering that action.
- The owner granted standing authorization on 2026-10-07 for merges and pushes related to this project across `qorqut-core`, `qorqut-browser`, and `qorqutbot-ui`. Manager may integrate and merge reviewed changes without another permission request. Workers retain their assigned ownership and merge scope; this does not authorize deployment or settle the open VM choice.
- The owner explicitly authorized the initial planning repository and its first `main` commit/push. That bootstrap exception does not authorize future direct development on `main`.
- Run focused meaningful tests for changed contracts. Integration tests use temporary data, isolated ports/VMs and synthetic transcripts. Existing Qorqut services and owner sessions are not test fixtures.
- Keep tests at the boundary that owns the behavior, with a few shared integration flows. Do not add usage examples, example applications or duplicated test suites just to demonstrate repository reuse.
- Commit task-owned plans, implementation, tests and evidence together. Report branch/commit, checks, pushed status, and merge/deployment status accurately.
- Update the global plan when module boundaries, dependencies, public contracts or acceptance gates change. Record a decision before relying on a new architecture assumption.
- Keep secrets, browser profiles, native CLI stores, transcripts, VM disks and operational backups outside Git. Preserve applicable notices when adapting source.

<!-- ACLA LOCAL EXECUTOR POLICY -->
## Local ACLA launch policy

For new ACLA Workers on this machine, this owner-selected policy takes precedence
over packaged skill defaults:
- General work: Claude Code, Sonnet 5.5, xhigh effort; self-review disabled.
- UI work (frontend components, layout, styling, interaction, or UI fixes):
  Claude Code, Sonnet 5.5, medium effort; self-review disabled.
- Manager selects the UI exception at launch. Use
  --executor-backend claude-code --worker-model claude-sonnet-5-5
  --worker-effort medium --reviewLoop false for UI assignments; use
  --worker-effort xhigh instead for other assignments.
- Do not change effort based on complexity, task length, or Worker preference.
  A newer explicit owner instruction may override this policy.
- Existing Workers retain their saved model/backend/effort; do not silently
  change their identity to apply new defaults.
- Self-review disabled means no internal reviewer subagents. Manager still
  receives completion reports and reviews unless the owner says otherwise.
<!-- END ACLA LOCAL EXECUTOR POLICY -->
