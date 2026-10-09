# qorqut-browser

Independent browser MCP service within [Qorqbot](../README.md), with persistent
agent-owned Chrome profiles, agent browser tools and TigerVNC human control.
It imports neither core nor UI and keeps its own dependencies and service process.

See [setup and operations](docs/OPERATIONS.md), the [wire protocol](docs/PROTOCOL.md),
and [connected core/browser/UI evidence](../core/docs/evidence/browser-connected-manager-acceptance.md).
That evidence covers the local host flow; real-guest and full-system acceptance
remain unfinished. Common build commands live in [development](../docs/DEVELOPMENT.md).
