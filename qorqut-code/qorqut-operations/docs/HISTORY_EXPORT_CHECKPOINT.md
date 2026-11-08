# Operational history: row exports and the snapshot engine

Package `qorqut_operations.history_exports`. It holds both halves of the operational history: the trusted row-export library
(`ExportCatalog` and its contributions) and the generic read-only snapshot engine that versions the exported rows and the
authored areas in a private Git repository ([Snapshot engine](#snapshot-engine) below). The host decides where things are and
when to run; it keeps the installation's area resolution, cross-feature export catalog and lifecycle wiring
(`qorqut.operations_history.composition`).

The package is a library. Importing it or constructing anything in it opens, writes and commits nothing, loads no plugin and
registers nothing with HTTP/MCP; it is not re-exported from `qorqut_operations`, `storage`, the schema, the assembly or any
dependency manifest. The export half (`ExportCatalog`) works only through an existing read-only `Database` unit, runs no Git
command and writes no file.

## What a domain declares (all frozen values built by trusted assembly code)

| Value | Meaning |
|---|---|
| `HistoryContribution(owner, exports, classifications)` | One domain's decisions. The owner is unique in a catalog and a contribution names at least one table. |
| `TableExport(table, columns, key, structured, selector, shard_hex)` | An exported table: exact scalar columns with closed types, the stable ordered key, structured projections, an optional trusted row selector, and optional hash-prefix sharding (0 to 3 hex digits of the SHA-256 of the canonical key). |
| `TableClassification(table, kind, reason, canonical)` | A table that is not exported: `represented` (`canonical` is the exported table that is canonical), `technical_excluded`, `private_excluded`, or `separate_history` (`canonical` names the history mechanism). A reason is required; the excluded value is never copied. |
| `StructuredField(schema, project)` / `declared_json(schema)` | A selected JSON/container column. The projector receives the raw stored value and must return exactly the declared closed `Obj`/`Arr`; there is no default projector. `declared_json` is an optional helper that parses JSON text and keeps only declared properties at any depth. |
| `Text`, `Integer`, `Real`, `Boolean`, `Obj`, `Arr` | The only value types. Scalars are plain `str`/`int`/finite `float`/`bool` (SQLite 0/1 accepted for `Boolean`); `Obj` has exactly its declared properties; `Arr` is bounded; `nullable` is explicit; `choices`/`max_length` optional. Bytes and every other container are rejected. Nesting is limited to four levels. |
| `ExportCatalog(contributions)` | The frozen catalog. Construction rejects non-contribution values, duplicate owners, a table defined twice (in exports or classifications, within or across contributions), invalid names, keys that are not declared non-null text/integer columns, a missing/non-callable projector or selector and a `represented` table whose canonical table is not exported. |

Dynamic names never reach SQL: table and column names must match `^[a-z][a-z0-9_]{0,63}$` and are quoted after validation.

## `ExportCatalog.export(uow)`

1. The unit must be an existing read-only unit (`uow.writable is False`) of an Operations `Database`; a writable unit or any
   other object is `history_export_read_only`. The schema identity (target name, version, digest) comes from the unit's
   assembly. A selector that tries to write fails inside SQLite's `query_only` unit (`history_export_failed`) and leaves the
   database unchanged.
2. **Coverage reflection.** Real tables are listed from `sqlite_master` (type `table`, not `sqlite_*`) by name only, including
   tables created by direct SQL that have no repository specification. Reflection never selects a value. A classified or
   exported table that does not exist, or a declared column/key the table lacks (`PRAGMA table_xinfo`), is a
   `HistoryCompositionError`. A real table no contribution names is **missing**: it is listed, `coverage_complete` is false,
   and nothing is dumped.
3. **Rows.** Each exported table is read once: through its selector, or with `SELECT "t"."a", "t"."b" ... FROM "t"` over the
   declared source columns (never `SELECT *`). The selector is trusted source code and is passed `(uow, RowRequest)`;
   `RowRequest.select(uow, where, parameters)` runs the same explicit quoted column list with the selector's own filter or
   join, so a table that mixes ordinary item discussion with a private direct channel is exported only by a selector that
   filters it. Selected rows must hold exactly the declared source columns; extra, missing, wrongly typed, null (when not
   nullable), opaque (dict/list/bytes) values and duplicate stable keys fail with `history_export_invalid`. Projector output is
   checked against its closed schema. Errors name only the table, field and Python type, never a value.
4. Rows are sorted by the stable key; the result is immutable (`HistoryExport`) with `format`, `version`, `schema`, the
   per-table `ExportedTable` (rows, key, shard digits, `shard_of(row)`), `missing`, `coverage_complete`, the manifest and
   `manifest_bytes()` (canonical JSON). The manifest lists, per exported table, owner, row count, scalar column types,
   structured schemas, key, sharding, whether a selector filters it and the **names** of table columns that are not exported;
   per excluded table its classification, owner, reason and canonical owner; and the missing tables. No timestamp, path or
   process-dependent value enters it, so identical state yields identical bytes.

All work happens inside the supplied unit, so every table and the host's area selection come from one snapshot; the unit's
owner closes it before any file or Git work.

## Snapshot engine

`History(database, *, locations, resolve_areas, safe_segment, catalog, areas, clock, git_runner=None, git_timeout=300)` has
explicit `initialize()`, read-only `validate()`, `snapshot()` and `status()`; nothing runs on construction. It moved here
unchanged in behavior from the host: the same layout and refs (`refs/heads/history` in a private bare repository), the same
`history-status.json` / `history-files.json` / `history.lock` files beside it, the same capture filters, limits, private
exclusions, error codes and `HistoryObserver` behavior (the observer is the coalescing, rate-limited background trigger; the
host binds it to committed changes and owns its start and stop).

The engine never imports a path object. The host supplies:

| Argument | Contract |
|---|---|
| `locations()` | Returns `(operations_root, history_repository)`: absolute paths, the repository directly inside a runtime directory below the root. Raises `DomainError("history_unavailable")` for a location the host refuses. It is called again on every use, so the host's own path validation (symlinks, traversal) runs each time; nothing is cached. |
| `resolve_areas(areas)` | Receives the validated, label-sorted area selection and returns one `ResolvedArea` per selected area, in order: `ResolvedArea.located(area, parts)` (path components relative to the operations root; normalized exclusions are applied) or `ResolvedArea.refused(area)` for an area whose existing path the host refused (reported as an excluded symlink and never read). The engine then refuses any overlap between resolved folders. |
| `safe_segment(value)` | The host's existing rule for one registered ID or slug (truthy when it is one safe path segment). The engine defines no naming rule of its own. |
| `catalog`, `areas(uow)`, `clock()` | The trusted `ExportCatalog`, the provider of typed areas (`GlobalFiles`, `GlobalAgentFiles`, `BusinessAgentFiles`, `BusinessHeadquarters`, each with optional `excluded` generated subtrees) called inside the export's read unit, and the attempt clock. |

Descriptor and no-follow checks stay inside the engine: the operations root, runtime directory and repository are opened with
`O_NOFOLLOW` descriptors and re-checked before every Git command, so a runtime location replaced while in use is refused. A
history failure never rolls back, repeats or approves accepted Operations work.

## Not here

Final production table classifications and export definitions (they belong to the owning domains once the target schemas
settle; no mass export of the v1 schema is attempted), engine-table classifications, scheduling, the installation's path and
area resolution, the private-DM policy answer (this library has no DM knowledge; the selector is where a domain keeps that
exclusion), and any wiring into application startup or the change publisher.

## Checks

`tests/test_history_snapshot.py` (engine: rows and authored areas, boundaries and private exclusions, status, Git, concurrency),
`tests/test_history_adapters.py` (the host-callable contract and import independence), `tests/test_history_cadence.py`,
`tests/test_history_observer.py` and `tests/test_history_packing.py` use `tests/history_snapshot_support.py`: a synthetic
layout and adapters, a real temporary target store and a recording Git runner, never a host.

`tests/test_history_exports.py` uses a synthetic domain and temporary target stores. It covers selective, closed-schema export
(safe identity/lifecycle fields, a declared nested projection that drops a nested secret, a filtered ordinary-discussion table
that omits the private channel, secret and opaque tables excluded), one consistent snapshot while another connection commits,
deterministic bytes, unclassified tables reported as missing and never selected, invalid declarations, writable-unit and
mutating-selector rejection, and projector leaks with no value in the error. `tests/test_packaging.py` applies its static
no-host/no-lazy-import guards to this package too.

## Left to the host and owning domains

Per-domain table/column/structured-field definitions and classifications for every composed table (Tasks, Actions, tags,
relationships, revisions, item threads, principals/businesses/profiles, inbox, Signoffs, routines/jobs, other retained records),
each reviewed for secrets and private payloads; the private-DM history inclusion answer; engine-table classifications;
host-side wiring that constructs the catalog beside the composed assembly.
