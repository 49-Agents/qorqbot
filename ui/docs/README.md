# Documentation index

The owner agreed D36 on 2026-10-08: manual CLI startup/configuration in Terminal,
with one logical inbox-owning session and Chat as another view. Read the
[owning core scope](https://github.com/qorqutbot/qorqut-core/blob/main/docs/TERMINAL-OWNED-SESSIONS.md)
and [the current UI correction](MANUAL-CREATION.md). Earlier provider/start and
automatic-switch requirements below are superseded; terminal/browser integration
still needs its actual backend contract.

Authoritative plans now follow the three repositories:

- [Core documentation](https://github.com/qorqutbot/qorqut-core/blob/main/docs/README.md): global implementation plan, architecture, contracts, decisions, porting, acceptance and core modules.
- [Browser plan](https://github.com/qorqutbot/qorqut-browser/blob/main/docs/BROWSER-PLAN.md): independent browser MCP, controls, TigerVNC and core handoff integration.
- [UI plan](UI-PLAN.md): single-page chat, agents, viewers and inspector.
- [First connected interface](SHELL-IMPLEMENTATION.md): concrete shell, API client,
  sign-in, durable composer and direct browser checks.
- [First UI acceptance](evidence/ui-shell-manager-acceptance.md): independently
  reviewed shell and real compiled-UI/core checks, including dated files and restart.

Original planning baseline: `codex/chat-ui-plan`, `e7d2b56`. Acceptance is bounded
to the evidence above; native replies, runtime and browser/file cards remain pending.
