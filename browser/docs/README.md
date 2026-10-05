# Browser documentation

- [Browser work package and ownership](BROWSER-PLAN.md)
- [Core architecture](https://github.com/qorqutbot/qorqut-core/blob/main/docs/ARCHITECTURE.md)
- [Shared integration contracts](https://github.com/qorqutbot/qorqut-core/blob/main/docs/CONTRACTS.md)
- [Implementation order and gates](https://github.com/qorqutbot/qorqut-core/blob/main/docs/IMPLEMENTATION-PLAN.md)
- [Source porting references](https://github.com/qorqutbot/qorqut-core/blob/main/docs/PORTING.md)
- [Acceptance requirements](https://github.com/qorqutbot/qorqut-core/blob/main/docs/ACCEPTANCE.md)
- [UI plan](https://github.com/qorqutbot/qorqutbot-ui/blob/codex/chat-ui-plan/docs/UI-PLAN.md)

The browser plan owns local service behavior. Core owns application state and coordination. Package wire schemas will be implemented here and consumed through a versioned client; no fourth repository is needed.
