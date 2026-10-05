# M10 — HTTP, Qorqut MCP, operator client and chat workspace

Status: Planned. Owns R02/R08/R12/R13/R15/R16 transport/presentation behavior. Depends on M00 from the first phase and integrates each domain as it becomes available, especially M01/M06/M07/M09 for the chat scope.

Repositories: `qorqut-core` owns HTTP/MCP, operator commands, sessions, application configuration API and stream proxies. `qorqutbot-ui` owns the frontend, API client and viewer assets. This is a cross-repository work package, not a reason to mix source ownership.

## Outcome and boundary

The owner and agents use core's shared application operations. Core MCP advertises discovery, lifecycle, messages, status, request completion and history/file operations. Browser's separate MCP server advertises browser actions and user-input requests. A small operator client and one-page chat workspace make conversation, browser intervention and recovery practical. [UI-PLAN.md](../UI-PLAN.md) specifies the owner's layout/interactions and supersedes the initial generic console scope.

Transports adapt schemas, authentication and streaming. They do not implement domain rules or query/write tables behind the application layer. Every registered actor can invoke the same implemented public operations; there is no role-conditioned tool inventory or signoff path.

Core runs fully headless and owns all application coordination/configuration. UI reads non-secret effective configuration and submits supported changes through `config_get/update` or agent/runtime operations; core validates and commits them. The frontend owns only presentation state. Publish core API schemas/version from core, and let UI consume them with a small typed client; no fourth contracts repository or copied business-rule layer. Reject unsupported API capabilities before dependent actions.

## Public surfaces

- `/mcp`: core's official SDK Streamable HTTP endpoint, configured in both CLIs as `qorqut-core`. A separate `qorqut-browser` registration targets browser's local service; no browser tool handlers are registered here.
- `/api/v1/operations/<name>`: shared typed operations with the result/idempotency contract.
- `/api/v1/events`: reconnectable event stream with durable cursor and recovery epoch; reconnect after restore invalidates old-epoch cursors. Events are hints to reread current records when appropriate.
- `/api/v1/agents/<id>/terminal/<run_id>/ws`: exact-run terminal attachment.
- `/api/v1/agents/<id>/surfaces/<surface_id>/ws`: exact browser/machine-control-bound VNC proxy, routed through runner or private libvirt console as appropriate.
- `/viewers/desktop`: UI-owned same-origin viewer shell addressed by agent/surface/request IDs, without credentials in the URL; suitable for inline/modal/inspector presentation. Its stream endpoint remains core-owned.
- `/api/v1/artifacts/<reference>`: bounded/downloadable file/raw-history content with path/identity validation.
- `/health/live`, `/health/ready`: process liveness versus ability to perform required durable work; readiness includes storage and worker health but does not falsely certify every agent.

Use the operation inventory in [CONTRACTS.md](../CONTRACTS.md). Only advertise implemented handlers. Generated MCP tool schemas and HTTP validation must come from the same models. Mutations carry idempotency keys; browser/terminal reads and viewer connections never launch resources.

Describe `browser_request_input` by the user action needed and `files_reference` by the file to show/review/notify. Do not put iframe/modal/toast/layout instructions in agent-facing tool descriptions or require agents to construct viewer URLs. Shared operations resolve target identities and commit cards atomically; transports publish only committed state, never infer effects from tool-name text in a transcript.

Browser owns its MCP models/descriptions/handlers. Core provides an authenticated handoff HTTP bridge to its shared operations and a versioned client through the runner for browser control/commands. Keep browser tools out of core's MCP inventory. Core HTTP/operator routes may forward browser commands through the same browser-service gate; they do not execute a second implementation. File-reference models remain in core. Tools depend on durable operations, not the presence of a frontend connection.

## Identity without permissions

Use a registered transport identity for each logical agent and the installation owner. The server derives `actor_id`; a body-supplied sender cannot impersonate another agent. Local operator initialization issues credentials without scanning personal config.

For the console, exchange an installation-local sign-in credential for a same-origin HttpOnly session and validate mutation/WebSocket origins. Do not put runner credentials in JS or URLs/logs. These transport controls establish attribution and prevent accidental cross-site requests; they do not restrict which v1 operations a registered actor may perform.

The private libvirt interface and configured owner-facing listen address are explicit installation inputs. Default exposure is local/private, not a public hosted product. Retain ordinary TLS deployment support without inventing a separate account/login platform.

## Single-page workspace

The independently built `qorqutbot-ui` frontend presents three columns, with no separate agent-detail navigation page:

1. Left: agent list with display name/icon, attention/state hints and creation picker with twenty icons. Click selects chat; double-click name edits it, with an accessible keyboard/touch Rename alternative. IDs remain available in details/disambiguation.
2. Center: `conversation_list` history and durable `messages_send` composer, preserving drafts/scroll. Browser-action cards contain a full-width inline interactive viewer and top-right modal expansion of the same session, with Done/Cancel/note. File-reference cards show metadata/copy path; direct opening/preview is deferred.
3. Right: collapsible inspector with what the agent is doing, activity/freshness, Browser and separate Machine/Desktop, terminal and compact recovery details. Observer mode alone never changes control; takeover uses the existing typed operations/epochs.

Use pinned upstream viewer assets and modest CSS/JS. Ultra-minimal dark black/white/gray surfaces, thin lines and restrained typography; no purple, neon or futuristic decoration. Reference images will refine exact proportions. On narrow screens use navigation/inspector drawers so chat stays usable. No business dashboard, rich editor or generic widget framework is required.

Serve the static UI and core API at one browser origin through a conventional proxy or optional core static-file mount. UI code never imports Python modules, accesses SQLite or receives runner credentials. Building core does not require UI sources/assets; updating the UI does not migrate or restart agents. A different project's UI can consume the same core operations and events.

Responses and event streams are bounded. Lists/search use stable cursors and plain escaped text. Large messages/files/raw streams use artifact references/downloads. Closing a page/SSE/WS must release only that viewer, not execution or pending handoffs.

Render cards by durable request/reference ID, not transient toast state or parsed Markdown. Refresh, repeated tool calls, provider changes and late native parsing must not duplicate them. Mount one UI viewer per selected surface; transfer inline/modal/inspector without changing Chrome/profile/request. Fit framebuffer scaling to viewport, preserve pointer/touch coordinates and keyboard focus, and prevent background terminal output from stealing focus. Completed historical browser cards do not reconnect automatically. Selecting another agent disposes viewer connections without completing its pending request.

## Operator client

Core provides commands for init/migrate/serve/diagnose, configuration, agent create/list/get/status, runtime start/stop/switch and terminal control, messages, history, operation/delivery reconciliation and backup/export. Normal online commands call HTTP operations; they are not a second direct SQLite writer. Offline init/migration/restore use explicit maintenance mode and the same schema/validation primitives. Restored external-effect work stays visibly held until reconciled; connecting a console never releases it.

Keep output useful for both humans and scripts: stable JSON mode plus concise default text. An accepted operation prints its ID and offers explicit follow/poll behavior. It must not print “started” when the system only queued a start.

## Implementation sequence

1. **M10.01 Thin transport (core):** official SDK/ASGI composition, shared schemas/API version, actor binding, health and operation dispatch, with no UI dependency.
2. **M10.02 Contract parity (core):** expose configuration/agent/status/message operations as their modules land; add consistent paging/errors/idempotency.
3. **M10.03 Operator client (core):** implement API-backed commands and clear async operation tracking.
4. **M10.04 Streams/proxies (core):** add durable event cursor and exact target terminal/desktop proxies with reconnect/backpressure.
5. **M10.05 Chat shell/cards (UI):** create its API client and static build; implement the three columns, icon picker/inline rename, conversation/composer, inline/expanded browser card and file-reference chip. Use the UI plan's monochrome direction and defer file renderers.
6. **M10.06 Native/client verification:** actual Claude/Codex discovery/calls to two separate MCP servers, with no duplicate browser tools, plus real-browser keyboard/pointer/touch, inline expansion/inspector transfers, reconnect/replay, separate machine takeover, responsive layout and focus tests.

## Source decision and acceptance

P14/P16: use Qorqut/agent-dms SDK integration, exact browser/desktop routing, iframe cleanup/focus and existing Chat attachment-chip patterns. Replace policy/business routing and Guacamole coupling. Existing preview renderers are reference material for later work; do not port them into this increment. Routing fixtures alone do not prove real VNC/native input.

Test HTTP/MCP parity, all-allowed actor attribution, invalid arguments, replay/conflicts, paginated results, event reconnect, dropped streams, wrong agent/surface/epoch, same-origin sessions, escaping, terminal/manual control, browser Done/Cancel and progress. Add all UI acceptance cases linked from UI-PLAN.md, including twenty icons, rename conflicts, durable cards without preview/content requests, preserved composer state and exact native echo linking. Native-client tests must prove real MCP use; SDK-only tests are labeled separately.

Keep domain and transport tests in core, interaction tests in UI, and share a few integrated flows. Verify core operates with UI absent/disconnected and that an unsupported API version fails clearly. No usage examples or duplicate implementation-mirroring tests are needed.

**Done:** G4/G6 demonstrate complete backend access and the small human intervention loop. UI state never claims uncertain delivery, unobserved activity or queued operations have completed.
