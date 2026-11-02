# Move to project

Three registered operations move existing Tasks, Stories and Customer records, with their whole descendant trees, from
Global or one Project into another active Project. Every Task keeps its id, its internal parents and everything attached
to it. Only the owning scope changes. This is not Project nesting, not a conversation merge and not a copy.

| Operation | Kind | Purpose |
|---|---|---|
| `tasks_move_targets` | read | the active Projects a move from this source may go to |
| `tasks_move_to_project_preview` | read | exactly what a move would carry, what it detaches and what blocks it |
| `tasks_move_to_project` | mutation | the atomic move; needs the preview `token` and an `idempotency_key` |

All three take an explicit `source_business_id` (a Project UUID, or `null` for Global). They are available on the root
route `POST /api/root/operations/NAME`, on a Project route (which must name the same Project, otherwise
`validation_error`) and as MCP tools. The destination is always an explicit active Project UUID.

## Authority

Only the installation owner and the exact configured active Ordubashy may call them; ordinary workers, other agents and
other humans are refused whatever the transport. On every call, including a replayed receipt:

- the caller is refreshed and the identity gate is applied again;
- the current reach of each named Project is checked (a Project the caller cannot reach is never disclosed);
- the ordinary policy decision, including per-agent overrides, of `tasks_get` and `tasks_update` in the source and of
  `tasks_create` and `tasks_update` in the destination must currently be `allow`. A denied or Signoff-gated Task tool is
  never bypassed by moving; the failure is `policy_denied` and nothing is written;
- an archived or missing destination fails (`business_archived` / `not_found`) with no effect.

A replay with the same idempotency key returns the saved receipt after re-checking the identity and the destination, and
never executes the move again. The operations are not Signoff-capable: a policy that would stage them is `invalid_policy`.

## What a move does

Preview reads, and the apply recomputes inside the write transaction, the full tree and every affected related row. The
`token` is a SHA-256 over that exact state and the calling principal, so any change after the preview (a new descendant, a
comment, a private message, a link, a Focus card, a new collision, a changed destination) is `revision_conflict`: preview
again. A state that has become incompatible is `move_blocked` with readable `blockers`. Neither writes anything.

| Dependency | Treatment |
|---|---|
| Tasks | same id; `business_id` changed; revision +1; selected roots become top-level (the old parent stays untouched, shown as `previous_parent_id`); archived and completed descendants move too |
| thread messages, follows, tags, snooze wakes, completions | scope only; ids, bodies, authors, times and events unchanged; explicit follows and unfollows preserved |
| relationships | edges internal to the move follow; assignments must remain valid in the destination |
| private owner–Ordubashy conversations | same conversation id, messages and read state; source scope and key follow the Task |
| Focus cards | same card, order and notes; the card's source key and every exact reference to the Task are rewritten; the board sequence advances |
| Project links | links owned by the destination become redundant and are removed; others are retargeted |
| CRM memberships and observations | same entry and event ids; their Task scope follows; the Table stays where it is |
| inbox subscriptions | follow the Task; pending events keep their original scope and stay deliverable only to a recipient who may read the Task where it is now |
| audit | `task_moves` (append-only): move id, Task, both scopes, creation time, previous parent, revisions, actor, time |

The relocation announces nothing: no Task event, notification or change-feed row is produced for it.

## Blockers

Blockers are concrete incompatible data. They are listed in the preview and never silently dropped or worked around.

- `task_id_exists`, `conversation_exists`, `focus_card_exists`, `message_id_exists`, `follow_exists`, `completion_exists`,
  `tag_exists`, `relationship_id_exists`, `crm_record_exists`, `subscription_exists` - the destination already holds the
  same identity or key; nothing is merged;
- `email_binding` - a Task with a non-null email conversation binding (this operation does not move mailbox ownership);
- `fields_invalid` - typed fields that do not validate under the destination's own configuration;
- `crossing_relationship`, `reviewed_relationship`, `external_task_link`, `assignee_unavailable` - a dependency or
  reference that could not be carried without changing its meaning;
- `unsupported_binding` - an active binding this operation does not rebind: rows of any other table that reference
  `tasks` by foreign key (for example Paper's `outreach_handoffs`), Overseer development bindings, pending or approved
  Signoffs, queued or running jobs, active or paused routines and live delegated assignments that name a moved Task;
- `history_too_large`, `relationships_too_large`, `more_blockers` - bounds.

Selection and access problems (missing Task, same or unreachable scope, oversized selection, more than 1000 projects) are
ordinary errors, not blockers.

### What counts as a binding

A binding must name the selected Task itself, in the selected Task's scope. Matching is typed and scoped, never a search
for the id as text:

- recognised typed references: `{type: task, id}`, `{resource_type: tasks, resource_id}`, `{task_id}`,
  `{item_type: task, item_id}` and `task_ids`. Each is paired with its effective scope - the scope it names itself
  (`business_id`, `namespace` such as `business:<UUID>`, or `scope: global`), otherwise the explicit scope it inherits from
  the nearest enclosing object that names one, otherwise the scope of the job, routine, Signoff or assignment that holds it.
  So `{business_id: A, items: [{type: task, id}]}` binds the Task in A even inside a Global job, and a child that names
  another scope overrides the one it inherits. A Signoff's `change_set` is read the same way (`target_namespace`, `arguments`,
  `affected_scope`, `expected_targets` and `changes` resources);
- a delegated assignment also lists bare Task ids in its own scope: `links.tasks`, `scope.source_ids.tasks` and
  `scope.record_ids.tasks` (a Global assignment's reach is its `allowed_business_ids`);
- free text, an id inside a message body, and a typed reference that explicitly names another scope or another Project's
  same-id Task are **not** bindings and never block.

Discovery is complete or it fails. Each table is read up to the bound plus one row; if more active rows exist than the
bound, the preview and the apply fail with `result_too_large` (finish or cancel some first). A move is never granted from
a partial scan. The matching bindings feed the preview token, so one that appears between the preview and the apply is
`revision_conflict`/`move_blocked`, not a silent move.

Historical records are **preserved** and are not blockers: immutable message references and payloads, Signoff notes,
Focus notes, inbox events and deliveries, retained checkpoint findings, incident impacts, terminal jobs and Signoffs.
They keep naming the old scope and stay navigable through the location resolver and the historical reference scope below.

## Old references

`task_moves` also serves a narrow resolver. A read or mutation of a moved Task in its old scope returns `record_moved`
(HTTP 409) with `details.destination {id,name,slug}` and `details.task {type:'task',id,business_id}`, only to a caller who
may read the Task where it is now; anyone else gets the ordinary `not_found`. A mutation never writes into the new scope.
Global is a real scope: a Global Task that moved to Project A and then to Project B resolves, from Global, to B. Resolution
is bounded, follows successive moves, never prefers history over a current exact-scope row and gives up on an ambiguous
identity (a reused bare id) instead of guessing.

### Historical reference scope

A note's references are stored as `{type, id}` relative to the scope the note was written in, and a note never changes. When
the Task that holds a note (or the Task the note names) moves, the display - never the payload - resolves each reference:

1. `thread_messages.reference_namespace` is a nullable pin. `NULL` means the message never needed one. The first guarded
   relocation of its Task fills it with the exact old namespace (`global` or `business:<UUID>`); a later move keeps it. A
   message written after a move starts `NULL` and is pinned by the next move, so successive moves and notes written between
   moves each read in the scope they were written in. Only the armed relocation may write it, and only that exact value;
   bodies, `refs_json`, event associations, authors and times are unchanged.
2. A reference is first resolved in its written scope (the pin, else the reader's own scope). A record at that exact scope
   always wins, so a same-id Task in the destination is never mistaken for the original target.
3. For a Task that is no longer there, the audited resolver names where it is now, and only a caller who may read it there
   sees its label and place. Otherwise the reference is `{unavailable: true}` and discloses nothing (no access, an
   ambiguous identity or a record that never existed look the same).

A readable Task reference (and the other scope-qualified kinds - topics, materials and tables) always carries the place it
resolved in: `business_id` (an explicit `null` is Global), `business_slug` and `business_name`. A client therefore opens the
exact record and never reconstructs the scope, even when holder and target moved together and the reference is now in the
reader's own scope. Any other kind carries them only when it resolved outside the reader's own scope, and an unavailable
reference carries none. This applies to the Task and Action discussion readers, to the non-strict Signoff and Focus note
projections and to CRM note-derived discovery. Strict validation and every write or mutation lookup never redirect: they
resolve in the caller's current scope and a moved target is refused there. There is no conversation merge and no Project
nesting.

CRM discovery has two streams that follow a record. The Domain's membership developments follow every stay of the record,
each within its time bounds. The host's linked activity (`crm_activity_list` with `include_linked`) opens the Tasks, Signoffs,
Actions and documents the record's own comments cite, and reads each comment's references in the scope it was written in: the
pinned namespace once the record has moved, else the record's current scope. A cited Task is the exact Task of that scope while
one is there, so a same-id Task in the destination is never substituted; otherwise it is the place the audited resolver names,
but only when the caller may read it there. Other kinds keep their origin scope. Every discovered source is still opened with the
caller's own current authority on every page (one the caller cannot read is skipped without a trace), direct links and
relationships keep their current-scope meaning, and the page cursor and its fingerprint are unchanged.

## Guard

The immutable-scope triggers still refuse every write that changes a Task's owning scope, a discussion entry's scope, a
snooze wake, a CRM membership's record scope or an observation's scope. They now consult one SQLite callback,
`relocation_permits(old, new, task_id)`, registered on every connection with a default answer of "no". Only
`UnitOfWork.relocating(source, destination, ids)` inside the prepared, authorized transaction arms it, for exactly those
ids and that pair of scopes, and it is cleared (and foreign-key deferral reset) in `finally`, so a pooled next unit
never inherits it. Other identity columns (ids, authors, creation times, message bodies, event associations) stay
immutable during a relocation.

## Operating notes

- Install: the store needs the offline upgrade in [CRM-UPGRADE.md](CRM-UPGRADE.md) (new `task_moves` table, the nullable
  `thread_messages.reference_namespace` column and five rewritten triggers). The service does not migrate it.
- A new column or table that names a Task must be classified in `bazarlyk/domains/tasks/relocation.py`
  (`TASK_COLUMNS`); `tests/test_task_move_inventory.py` fails otherwise, and an unclassified foreign key to `tasks` with
  rows already blocks a move at run time.
- Operations-side behavior and tests: `qorqut-operations/docs/EXTRACTION.md` (Task relocation) and
  `tests/test_task_relocation.py`.
