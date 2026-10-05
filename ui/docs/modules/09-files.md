# M09 — Files, scratchpads, memory and portable export

Status: Planned. Owns filesystem aspects of R01/R11/R14 and file-reference R15. Depends on M00/M01, M03 for guest-local inspection and M02/M07 for export; full backup coordination belongs to M11.

Repository: `qorqut-core`.

## Outcome and boundary

An agent works with ordinary files in its persistent workspace. `persona.md`, memory, notes, research, data and scratch material require no special database document object. M01 seeds the folder; this module provides access/snapshots/export without a separate agent-template package. Stable IDs and a clear folder layout make the agent's accumulated work independent of CLI/provider changes.

The host owns the data-root path. Guests see `/workspace` through virtiofs. The host-global agent uses the same folder directly. Native HOME/browser/CLI data stay in the state-disk or host runtime area, outside authored workspace exports unless a full export is requested.

## File APIs and memory

Provide bounded `files_list/read/write` and `memory_get/update` conveniences through the shared application. Operations take target agent ID and a workspace-relative path. All registered actors can select any agent under v1 policy; path validation maintains the configured filesystem target rather than adding ownership permissions.

Use descriptor-safe path resolution, reject traversal and clearly define symlink handling: API reads/writes must remain within the selected workspace; unsupported escaping links return a typed error. Native shell tools retain their normal unrestricted behavior. Do not accidentally expose host runtime credentials through the ordinary workspace file API.

API writes use a per-path cooperating-writer lock, optional expected content hash, atomic replacement and fsync where required. These guarantees cover API participants. External editor/shell writes use ordinary filesystem semantics; the service must not claim universal compare-and-swap or a version of every direct edit.

`memory.md` remains a plain authored file. The convenience operation has a bounded configurable size and returns content hash plus API-write revision where applicable. Direct edits are detected by content, not assumed to have an API revision. For M08, read a checked stable snapshot and retain its exact bytes in the packet/immutable attachment. Concurrent changes cause retry or explicit unavailability; an unreadable expected source cannot silently become stale unrelated memory. This is not universal filesystem version history or a transaction across uncooperative writers. Keep the API small and preserve access by standard CLI tools.

Transient scratch files remain persistent until explicitly changed by their authors. Browser downloads go to `downloads/`; extracted browser observations can go to `scratch/browser/`. Do not automatically delete scratch/research because an agent becomes idle or its CLI changes.

## Reference a file to the user

Add `files_reference(path, ...)` as an intent-only tool. Default agent/conversation comes from the caller; owner calls select the active agent explicitly. Resolve a relative path in that workspace, or inspect an absolute native path in the exact target environment. Map `/workspace` correctly; other VM paths use the runner's structured file-inspect route, never host-path guessing. Onbash absolute paths are host-environment paths. Validate a regular source file and save normalized location/provenance plus observed metadata, not implicit file contents/version history.

Atomically persist reference/event/conversation entry with idempotency. UI displays a compact durable file chip with label/note and copy-path action; optional new-event toast is secondary. Direct opening and preview are explicitly deferred. Replay/transcript parsing cannot produce duplicate cards or claim the user opened the file. Missing/unreachable sources fail before success; later changed/missing/unmapped paths remain explicit. Keep original attribution and references in exports across provider changes. See [UI-PLAN.md](../UI-PLAN.md) and shared contracts for metadata bounds.

## Export format

Distinguish authored-work export from a full portable agent bundle. A full bundle includes:

- Agent identity/config revision and layout/schema versions.
- Its central status/state/creation/message/history/operation/file-reference/conversation-entry records with original IDs and cross-agent references.
- Authored workspace, exact memory and required attachments/context packets.
- Source manifests and immutable transcript inventory/hashes.
- Stopped VM root/state disks or host native HOME, with base-image digest/dependencies.
- Consistent runner/spool database, durable command receipts and unacknowledged events; this is inside a VM state disk but a separate `runtime/spool.sqlite3` for onbash. Use SQLite backup/checkpoint semantics, never copy only a live main file while omitting WAL commits.
- Runtime capability/image/protocol pins and actual quiescence/backup evidence.

A single-agent export can reference counterpart agent IDs without importing those agents. Imported historical messages retain original attribution. A missing live counterpart appears as an external historical reference, not a fabricated current agent. Preserve original event IDs and source installation/sequence as provenance, assigning destination-local journal cursors on import; repeated import deduplicates original event identities.

Full export uses M11 stopped quiescence. A single-agent restore into an unoccupied identity preserves the ID; conflict with an existing identity fails until explicitly resolved. A full-installation restore instead activates an inactive staged data root. Clone is explicit and creates a new agent ID/conversation plus provenance, never starts another writer using the old native source coordinates. V1 rejects cloning onbash into a second host agent. Registration/transport credentials and the active recovery epoch are reissued on import; historical identities remain unchanged. Source graph and raw hashes remain verifiable.

Restore exact base images by digest and regenerate destination domain/virtiofs paths. Relocate a qcow2 backing filename only to the same verified image; never substitute another base as a convenience. Keep imported unfinished external-effect operations/deliveries held for recovery review: the source may have performed them after the snapshot. No automatic CLI start or pending-input replay follows import.

Do not claim that copying the folder alone reconstructs all central coordination records. Produce a manifest/export snapshot that makes that requirement explicit. Do not use Git as a transcript or VM backup.

## Implementation sequence

1. **M09.01 Layout:** create and document the workspace directories/ownership/mount expectations with M01/M02.
2. **M09.02 Atomic file access:** implement bounded streaming reads/downloads and atomic writes with path/hash behavior; retain ordinary filesystem compatibility.
3. **M09.03 Memory:** add the plain-file checkpoint convenience and exact snapshot inputs for M08.
4. **M09.04 Authored export:** produce a manifest of selected workspace files and hashes without pretending it is a full runtime backup.
5. **M09.05 Full bundle:** coordinate stopped export through M11, include DB slice/archive/disk/image inventory, and validate before import.
6. **M09.06 Restore/clone:** implement explicit identity conflict handling, reissued transport credentials, raw-source lineage and no automatic model start.
7. **M09.07 File references (Phase 4):** implement target-environment metadata resolution and atomic reference/event/conversation-entry creation. Leave rendering/preview out; preserve path provenance and missing/changed outcomes for later access.

## Source decision and tests

P13: extract useful safe/atomic file primitives and backup inventory ideas. Qorqut's registered document catalog, role permissions and general document history are more machinery than this requirement needs.

Test traversal/symlink changes, Unicode/large files, interrupted writes, concurrent API updates, direct-file edits/stable snapshots, unchanged memory hashes, workspace persistence across provider/VM changes, export cross-agent references/event cursor remapping, onbash spool receipts, missing attachments, corrupted manifest/object, identity conflict, rejected second host clone, recovery holds, and restore under another absolute data root without any Qorqut checkout.

Reference tests cover repeated keys, native absolute versus workspace paths, VM disconnection, onbash resolution, non-regular files, later mutation/deletion and import relocation. Assert one saved card and no file-content fetch, preview or unintended notification effect from replay.

**Done:** ordinary native tools and platform APIs see the same persistent files, M08 can capture exact memory/artifact versions, and portable restore proves the bundle actually contains the advertised data.
