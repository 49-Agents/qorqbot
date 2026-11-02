# qorqut-business-docs-versioning

Shared document revision history and append-only business activity for Qorqut.

A small standard-library Python package (Python 3.12+) that stores, through a
SQLite connection the host already owns:

1. **Document history** — immutable full snapshots of text documents, with
   history listing, on-demand diffs and restore-as-new-version.
2. **Business activity** — an append-only, business-scoped log of facts such as
   "task completed" or "document updated".

The two record types share one package but are separate tables and APIs. An
event records an action that already succeeded; it is not a command, work
queue, approval or replayable business model. The package is a library, not a
plugin, service or adapter. Version 0.2.0 includes the trusted import call used
by the Qorqut integration (the host and qorqut-context consume it through exact
Git commit pins).

## Install and test

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
.venv/bin/python examples/document_and_task_history.py
```

There are no runtime dependencies; `pytest` is the only test extra. The test
suite uses temporary databases only. `tests/test_install.py` builds the wheel
from a copy of the source, installs it into a fresh virtual environment without
network access, and uses it from a directory outside the checkout (skipped if
`setuptools` is not importable in the test interpreter).

## Schema initialization and ownership

```python
import sqlite3
from qorqut_business_docs_versioning import HistoryStore, initialize_schema

connection = sqlite3.connect(path, isolation_level=None)
connection.execute("BEGIN IMMEDIATE")
initialize_schema(connection)
connection.commit()
store = HistoryStore(connection)
```

- The host owns the connection. The package never opens another database,
  closes the connection, or changes `row_factory`, `isolation_level`,
  `autocommit`, PRAGMAs or `user_version`. It reads through its own cursors, so
  any caller `row_factory` is fine. The connection's `text_factory` must stay the
  default `str`.
- Importing the package or constructing `HistoryStore` touches no database.
  `initialize_schema` must be called explicitly, inside the caller's
  transaction. It is repeatable (`IF NOT EXISTS`), does not use `executescript`,
  and only creates `qbdv_`-prefixed objects:
  - `qbdv_document_versions` (primary key `namespace, document_id, version`);
  - `qbdv_business_events` (integer `sequence` primary key, unique index
    `qbdv_business_events_identity` on `business_id, event_id`, index
    `qbdv_business_events_business_sequence`);
  - `BEFORE UPDATE` / `BEFORE DELETE` triggers on both tables that abort.
- There is no mutable document head; the latest version is the highest version.
- The triggers make history immutable through normal SQL `UPDATE`/`DELETE` and
  the API has no edit/delete calls. This does not protect against a database
  owner who drops triggers or changes the schema.

## Transactions, concurrency and retries

- Every schema and mutation call (`initialize_schema`, `save_document`,
  `restore_document`, `import_document_version`, `append_event`) requires an active caller-owned
  transaction (`connection.in_transaction`), otherwise it raises
  `TransactionRequired`. Reads need no transaction or write lock.
- The package never commits or rolls back. The host commits its own domain
  update and the history entries together, or rolls them back together.
- Concurrent writers should start with `BEGIN IMMEDIATE`, so the version check
  and insert run under SQLite's write lock. SQLite locking errors
  (`sqlite3.OperationalError: database is locked`) and constraint errors
  propagate unchanged; the package preserves their original base and labels, with no automatic retry.
- Documents use optimistic concurrency: `expected_version` must equal the
  current version (0 before the first save). A stale value raises
  `VersionConflict` with `expected_version` and `current_version`; newer content
  is never replaced.
- There is no document idempotency subsystem. Every successful save is a new
  version, even with identical content. If a host is unsure whether a save
  committed, retrying with the same `expected_version` fails closed with
  `VersionConflict`; the host reconciles through its own operation receipts
  (or by reading the latest version) and can skip redundant saves itself.
- Events are idempotent by `(business_id, event_id)`, described below.
- All SQL values are bound parameters.

## Namespaces versus authorization

`namespace`, `document_id`, `business_id`, `author_id` and `actor_id` are opaque
nonblank strings chosen by the host. Namespaces such as `business:<uuid>` and
`global` are caller conventions; the package does not route on them. Every
document call is scoped by both namespace and document ID, and every event call
by business ID; nothing reads across those scopes.

A namespace is not a permission. `author_id`/`actor_id` are recorded as given,
not authenticated. The host must decide, before calling the package, whether
the actor may change that document or complete that task, and obtain any owner
confirmation or signoff its policy requires.

## Document API

`HistoryStore(connection)`:

| Call | Result |
|---|---|
| `save_document(namespace, document_id, content, author_id, *, expected_version, reason="")` | Appends a full snapshot as the next version; returns `DocumentVersion`. |
| `get_document(namespace, document_id, *, version=None)` | Latest or selected `DocumentVersion`; `NotFound` if absent. |
| `list_versions(namespace, document_id, *, before_version=None, limit=50)` | `VersionPage` of metadata, newest first. |
| `diff_document(namespace, document_id, from_version, to_version)` | Unified diff string. |
| `restore_document(namespace, document_id, source_version, author_id, *, expected_version, reason="")` | Appends the content of `source_version` as the next version; returns `DocumentVersion`. |
| `import_document_version(namespace, document_id, version, content, author_id, *, created_at, reason="", restored_from_version=None)` | Trusted maintenance import of an existing version with its original author and time; see below. |

`DocumentVersion` fields: `namespace`, `document_id`, `version` (1, 2, 3 …
within the namespace/document), `content`, `content_sha256` (hex SHA-256 of the
UTF-8 content), `author_id`, `created_at` (UTC ISO-8601), `reason`,
`restored_from_version` (`None` for ordinary saves). `DocumentVersionInfo` has
the same fields without `content`.

Content must be a `str`; empty text is allowed and text is stored exactly, with
no whitespace, newline or Unicode normalization. `expected_version` is required
and keyword-only; booleans and negative values are rejected. Invalid inputs
raise `TypeError` or `ValueError`.

Full snapshots are stored and diffs are computed on demand from the two exact
saved contents. Lines are split only on `\n`, labels name the namespace,
document and version, and a line without a final newline is followed by
`\ No newline at end of file` so no combined lines appear. Equal contents give
an empty string.

Restore never rewinds or overwrites history: it appends the old content as a
new version with `restored_from_version` set, using the same `expected_version`
rule as save.

There is no document-kind registry, required structure, metadata bag or title
lifecycle; consumers own those.

### Importing existing history (trusted maintenance)

`import_document_version(namespace, document_id, version, content, author_id, *,
created_at, reason="", restored_from_version=None)` copies an already existing
version from a host's older history store, for example during a schema
migration. It is meant for trusted host maintenance code, not for ordinary
edits, which keep using `save_document`/`restore_document` with the current
clock and `expected_version`.

- It requires the caller's active transaction, like every other mutation.
- It preserves the supplied content, author, reason, version number and
  `restored_from_version`. `created_at` must be an ISO-8601 timestamp with a
  timezone and is stored normalized to UTC; the package invents neither an
  author nor a date.
- `version` must be the next contiguous version (1 for an empty document). A
  gap raises `ImportConflict` with `existing=None`.
- Importing a version that is already stored with identical content, author,
  normalized `created_at`, reason and restore provenance returns the stored
  record unchanged, so a retried import is a no-op. Any difference raises
  `ImportConflict` with the stored record in `existing`. History is never
  replaced or renumbered.
- `restored_from_version`, when given, must be an earlier version.

## Activity API

| Call | Result |
|---|---|
| `append_event(business_id, event_id, kind, actor_id, summary, *, subject_type, subject_id, occurred_at, data=None)` | Records a fact, or returns the identical earlier record; returns `BusinessEvent`. |
| `get_event(business_id, event_id)` | `BusinessEvent`; `NotFound` if absent in that business. |
| `list_events(business_id, *, before_sequence=None, limit=50)` | `EventPage`, newest appended first. |

`BusinessEvent` fields: `sequence`, `business_id`, `event_id`, `kind`,
`actor_id`, `summary`, `subject_type`, `subject_id`, `data` (dict),
`occurred_at`, `recorded_at`.

- `kind` is free text (for example `task.completed`, `document.updated`); the
  package has no list of kinds. Callers decide which successful actions count.
- `data` is an optional JSON object (default `{}`). Non-JSON values, tuples,
  sets, non-string keys (at any depth), NaN and Infinity are rejected rather
  than coerced. It is stored as canonical JSON (sorted keys).
- `occurred_at` is the caller's time for the underlying action, an ISO-8601
  string with a timezone (`Z` or an offset). It is normalized to UTC. The
  package never invents it. `recorded_at` is the separate UTC time of the append.
- `sequence` is a database-generated increasing integer that orders **appends**,
  not occurrence. Imported or late facts get a later sequence even if they
  occurred earlier; sort by `occurred_at` yourself when that is what you need.
- `(business_id, event_id)` is unique. Retrying with the same normalized
  contents returns the original event (same `sequence` and `recorded_at`)
  without another row. Key order in `data` and equivalent timezone spellings
  are not differences. Any other difference raises `EventConflict`, whose
  `existing` attribute holds the stored event.

### Corrections

There is no edit or delete. A correction, reversal or task reopening is a new
event (for example `task.reopened`) whose `data` can reference the original
`event_id`. The package does not act on that reference, change the original,
infer current state or undo anything outside the database.

### Why the completion event belongs in the task's transaction

The event says the task *was* completed. Appending it in the same transaction
as the host's task update means the log contains exactly the completions that
committed: if the update rolls back, so does the event, and a committed
completion always has its event. Writing it afterwards, or from a queue, could
record a completion that never happened or miss one that did. The same applies
to `document.updated` alongside a document save.

## Pagination

Both lists return newest first with an exclusive integer continuation:
`VersionPage.next_before_version` / `EventPage.next_before_sequence`. Pass it
back as `before_version` / `before_sequence` to get the next page; it is `None`
when there are no more items. `limit` must be 1–200. A missing document or
business gives an empty page. Pages never include other documents, namespaces
or businesses.

## Example

[`examples/document_and_task_history.py`](examples/document_and_task_history.py)
uses an in-memory database with hypothetical host tables. It saves a project
brief and completes a task, each with its history in the same transaction, then
shows a failed transaction rolling back the host update, brief version and
events together. It is an integration illustration, not an installed Qorqut
adapter.

## Deliberately outside this package

- HTTP server, MCP tools, background processes, filesystem watching, Git
  storage, LLM calls, attachments, rich text, branching/merging.
- Permissions, approval or signoff; the host authorizes before calling.
- ORM, storage adapters or generic schema engines.
- Event sourcing or state rebuilding from events.
- Automatically migrating existing Qorqut histories; hosts decide what to import
  through `import_document_version`. Email, Business Tables and learning
  records keep their own semantics — email send receipts and reconciliation,
  per-mutation table revisions with their Git history, and learning review
  state — which are specific to those features and already have their own
  authoritative storage. They are not re-expressed as generic documents or
  events here; a later consumer may choose to also record activity events.

## Errors

| Exception | When |
|---|---|
| `TransactionRequired` | A schema/mutation call without an active transaction. |
| `VersionConflict` | `expected_version` differs from the current version. |
| `EventConflict` | An event identity is reused with different contents. |
| `ImportConflict` | An imported version leaves a gap or differs from the stored version. |
| `NotFound` (also a `LookupError`) | Missing document, version or event. |
| `TypeError` / `ValueError` | Invalid arguments. |

All five package exceptions derive from `HistoryError`. SQLite errors are not
wrapped.
