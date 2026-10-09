Current repository layout: [D61, one Qorqbot repository](../../docs/MONOREPO.md).
The module boundaries remain; older references to separate repositories describe
the pre-migration arrangement.

# Browser documentation

- [Browser work package and ownership](BROWSER-PLAN.md)
- [Browser MCP/control/handoff protocol v1](PROTOCOL.md)
- [Local desktop setup and operations](OPERATIONS.md)
- [Host desktop functional evidence](evidence/browser-desktop.md)
- [Human viewer relay evidence](evidence/browser-human-stream.md)
- [Core architecture](../../core/docs/ARCHITECTURE.md)
- [Shared integration contracts](../../core/docs/CONTRACTS.md)
- [Implementation order and gates](../../core/docs/IMPLEMENTATION-PLAN.md)
- [Source porting references](../../core/docs/PORTING.md)
- [Acceptance requirements](../../core/docs/ACCEPTANCE.md)
- [UI plan](../../ui/docs/UI-PLAN.md)

The browser plan owns local service behavior. Core owns application state and coordination. Package wire schemas will be implemented here and consumed through a versioned client; no fourth repository is needed.
