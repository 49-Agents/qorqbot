# Working on Qorqutbot

This is one repository with three modules. Read `docs/MONOREPO.md`,
`core/docs/IMPLEMENTATION-PLAN.md`, `core/docs/CONTRACTS.md`, and the owning
module's AGENTS.md and plan before implementation. Earlier three-repository
requirements are superseded by the owner's monorepo decision.

- `core/` owns coordination, application configuration, agents, runtime, inbox,
  messaging and history. `browser/` owns its independent MCP service and browser
  tooling. `ui/` owns presentation. Keep these boundaries and separate builds;
  browser imports neither core nor UI, and UI uses the core API.
- Inspect Git status first. Preserve existing work and develop on a dedicated
  `codex/` branch in an isolated worktree. The initial monorepo import is an
  explicitly authorized bootstrap. Related merges and pushes have standing
  owner authorization; deployments must stay within the requested scope.
- This migration does not move or restart the existing installation. Never use
  owner sessions, profiles, credentials or databases as test fixtures.
- Preserve native capture, durable messages and handling receipts. Chat is a
  derived view, not a complete raw archive. Creating an agent prepares its
  workspace/shell; the owner starts the CLI and manages its provider/model.
- Keep tests focused on changed behavior. Do not add example applications,
  duplicate suites or a new framework merely because modules share a repository.
- Keep operational data outside Git. Preserve source notices. Update the global
  plan when architecture or public contracts change, and report bounded checks
  honestly.
- Author AND committer timestamps must never fall between 09:30 and 20:30
  inclusive on weekdays in `America/Puerto_Rico`. Enable `.githooks` and run
  `python3 scripts/check_commit_dates.py --all` before publishing. The imported
  history uses October 5–9, 2026, 07:05–08:59 UTC−4 by explicit owner request.
