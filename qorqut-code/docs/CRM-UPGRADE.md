# Offline Upgrade To The CRM-Capable Store

`bazarlyk.migration.crm_upgrade` copies a stopped **pre-CRM** Operations store into a **new** database file for the
current composition. It is an explicitly invoked offline command: nothing runs it at startup, no default path
exists, it never modifies the source, never writes to an existing path and never touches a live instance. This
source delivery supplies and tests the tool; it does **not** run it on any real installation.

## What changes

The target is whatever `host_assembly` composes, which imports the Domain package's own schema declarations. The
upgrade compares that compiled inventory with a frozen fingerprint of the pre-CRM store and reports the difference:

- **New tables** for CRM membership (`crm_entries`) and recorded observations (`crm_activity_events`), with their
  indexes and the identity-only capture triggers.
- **Rebuilt tables** where the stored definition changed so a Table can be a source: the shared discussion
  (`thread_messages`, `thread_follows`) and the private conversations (`owner_conversations`) admit `table`, and
  `data_tables` gains `table_kind` and `crm_config_json`. Existing Tables take the column default, so every existing
  Table is a **general** Table.
- From the deployed v1.3 release, every other table, index, trigger and row is carried byte for byte. The older
  Project-only core baseline additionally gains the already accepted Overseer tables in the current composition.

Rows keep their exact `rowid`, text, identifiers, source keys, inbox references and revisions. The next-id counter of
every `AUTOINCREMENT` table (including `inbox_changes` and the private message sequence) is carried, so no retired id
is reused. The upgrade writes no dataset, matches no contact, converts no row to a Task and writes no Contextbase
record.

## Preconditions

1. The service is **stopped**, so SQLite has checkpointed and removed any `-wal`/`-shm` file. An existing `-wal`,
   `-shm` or non-empty `-journal` beside the source is an active database and is refused. (A standalone backup made
   with the engine's backup is also a valid source.)
2. The source is exactly the pre-CRM `qorqut-v1.2` target: the Operations application id, the recorded target name,
   version and digest, and every table, index and trigger of the frozen fingerprint
   (`app/bazarlyk/migration/crm_baseline_fingerprint.json`, plus `crm_release_fingerprints.json` for the deployed
   v1.3 release: per object its owner table and the SHA-256 of its stored definition; no row data). The release file
   recognizes the exact core+Overseer composition and the exact composition with Content, Fastmail and Paper.
   Other extension combinations, hand-edited schemas, already-upgraded stores and unknown targets are refused.
3. The destination path does not exist (nor its `-wal`/`-shm`/`-journal`) and is not beneath a symbolic link.

## Procedure

```
python -m bazarlyk.migration.crm_upgrade --source OLD.sqlite3 --destination NEW.sqlite3 --dry-run
python -m bazarlyk.migration.crm_upgrade --source OLD.sqlite3 --destination NEW.sqlite3
```

For the deployed installation with extensions, add `--installation-config /absolute/path/to/installation.json`
to both commands, naming its existing trusted configuration. This selects the same installed extensions for the
target schema; it starts no extension service and sends nothing. Omitting or changing that selection is refused
instead of discarding extension tables. The configuration is read only and never included in the report.

`--dry-run` performs the same build and verification in a throwaway directory and publishes nothing; run it first.
Exit status 0 prints a JSON report (counts, names and hashes only - never a row, title or message body): the source
path and SHA-256 with `"unchanged": true`, the old and new target identities, the row count of every carried table,
`new_tables`, `rebuilt_tables`, `added_columns`, new/changed indexes and triggers, the published file's hash, and the
`checks`. Status 2 prints `{"ok": false, "error": {code, message, details?}}` on stderr and leaves nothing behind.

The build is one transaction in a private temporary file beside the destination, with the source attached read-only
and immutable: target tables are created, the rows are copied by column name (so the copy fires no trigger and adds
no `inbox_changes` row), seed rows are added where absent, the id counters are carried, and only then are the indexes
and triggers created. The new target identity, `user_version` and application id are written last.

The result is published only after **all** of these hold, otherwise the temporary file is removed:

- the compiled inventory equals the stored one (same tables, indexes and triggers, owner tables and definitions);
- every carried table equals its source - same count and the same `rowid` + column content in both directions - and
  the counters match;
- `PRAGMA foreign_key_check` and `PRAGMA integrity_check` are clean;
- the engine's own `Database.open_existing` validation (identity, inventory, plugin validators) accepts the file;
- **immediately before publication**, the source's identity and content (device, inode, size, modification time and
  SHA-256) are identical to what was read at the start, and no `-wal`, `-shm` or non-empty `-journal` has appeared
  beside it.

Every check that can fail runs *before* the destination exists. Publication is the last step: the temporary file is
given owner-only permissions and flushed, then published by an exclusive hard link, so a destination that appears
in the meantime is never replaced (`upgrade_destination_exists`). There is no fallible validation after that link, so
the command never reports a failure while leaving an output behind. If the final directory flush fails, only the exact
file this run created (compared by device and inode) is removed - never a path another actor has since put there -
and the run reports `upgrade_failed`.

## Upgrading the current CRM store to Move to project

Move to project ([TASK-MOVE.md](TASK-MOVE.md)) changes the store, so an installation running the deployed CRM release
(`v13-crm-20261028`) needs this same offline upgrade once before the new code serves it. Nothing migrates at service
start, and the new code refuses the old store.

What differs from the CRM store, and nothing else:

- one **new table**, `task_moves` (the append-only audit of relocations), with its two indexes and its two immutability
  triggers;
- one **added column**, the nullable `thread_messages.reference_namespace`, which is why `thread_messages` is listed as a
  rebuilt table. It is `NULL` on every carried row ("never needed a pin"); the first guarded relocation of a Task fills it
  with the exact old namespace (`global` or `business:<UUID>`) so the immutable note keeps the scope its references were
  written in;
- five **rewritten triggers**, each narrowed to recognize the one armed relocation and still refusing every ordinary or
  raw write: `tasks_scope_identity`, `task_snooze_wakes_immutable`, `thread_messages_immutable_update`,
  `crm_entries_identity` and `crm_activity_events_immutable_update`. The message trigger additionally protects
  `reference_namespace`: it may only be filled, and only with the exact old namespace, inside the armed relocation.

Every other table is untouched, and every row (including every `thread_messages` row, body, `refs_json`, event
association, author and timestamp), `rowid` and counter is carried byte for byte; the copy is by column name, so it fires
no trigger. The frozen
fingerprint file also holds the **current CRM** store, generated from the unmodified base composition (`qorqut-code`
`e695a994`, `qorqut-operations` `11d056bd`) as schema definitions only: `crm_release_fingerprints.json` baselines 2
(core) and 3 (Content, Fastmail and Paper), matching `tests/fixtures/crm_release_schemas.json`. The deployed store's
extension set must be passed with `--installation-config` exactly as above; any other set is refused.

```
python -m bazarlyk.migration.crm_upgrade --source CRM.sqlite3 --destination MOVE.sqlite3 --installation-config INSTALL.json --dry-run
python -m bazarlyk.migration.crm_upgrade --source CRM.sqlite3 --destination MOVE.sqlite3 --installation-config INSTALL.json
```

The dry run must report `new_tables: ["task_moves"]`, `rebuilt_tables: ["thread_messages"]`, `added_columns:
{"thread_messages": ["reference_namespace"]}` and exactly the five changed triggers above. Run it against a stopped service or a standalone backup, never the live file; keep the CRM file as the
rollback. Installing the output, setting the release pins and restarting are the owner's reviewed release steps and
are not done by this tool or by this source delivery.

A **fresh** store is created from the same composition and carries the same objects (`task_moves`, the
`reference_namespace` column and the five triggers); `test_task_move_upgrade.py` checks that the upgraded store's objects equal the compiled target, that every
carried row is identical, and that plain SQL, with or without the engine's callbacks, cannot relocate a Task, its
discussion, its CRM membership or its observations afterwards.

## Refusals

| Code | Meaning |
|---|---|
| `upgrade_source_invalid` | missing, symlinked or non-regular source; not a readable database; failed integrity or foreign-key check |
| `upgrade_source_active` | `-wal`, `-shm` or a hot `-journal` beside the source |
| `upgrade_source_unrecognized` | not a recognized frozen source (the pre-CRM target, a deployed release or the current CRM store; identity, version, application id or any schema object differs; the first differences are named) |
| `upgrade_already_current` | the source is already a current-schema store: upgrading twice, or feeding the output back in |
| `upgrade_destination_exists` | the destination, or a sidecar, exists (also if it appears during the build) |
| `upgrade_destination_invalid` | the destination crosses a symbolic link or has no directory |
| `upgrade_incompatible` | the target would drop a source table or column (never dropped silently) |
| `upgrade_failed` | a verification step failed, the source changed or an active sidecar appeared during the run, or the output could not be published |

## Installing and recovering

The output is a plain database file. Placing it at an instance's database path, starting the service (which enables
WAL after the usual target validation), and setting up the instance's operational-history store and document files
are the owner's reviewed installation steps, outside this tool. Keep the old file untouched: it is the complete
rollback. After installing, the first history publication snapshots Tables with the Domain's new fields; the
database itself, with recorded observations, is covered by ordinary database backups, while `crm_activity_events`
and private conversations stay out of the broad Git exports (see [CRM.md](CRM.md)).

## Evidence and limits

`tests/test_crm_host_upgrade.py` builds a populated synthetic pre-CRM store from the frozen schema fixture
(`tests/fixtures/crm_baseline_schema.json`, generated once from the base commit and itself checked against the
fingerprint) and proves: the source hash is unchanged; the report names exactly the expected new and rebuilt tables
and added columns; every baseline row and counter is carried; triggers and immutability work afterwards; a Table is
admitted by the widened sources in the new store and refused in the old one; the result equals a freshly created
store; a dry run writes nothing; and every refusal above, including an existing destination, a second run, an
upgraded store used as a source, an unknown or edited schema, an active WAL or hot journal, symlinks, a failed build
and a destination that appears mid-run, leaves no output. It also proves the final-check ordering: a source that is
modified, touched or given an active sidecar (`-wal`, `-shm`, `-journal`) while the copy is built, a failed engine
validation, a failed link and a failed directory flush all leave the source byte-identical and **no** output (a failed
flush removes only the file this run created and never a replacement another actor put at the path).

The suite now uses the integrated Domain's real DDL and widened sources. A separate subprocess invokes
`python -m bazarlyk.migration.crm_upgrade` for dry-run, successful upgrade and existing-output refusal, checking source
hash preservation, target integrity and foreign keys. The in-process tests retain the detailed failure-path coverage.

These checks use disposable synthetic data only. `test_crm_release_upgrade.py` additionally rehearses both frozen
deployed-release compositions, including a retained Content invocation, through the real CLI and rejects a mismatched
extension selection or altered schema. No live database has been upgraded. Ordinary database backup/restore is separately exercised with
CRM membership, discussion and recorded observation bodies in `test_crm_integration_flows.py`.
