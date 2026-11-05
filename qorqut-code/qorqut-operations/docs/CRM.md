# CRM tables (portable domain)

`qorqut_operations.crm` and the `tables` module together hold the portable CRM: a **Table of kind `crm`** whose rows are
*memberships of existing Task records*, a shared **discussion for every Table**, and **append-only recorded observations**
(emails, messages, developments) about a Task. There is no CRM-specific Task, customer store, mailbox, provider call, URL
fetch or second permission engine here. A trusted host composes it (see Composition) and supplies the unified activity page,
the dashboard and every provider.

## Tables: general and CRM

`data_tables` gains two columns. `table_kind` (`general` default, or `crm`; immutable) and `crm_config_json`
(`{stages: [{id, name, archived?}]}`, set exactly when the kind is `crm`). The Table DTO adds `table_kind` and `crm_config`
(`null` for a general table). A general table keeps its SQL contract, rows, `tables_rows_*` behavior and its
`schema_version` 1 snapshot bytes unchanged.

A CRM table holds **no data rows** (`tables_rows_write` is refused; the database also refuses a `data_rows` insert for it).
`tables_rows_list` on a CRM serves its entries with `crm_entries_list` semantics. Columns are `{id, name, type}` plus an
optional `record_field`. Without it the column stores a **local** value on the membership. With it the column is a live,
**read-only projection** of a Task field and can never be written through membership cells. Whitelist: `title`, `task_type`,
`status`, `overview`, `customer_kind`, `customer_state`, `customer_profile.{company,contact_name,email,phone,website,notes}`,
`tags` (json) and `fields.<custom field id>`; the column type must agree with the Task field definition of the Table's scope. A
custom field is rendered under the definition of the **Task's own scope**, read on every request: where that scope has no such
definition the cell is `null`, and where the definition's type is one the column cannot hold (a date field in a text column)
the cell is `null` with `cell_errors: {column: "type_mismatch"}`, however the stored value looks; search sees only what is
shown. A field that does not apply to a Task's type (a Customer field on a Story) renders `null`. A column's binding cannot change after creation. A
stage in use by any membership (removed ones included) cannot be dropped, only archived; an archived stage keeps working for
the entries that hold it but is not assignable. An archived CRM is read-only: schema, stages and entries are frozen until
it is unarchived (comments are still allowed).

## Memberships (`crm_entries`)

A membership is `(scope, table, record)`: `record_task_id` with the exact scope of the Task (`record_business_id`, `NULL` =
Global, and `record_namespace`), a position, an optional stage, local `cells_json`, a soft `removed` flag and a revision.
The Task keeps its own source scope; the membership belongs to the Table's scope. A Task may belong to many CRMs, at most
once actively in each. Removing is soft (the Task, its thread and its history are untouched) and adding the Task again
restores the same entry, position and local values.

| Operation | Arguments | Result |
| --- | --- | --- |
| `crm_entries_list` | `table_id`, `query?`, `stage_id?`, `equals?`, `order_by?`, `descending?`, `include_archived?`, `include_removed?`, `limit` (default 50, max 100), `cursor?` | `{items, next_cursor, table_revision}` |
| `crm_entries_add` | `table_id` + exactly one of `record` (a scoped Task reference) or `create` (tasks_create fields, a Customer unless `task_type` says otherwise), `cells?`, `stage_id?` | `{table, entry, created_record, job_id?}` |
| `crm_entries_update` | `table_id`, `entry_id`, `changes: {cells?, stage_id?}` | `{table, entry, job_id?}` |
| `crm_entries_remove` | `table_id`, `entry_id` | `{table, entry_id, removed: true, job_id?}` |

Writes take the Table's `expected_revision`, bump it once per changing batch and capture one exact snapshot. A write that
changes nothing (re-adding an active member with the same values, removing a removed entry) succeeds without a revision,
a version or an event. Re-adding an active member with different values is a `conflict`; use `crm_entries_update`.
`create` is **one atomic change**: `tasks_create`'s own prepare, policy and apply run inside the Table's plan and
transaction (both permission sets must allow it), so a failure leaves neither the Task nor the entry. Shared fields are
edited on the Task with `tasks_update`; a Task edit never changes the Table's revision.

An entry shows `cells` (local values plus the current projected Task fields, with `cell_errors` for a stored value that no
longer fits its column), `local_cells`, the list-sized public Task `record` and `record_ref`
(`{type: "task", id, business_id}`). Authority is recomputed on every read, in the **Task's own scope** and with the Task's
own policy (`tasks_get`): linking or membership grants nothing. A Task the caller cannot read appears only as
`{id, revision, position, unavailable: true, removed}`: no identity, scope, stage, value or label. Queries, `equals` and
`stage_id` never match such an entry, and sorting is by local columns only (`order_by` on a projected column is refused), with
hidden entries always after readable ones, in position order, so no hidden value decides an order. Editing needs the caller
to read the Task; removing does not. Cursors are bound to the scope, table, table revision, filters and sort.

Approval re-derives everything from current state: scope, both permission sets, the pinned Task and Table revisions, and
that the plan changes only what the request names (a tampered target is `invalid_plan`).

## Table discussion

`thread_messages` / `thread_follows` accept `item_type = 'table'`, so every Table (general or CRM) has the discussion Tasks and
Actions have: `tables_thread_get`, `tables_comment` (typed `note_refs`, `summary`, `relay`, attribution) and `tables_follow`.
The creator and commenters follow by default, an explicit unfollow is sticky, and following a Table follows none of its
members' Tasks. The owner/team channel rule is the existing one: the installation owner's team comments are refused with
`channel_required` (`tables_comment` is **not** in the business Task exemption). An archived Table can still be discussed.
Reads and comments use the Table's own scope and permission. A staged comment's typed references are resolved again, strictly,
when it is approved or applied (a source that became unreadable stops the comment; reading old comments still shows such a
reference as unavailable).

A membership change appends one `system` message whose `change_json` is
`{kind: "crm_membership", entry_id, record: RecordRef, operation: added|updated|removed|restored, changed_fields: [column ids],
stage?: {from, to}}` with a generic body (never a Task title, profile or cell value). Readers resolve `record` under their own
authority: a reader who cannot read the Task gets `{kind, entry_id, operation, unavailable: true}`; others additionally get
`record_label`.

Inbox: a comment is announced once by the discussion engine (`data_tables`/`note_added`, optional delivery to the Table's
followers). Data changes (a membership, a stage, an edit) are announced once by the capture mapping (`crm_entries` and
`data_rows` map to `data_tables`/`rows_changed`, delivered to the Table's followers and subscribers), so one semantic change is
never announced twice. `inbox_subscribe`/`inbox_unsubscribe` for `data_tables` are the same follow state as `tables_follow`, and
every unfollow route (`tables_follow`, `inbox_unsubscribe`) also removes the agent's older explicit `data_tables` subscription
to that Table in that scope in the same transaction, through the one `discussion.follows.set_follow` / `drop_legacy_subscription`
rule; other agents' subscriptions and already delivered events are untouched.

## Recorded observations (`crm_activity_events`)

`crm_activity_record(task_id, kind: email|message|development, summary, body, occurred_at, source, event_key,
corrects_event_id?)` -> `{event, duplicate}` records what an authenticated actor observed. It sends, fetches, verifies and
approves nothing, changes no Task field and is attributed to the recorder. `source` is data only (`kind` email, message,
external or manual; bounded `provider`, `account_key`, `thread_id`, `message_id`, `title`; an `http`/`https` `url` without
credentials, never fetched). `occurred_at` must carry a timezone and not lie in the future. Events are immutable (database
triggers refuse update and delete, including deleting the Task). `event_key` (at most 300 characters) is unique per Task: the
same key with the same content returns the original (`duplicate: true`), different content is a `conflict`. A correction is a
new event naming an earlier one of the same Task. Limits: identifiers 500, URL 4000, title and summary 500, body 20000.
Authority is `crm_activity_record` plus `tasks_comment` in the Task's own scope, and recording is a shared-history write held to
the Task-comment channel rule too: the installation owner's Global write is refused with the same `channel_required`
(`refused_operation: tasks_comment`), while the owner exception for a project Task applies unchanged. The guard runs at
preparation, validation, application and when a saved result is replayed; the event stays attributed to its authenticated
recorder.

## Composition methods for the host

* `CRMOperations.local_activity(ctx, task_id, args, uow)` -> `{items, next_cursor, as_of}`: one newest-first page over the
  record's own discussion (`origin: own`), its recorded events (`recorded`) and the membership developments of CRMs the reader
  can read as well (`crm_membership`: a development appears only while the reader may read its Table and its discussion,
  `tables_get` and `tables_thread_get`, in the Table's own scope, with no label or count for a Table they may not), in the total order `(occurred_at, activity id)`. `limit` (max 100) and `cursor` are
  accepted; the first page fixes an ingestion cutoff `as_of` that the cursor carries, so a later record, even a backdated one,
  never joins an ongoing browse and nothing is skipped or repeated. The Task must be readable now. The host merges linked
  sources (email, tickets) itself and owns `crm_activity_list`. A record may be in any number of CRMs: the development scan runs in
  bounded batches until the page is full or the developments are exhausted, so unreadable Tables never hide a later readable one
  or the continuation of a page (the cost of a page grows with the unreadable developments newer than it).
* `CRMOperations.resolve_record(ctx, ref, uow, builder=None)` -> the full public Task DTO under current Task read policy
  (pinned as an expected target on `builder` when preparing).
* `CRMOperations.field_definitions(ctx, uow)` and `list_entries(...)` are the seams the Table service uses (`bind_crm`).

## Composition

Build `CRMOperations(application, tables, tasks, items, clock=...)` after `TaskOperations` and the frozen `ItemResolver`,
call `tables.bind_crm(crm)` and `crm.register(registry)`, install `storage_contribution()` (owner `crm`) with the
`data_tables` DDL, add `CATALOG_ENTRIES` and `CLASSIFICATIONS`, and put the new operation names in the policy
(`GLOBAL_TABLE_OPERATIONS` already lists them for the Global-table rule). Nothing is discovered by name.

### Permission prerequisites

The host's policy owns the mapping of each new operation to the existing decision it derives from (`crm_entries_list` ←
`tables_rows_list`; add/update/remove and `tables_comment` ← `tables_rows_write`; `tables_thread_get` and `tables_follow` ←
`tables_get`; `crm_activity_list` ← `tasks_get`; `crm_activity_record` ← `tasks_comment`). This package adds no resolver and no
allow path of its own; it keeps the checks only it can make:

* The mutations declare their prerequisite as a `derived_effects` name, so for a **fresh call** the engine resolves it with the
  host's policy and the stricter decision (deny, then Signoff) governs preparation and approval. For `tables_comment` it is a
  permission decision only: the row-write lifecycle is not run, so an archived Table can still be discussed. `tables_follow`
  needs the read decision and grants no right to change anything.
* Declaring `derived_effects` does **not** check them when the engine returns a **saved idempotency receipt**: that path skips the
  plan. Every new mutation therefore has a `replay_validator` that, while the receipt would otherwise be returned, decides again
  the operation's derived effects (plus `tasks_create` for a create-and-add), that the Table (or Task) still exists in its scope,
  and that whatever the saved result shows is still available to the caller: an add/update result needs `tasks_get` on the entry's
  Task in its own scope (a created Task included, an archived project excluded); a comment result needs its typed references
  again; a removal's result names no Task data and needs none. A refused replay never re-executes the mutation.
* A delegated worker's assignment names the Task and Table records it may read. Every embedded Task read (list, search and
  filter, the Table thread's record labels, `resolve_record`, `local_activity`, prepare and approval validation, and a saved
  receipt) is authorized for that exact Task with the same `task_id` selector a direct `tasks_get` carries, per Task and not per
  scope, and every Table source with its `table_id`. A CRM row therefore never opens a Task the assignment refuses: it is the
  bare unavailable placeholder, matches no search or local-value filter, and its development is redacted.
* A create-and-add plan also carries `tasks_create`'s own change, so the real Task-creation decision (and its frozen targets and
  effects) applies to it; adding an existing Task does not need that decision.
* `tables_rows_list` on a CRM is the same read as `crm_entries_list`: the dedicated operation's effective decision (and a
  delegated assignment's allowance of it) is also required, so the generic name cannot open what a deny on the new one closes.
  General Tables keep exactly their previous behaviour.

## History, recovery and exports

Each Table revision has a snapshot in `table_versions`, published to `refs/heads/qorqut-tables`. A CRM's snapshot is
`{schema_version: 2, table_kind: "crm", table, entries}` (memberships with scoped Task references and local cells, in id order,
never a copy of a Task), and a general snapshot is the original `schema_version` 1 document. `crm_entries` is classified
`separate_history` (the table history owns it) and `crm_activity_events` `private_excluded` (recorded message bodies stay out
of broad Git exports; the database backup retains them). A restore of the database restores both tables with the Tasks they
point at; the Git projection is rebuilt from current SQLite state by `TableHistory.project`.

## Known integration points (host-owned)

The `data_tables` DDL changed (two columns, a cross-column check, two guard triggers and the two new keys in its capture trigger), so a host's schema fingerprint and migration must adopt it; the
host maps `tables_thread_get`, `tables_comment`, `tables_follow` and the CRM operations into its catalog, MCP, dashboard and
policy defaults, and composes the unified activity page from `local_activity` plus its linked sources.
