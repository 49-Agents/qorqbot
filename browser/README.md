# qorqut-browser

Independent browser MCP service for persistent agent-owned Chrome profiles, agent browser tools, TigerVNC remote control and application-integrated human takeover. It imports neither core nor UI.

The standalone local package includes a foreground Chrome/TigerVNC/Openbox
supervisor, an independent MCP/HTTP service and durable command/control receipts.
See [setup and operations](docs/OPERATIONS.md), the [wire protocol](docs/PROTOCOL.md)
and [functional evidence](docs/evidence/browser-desktop.md). Core/UI handoff,
noVNC/input fencing and real-guest acceptance remain separate integration work.
