# Work groups: ordinary parent Tasks

A work group is an ordinary Task with child Tasks. The host composes this on the Operations
engine's parent/child Task support and its relationships contract. There is no separate Story
kind, table, alias, redirect or compatibility route.

## The model

* A **work group is an ordinary Task.** Its children are Tasks with `parent_id`. Anything else a group relates to (Materials,
  tables, uploads, assets, other Tasks, Signoffs) is an ordinary `concerns` relationship to the group Task. A
  resource can concern several groups and a group can have any number of resources. Hierarchy is never inferred from a link.
* The parent is completed and reopened with the normal Task operations and permissions. Reopening or snoozing a parent
  or a child changes nothing in the other. Closing is where the two meet, so that no open work is left under a closed
  parent (`qorqut_operations/tasks/lifecycle.py`): an agent cannot close a Task that has open work Tasks directly
  beneath it, cannot put new work under a closed parent, and closes a whole group with
  `tasks_complete {children: "close"}`, which closes every open work Task under the parent in the same write. A person
  closing a Task in the dashboard is not stopped, and Stories, Customers and Email Tasks are never closed along.
* The Task is the one record of who holds a piece of work and where it stands. A Todo board entry is written by the
  engine for each recorded worker that keeps a board, for as long as the Task is open and unarchived
  (`agents/attention.py::follow_task`, run from `domains/tasks/closure.py` inside every Task write); `todo_add` and
  `todo_remove` record or clear the worker. A Task's Focus card shows the Task's `status_line`, and a `reason` given
  for a Task card is written to that line. `records_health` lists what still disagrees and what
  `records_reconcile` would repair; `records_reconcile` (owner or coordinator, once after an upgrade) applies it.
* A thread message is for the owner or it is a working note (`qorqut_operations/discussion/threads.py`). The audience
  is derived from what is stored, with no extra column: the owner's own messages, owner relays, the closing note of
  `tasks_complete`, an agent's first note after the owner's own message, and notes an agent posts with
  `for_owner: true` (at most 600 characters, no record identifiers; the marker is kept in the message's
  attribution). Everything else, system entries included, is a working note. `tasks_get`, `tasks_thread_get` and
  `tasks_notes_list` take `audience: "owner"` and report how many notes were left out; the dashboard thread asks
  for the owner's part by default and offers "Show all". Agents read everything by default.
* Group text is the Task's own fields and its thread. `tasks_get` returns `parent_id`, `parent {id,title,status}` and
  `children`.

## Consumers

| Consumer | Contract |
| --- | --- |
| Notes, context upload, filing notes, assets, tables, data uploads | optional `task_ids` (array of existing Task ids of the same business, max 200); each becomes a `concerns` relationship from the new item to the Task in the same plan (`domains/items/associations.py::link_new_resource`) |
| Email conversations, reply routing, platform submission | resolve Tasks |
| Focus / attention | source types are `task`, `signoff`, `email`; close or reopen a group with `tasks_update` / `tasks_complete` |
| Inbox | `inbox_subscribe {source_type:"tasks", include_related:true}` is the Task-group subscription (below) |
| Business briefing | key `work_groups` (parent Tasks of the open work with `open_children`) |
| Learning sources / background | Task notes are `thread_messages` of `item_type='task'` |
| Change feed | a Task change (including its thread entries and tags) is the `tasks` kind |

Read and write groups with `tasks_list/get/create/update/complete/assign/comment/thread_get/follow`,
`relationships_*` (`concerns`) and `context_items_list/get`. The `stories_*` operations remain in the catalog
only as **retired metadata** (`composition/catalog.py`, no handler) so a published role or a saved override
naming them still parses.

## Related-work subscription

`inbox_subscribe` on a Task with `include_related: true` keeps the sticky Task follow and one `inbox_subscriptions` row.
While the Task is followed the subscriber also receives events about the Task's direct children (`parent_id`) and about items
that share a non-dependency relationship with it in either direction. It is one hop and never recursive; links added later
count because the relationship is read when the event is made; deliveries are optional (never `required`); reads recheck the
subscriber's access. Unfollowing the Task (`tasks_follow` or `inbox_unsubscribe`) stops the related events until it is
followed again. `assigned_to` and `blocked_by` are not group membership. There is no second Task discussion or subscription
writer (`inbox/related.py` only decides who is routed).

Inbox subscription rows: a followed related-work Task shows `include_related: true`, `managed_by: "thread_follow"` and its
removable subscription `id`; a retained row whose follow is off shows `managed_by: "related_work"`.

## Context filing

A Task is `tasks` (`parent_id`, `fields_json`, `status`) with its labels in `item_tags`/`tags` and its discussion in
`thread_messages`; group membership is `relationships` rows with role `concerns` (from the resource, to the Task).
`documents.snapshot_for_job` hands the filer `tasks`/`more_tasks` and accepts `task_ids` per note. The registered programs
`context.file` and `platform.dispatch` are version 3, so jobs queued under older digests are refused and must be re-queued.

## Focused checks

`app/tests/test_host_parent_tasks.py` (composed host + the Operations package): parent/child Tasks with a multiply linked note
and table; scoped `task_ids`; parent complete/reopen without propagation; Focus presentation of a group Task; the retained
Signoff applying the exact Task-backed plan (stale-hash refusal, once-only apply); the related-work subscription; the installed
catalog has no `stories_*` handler. Also re-run: `test_host_neutral_items`, `test_host_foundation_schema`,
`test_host_foundation_policy`, `test_host_operational_contracts`, `test_host_operational_records` and
`test_host_retained_operations`.
