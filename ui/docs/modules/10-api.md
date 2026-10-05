# M10 — HTTP, Qorqut MCP, operator client and minimal UI

Status: Planned. Owns R02/R08/R12/R13 transport behavior. Depends on M00 from the first phase and integrates each domain as it becomes available.

## Outcome and boundary

The owner and agents use one backend contract. Qorqut MCP advertises agent discovery, lifecycle, messages, status, browser assistance and history/file operations. A small operator client and console make setup and recovery practical without building a business dashboard or full chat application.

Transports adapt schemas, authentication and streaming. They do not implement domain rules or query/write tables behind the application layer. Every registered actor can invoke the same implemented public operations; there is no role-conditioned tool inventory or signoff path.

## Public surfaces

- `/mcp`: official SDK Streamable HTTP endpoint, configured locally in both CLIs as `qorqut`.
- `/api/v1/operations/<name>`: shared typed operations with the result/idempotency contract.
- `/api/v1/events`: reconnectable event stream with durable cursor and recovery epoch; reconnect after restore invalidates old-epoch cursors. Events are hints to reread current records when appropriate.
- `/api/v1/agents/<id>/terminal/<run_id>/ws`: exact-run terminal attachment.
- `/api/v1/agents/<id>/desktop/ws`: current browser-control-bound VNC proxy.
- `/api/v1/artifacts/<reference>`: bounded/downloadable file/raw-history content with path/identity validation.
- `/health/live`, `/health/ready`: process liveness versus ability to perform required durable work; readiness includes storage and worker health but does not falsely certify every agent.

Use the operation inventory in [CONTRACTS.md](../CONTRACTS.md). Only advertise implemented handlers. Generated MCP tool schemas and HTTP validation must come from the same models. Mutations carry idempotency keys; browser/terminal reads and viewer connections never launch resources.

## Identity without permissions

Use a registered transport identity for each logical agent and the installation owner. The server derives `actor_id`; a body-supplied sender cannot impersonate another agent. Local operator initialization issues credentials without scanning personal config.

For the console, exchange an installation-local sign-in credential for a same-origin HttpOnly session and validate mutation/WebSocket origins. Do not put runner credentials in JS or URLs/logs. These transport controls establish attribution and prevent accidental cross-site requests; they do not restrict which v1 operations a registered actor may perform.

The private libvirt interface and configured owner-facing listen address are explicit installation inputs. Default exposure is local/private, not a public hosted product. Retain ordinary TLS deployment support without inventing a separate account/login platform.

## Minimal console

One small static application is enough:

1. Agent directory: name/ID, status, activity/freshness, capture health and pending user input.
2. Agent detail: operation progress, current run/config observation, links to terminal and desktop, recent message/history references.
3. Terminal: existing tmux mirror, resize, `terminal_control` take/release with current epoch and explicit disconnected-human state, CLI start/stop and operation reconciliation.
4. Desktop: noVNC viewer, view-only versus human control, pending prompt/note and Done/Cancel.
5. Diagnostics: failed/uncertain jobs and capture gaps with exact IDs and readable recovery reasons.

Use pinned upstream viewer assets and modest CSS/JS. No framework migration, rich notes editor, Kanban, business pages, model-provider account catalog or chat design system. A complete Chat UI can be built later over the stable history APIs.

Responses and event streams are bounded. Lists/search use stable cursors and plain escaped text. Large messages/files/raw streams use artifact references/downloads. Closing a page/SSE/WS must release only that viewer, not execution or pending handoffs.

## Operator client

Provide commands for init/migrate/serve/diagnose, agent create/list/get/status, runtime start/stop/switch and terminal control, messages, history, operation/delivery reconciliation and backup/export. Normal online commands call HTTP operations; they are not a second direct SQLite writer. Offline init/migration/restore use explicit maintenance mode and the same schema/validation primitives. Restored external-effect work stays visibly held until reconciled; connecting a console never releases it.

Keep output useful for both humans and scripts: stable JSON mode plus concise default text. An accepted operation prints its ID and offers explicit follow/poll behavior. It must not print “started” when the system only queued a start.

## Implementation sequence

1. **M10.01 Thin transport:** official SDK/ASGI composition, shared schemas, actor binding, health and operation dispatch.
2. **M10.02 Contract parity:** expose agent/status/message operations as their modules land; add consistent paging/errors/idempotency.
3. **M10.03 Operator client:** implement API-backed commands and clear async operation tracking.
4. **M10.04 Streams/proxies:** add durable event cursor and exact target terminal/desktop proxies with reconnect/backpressure.
5. **M10.05 Small console:** implement only the five surfaces above with pinned assets and backend-native state.
6. **M10.06 Native client verification:** run actual Claude/Codex MCP discovery/call/reconnect tests and real-browser terminal/desktop/handoff tests.

## Source decision and acceptance

P14: use Qorqut/agent-dms SDK integration patterns and errors, not their policy/bootstrap stack. Their browser routing fixtures help test stale-target behavior, but real VNC/native input must also be verified.

Test HTTP/MCP parity, all-allowed actor access with accurate attribution, invalid arguments, replay/conflicts, large/paginated results, event reconnect, dropped streams, wrong run/browser epoch, same-origin sessions, escaping, terminal/manual control, browser Done and operation progress. Native-client tests must prove real MCP use; SDK-only transport tests are labeled separately.

**Done:** G4/G6 demonstrate complete backend access and the small human intervention loop. UI state never claims uncertain delivery, unobserved activity or queued operations have completed.
