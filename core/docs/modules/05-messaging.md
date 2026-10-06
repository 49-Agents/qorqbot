# M05 — Durable messages, terminal delivery and handling

Status: Planned. Owns R07 and parts of R02/R09. Depends on M00/M01/M03; capture readiness comes from M07. All public messaging operations are available to all registered actors.

Repository: `qorqut-core`. Messaging is an internal module sharing the application transaction and runner input path; it needs no separate comms service, package or UI dependency.

## Outcome and boundary

Agents can discover each other, send complete messages, retain threaded history, receive queued messages when runnable, and explicitly acknowledge handled work. The host coordinates persistence; the runner performs a generation-bound input attempt. A message is not a hidden task record or permission grant.

Store messages independently of delivery state so terminal problems cannot lose the conversation. Preserve full bodies and original attribution. Any display summary is derived and cannot replace the body.

An otherwise valid stored message may contain control characters that cannot be
safely sent as bracketed-paste text. If the runner returns `invalid_argument`
with reason `unsafe_terminal_text`, retain the exact body and mark delivery
failed with that reason. Do not normalize the body or repeatedly defer/retry this
permanent payload failure. D23 and CONTRACTS section 6 define the transport rule.

## Records and transitions

Use thread/message records, one pending delivery record per recipient, separate immutable attempt identities, and handling receipts. V1 is direct messaging, not group chat. Reply IDs preserve the thread and original authorship.

Sending atomically inserts message, event, delivery and required job. Idempotent retry returns the same message. Reads never acknowledge. A reply never acknowledges its parent unless an explicit acknowledgment is also requested as a separate operation/batch.

Handling records the actual authenticated actor and exact message IDs. All-allowed v1 permits an actor to handle another agent's message intentionally; do not falsely attribute that acknowledgment to the recipient. Record `handled_by` and optional note/outcome. Duplicate compatible acknowledgments return the prior receipt.

Offline recipients retain pending deliveries indefinitely. Delivery does not start an offline agent automatically; callers can explicitly request `runtime_start`. Pending input, human terminal control, setup/login or unknown composer state defers delivery with a typed reason.

## Dispatch and uncertainty

The dispatcher selects the recipient's current capture-ready run, creates a stable attempt and sends the exact payload with message/thread/sender identity. It passes no arbitrary tmux path or caller-selected pane. The runner checks run generation/boot/process identity immediately before paste.

Known no-input outcomes defer or fail without ambiguity. Submitted means normal Enter was sent with the expected evidence; it does not mean the native model processed the message. A crash/timeout after possible paste/Enter is uncertain. Retry of the same attempt retrieves the runner receipt; absence of a trustworthy receipt after possible input is not permission to send again.

`deliveries_resolve` records an explicit inspected resolution. A deliberate new attempt after uncertainty must be a new, linked attempt with a visible rationale. Delivery metadata in the message lets the recipient recognize already handled message IDs and avoid repeating work. This improves reliability; it does not promise exactly-once external business effects.

Restoring/importing a backup holds unfinished deliveries for `recovery_review`. A record pending at snapshot time might have been submitted later in the original installation. Preserve that uncertainty until existing receipts/evidence reconcile it or an actor explicitly resolves it. Starting the restored CLI does not release this hold automatically.

Default dispatch waits for an idle empty composer. Queue-while-busy can be an adapter capability later, only with native tests. Coalescing notification hints is allowed, but complete messages/IDs and per-message handling must remain independently retrievable.

## Implementation sequence

1. **M05.01 Durable domain:** add thread/message/delivery/handling records, full-body validation and idempotent send/reply/history pagination.
2. **M05.02 Offline inbox:** expose pending messages, exact acknowledgment and compact directory links. Test current status/state freshness without making status refresh a prerequisite to sending.
3. **M05.03 Terminal outbox:** connect pending deliveries to current runs through M03 with per-agent serialization and durable attempt IDs.
4. **M05.04 Reconciliation:** recover runner receipts after reconnect, preserve uncertainty and implement explicit resolution/retry history.
5. **M05.05 Completion sources:** use the same message insertion path for browser Done and selected lifecycle notifications, preserving source request/operation IDs and actor attribution.
6. **M05.06 Multi-agent acceptance:** exercise agent-created peers and long-running exchanges using isolated native fixtures, followed by disposable real provider smoke tests at G6.

## Source decision

P07: Qorqut's immutable full messages, atomic inbox delivery and terminal outbox are strong references. Agent-dms contributes compact idempotency/receipt/pagination ideas. Remove business scoping, role restrictions, forced manager/worker workstreams, status-before-send policy and any assumption that a terminal nudge itself handles work.

## Tests and acceptance

- Parallel/retried sends commit one complete message and delivery, including a lost HTTP/MCP response.
- Replies keep their thread, cannot rewrite parents, and leave parent handling unchanged.
- Offline/archived/unready agent behavior is explicit; queued messages and history survive restart/archive/restore.
- Wrong run, old generation, recycled PID, busy composer and human input never receive blind paste.
- Crash before/after paste/Enter/receipt proves no automatic duplicate after uncertainty.
- Handling lists validate all IDs before writing; retries are harmless and actor attribution is preserved.
- Browser completion retried over the network produces one completion message and one input completion.

**Done:** G4 proves durable communication across three independent agents and onbash, with separate evidence for saved, submitted and handled messages.
