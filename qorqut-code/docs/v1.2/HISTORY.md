# Operational Git history in v1.2

Status: included in the accepted scope; design/code preparation only. No real data is read or exported during planning.

## Purpose and authority

Port the v2.2 snapshot mechanism to record operational history and selected authored files in `operations/.runtime/history.git`. SQLite remains the authoritative operational store. Context owns its separate published graph/history; source repositories own code; raw transcripts remain separately archived.

This is a local history service, not an editable record store, migration input, automatic backup/restore system or a way to replay operations. A Git failure never rolls back, repeats, approves or completes operational work. No automatic push, pruning, restore, reset or history-branch cleanup is added.

## Reuse and adaptation

Reuse the pinned donor's `app/qorqut_app/history/` capture, deterministic JSON export, sharding, isolated Git execution, locking, incomplete-file handling and health concepts. Move the host service into the target host; put the row-export contract in Operations.

The current donor exporter lists only its foundation tables. That cannot cover retained v1 domains automatically. Its file scanner also assumes every agent lives under global `agents/{id}`. Replace both assumptions explicitly: compose trusted per-domain export definitions and obtain each allowed authored area through the registered global/business path contract.

The snapshot service must not import the application registry to bypass permissions, inspect every SQL column by reflection or blindly serialize credential/configuration tables.

## Coverage contract

Before implementation, register every composed table under one of: exported columns, represented by another canonical revision/event export, technical state excluded, private/sensitive state excluded, or owned by a separate history mechanism. Record stable keys, format/version and the owning module. Missing classifications make coverage incomplete; they never cause automatic wholesale export.

| Record group | Planned treatment |
| --- | --- |
| Tasks, Actions, tags, typed relationships, immutable revisions and ordinary item-thread discussion | Export canonical records/revisions and attributed events with owning scope; no second synthetic Action log |
| Principals, businesses, role/profile metadata | Export explicit non-secret identity/scope/configuration fields; exclude authentication bindings and credential material |
| Inbox/subscriptions | Export the selected ordinary work/delivery state needed to explain operational history, without provider input secrets or private-message bodies leaking through event payloads |
| Signoffs | Export safe identity, lifecycle, revision and outcome summaries; exact prepared payloads/file bytes/recovery claims remain in the authoritative protected store |
| Routines and runs | Export definitions/results through explicit safe fields; preserve IDs/definitions and states without copying arbitrary environment, secret arguments or opaque provider payloads |
| Other retained business records | Domain-owned explicit column/export contracts, reusing existing public-export rules where applicable |
| Private owner/agent DMs | Excluded from additional operational Git snapshots, preserving v1 (owner decision, October 13, 2026). Private conversations retain their authoritative storage; do not expose messages indirectly through events |
| Native Chat, raw transcripts, session bindings, credentials, tokens, exact execution/idempotency receipts and raw job state | Excluded from this Git snapshot; retained by their existing authoritative/archive owners |

“Safe fields” must become an explicit table/column and structured-payload schema, not a runtime guess or a claim that filename scanning removes secrets. Record intentional redaction/exclusion in the export manifest without copying the excluded value. The snapshot is not a full-fidelity recovery export.

Private-DM scope affects Git coverage only. It does not change MCP/UI conversation visibility or give business agents a global history browsing tool. No generic agent API exposes the entire history repository. Local file modes/API restrictions do not create OS isolation between processes already sharing the same Unix account.

## Authored files

Include regular authored files in the approved root/global `files/`, registered global-agent `files/`, registered business-agent `files/` and business `hq/` areas. Capture exact bytes, including permitted binaries, and preserve execute bits. Directories and file targets come from trusted registration/path resolution, not caller tags or arbitrary absolute paths.

Exclude code/role repositories, Context, raw transcripts, generated provider instructions/configuration, credentials/environment files, browser profiles, caches, virtual environments, dependency trees, `.runtime`, nested repositories, symlinks and special files. Derived table/export projections should not be recaptured as if they were independently authored documents; classify them explicitly with their domain owner.

Retain the donor's descriptor-relative traversal and changed-file detection. An unreadable or concurrently changing expected file makes capture incomplete. Keep its previous saved version, report that fact and retry on a later snapshot; do not silently record it as deleted or claim the stale copy is current. Intentional current deletion removes the path from the new tree while keeping older commits.

Name/header exclusions are useful controls, not a guarantee that arbitrary authored notes contain no sensitive text. No external publication is performed.

## Relationship to existing history

The inspected v1 source already has several different contracts:

| Existing mechanism | v1.2 treatment |
| --- | --- |
| Per-mutation `tables.version` / `tables.global-version` snapshots and `qorqut-tables` references | Preserve the exact revision receipts and domain retry behavior; a coalesced general snapshot cannot substitute for them |
| `qorqut-business-docs-versioning` document versions/events | Preserve typed revision/source evidence, adapt moved Task/Action/Story callbacks and target paths |
| Nightly maintenance exports/optional `bazarlyk-history` | Preserve the registered program's existing behavior; classify its target under retained history configuration rather than redirect it silently |
| Context's published Git history | Owned by the Context engine and excluded from operational file capture |
| Source repository history | Unchanged by the operational history service |

The new snapshot history is the broad operational overview; existing per-record history remains the source for its exact revision APIs and receipts. These have different permanent purposes and are not two editable operational authorities or a temporary old/new rollout.

Never merge, rebase, delete, force-push, check out or repurpose the existing `qorqut-tables` or `bazarlyk-history` refs during this work. They are system data history. Their real locations and eventual relocation are later data work, not a reason to access them now. Synthetic tests may construct their own independent repositories.

## Capture and scheduling

1. Explicit synthetic-instance initialization creates/validates the private history repository. Normal import/construction does nothing; normal serve validates its configured target without repairing an unrelated repository.
2. Read all declared row exports through one consistent Operations read unit, then close that unit before filesystem/Git work.
3. Capture registered authored areas and build deterministic JSON-line/sharded trees. Identical state produces identical bytes and no new commit.
4. Serialize snapshots with the instance history lock. Update the history ref with compare-and-swap; preserve unexpected changes and report conflicts.
5. Record complete/incomplete/failed status, snapshot commit, time, exported table counts, exclusions and retryable capture failures. Preserve the latest known complete snapshot separately from the latest attempt.

Implement coalesced post-commit wake hints and retain the donor's bounded periodic file scan (initial target 60 seconds). The pinned donor runs history periodically but does not actually wire history into its commit wakes; its README attribution was incorrect. This is a host observer, not a new user routine and not a replacement routine scheduler. A commit hint is advisory; a missed hint cannot lose the next snapshot. Automatic passes keep a monotonic minimum gap (default 60 seconds) after each pass finishes; hints inside it stay coalesced and are served by one pass at the deadline.

Each captured database state is consistent. Database rows and filesystem bytes are collected at different instants; do not advertise a cross-store atomic restore point. Multiple intermediate file edits may coalesce between snapshots. Immutable operational revisions remain individually represented when their canonical revision rows are exported.

Git commands run with the donor's isolated environment, disabled hooks/prompts/network protocols, temporary index and private local repository. They never change a source checkout, execute user Git hooks or write an owner's index. A backend restart reconciles status/ref observations without replaying domain mutations.

## Focused acceptance

- A synthetic Task/Action/thread mutation appears once with correct scope and attribution; unchanged state makes no commit.
- A retained-domain exporter participates in the same read snapshot, and an unclassified table is reported rather than leaked or silently forgotten.
- Credential/token fields, private DM bodies under the selected policy, raw native transcripts and provider configuration do not appear in files, row exports or nested event payloads.
- Registered global and business agent files are covered; code repositories, symlinks and generated files are excluded.
- Concurrent changes/read failures are incomplete and retain prior bytes without presenting them as current; later retries resolve accurately.
- Git/ref/lock failures leave accepted SQLite work intact. Signoff/routine execution is not retried by history.
- Existing table/document revision receipt behavior still works on isolated fixtures; no source or real system-history ref is touched.

The completion report must distinguish source tests from actual instance activation. No history service is started against the user's real records as part of this plan or the code-only milestone.
