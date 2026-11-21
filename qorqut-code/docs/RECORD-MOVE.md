# Move tables and notes to another project

Whole Tables (general or CRM) and project notes move between projects the same way Tasks do ([TASK-MOVE.md](TASK-MOVE.md)):
a read-only preview that lists what travels and what blocks, then one all-or-nothing transaction bound to that exact
preview. Ids never change. Nothing is copied into a second record, recreated or merged.

| Operation | Effect | |
|---|---|---|
| `tables_move_to_project_preview` | read | what moving these Tables would carry and what blocks it |
| `tables_move_to_project` | records | the move, bound to the preview token |
| `notes_move_to_project_preview` | read | the same for project notes |
| `notes_move_to_project` | records | the move, bound to the preview token |

Destinations are the ones `tasks_move_targets` lists. The dashboard offers **Move to project…** in a project table's
header and in a project note's menu.

## Authority

The same as a Task move: the installation owner or the exact configured active Ordubashy, current reach of both projects,
and an `allow` decision of `tables_get` + `tables_update` (notes: `notes_get` + `notes_update`) in the source and
`tables_create` + `tables_update` (notes: `notes_create` + `notes_update`) in the destination. A Signoff-gated or denied
tool is never bypassed. Global tables and Global notes do not move.

## What a table move carries

| Stored with the Table | Treatment |
|---|---|
| `data_tables` row | moves; revision +1 |
| `data_rows`, `table_versions` | move unchanged. A history capture keeps the path it was committed under, so earlier revisions stay readable; the next revision is captured under the destination |
| `crm_entries` | move; each membership keeps naming its Task wherever that Task is |
| Table discussion (`thread_messages`), follows, inbox subscriptions | move; the first move pins a message's `reference_namespace` to the scope it was written in |
| The uploaded file the rows came from (`data_upload`) | moves with the table (see notes below) when no table staying behind uses it |
| Project links naming the Table | retargeted (dropped when the destination itself owns the link) |
| Private owner–Ordubashy conversation, Focus cards, Signoff input links | follow (host adapter `qorqut/domains/tables/relocation.py`) |
| Relationships | see below |

## What a note move carries

Only `project_owned_note` documents move on their own. The owner's journal, Context documents, email messages and
assets do not.

- The `documents` row changes project (`project_id`, `namespace`); id, kind, relative path, content and metadata are
  unchanged.
- The file is **copied** to the same relative path in the destination project's folder inside the transaction, so a
  rollback leaves the source untouched (a stray copy is recognised by its content and reused by the next attempt). The
  source file is retired by a sweep once the move is committed: only when the recorded move is the document's latest, no
  row names the old path and the source still holds exactly the moved content. Every move preview sweeps first.
- Earlier versions in `qdv_document_versions` are copied to the destination namespace with their version numbers,
  authors and times. The originals are immutable and stay where they were written.

## Relationships

An edge whose every other end moves too, is an agent, or already sits in the destination (a Task, Table or note moved
there earlier, so the edge reunites them) travels. Any other edge stays in the source project as history and is still
read from both ends: a reader that finds an end missing follows `task_moves`, `table_moves` or `document_moves` to where it
is now. A travelling edge with Context review decisions stays behind; for a workflow role (`assigned_to`, `blocked_by`)
that blocks the move instead.

## Blockers

| Code | Meaning |
|---|---|
| `history_pending` | the table's latest change is not yet saved to its Git history |
| `upload_in_use` | a table staying behind uses the same upload |
| `unsupported_binding` | pending approvals, queued or running jobs, active routines or delegated assignments name the table |
| `document_changed` | a note's file differs from its record (edited outside the service) |
| `path_exists` | the destination already has a document, or a different file, at the note's path |
| `unsupported_document` | not a project note |
| `*_id_exists`, `history_exists`, `conversation_exists`, `focus_card_exists` | the destination already holds the same id |
| `reviewed_relationship` | see Relationships |

## Guard and audit

`table_moves` and `document_moves` are append-only (immutable, retained) and are the only input of
`qorqut_operations.storage.record_moves.moved_location`. The two scope guards that refuse a Table's discussion entry or
CRM membership changing project accept exactly the Tables an armed move names (`relocation_permits(..., 'table:<id>')`).

## Install

The store needs the `record_moves` transform of the ordered offline upgrade (`python -m qorqut.migration.upgrade`): it
adds the two audits and replaces the two guards. Reverting restores both guards and drops the audits, and requires explicit
permission to discard recorded moves. Tests: `app/tests/test_table_move.py`, `app/tests/test_note_move.py`,
`app/tests/test_task_move_history.py`, `app/tests/test_migration_upgrade.py`.
