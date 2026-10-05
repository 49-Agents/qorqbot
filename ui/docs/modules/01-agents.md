# M01 — Agent identity, workspaces, status and presence

Status: Planned. Owns R01/R02/R04/R08/R09. Depends on M00; uses M02/M03 observations and M09 filesystem operations through narrow interfaces.

## Outcome and boundary

Every agent has a permanent UUID and folder, an editable name, one main conversation, current status and an inspectable state history. The directory is available to all registered actors. No business namespace, fixed role roster, permission grant or model name determines identity.

Agent lifecycle, environment state, CLI state, observed activity and the natural-language status are separate fields. This module owns the directory/current projections and permanent status/activity transitions; it does not launch processes directly.

## Registration and files

The transaction for creation records agent/conversation IDs, actor, name, kind, initial configuration, “Initializing.” status, creation/status events and a provisioning job. The request's `start` value determines whether provisioning proceeds to a CLI launch. Return the ID immediately with accepted operation progress.

The job creates `agents/<id>` and its private runtime subdirectories, then `workspace/{notes,research,files,downloads,scratch}`, authored instruction/memory seeds and the generated identity projection. Verify a pre-existing folder's ownership marker before reusing it. A committed registration without a folder remains a recoverable provisioning operation. An unregistered folder is not automatically adopted or deleted.

Initialization creates the reserved host-global onbash once. A uniqueness constraint prevents another host-global identity. Archival preserves the original ID and files; restore uses that identity. Global does not imply a separate permission role.

Names are labels and may repeat. All operations and messages use IDs. The API always returns IDs alongside names so ambiguous labels cannot redirect execution.

Lifecycle intent has a revision separate from display/config edits. Resource readiness moves provisioning to active before a start may execute. Archive closes launch admission and invalidates queued start/restart/switch intent under the per-agent lifecycle lock; jobs recheck that intent before effects. Already executing effects settle before archival stops and verifies all managed resources. Only explicit `agents_restore` reopens an archived identity; its default is `start=false`. It does not import a backup or create a new identity. See the shared lifecycle precedence contract for CLI-only stop versus whole-environment archival.

## Status and activity

Use the exact word-count/size/normalization contract from [CONTRACTS.md](../CONTRACTS.md). Changed status text updates the current row and appends old/new text with actor/revision/time in one transaction. History has no TTL. Unchanged confirmation can update a review timestamp without adding a fictitious change.

The runner durably emits hook/process observations with run ID, environment generation, boot identity and source sequence. The host accepts late historical observations into the journal while refusing to let an old run replace the current projection. `working` needs native evidence; an alive process alone does not prove work. A lost connection yields unknown/unreachable, and explicit verified termination yields offline.

Persist freshness transition to unknown once. Repeated heartbeat or stale polling does not fill history with unchanged entries. Display last-seen and status-update timestamps separately. A pending browser request adds `needs_user_input`; it must not erase evidence of unrelated ongoing work.

## Implementation sequence

1. **M01.01 Registry:** add agent/conversation/current-status/current-activity records and global-agent uniqueness. Implement `agents_list/get/create/update` with revision/idempotency behavior.
2. **M01.02 Folder lifecycle:** implement the idempotent folder/projection job and reconciliation using M00 receipts. Preserve authored files during every re-preparation.
3. **M01.03 Status history:** implement `agents_status_set`, change journal, unchanged confirmation and event pagination.
4. **M01.04 Presence:** ingest bound runner sequences, derive current activity/freshness and preserve historical transitions. Add exact hook fixtures through M04.
5. **M01.05 Archive/restore:** fence lifecycle intent, invalidate queued launches, request stop through M02/M03/M06, verify all managed execution stopped, mark archived and retain history. Failed stops remain inspectable archiving/error operations, never “archived” while secretly running. Explicit restore verifies resources before reopening launch admission.
6. **M01.06 Directory presentation:** expose compact identity/status/state rows with cursors, capture readiness and unresolved input. Reads never provision or start agents.

## Source decision

P01/P02: retain Qorqut's identity-versus-session concept and verified native observation mapping. Rewrite name/business path derivation, fixed roles and overwritten/pruned status storage. Agent-dms's compact directory is useful, but its 30-word limit, current-row-only statuses and stale-status-before-send gate are not the required contract.

## Tests and acceptance

- Concurrent/retried create yields one UUID, folder, creation event and provisioning job; independent requests create independent agents.
- Rename does not change paths, message addresses or conversation history. Duplicate labels remain unambiguous by ID.
- Status tests cover 0, 1, 100 and 101 words, Unicode whitespace, character cap, same-text confirmation and concurrent revisions.
- All changed statuses and observed transitions survive restart and are ordered/retrievable with actor attribution.
- Delayed old-run observations are retained historically without corrupting current state. PID reuse, guest reboot and connection loss produce the correct state.
- Exactly one onbash survives init retry, archive/restore and service restart.
- Archive/restore preserves files/history and does not recreate another identity. A stopped agent can still be inspected and receive queued messages.
- Concurrent archive/start/switch cannot leave an archived agent running; startup failure after launch retains input ownership until process exit is verified.

**Done:** R01/R08/R09 pass through both HTTP and MCP; creation uses the same path for owner and agent; all state displays distinguish uncertainty from confirmed inactivity.
