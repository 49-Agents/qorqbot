# Foundation r1 implementation evidence

Date: 2026-10-07. Branch: `codex/foundation-r1`. Base:
`db25fc97a4127f8f46f3092ed96578bcdba685ce` (initial status clean).
Run: `649fed24-a7af-40d7-9d41-382ab248483a`.
Worker: `11ba72b8-4071-471a-b735-a16b6ce06a53`, bound executor conversation
`01a11826-a109-7703-a680-03f1b5a95569`. No delegated or internal reviewer work.

This is the bounded M00 increment. No global G0–G6 gate or whole module is marked
complete. This document reports implemented interfaces for Manager review and
freezing; it does not establish dependent modules' contracts.

## Manager amendment retained

ACLA message 2, `MANAGER_DECISION — foundation r1 clarification 1`, directed:

1. `config_update` atomically commits defaults/revision, event and historical
   result, with no job because it has no external effect. An unadvertised fixed
   test operation/handler demonstrates state/event/job/result atomicity.
2. Recovery activation is an explicit local maintenance callable outside the
   public registry. Require exclusive installation maintenance ownership and
   expected installation/recovery identity, generate a fresh UUID, atomically
   hold unfinished external effects under `recovery_review`, and preserve prior
   evidence. Never activate recovery on ordinary startup, migration or config
   mutation. No backup/import implementation in this increment.
3. Public `operations_reconcile` is deferred to domain/transport integration.
   Implement the narrow internal file-install inspector/reconciler now, verifying
   staged/final bytes and receipts without assuming an unknown effect did not
   happen. Advertise only config operations, operation lookup and event paging.

These instructions were incorporated without changing shared planning files.

## Delivered boundary

The package has no executable entrypoints, web server, runner, model client,
automatic worker thread or import-time initialization. Pydantic v2 provides the
input/output/error models. SQLite owns explicit installation/default/actor/
operation/event/job/file-receipt tables; no generic document store or message/
transcript tables were added. Schema version is 2; application ID is `0x514f5251`.

`InstallationSettings` takes an installation UUID, absolute source/release/data
roots, listen address/port, optional private IPv4 network and bounded busy timeout
(1–60,000 ms, default 1,000). Roots cannot overlap the data root or cross symlinks.
These are static inputs; this package opens no listener or network. Defaults must
be supplied as `NewAgentDefaults(cli=CLIDefaults(provider, model),
resources=ResourceDefaults(cpu, memory_mib, disk_gib))`. Providers are `claude` or
`codex`; the model is an explicit nonblank bounded identifier. Resource bounds
are positive representable integers, not measured capacity. No personal CLI
configuration is read. Saved defaults start at revision 1 and do not reference
or update any existing agent state.

## Callable interfaces

| Interface | Implemented behavior |
| --- | --- |
| `Application.initialize(settings, initial_defaults, initial_owner, *, clock, id_factory, fault)` | Explicitly creates static installation file/schema/metadata/owner registration; requires all initial resource/CLI defaults. Returns the assembled application holding the installation lock. Repeated initialization preserves saved defaults/identities. |
| `Application.open(settings, *, clock, id_factory, fault)` | Opens a compatible existing installation; never migrates, rotates recovery epoch, starts a job or creates an actor. |
| `Application(settings, database)` | Pure assembly using an explicitly supplied database. |
| `Application.close()` | Releases host ownership after active work has finished. |
| `load_settings(path)` | Reads only an explicit, bounded static installation JSON file. |
| `Application.invoke(name, Invocation(actor_id), arguments)` | Shared registered operation path, returns typed `OperationResult`; validation/domain errors return typed errors without echoing supplied input. Transport must construct the invocation from authenticated identity. |
| `register_actor(tx, Actor(actor_id, kind, agent_id))` | Trusted SQL-only registration primitive; refuses rebinding. It is not a public tool. Agent actors require an agent UUID; owner actors do not. |
| `Application.activate_recovery(context, RecoveryActivation(...))` | Exclusive maintenance callable with idempotency key and expected installation/recovery UUIDs. Rotates the epoch, journals preserved job evidence and holds unfinished external jobs. Not registered publicly. |

The immutable registry has exactly four descriptors, with both Pydantic argument
and output models available for schema generation:

| Name | Arguments | Output data |
| --- | --- | --- |
| `config_get` | `ConfigGet`: no fields | `ConfigView`: defaults/revision, installation/recovery IDs, `allow_all`, fixed twenty icon keys, API/event payload version 1. Runner/browser protocol support lists are empty pending integration. No static paths, raw config or credential fields. |
| `config_update` | `ConfigUpdate`: `idempotency_key`, `expected_revision`, complete `defaults` | Historical `ConfigView` after the synchronous change; `outcome=completed`, operation UUID and event cursor. No job. |
| `operations_get` | `OperationsGet(operation_id)` | `OperationView`: attributed operation metadata, immutable original result, separately evolving typed `JobRecord` values. |
| `events_list` | `EventsList(after_seq=0, recovery_epoch=None, limit=100)` | `EventPage`: items, next cursor, recovery epoch and `has_more`; limit 1–200. Nonzero continuation requires the matching recovery epoch. |

Every mutation validates before hashing. Hash input is canonical sorted UTF-8
JSON of validated arguments plus version 1, excluding the idempotency key.
Receipts scope keys by actor + operation name + key (1–200 characters). Exact
replay precedes revision checks and returns the saved result even after newer
changes. A different validated payload conflicts. Revision mismatches mutate
nothing. All registered actors use `allow_all`; there is no permissions engine.

`Operation(name, arguments, mutation, handler, output)` and
`Application.execute(operation, context, arguments)` are the internal assembly
seam used by the fixed test operations. A handler returns `Change(data,
event_seq, outcome)` and performs SQL-only work in the supplied transaction.
They are not a dynamic registration endpoint, arbitrary job language or a tool.

## Transactions, journal and jobs

`Database(path, lock, *, busy_timeout_ms, clock, id_factory, fault)` supplies
`validate()`, explicit `migrate()`, `transaction(writable=True)` and `read()`.
The transaction yields `Transaction(db, connection, writable)`; SQL primitives
require `tx.require_write()`. Commit/rollback and connections are managed by the
context. Mutations use `BEGIN IMMEDIATE`; runtime connections enforce foreign keys,
FULL synchronization and bounded busy waits. Busy errors are retryable
`storage_blocked`. `last_timing` reports wait/hold seconds and commit status
without logging arguments. Clocks return UTC RFC 3339 strings; UUID factories
default to `uuid4`.

Migrations are a fixed tuple of SQL statement lists with stored checksums. DDL,
version/checksum records and foreign-key checks commit together. Compatibility
refusal precedes mutation. Because SQLite `mode=ro` can write WAL sidecars,
checkpointed databases are probed using `immutable=1`; nonempty WAL state is
inspected in a private disposable database/WAL copy with source stability checks.
This can require temporary disk space equal to the database plus WAL; it is not
a backup API. Known older schemas require explicit migration before normal open.

`HostLock(data_root/locks/host.lock)` uses nonblocking `flock`, is never unlinked,
rejects a second process and rejects inherited post-fork authority. Lifetime
`use()` guards keep ownership through effects. `maintenance()` drains active
work and blocks new admission. A shared in-process effect gate serializes job
execution and file installation/reconciliation even across multiple assemblies
sharing that lock. A lease is not the effect fence.

`append_event(tx, EventInput(...))` returns the increasing local cursor. Payloads
are bounded to 64 KiB. Source installation/sequence and original event IDs are
unique; exact imported repeats replay and conflicting provenance rejects.
Occurred/recorded times are distinct. SQL triggers forbid changing/deleting
events and historical operation receipts. Corrections require new events.

`Jobs(db, fixed_handlers)` supplies SQL-only
`enqueue(tx, kind, typed_arguments, operation_id=..., target_id=None,
expected_generation=None)`, plus `claim`, `progress`, `finish`, `expire`,
`run_next`, `reconcile` and `get`. The production assembly includes only
`file_install(FileJob(receipt_id))`. Handler names are fixed in the assembly;
no executable code is stored. Claims have UUID tokens, worker/epoch binding and
1–3,600 second leases. Progress stores phase/count/total. Saved state covers
queued/waiting/running/completed/failed/uncertain/cancelled, attempt count,
typed reason/error and next inspection time. Claim/finish transitions append
events, retaining previous lease/progress/error evidence.

Expired external jobs become uncertain and cannot be normally claimed again;
the fixed inspector must inspect the actual file effect. DB-only jobs can be
reclaimed. Stale tokens/epochs/leases cannot finish or renew a job. Recovery holds
queued, waiting, running and uncertain external jobs; normal restart retains the
existing epoch and queued work. The original operation result never evolves.

## File receipt interface

1. `Files.stage(target_path, chunks, expected_sha256=..., mode="replace")`
   streams byte chunks outside SQL into a UUID-named file in the existing target
   parent; hashes, fsyncs the staged file and fsyncs the directory. Returns typed
   `StagedFile`. Paths are normalized relative to the data root, cannot traverse
   symlinks, and cannot target installation/DB/lock files. Mode `immutable`
   permits only absent or byte-identical final content. Replacement requires an
   explicit prior hash or explicit absence. New staged files use mode 0600.
2. `Files.attach(tx, staged, operation_id=...)` inserts the receipt and fixed job
   inside the caller's mutation transaction. That transaction can also commit
   domain state, event and original operation result. No file access occurs here.
   One unresolved receipt reserves each destination.
3. `Files.install(receipt_id, inspection_only=False)` runs outside SQL, retains
   directory descriptors and validates parent device/inode and expected/staged/
   final hashes. It renames within the parent, verifies/fsyncs final content,
   fsyncs the parent and returns `VerifiedInstall` evidence.
4. `Files.finalize(tx, verified)` is SQL-only. It commits the installed receipt and
   event; later domain callers can commit their own rows/watermark alongside it.
   The current recovery epoch must match the verification. This does not
   implement any archive chunk, file-reference or authored-file public operation.
5. `Jobs.reconcile(job_id)` calls the fixed file inspector. In ordinary recovery
   it can finish a verified staged rename or recognize verified final bytes.
   A restore hold permits final-byte inspection/fsync only; missing proof retains
   `recovery_review` and never renames historical staged bytes. Unknown or damaged
   files remain unresolved. No orphan deletion/garbage collection is implemented.

There are separately recoverable commits for installed file receipt and terminal
job state; a crash between them is resolved by inspecting the final file. No
successful file receipt is committed before final file and parent fsync.

## Passed checks and environment

All checks used isolated temporary synthetic state. No services, accounts or
native CLI stores were used. The exact test/build commands were:

```text
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
.venv/bin/python -m pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir dist .
.venv/bin/python -m venv .venv/install-check
.venv/install-check/bin/python -m pip install --no-cache-dir -r requirements.lock
.venv/install-check/bin/python -m pip install --no-cache-dir --no-deps --force-reinstall dist/qorqut_core-0.1.0-py3-none-any.whl
env -u PYTHONPATH .venv/install-check/bin/python -I -m unittest discover -s tests -v
.venv/install-check/bin/python -m pip check
```

Source suite: **26 passed**. Installed-wheel suite: **26 passed**. Both dependency
checks: no broken requirements. Wheel build/install succeeded; final wheel SHA-256
`ea69d181624930b65c9b9378e19bc0086cfbddabcd172da6140edc98ca1769eb`.
An additional isolated stdin probe verified installed-package location, packaged
NOTICE, four input/output JSON schemas, explicit initialization and mutation
replay, with an audit hook refusing `/home/alp/qorqut/` access. Import tests reject
filesystem writes, SQLite opens, subprocesses and sockets; assembly is tested
without a database connection.

The suite covers concurrent identical requests, normalized hash replay/conflict,
revision races, distinct actors, rollback at mutation/event/job/result/commit
boundaries, saved-result replay after later revisions, failed migrations and
prior-event/operation preservation, nonmutating unsupported DELETE/WAL schemas,
busy timing, a real second-process lock attempt, restart/lease/progress/stale
completion, exclusive recovery maintenance, restore holds, receipt/staging/
rename/fsync boundaries, corrupt stages, authored edits and unknown orphans.
Child processes use abrupt `os._exit` before/after operation commit and after
rename, proving recovery without Python cleanup. A blocked external handler
allows unrelated config reads/writes; supplied external-effect entrypoints reject
calls from a write transaction.

Observed: Python **3.12.3**, SQLite **3.45.1**, Linux
**6.8.0-138-generic x86_64**. `stat -f` reported `ext2/ext3` filesystem family.
Runtime/build versions in `requirements.lock` were installed and checked:
Pydantic **2.12.3**, pydantic_core **2.41.4**, annotated-types **0.8.0**,
typing_extensions **4.16.0**, typing-inspection **0.4.4**, setuptools **80.9.0**,
wheel **0.45.1**. No test dependency beyond stdlib unittest was added.

## Source provenance and limits

Read the exact P00 Qorqut revision
`e71fd1e527368bfd41b9815126f4bf33452aeebb`: `storage/database.py`,
`storage/receipts.py`, `application/engine.py`, and the related
`storage/safe_io.py` failure/descriptor behavior. This is a compact schema and
application rewrite, informed by transaction/replay/descriptor patterns; no
business migrations, repositories, policy preparation stack or Qorqut imports
were brought over. Inspected file SHA-256 values respectively:

```text
database.py 4368427c65f94faf0b61baaf8cc25907be8cb2a724c33d79df8a4ce53aae7449
receipts.py 79a6b2e1ed180ad138b9d7fd0ccfc004322452439b07dffb16abd78f986a9ad6
engine.py e850da9d083fa29aef84f5533b776e6321c9955d5a0aaf182add190e2fe0d5af
safe_io.py 23d410b0a8c92283c6b9ec43c646dceee19044f8a3bac4f26dc74328b3c20e14
```

Read agent-dms `storage.py`, `service.py` and LICENSE using `git show` at
`22a9aaa484d2d1f20fc0ac2ea30ead706c2784e8`; its MIT notice is retained in packaged
`storage/NOTICE`. No dashboard code was needed by this boundary. Source roots
were read only; no live configuration/database/history/profile was imported.

Durability assumes local persistent storage honoring SQLite WAL/FULL, file fsync,
directory fsync, atomic same-directory rename and flock. These checks are process
crash/fault tests, not power-loss or hardware durability certification. Guards
cover supplied effect helpers; trusted future handlers still must obey the
SQL-only transaction contract. Arbitrary unmanaged shell writers are not fenced
by these cooperative locks. Authored-file hash checks are optimistic, not a
filesystem compare-and-swap guarantee.

Receipt parents must already exist and retain their device/inode binding. A
relocated restored filesystem needs separately verified rebinding in later
recovery integration; this increment does not silently do it. Restore/import,
backups, public reconciliation decisions, agent lifecycle, authentication
transports, actual archive watermarks, HTTP/MCP/operator CLI, VM/runner/browser/UI,
capacity measurements and real native/provider acceptance remain unimplemented
or untested here. No merge, deployment, release or PR creation is part of this
increment. Final commit and push outcome are reported through ACLA.
