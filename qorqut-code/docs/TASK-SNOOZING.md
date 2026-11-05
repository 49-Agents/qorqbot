# Shared task snoozing

Deferral is metadata, not a status or priority system. It applies identically to
business and Global tasks, using existing task read/write permissions. It is
shared by all assignees. Status, due date, dependencies, ownership, notes and
scope remain unchanged.

Use `tasks_update` with the current `expected_revision`:

```json
{"task_id":"follow-up", "changes":{"snooze":{"until":"2030-01-02T09:00:00+03:00"}}}
```

`snooze: {"indefinite": true}` means until further notice; `snooze: null` explicitly
unsnoozes. `tasks_create` accepts the same field. Until must be a future timestamp
with a timezone and is normalized to UTC. Empty/contradictory snoozes and naive
dates are rejected. Existing revision, policy, signoff, batching and exact-retry
checks apply; there are no separate snooze tools or grants.

Reads return `snooze` (saved metadata or null) and `snoozed` (effective right now).
`tasks_list` defaults to `snooze_filter: "active"`; choose `"snoozed"` or `"all"` to
review deferred work. Closed/archived/status/assignee filters remain independent.
Active means not snoozed, not a replacement for `include_closed`. Filter choices
are bound into pagination cursors. Pagination is not a snapshot: tasks can expire
or be edited between pages. Compact MCP receipts retain both snooze fields.
Replayed write receipts are saved results, not fresh time observations; use
`tasks_get` for current effective state.

At `until <= now`, an unfinished, unarchived task appears in active reads again.
No scheduler, job, background mutation, reminder or agent wake is created. Closed
and archived tasks are never reopened/unarchived by expiry. Stored timestamps can
remain after expiry; unrelated edits and already-prepared approvals still work.
Explicitly setting a new snooze requires a future time.

Bootstrap task attention, queued-work briefings and owner counts exclude snoozed
work. Memory evidence remains available and labels snoozing; timed expiry changes
its observation token without rewriting the source. Snoozing does not dismiss
historical evidence or inbox events. Dependency resolution and unfinished-work
safety checks (including deactivation/founding handoff) still include deferred work.

`/api/me/tasks` includes deferred business records for dashboard Active/Snoozed/All
views. `/api/biz/<slug>/tasks` defaults to active; `?all=1` includes snoozed/closed
records, and `snooze_filter` can be explicit. Neither inherits Global tasks. Global
UI uses scoped typed operations. Dates display in the user's local timezone.

Migration 047 adds two columns and extends task capture triggers, preserving
records and history. Backups/exports include the columns; new Story task snapshots
include snooze metadata. Deploy core/migration before the dashboard and role.
Nothing here restarts agents; existing sessions may need refreshed tool metadata
and instructions.

Signoff snoozing is unchanged: owner-controlled notification deferral, not task
deferral, signoff approval, a state change or a new agent permission.
