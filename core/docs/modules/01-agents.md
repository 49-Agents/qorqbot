# M01 — Agent identity, workspaces, status and presence

Status: Planned. Owns R01/R02/R04/R08/R09/R16. Depends on M00; uses M02/M03 observations and M09 filesystem operations through narrow interfaces.

Repository: `qorqut-core`. Agent management is an internal module, with no separate agent/template package.

## Outcome and boundary

Every agent has a permanent UUID/folder, editable `display_name`, separate `icon_key`, one main conversation, current status and inspectable state history. The directory is available to all registered actors. No business namespace, fixed role roster, permission grant or model name determines identity.

Agent lifecycle, environment state, CLI state, observed activity and the natural-language status are separate fields. This module owns the directory/current projections and permanent status/activity transitions; it does not launch processes directly.

Core also owns agent personas, descriptive roles and any future permissions policy. `persona.md` is the one editable persona/role/instruction file; role text guides the agent without granting privileges. V1 remains `allow_all` with no permission engine. A folder initializer with a small default persona seed is sufficient. Generated CLI entrypoints refer to the authored file and never overwrite it on refresh; changes affect subsequent preparation/context capture, not a claimed automatic live reload.

## Registration and files

The transaction for creation records agent/conversation IDs, actor, display name, icon key, kind, initial configuration, “Initializing.” status, creation/status events and a provisioning job. The request's `start` value determines whether provisioning proceeds to a CLI launch. Return the ID immediately with accepted operation progress.

The job creates `agents/<id>` and its private runtime subdirectories, then `workspace/{notes,research,data,files,downloads,scratch}`, `persona.md`/`memory.md` seeds and the generated identity projection. Optional initial persona text replaces the default seed for creation only. Verify a pre-existing folder's ownership marker before reusing it; retries preserve existing authored bytes. A committed registration without a folder remains a recoverable provisioning operation. An unregistered folder is not automatically adopted or deleted.

Initialization creates the reserved host-global onbash once. A uniqueness constraint prevents another host-global identity. Archival preserves the original ID and files; restore uses that identity. Global does not imply a separate permission role.

Display names are labels and may repeat. All operations/messages use IDs; the API returns IDs alongside presentation metadata. Ordinary UI rows use names/icons, with IDs in details/disambiguation. Core owns the twenty-icon key catalog in [CONTRACTS.md](../CONTRACTS.md#2-agent-record-and-layout), default `circle`, and `terminal` for onbash; UI owns the artwork. Rename/icon changes use revision-checked `agents_update`, append attributed presentation history, and update the projection without changing paths, sessions or lifecycle intent. No second mutable `name`/slug or avatar-upload subsystem.

Lifecycle intent has a revision separate from display/config edits. Resource readiness moves provisioning to active before a start may execute. Archive closes launch admission and invalidates queued start/restart/switch intent under the per-agent lifecycle lock; jobs recheck that intent before effects. Already executing effects settle before archival stops and verifies all managed resources. Only explicit `agents_restore` reopens an archived identity; its default is `start=false`. It does not import a backup or create a new identity. See the shared lifecycle precedence contract for CLI-only stop versus whole-environment archival.

## Status and activity

Use the exact word-count/size/normalization contract from [CONTRACTS.md](../CONTRACTS.md). Changed status text updates the current row and appends old/new text with actor/revision/time in one transaction. History has no TTL. Unchanged confirmation can update a review timestamp without adding a fictitious change.

The runner durably emits hook/process observations with run ID, environment generation, boot identity and source sequence. The host accepts late historical observations into the journal while refusing to let an old run replace the current projection. `working` needs native evidence; an alive process alone does not prove work. A lost connection yields unknown/unreachable, and explicit verified termination yields offline.

Persist freshness transition to unknown once. Repeated heartbeat or stale polling does not fill history with unchanged entries. Display last-seen and status-update timestamps separately. A pending browser request adds `needs_user_input`; it must not erase evidence of unrelated ongoing work.

## Implementation sequence

1. **M01.01 Registry:** add agent/conversation/current-status/current-activity records, display-name/icon catalog validation and global-agent uniqueness. Implement `agents_list/get/create/update` with revision/idempotency behavior, including default icon for agent-created peers.
2. **M01.02 Folder lifecycle:** implement the idempotent folder/projection job and persona/memory seeding using M00 receipts. Preserve authored files during every retry/re-preparation; edit persona through ordinary files/M09 operations.
3. **M01.03 Status history:** implement `agents_status_set`, change journal, unchanged confirmation and event pagination.
4. **M01.04 Presence:** ingest bound runner sequences, derive current activity/freshness and preserve historical transitions. Add exact hook fixtures through M04.
5. **M01.05 Archive/restore:** fence lifecycle intent, invalidate queued launches, request stop through M02/M03/M06, verify all managed execution stopped, mark archived and retain history. Failed stops remain inspectable archiving/error operations, never “archived” while secretly running. Explicit restore verifies resources before reopening launch admission.
6. **M01.06 Directory presentation:** expose compact identity/status/state rows with cursors, capture readiness and unresolved input. Reads never provision or start agents.

## Source decision

P01/P02: retain Qorqut's identity-versus-session concept and verified native observation mapping. Rewrite name/business path derivation, fixed roles and overwritten/pruned status storage. Agent-dms's compact directory is useful, but its 30-word limit, current-row-only statuses and stale-status-before-send gate are not the required contract.

## Tests and acceptance

- Concurrent/retried create yields one UUID, folder, creation event and provisioning job; independent requests create independent agents.
- Rename does not change paths, message addresses or conversation history. Duplicate labels remain unambiguous by ID.
- All twenty icons round-trip as stable keys; unknown keys fail. Concurrent rename/icon edits enforce revisions; changing onbash's display name never creates/replaces its reserved identity. M10 supplies double-click and accessible rename controls.
- Status tests cover 0, 1, 100 and 101 words, Unicode whitespace, character cap, same-text confirmation and concurrent revisions.
- All changed statuses and observed transitions survive restart and are ordered/retrievable with actor attribution.
- Delayed old-run observations are retained historically without corrupting current state. PID reuse, guest reboot and connection loss produce the correct state.
- Exactly one onbash survives init retry, archive/restore and service restart.
- Archive/restore preserves files/history and does not recreate another identity. A stopped agent can still be inspected and receive queued messages.
- Concurrent archive/start/switch cannot leave an archived agent running; startup failure after launch retains input ownership until process exit is verified.

**Done:** R01/R08/R09 pass through both HTTP and MCP; creation uses the same path for owner and agent; all state displays distinguish uncertainty from confirmed inactivity.
