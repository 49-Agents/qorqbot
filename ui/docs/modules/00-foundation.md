# M00 — Foundation, transactions and durable work

Status: Planned. Owns R02/R07/R09/R12/R14 infrastructure. Depends on the shared [contracts](../CONTRACTS.md), and supports every other module.

Repository: `qorqut-core`.

## Outcome and boundary

A small application layer performs each domain operation once, records its effects and schedules recoverable external work. HTTP, MCP and the operator client call that same layer. There is one host database and no model call, network call or subprocess inside its write transactions.

This module owns configuration loading, typed errors/results, SQLite connections/migrations, actor identity records, operation/idempotency receipts, the append-only event journal, fixed-handler jobs/outbox and shared file-effect helpers. Domain-specific state machines remain in their modules. This is not a general workflow engine, event-sourcing framework or arbitrary executable job scheduler.

Core owns all application configuration: static installation settings, revisioned new-agent defaults and agent settings. `config_get` returns a non-secret effective view; `config_update` changes supported defaults with revision/idempotency/event guarantees. Defaults are resolved into each new agent's configuration; changing them does not mutate running agents. UI is an API client and the browser package receives explicit configuration. Neither becomes a second configuration authority. V1's effective operation policy is `allow_all`.

## Source decision

Use P00 in [PORTING.md](../PORTING.md). Read Qorqut transaction/receipt behavior and agent-dms's smaller storage/service design. Implement a fresh compact schema rather than bringing over business tables, prepared signoffs and a large policy context.

## Durable contracts

- Use SQLite WAL, foreign keys and `synchronous=FULL` on local persistent storage. State the filesystem durability assumption explicitly. A locked/busy DB has a bounded wait and a typed retryable outcome.
- Validate operation arguments before mutation. Bind actor from transport. Canonical validated arguments determine the idempotency hash.
- In one transaction, update current records, append all corresponding events, create required jobs and save the operation result. A crash cannot leave a reported successful status without its history or a message without its pending delivery.
- Use one typed jobs table with fixed handlers. Store target IDs/generations, attempt count, phase, next attempt/reconciliation time and last typed error. A handler inspects the real effect before retrying.
- A job lease limits normal concurrent work, but external writes also use the relevant host-local or runner-local lock. Do not treat lease expiry as proof the previous process stopped.
- Distinguish service restart from backup restore/import. A fresh recovery epoch fences restored control; unfinished restored external-effect work stays held for domain-specific reconciliation. `operations_reconcile` exposes the fixed handlers' inspect/retry/cancel paths, not arbitrary executable recovery code.
- Polling/events have monotonic cursors. Journal correction is append-only. Exports include schema/event payload versions.
- Large files/archives are outside SQL rows. File installation uses staged bytes, content hash, fsync, atomic rename, parent-directory fsync and a receipt that reconciliation can finish. M07 specializes this for immutable chunks.

## Implementation sequence

1. **M00.01 Configuration and package:** create the smallest installable core package/host/runner entrypoints and typed config; pin the independent browser release/protocol at integration. Separate source, release and data roots. Importing modules starts no worker, modifies no database and creates no agent. Core must run without a UI checkout or asset bundle.
2. **M00.02 Storage:** add initial explicit migrations, connection/transaction helpers, schema version refusal and a migration command. Keep migrations small and tested against the previous schema.
3. **M00.03 Operations:** create a registry of concrete operation descriptors and handlers. Generate transport schemas from shared models. Implement idempotency, revision conflicts, canonical result/error envelopes and configuration read/update operations.
4. **M00.04 Journal/jobs:** implement atomic event/outbox writes, bounded worker claims and fixed-handler dispatch. An application restart resumes queued/waiting jobs; running external work is inspected rather than blindly repeated.
5. **M00.05 File receipts:** implement the common crash-safe file installation primitive and orphan reconciliation. Do not delete an unknown file simply because no receipt currently points to it.
6. **M00.06 Test hooks/diagnostics:** inject clocks, UUIDs, fault boundaries and external ports. Expose job health and transaction wait/hold timing without logging arguments or transcript content.

## Tests and acceptance

- Concurrent identical operations yield one result/event/job; conflicting payloads under the same key fail deterministically. Include configuration revisions and confirm default changes leave existing agents intact and expose no secrets.
- Kill/fail before and after each commit: successful mutations retain both current state and history; uncommitted work is absent.
- A worker restart sees queued jobs; stale worker results cannot replace newer domain state.
- A backup predating an actual external effect cannot replay that effect automatically on restore; imported journal entries receive destination-local cursors and retain original provenance.
- A long external operation does not hold a SQLite transaction or block unrelated reads.
- File failure at write/fsync/rename/DB boundaries leaves either the old valid file or a recoverable installed object, never a false successful receipt.
- Migrations preserve IDs, messages, event sequences and archive references. Startup refuses an unsupported schema without modifying it.
- Two service processes cannot both become the installation's active reconciler; a second process fails with a clear diagnostic.
- Tests use temporary roots and databases; no sibling Qorqut imports or hidden fixture source paths.

**Done:** G1 foundation tests pass, application assembly is side-effect-free, and every subsequent module can use the same transaction/event/job/idempotency primitives without implementing its own queue.
