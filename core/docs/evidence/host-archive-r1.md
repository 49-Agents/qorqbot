# Host archive r1 implementation evidence

Verification: 2026-10-08 UTC. Branch `codex/host-archive-r1`; original clean
Manager design/source base `e9d87155588ccce89243eae6d83a8fb28aeed95e`.
Implementation, tests and initial evidence commit:
`cadd709adf5d83e8dd0b48a226cb44de4f51a0cf`. This evidence-only delivery update
records the exact tested revision; the final evidence/delivery commit and
verified pushed SHA are reported through ACLA without a self-referential hash.

Run `649fed24-a7af-40d7-9d41-382ab248483a`; worker
`e98a109c-ec52-4aa1-9d4b-f1d75a1f0167`; bound executor conversation
`01a11950-9d03-7aa0-94b3-eb7069720325`; review thread
`9f85ac25-bbaf-4ecb-b2cf-18948a250ee7`. Saved Codex/Astra identity retained;
no nested agents or internal reviewers. This is implementation of the supplied
HOST-ARCHIVE design and D31, pending fresh independent review. No complete M07,
G3, other G0–G6 gate, deployment or whole-system readiness is established.

## Owned changes

- `transcripts/archive.py`: the four internal callables, using the existing host
  database, installation lifetime/effect gates, caller rule, event journal and
  descriptor/workspace helpers.
- `transcripts/archive_models.py`: frozen metadata/state/receipt values and
  revalidation of supplied capture dataclasses, strict signed counters,
  canonical UUIDs, UTF-8 metadata and shared UTC timestamp normalization.
- `storage/migrations.py`: migration 4 only, six `archive_` tables and their
  constraints/indexes/permanence guards. Migrations 1–3 are unchanged.
- `app.py`: two passive assembly lines for `Application.archive`.
- `tests/archive/`: 21 owning tests, including one shared real capture-spool
  transfer flow. The narrow fixture reuses `agents.support.AgentCase` and the
  accepted foundation installation/clock/fault support.
- This evidence file. No existing test, packaging/pin/NOTICE, generic storage,
  workspace/agent, runner/provider/recorder/probe, transport or global plan
  changes were needed. The registry still has nine public operations and three
  fixed job handlers; archive is not advertised as a public mutation or tool.

## Implemented boundary

`register(context, binding, source, source_epoch, metadata)` atomically records
the exact declared run, source, epoch and unique link. It checks the open
installation, actual registered agent and same-agent parent/source lineage.
Original actor/time and evidence remain unchanged on replay. Registration starts
no filesystem work. Source, epoch and link events commit in that transaction;
the epoch event references exact immutable metadata with its canonical hash,
keeping the journal bounded even when several fields contain 4096 four-byte
characters. That bounded metadata hash is computed before opening SQL.

Registration is declared lineage, not proof of native process ownership,
authentication, a current run or a live provider. Historical run recovery and
generation coordinates are preserved; compatible distinct runs can link an
existing epoch and old registered runs can drain late bytes. No current-runtime
projection advances. Agent lifecycle remains provisioning and capture health
remains unknown in its ordinary public view.

`receive(context, binding, chunk)` validates immutable bytes, exact length,
SHA-256, strict bounded coordinates and the registered link before file effects.
The installation use guard and shared effect gate span intake. A short SQL
snapshot precedes file work; short final SQL revalidates the lineage, host epoch
and contiguous cursor. No chunk creates a generic operation/file receipt/job or
ordinary per-chunk event.

Objects live only under the initialized agent's reserved
`history/chunks/<source>/<epoch>/`, with deterministic offset/length/hash names.
The complete existing ownership marker is checked against installation, agent,
creation operation and provisioning job. Descriptor traversal rejects symlinks
and non-directory ancestors. Opened parents and the root are revalidated before
evidence is committed. Reserved descendant links are fsynced, including links
found on a retry after an interrupted mkdir/fsync. Agent/workspace directories
are never repaired or reseeded.

New bytes use an exclusive private sibling stage, flush/file fsync, Linux
`renameat2(RENAME_NOREPLACE)` through the unchanged workspace helper, verified
regular final bytes, file fsync and parent fsync. There is no replacing rename
fallback. Known deterministic orphans can be verified/reused; unknown stages are
left alone. Existing objects must have stable regular-file identity, exact size
and hash and a single link. All file hashing/writes/fsync are outside SQL.
Receipt insertion and watermark advancement commit together before a returned
`ArchiveReceipt` exists. The receipt contains the full supplied binding, source,
epoch, current host recovery epoch and committed watermark.

Exact repeats verify their accepted object and return the current watermark
without changing inventory/events/attribution. Rechunking, overlap and changed
accepted bytes preserve a deterministic conflict object, then atomically insert
one unique conflict receipt, latch the epoch and append one attributed conflict
event. Accepted bytes/watermark remain unchanged. Repeated identical conflicts
deduplicate; new intake and ACKs remain closed. A declared successor can retain
divergent bytes from zero without modifying its predecessor.

Only a returned receipt permits the future collector to send an ACK. This module
never calls `CaptureSpool.acknowledge`/`reclaim` and starts no collection loop.
`state` is SQL-only historical inventory. `read_chunk` revalidates stored exact
coordinates, opens/verifies one bounded committed object and returns a `Chunk`;
it does not fsync, write, repair or authorize an ACK. Missing/damaged committed
objects retain their SQL evidence and fail with storage/uncertain-effect errors.

Each epoch retains its registering host recovery epoch. Recovery activation
waits for admitted intake through the existing use guard. Held epochs reject
receive, including exact byte retries, and new run links before filesystem
effects. Identical registration returns the held state without refreshing that
fence. A successor cannot bypass a held predecessor. Historical state and raw
reads remain available; this increment has no hold release or restore audit.

## Checks and actual results

All work used this checkout, private virtual environments/build outputs and
temporary synthetic data. Source environment creation/install:

```sh
/usr/bin/python3.12 -m venv .venv
.venv/bin/python -I -m pip install --disable-pip-version-check --no-cache-dir -r requirements.lock
```

The source suite commands used `PYTHONDONTWRITEBYTECODE=1` and `PYTHONPATH=src`:

```sh
.venv/bin/python -m unittest discover -s tests/archive -t tests -v
.venv/bin/python -m unittest discover -s tests -p test_foundation.py -v
.venv/bin/python -m unittest discover -s tests/agents -t tests -v
```

- Initial archive run: 21 tests, **20 passed / 1 failed**, 11.655 s. The passive
  assembly fixture omitted required `listen_address`/`listen_port`. Supplying
  those explicit synthetic settings corrected the fixture; its focused rerun
  passed (1 test, 0.277 s).
- Foundation: **26 passed**, 12.162 s, once. Agent suite: **19 passed**,
  19.340 s, once. No timing, concurrency assertion, fault coverage or old fixture
  was weakened. The previously documented foundation 70 ms busy-timeout
  sensitivity did not recur; these passes do not erase that limitation.
- A subsequently added overlong-fractional-timestamp regression **failed once**
  (1 test, 0.272 s): shared UTC normalization shortened the input before a text
  bound could reject it. `ArchiveMetadata` now bounds supplied time text before
  applying the shared timestamp validator. Focused validation, registration and
  transfer reran successfully (**3 passed**, 3.573 s).
- The bounded metadata hash was then moved before SQL, and stored read
  coordinates explicitly revalidated before building/opening an object path.
  The SQL-free hash assertion passed. The final owning source suite reran for
  those archive refinements: **21 passed**, 13.959 s, no failures/skips.
- Two final fixture refinements made the self-parent error assertion exact and
  the noncanonical UUID input deterministic. Both affected tests passed in a
  focused rerun (**2 passed**, 0.841 s); production bytes did not change.

Focused source commands used `PYTHONPATH=src:tests`:

```sh
.venv/bin/python -m unittest -v archive.test_inventory.InventoryTests.test_validation_rechecks_dataclasses_and_models_before_any_effect
.venv/bin/python -m unittest -v archive.test_inventory.InventoryTests.test_validation_rechecks_dataclasses_and_models_before_any_effect archive.test_inventory.InventoryTests.test_registration_replay_links_metadata_attribution_and_atomic_rollback archive.test_transfer.TransferTests
.venv/bin/python -m unittest -v archive.test_inventory.InventoryTests.test_registration_replay_links_metadata_attribution_and_atomic_rollback
.venv/bin/python -m unittest -v archive.test_inventory.InventoryTests.test_parent_epochs_and_distinct_source_identity archive.test_inventory.InventoryTests.test_validation_rechecks_dataclasses_and_models_before_any_effect
```

The 21-test owning suite covers:

- Register/replay/run relink, full metadata, first attribution, wrong parent/
  lineage, transaction rollback, exact epochs and SQL update/delete/REPLACE
  guards. A v3 database migrates explicitly to v4 with prior data/checksums
  preserved. Unknown callers/records and uninitialized workspaces cannot cause
  archive effects; native locator strings are never opened.
- A real independent `CaptureSpool.pending` flow with binary bytes, Unicode,
  a 9 MiB single raw record and an incomplete suffix. Pagination alone advances
  no ACK. The first committed response and then the next returned ACK are
  deliberately dropped. After host restart, only returned receipts are applied
  explicitly; all runner payloads are reclaimed. Runner and host restart, then
  bounded host reads reconstruct the entire exact stream from immutable objects.
- Fourteen fault boundaries around stage fsync, no-replace publication, parent
  fsync, finalization, SQL receipt/watermark/commit and return. No failed call
  yields a receipt; replay verifies existing orphans and retains their inode.
  Actual injected file/directory fsync errors and unsupported no-replace failure
  cannot advance SQL. Two owned subprocesses actually `os._exit(73)` after
  publication and after SQL commit; reopened state has the correct watermark
  and exact replay. Each subprocess asserts and reports its imported package
  root before acting.
- Gaps, changed hashes/shapes, overlap/rechunk conflicts and deduplicated conflict
  evidence; conflict-publication failure/replay; corrupt/missing committed
  objects and missing parents without destructive repair. Ordinary raw reads
  are trapped against mkdir/fsync and leave inventory unchanged.
- Concurrent identical calls through two Archive assemblies share the accepted
  effect gate. An unrelated real SQL write commits while intake is paused in
  file I/O. Future coordinates remain rejected. Wrong marker fields/phase,
  symlink ancestry, final symlinks/directories/FIFOs/hard links/occupied bytes,
  a racing destination, epoch-parent substitution and data-root substitution
  cannot overwrite or adopt outside evidence.
- Explicit recovery waits for in-flight receive, fences subsequent ACK-producing
  calls and new links/successors, preserves exact registration fences across
  reopen and keeps raw reads and historical runner binding intact.
- Audited import/assembly rejects SQLite opens, writes, mkdir, subprocesses,
  sockets and loading the atomic primitive at import. The existing nine
  operations, three handlers and schema 4 are asserted without opening storage.

## Installed package and provenance

```sh
.venv/bin/python -I -m pip wheel --disable-pip-version-check --no-cache-dir --no-deps --no-build-isolation --wheel-dir dist .
/usr/bin/python3.12 -m venv .venv/install-check
.venv/install-check/bin/python -I -m pip install --disable-pip-version-check --no-cache-dir -r requirements.lock
.venv/install-check/bin/python -I -m pip install --disable-pip-version-check --no-cache-dir --no-deps --force-reinstall dist/qorqut_core-0.1.0-py3-none-any.whl
```

Installed checks ran with cwd `.venv/wheel-tests`, containing a byte-identical
test-only copy of the owning tests and accepted agent/transport/foundation
support, with no `src` or product package. Command prefix was
`../install-check/bin/python -I -m unittest discover`:

| Arguments | Final wheel result |
| --- | --- |
| `-s tests/archive -t tests -p test_transfer.py -v` | 1 passed, 2.614 s |
| `-s tests/agents -t tests -p test_agent_flow.py -v` | 1 passed, 3.129 s |
| `-s tests/archive -t tests -p test_passive.py -v` | 1 passed, 0.236 s |
| `-s tests/archive -t tests -p test_inventory.py -k test_legacy_v3 -v` | 1 passed, 0.250 s |
| `-s tests/archive -t tests -p test_intake.py -k test_owned_process_exit -v` | 1 passed / 2 actual exits, 0.760 s |

The same five installed checks passed before the last two archive refinements
(2.694, 3.488, 0.289, 0.404 and 1.122 s respectively). The wheel was rebuilt/
reinstalled and these small checks rerun for the final production bytes. The
HTTP/MCP smoke is the unchanged existing real loopback/official SDK agent flow,
including installed console-server startup, idempotent cross-transport results,
all five agent operations and explicit real workspace steps. No unrelated
runner/provider/recorder matrices or whole-package suites were rerun.

The task-local `.venv/verify_package.py` runs in both source (`-P`,
`PYTHONPATH=src`) and installed (`-I`) environments. It verifies every **42 Python
files plus NOTICE** against source SHA-256, every **31 pinned distribution
versions**, migration 1–3 checksums against `git show` at the exact original
base, and every imported package file. It launches a separate bounded child
which reports actual origins for all 42 modules. Parent/child source imports
resolve under this checkout's `src/qorqut_core`; installed imports resolve under
`.venv/install-check/lib/python3.12/site-packages/qorqut_core`. The installed
crash and passive fixtures independently verify their actual child origins.
Both initial and final inventory probes passed. Source/installed `pip check`
and `git diff --check` passed. No build/install/other test failure occurred beyond
the two explicitly recorded failures above.

| Artifact | SHA-256 |
| --- | --- |
| Final wheel | `6df58eda7d2bc7ac9867174f11af90b64af232cbab00a49c9c7f564ca3b74d53` |
| Unchanged `requirements.lock` | `f84936aefd43ea97e2c74caac5e51bf1a9176ddb4214e827414ec6223e31b639` |
| Unchanged `pyproject.toml` | `753d092b92176a6c855b1f03b3b82ca30f7d8915bb6c77e2450624343fa44995` |
| Source/installed `storage/NOTICE` | `c9a80f7b863ca7ca6a1d640a5ad0cb72ca991eec41d862741038ef40a859ef0d` |
| Migration 4 | `68b55717b82af97d735125b64125b71ee6c6ca94a7d02bf313d0b859c2804dca` |

Private evidence remains under `.venv/`: `archive-source-{initial,final}.log`,
`archive-passive-corrected.log`, `archive-timestamp-{initial,corrected}.log`,
`archive-hash-boundary.log`, `archive-fixtures-final.log`, `foundation-source.log`,
`agents-source.log`, `archive-wheel-{transfer,passive,schema,crash}[-final].log`,
`agents-wheel-smoke[-final].log`, `*-package[-final].json`, build/install logs,
manifest, package verifier, virtual environments and test-only staging. These
are disposable local verification artifacts, not product examples or fixtures
copied from another lane.

## Cleanup and limits

Observed Python 3.12.3, SQLite 3.45.1, Linux 6.8.0-138-generic; the checkout
filesystem reports the ext2/ext3 family. Durability assumes local persistent
storage honoring SQLite WAL/FULL, file and directory fsync, flock and Linux
atomic no-replace rename. Fault injection and process exits do not certify
hardware power loss, filesystem failure or independent backup integrity.
Cooperative gates do not sandbox arbitrary unrestricted shell writers.

This is new implementation of the supplied design using unchanged accepted
helpers; no new upstream extraction, live Qorqut installation/configuration/
data, owner session, native store, credential or browser profile was accessed.
Existing source notices and package pins remain intact. Tests close/reap only
their own processes, remove their temporary host/runner stores and close the
owned loopback endpoints. No persistent process/service, cgroup, VM, network
configuration, native provider/model turn, global install or browser/UI change
was made. Only ignored task-local validation/build outputs remain.

Every receive verifies its own object; it is not an O(total-history) audit of
previous prefixes. Manual deletion/corruption and mismatched database/data
snapshots require explicit repair; success does not certify an independent
backup or archive-wide integrity. There is no parser, checkpoint completeness,
native tailer, authenticated collector/runner admission, backup/import, recovery
hold release, automatic cleanup or conflict resolver. The future collector must
authenticate lineage and apply only successfully returned receipts. No merge,
force push, rebase, PR, release or deployment is included. Manager owns review
and landing; edits stop after normal dedicated-branch push and completion report.
