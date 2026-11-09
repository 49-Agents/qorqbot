# Operations store transition: the additive agentic/Overseer schema

`qorqut.bootstrap.agentic_migration` is the one explicit, offline transition `v1.3-agentic-overseer-1` for an
existing Operations store. It adds exactly the two storage contributions the agentic inbox bridge and the Overseer
bridge own (`agentic-inbox`: 1 table and 2 triggers; `overseer`: 16 tables and 9 indexes), so a store created before
those contributions existed becomes openable by this revision's host. Nothing calls it automatically: ordinary
`Database.open_existing`, host startup, reads, chats, schedules and the fresh-instance initializer keep their strict
semantics and refuse a store whose composition does not match. There is no automatic upgrade, no background check and
no second migration framework.

## The frozen transition

Both compositions are compiled from the same trusted host composition and the same explicitly selected extensions:

* **predecessor**: the current host composition without the two named `StorageContribution` owners;
* **target**: the full current host composition;
* **delta**: only *added* schema objects. A removed object, a changed stored definition, a dropped column or any other
  unrelated schema difference is refused; the transition never repairs a predecessor. New tables start empty, no row
  is copied or rewritten, and the only changed value is `target_identity.digest`. `target_identity.name`, `version`
  and `created_at`, `PRAGMA application_id` and `PRAGMA user_version` stay unchanged;
* **plan**: a deterministic JSON record of the transition id, the canonical instance and database paths, the selected
  extension ids, both target identities, the added object names and one canonical plan digest. It contains no record
  contents and no credentials.

`inspect` recognises the store as exactly `predecessor`, exactly `target`, `missing` or `unknown` (partial schema,
copied-but-wrong identity, unrelated objects). `prepare` refuses anything but a recognised predecessor or target; an
already-target store merely produces the same plan, and `apply` on it is a validated no-op.

## Commands

Run with the prepared/installed host environment that will serve the instance, with the instance offline. All paths
are explicit; the instance root is the only location input (the database is `<instance>/operations/.runtime/operations.sqlite3`).

```sh
python -m qorqut.bootstrap.agentic_migration inspect --instance-root /abs/instance \
  [--extensions-config /abs/instance/config/extensions.json]

python -m qorqut.bootstrap.agentic_migration prepare --instance-root /abs/instance \
  [--extensions-config /abs/instance/config/extensions.json] --save /abs/private/plan.json

python -m qorqut.bootstrap.agentic_migration apply --instance-root /abs/instance \
  [--extensions-config /abs/instance/config/extensions.json] \
  --plan /abs/private/plan.json --backup /abs/backups/predecessor.sqlite3
```

* Every command prints one JSON object and exits `0` on success. A refusal prints
  `{"ok": false, "error": {"code": ..., "message": ..., "retryable": ...}}` and exits `1`.
* `--extensions-config` is the trusted installation-config file (`{"extensions": [...]}`). Omitted means no extension
  is selected. The plan records the selected ids; `apply` needs the same explicit configuration (or the same
  `ExtensionSet` through the API) and refuses another selection.
* `inspect` and `prepare` are read-only. They open SQLite `mode=ro` with `query_only`; on a fully checkpointed store
  (main file only) they open it `immutable`, so no lock, sidecar or other file is created. A store left with a complete
  `-wal`/`-shm` pair by an unclean stop is read through an ordinary read-only snapshot: pending WAL content is included,
  SQLite may use or update that existing shared-memory cache, and no missing file is created. An *incomplete* pair (only
  one of `-wal`/`-shm`) is refused before any connection exists, because this tool never checkpoints, deletes, copies,
  repairs or ignores WAL content — a safe offline inspection needs the main file alone or the whole coherent pair.
  They create no directory, binding or backup. A state is recognised only with the Operations `application_id`, the
  expected `user_version`, the exact identity and the exact schema; a wrong pragma is reported as `unknown` and refused
  by `prepare`, exactly as `apply` refuses it.
* The plan file and the backup destination are private files: an explicit absolute path in an existing directory owned
  by the service user with mode 0700; the files are 0600, never a symlink, and never replaced. `--save` refuses an
  existing file, and `--backup` refuses an existing file or symlink.
* `apply` takes the instance lifetime lock itself and refuses a running host. A coordinated offline caller that already
  holds a verified `InstanceOwnership` for the same instance lifetime path may pass it through the API
  (`apply_migration(paths, plan, backup=..., ownership=held)`); it is verified before every phase and never released by
  this module, so several explicit offline conversions can share one continuous exclusive lock.
* No SQL, table list or alternative migration is accepted from a caller. The DDL is the compiled trusted
  contributions' own statements, executed one by one in registration order inside one `BEGIN IMMEDIATE` transaction
  after SQL functions and foreign keys are installed and integrity/references are checked.

## API

```python
from qorqut.extensions import NO_EXTENSIONS
from qorqut.instance_paths import InstancePaths
from qorqut.bootstrap.agentic_migration import inspect_migration, prepare_migration, apply_migration

paths = InstancePaths(instance_root, {})
inspect_migration(paths, extensions=NO_EXTENSIONS)          # read-only report
plan = prepare_migration(paths, extensions=NO_EXTENSIONS)   # read-only canonical plan
result = apply_migration(paths, plan, backup=predecessor_backup, extensions=NO_EXTENSIONS)
```

`apply_migration` returns the transition identity, the two target identities, the additions, the plan digest, the
backup path and SHA-256 (no contents), and `applied` / `already_migrated`.

## Backup and recovery

The backup is a SQLite backup-API copy of the complete validated predecessor, written as one self-contained 0600 file
(no `-wal`/`-shm` sidecars) into the explicit private destination. It is validated against the predecessor composition
with the engine's snapshot validation before it is published, and it is retained on any later failure. It is not
restored automatically; a post-commit problem is reported and repaired forward, never by silently rolling back.

* **A refusal before `apply` writes anything** (`missing`, symlink, `unknown`, partial schema, wrong pragma, wrong
  digest, wrong plan, wrong instance or selection) leaves the store exactly as it was. Correct the cause explicitly and
  prepare again if the composition or selection changed. An incomplete WAL/shm pair must be brought to the main file
  alone or the complete coherent pair by the prior process's own shutdown/recovery, not by this tool.
* **A failure after the backup but before `COMMIT`** rolls back every DDL effect and the identity digest; the store is
  the predecessor again and the verified backup remains. Once the cause is resolved, run `apply` again with the same
  plan and a *new* backup destination.
* **After `COMMIT`** the store is the target composition and ordinary target opens pass. A repeated `apply` validates
  the target and reports `already_migrated` without DDL or another backup. Never start the older host on the migrated
  store and never restore the backup as an automatic rollback; the backup is a deliberate private predecessor copy.
* **An interrupted or unknown state** is never inferred or repaired: `inspect` reports `unknown`, `prepare`/`apply`
  refuse, and the backup is the preserved evidence. Do not edit the store with other tools or remove WAL sidecars.
* This transition does not initialize the agentic authoring tree, adopt profiles, grant permissions, create routines or
  start a service; those remain separate explicit offline steps.

When a release also carries another explicit offline conversion of the same store (for example the Project source
conversion), prepare this transition again *after* that conversion, under the same continuous offline ownership: the
predecessor is always the compiled composition of the code that will serve the instance, so a pre-conversion plan is
refused rather than applied.