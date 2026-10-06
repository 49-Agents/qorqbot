# Durable host transcript intake

Manager design, 2026-10-08. Base: reviewed combined core through `d4eaf74`,
including agents/workspaces and the local recorder probe. This is M07.03's host
storage boundary. Network collection, native ingestion, parsing, backup/import
and managed runtime admission follow separately. D16's host-pull protocol is
unchanged; this does not introduce a second upload service.

## Ownership and callable contract

Add `transcripts/archive.py` and, if useful for readability, `archive_models.py`.
Use the existing host `Database`, installation lock/effect gate, registered
`Invocation`, `CoreError`, bounded shared values and event journal. Add host
migration 4 without modifying migrations 1–3. Assemble a passive
`Application.archive = Archive(database, data_root)`; retain exactly nine public
operations and three existing job handlers. No background loop, public archive
tool, network endpoint, new database/dependency or generic storage framework.

These are internal byte-stream operations for a trusted future collector, not
native-session authentication. Validate the declared lineage; do not claim the
module proves a native process, runner identity or live provider owns it. The
future authenticated runner transport must establish that evidence before
calling this same boundary. V1 remains `allow_all`: require a registered caller
for attribution, with no new actor permission tiers.

Freeze these methods, using the existing capture `RunBinding`, `Source` and
`Chunk` values (revalidate their contents, rather than trusting a dataclass's
type alone):

| Method | Result and role |
| --- | --- |
| `register(context, binding, source, source_epoch, metadata)` | `ArchiveState`; atomically bind run/source/epoch and link the run; identical registration is inert |
| `receive(context, binding, chunk)` | `ArchiveReceipt`; install/verify exactly one bounded chunk and commit its receipt/watermark before returning an ACK-eligible result |
| `state(context, source_id, source_epoch)` | `ArchiveState`; SQL-only historical inventory, never an ACK permission |
| `read_chunk(context, source_id, source_epoch, offset)` | `Chunk`; read/verify one committed object by exact start offset, at most 256 KiB |

`ArchiveMetadata` is immutable: required `capability_evidence`, optional
`locator`, `file_identity`, `native_created_at`, `previous_epoch` and
`divergence_reason`. Text fields use valid UTF-8, no NUL and 1–4096 characters
when present; time uses the shared UTC timestamp validation. Previous epoch
and reason must be supplied together; a new epoch must differ from its existing
predecessor on that same source. Paths/identity strings are evidence only and
are never opened as host paths. A first epoch has neither predecessor nor reason.

`ArchiveState` contains source ID, epoch, historical `host_archived_offset`,
current host `recovery_epoch`, registration's `registered_recovery_epoch`, and
strict `recovery_held`/`conflicted` booleans. `ArchiveReceipt` contains the full
supplied run binding, source ID/epoch, current host recovery epoch and committed
`host_archived_offset`. Offsets/counters are strict integers bounded to SQLite's
signed range; IDs are canonical UUIDs. Do not return raw bytes in event payloads
or an operation envelope. `read_chunk` is the separate bounded raw read.

## Permanent inventory and source links

Use small `archive_` tables in the host database for runs, sources, epochs,
run/source links, accepted chunk receipts and conflicts. Source and run
identities, epoch metadata and accepted coordinates are permanent. Add foreign
keys/uniqueness and guards against rewriting/deleting their identity/evidence;
watermarks and the conflict latch are the intentionally mutable epoch state.
This is a host copy's inventory, not a second runtime authority.

Register the full exact run binding once per run ID. Require its installation
to equal the open installation, its agent to exist, and source installation/
agent to match. Reusing any run/source identity with different fields fails.
A parent source, when supplied, must already belong to that same agent and
installation; it cannot be itself. Distinct sources never deduplicate by text,
path, timestamp or native session name alone.

Registration is one short transaction: run, source, epoch, unique run link and
attributed events commit together. Emit `archive.source_registered` for a new
source, `archive.epoch_registered` for a new epoch and `archive.run_linked` for a
new link. Events contain bounded coordinates/metadata, never bytes. Store the
first registering actor/time. Replays neither change original attribution nor
refresh timestamps. There is no event/job/application-operation row per byte
chunk; its immutable coordinate receipt is the stream's idempotency key.

An existing source/epoch can link a distinct run on exact compatible lineage;
this never moves a cursor or changes its original metadata. Registration of a
successor records its predecessor/reason without changing prior data. Old run
and source epochs remain readable and eligible for late bytes under their exact
registered links. Do not require a run to be current, or compare its historical
recovery epoch to the current host epoch as an execution fence. These source
coordinates and host restoration admission are different facts.

No registration creates directories or launches resources. The agent workspace
must already be initialized before receiving files. Agent lifecycle can still
be `provisioning`; do not mark it active because archival works.

## Chunk validation and immutable objects

Before effects validate canonical IDs, registered exact run/source/epoch link,
strict `offset >= 0`, `1 <= length <= CHUNK_BYTES`, bounded `offset + length`,
exact immutable `bytes` length and lowercase 64-character SHA-256 matching the
payload. A bad hash/shape is `invalid_argument`, without raw input in its error.
Unknown records are `not_found`; incompatible registered lineage is
`target_changed`/`idempotency_conflict`. An offset ahead of the committed watermark
is a `revision_conflict`, with no file/cursor/ACK or out-of-order buffering.

Own the reserved path under the initialized agent folder:
`agents/<agent-id>/history/chunks/<source-id>/<source-epoch>/`.
Final object names are `<offset>-<length>-<sha256>.bin`, derived only from validated
coordinates. Conflicting objects go under that epoch's `conflicts/` directory
with the same naming rule. SQL references each immutable file; no canonical
mutable JSON manifest is required yet. The later manifest exporter reads these
records. `history/sources/` can remain empty in this increment.

Reuse the existing descriptor-safe `directory`/hash and narrow workspace
directory/no-replace helpers where appropriate. Do not modify their semantics.
Validate the final agent's complete ownership marker against its committed
installation/agent/creation-operation/provisioning-job tuple before effects.
Traverse every parent with directory descriptors and no symlink following;
reject non-directory ancestors, links/special final files and substituted
parents. Revalidate the opened agent/root ancestry before committing evidence.
Only create reserved archive descendants, never repair/reseed an agent workspace
or inspect another native HOME. Unknown files/stages are not cleanup targets.

Keep the installation lifetime use guard and effect gate across each receive.
Use short SQL snapshots/commits, with all file creation, hashing, writing and
fsync outside SQL. Write a private unique sibling stage using O_EXCL, flush and
fsync it, then publish with actual no-replace semantics and fsync its parent.
Fsync newly created directory links too. Unsupported atomic primitives fail
explicitly. Never use replacing rename after an existence check.

An existing deterministic final object may be reused only after its regular
file identity, exact size/hash and parent binding are verified; fsync the file
and directory before relying on it. This is recovery of known immutable bytes,
not adoption of an unknown workspace. Missing/damaged bytes for an already
committed receipt are `storage_blocked`/`uncertain_effect`: retain inventory,
withhold the ACK and require explicit repair rather than silently replace them.

After durable publication, revalidate lineage, host epoch and contiguous cursor
in a short transaction; insert the receipt and advance the watermark together.
Never save a watermark ahead of a durable file. The returned receipt is built
only after that transaction commits. Faults after commit but before return are
replayed by the same coordinate. A file installed before SQL commit is an
uncommitted orphan; the same receive can verify/reuse it. Unattached private
stages can remain unknown; no garbage-collection scan belongs in this increment.

This dedicated chunk path uses its source/coordinate inventory instead of
allocating a generic operation plus file job for every recorder fragment. It is
not a second general file-write API. M09 keeps the accepted `Files` receipt API;
archive code does not change or bypass authored-file write rules.

## Duplicate, conflict and ACK semantics

An exact repeat below the watermark must match one stored start/length/hash
receipt and its object. It leaves inventory/events unchanged and returns the
current committed watermark after verification. Rechunking an accepted range,
overlap at a different start or different accepted bytes is a conflict, not an
alternate representation of the same receipt.

Persist the conflicting bounded payload in its own immutable object, then
atomically save a unique conflict receipt, latch the epoch `conflicted=true` and
append one attributed `archive.chunk_conflict` event. Retain the first accepted
bytes/receipt/watermark. Repeated identical conflicts deduplicate; loss before
the conflict commit can be retried using its deterministic object. If preserving
conflict evidence fails, return storage failure without an ACK. New intake and
ACK generation on a conflicted epoch remain closed; reads remain available.
An explicitly registered successor epoch can preserve divergent bytes from zero.
Do not invent an automatic conflict resolver or clear the latch on retry.

Only a successfully returned `ArchiveReceipt` permits the future collector to
send a source/epoch-bound ACK. The module itself sends no network message and
does not call `CaptureSpool.acknowledge`/`reclaim`. SQL `state` is historical,
never a shortcut around receive verification. Previously committed prefixes
rely on their durable ordered receipts under the storage fault model; each
receive verifies its own object, not an O(total-history) full audit. Manual disk
deletion/corruption or copying mismatched database/data snapshots is not repaired
by a duplicate ACK. Do not claim independent backup or archive-wide integrity
from one successful chunk.

## Restore and read behavior

Save the current host recovery epoch when each archive epoch is first registered.
If it differs from the installation's current epoch, `state.recovery_held` is
true. `receive` and new run links to that held epoch fail `not_ready` before
file effects, even for a byte-identical replay. Identical registration cannot
refresh this fence. An intake already holding the installation use guard blocks
maintenance until its file/SQL work settles; revalidate at finalization anyway.

`state` and `read_chunk` may inspect held history. `read_chunk` validates exact
receipt/object coordinates, size/hash and no-follow ancestry; it writes nothing,
does not repair damaged bytes, and never returns an ACK. Full recovery audit,
release of restored archive holds, cross-installation import and backup creation
remain a later explicit design. A newly declared source epoch must not be
used as an implicit repair of a held one: registration of a successor whose
predecessor is recovery-held fails `not_ready` without changing inventory.
Historical runner bindings alone
cannot release host recovery admission.

Construction, imports, application setup/open and ordinary reads start no
collector or filesystem reconciliation. No speculative `healthy` readiness,
parser/checkpoint completeness or live provider coverage is inferred from raw
storage. Terminal/native/attachment streams retain their separate source types.

## Focused verification and delivery

Use synthetic initialized agents and independent temporary host/runner stores.
Keep one compact owning archive suite and one shared transfer flow:

- Register/replay/conflict/parent/run-link transactions, attribution, exact
  metadata, legacy v3 migration and unchanged public inventory/import behavior.
- Transfer real `CaptureSpool.pending` chunks through `receive`, then explicitly
  apply only returned ACKs and reclaim. Assert exact host reads after runner
  reclamation/restart; dropped ACK/response safely replays. Stream more than 8 MiB
  including binary/Unicode, a giant record and incomplete suffix without parsing.
- Crash/failure boundaries around stage fsync, no-replace, parent fsync, SQL
  receipt/watermark commit and response loss; include an actual owned process
  exit. No ACK ahead of either durability step. Report filesystem assumptions.
- A compact conflict/overlap/gap/hash/wrong-lineage set proves immutable accepted
  bytes, deduplicated preserved conflicts and no adoption or automatic retry of
  a held/unknown effect. Cover an installed orphan and corrupt/missing committed
  object; no destructive repair.
- Concurrent identical intake and stale/future coordinates under the effect
  gate; no SQL lock held through paused I/O. Wrong marker, symlink/parent
  substitution and occupied final paths cannot write outside the owned archive.
- Recovery activation fences old intake/ACKs, including exact registration
  retries, while raw reads retain original lineage; maintenance waits for an
  in-flight intake. Late bytes from a registered retired run work before a host
  restore hold. No provider/runtime state advances.

Run the new suite and affected foundation/agent suites once from source. Keep
old assertions/fault coverage; only exact schema/setup expectations may need
adjustment. Build/install the unchanged lock and run the archive transfer flow,
one existing HTTP/MCP agent smoke and passive import/schema checks from a
test-only cwd, verifying actual parent/child package origins and installed
contents. Do not rerun unrelated runner/adapter/recorder matrices unless a
specific failure justifies it. No usage examples, duplicate test matrices,
paid turns, owner data, global installs, services/cgroups/VM/network changes.

Evidence is `docs/evidence/host-archive-r1.md`: final code/evidence SHA, commands,
failures/corrections, cleanup, metadata/NOTICE provenance and limits. Commit and
push the dedicated branch, then stop edits for a fresh independent review.
No deployment or G3/whole-system acceptance is established by this increment.
