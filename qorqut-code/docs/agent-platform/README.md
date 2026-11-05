# Qorqut agent platform

These are application contracts. The application is one shared service with an MCP and
HTTP interface, explicit roles and optional capability plugins, database-backed
operational records, and owner Signoffs for protected changes.

## References

1. [Decisions](00-DECISIONS.md): product boundaries and engineering choices.
2. [Architecture](01-ARCHITECTURE.md): runtime, storage, context and approvals.
3. [Global ownership and review destinations](05-GLOBAL-SCOPE-AND-REVIEW.md): routing of
   global routines and protected changes. Business work retains its own review queue.
4. [Learning Applier and context library](LEARNING-APPLIER-IMPLEMENTATION.md): guided
   learning work, automatic context intake, bounded MCP reads and setup.
5. [Domain APIs](../../DOMAIN-APIS.md) and the [v1.2 contracts](../v1.2/CONTRACTS.md):
   the current operation, Signoff and worker contracts.
6. [Signoff input and notifications](../SIGNOFF-INPUT-NOTIFICATIONS.md),
   [agent inbox](../AGENT-INBOX.md), [messaging](../AGENT-MESSAGING.md) and
   [multi-source learning](../MULTISOURCE-LEARNING.md): review and follow-up.
7. [Owner and Ordubashy private conversations](../OWNER-PRIVATE-CONVERSATIONS.md): the
   private channel's identity, canonical binding, storage, operations and the owner
   team-write guard.
8. [Recovery](../../app/bazarlyk/storage/RECOVERY.md): backups, exports and restoration.

## Product boundaries

- Agents are Claude Code or Codex sessions in mirrored terminals; DeepSeek is background
  processing only.
- Tasks are durable queues, assignable to agents, teammates or the owner. Inbox events can
  nudge an already-running session; they never launch one.
- Project visibility and Signoffs remain within the selected project; Global has its own
  authorized agent roster and Signoffs queue.
- Signoffs uses Waiting/Responded views and retains optional owner feedback on approval and
  rejection. Platform submission and exact enqueue share one approval; execution outcomes
  remain separate from the decision.
- Roles define jobs, plugins add abilities, skills describe procedures.
- One shared MCP service; no human CLI requirement or strict OS isolation.
- SQLite owns operational state; Git provides exports/versioning.
- Learning application is an owner-led global role. Accepted context uses the existing
  filer; shared context tools support bounded browse, search and reads.

This directory is versioned in `qorqut-code/docs/agent-platform`.
