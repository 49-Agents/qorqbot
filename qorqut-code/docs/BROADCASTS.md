# Broadcasts and reminders

Broadcasts are durable, scoped fan-out records managed by Qorqut core. They
are separate from generic scripted Routines: creating a broadcast saves one
record and its delivery history; it does not create a script, task, or model
session. No task is created automatically when a delivery arrives.

## Operations

Use `broadcast_recipients` to discover active recipient UUIDs. With business
scope it returns that business; with authorized Global scope,
`all_businesses: true` includes authorized businesses and Global recipients.
Set `role_id` to filter by role. Select the returned IDs; names are not stable
identifiers and discovery does not expand scope.
All agents can set self-reminders. Learning Applier remains
unavailable as a recipient of other agents' broadcasts; its self-reminders
do not grant other agents permission to contact it.

Create with `broadcasts_create` and a `definition` containing `name`, `delivery`
(`inbox` or `input`), `message`, `recipients`, and `timing`. For example, this
sends once at an exact timezone-aware instant:

```json
{
  "definition": {
    "name": "Review progress",
    "delivery": "inbox",
    "message": "Please review the current progress.",
    "recipients": ["<agent-uuid>"],
    "timing": {"kind": "once", "at": "2030-01-02T12:00:00Z"}
  }
}
```

Supply a stable `idempotency_key` for a creation or mutation and reuse it after
an uncertain HTTP/MCP response. For immediate delivery, add `send_now: true`
beside `definition`; the dispatcher queues it without a confirmation step.

An elapsed interval repeats from its anchor and uses no calendar timezone:

```json
"timing": {"kind": "interval", "every": 15, "unit": "minutes",
            "at": "2030-01-02T12:00:00Z"}
```

The interval `unit` is `minutes`, `hours`, or `days`. Without `at`, the first
delivery occurs one interval after creation. Supply `at` to choose the first
delivery explicitly. A calendar schedule uses
cron syntax and an IANA timezone, for example:

```json
"timing": {"kind": "cron", "schedule": "30 9 * * 1-5",
            "timezone": "America/New_York"}
```

Add `max_occurrences: N` to pause after N scheduled rounds. It limits scheduled
rounds, while `broadcasts_send_now` records a manual occurrence separately and
does not advance the scheduled occurrence count or interval. Omit the limit
for an indefinite reminder. A delayed recurring reminder catches up once; it
does not replay every missed interval.

Use `broadcasts_list` and `broadcasts_get` for saved definitions and per
recipient delivery results. The sender, an authorized business owner, or
Ordubashy, or the owning business's business agent may use `broadcasts_update`, `broadcasts_pause`,
`broadcasts_resume`, and `broadcasts_cancel` within current scope. Mutations
against an existing record require its current `revision` as
`expected_revision`; stale revisions fail rather than overwrite newer work.
Cancelled records cannot be updated or resumed. A paused record can be resumed
unless its occurrence limit has been reached. A completed one-time reminder
needs a new delivery date before it can resume.

## Delivery and permissions

Inbox delivery identifies the author of the saved instruction and creates a
durable inbox event. Editing reattributes future deliveries to the editor;
already claimed deliveries retain their original author and text. The original
creator remains recorded and can still manage the reminder. The
recipient watcher checks immediately and every five minutes, so an offline
recipient receives it when the agent next checks; this does not guarantee
execution. The independent dispatcher checks due broadcasts every two seconds.

Input delivery pastes text followed by Enter to an existing recipient
terminal. The owning host or container broker pastes first, waits 200 ms for
client paste handling, then sends a separate Enter to the pane. Ctrl+Enter is
not used: it leaves text unsubmitted in the supported Codex client. Deploy
the companion dashboard broker with this core change. Only the installation owner and Ordubashy can select Input. It never
starts a terminal or agent and adds no confirmation step. Input results are
reported per recipient as `delivered`, `unavailable`, `uncertain`, `failed`, or
`denied`; an uncertain terminal write must not be blindly resent. Inbox results
are also recorded per recipient. Permission, scope, recipient activity, and
sender policy are checked again at delivery time, so revocation can produce a
`denied` result after a broadcast was saved.

Broadcasts and reminders do not grant permissions, assign tasks, approve
outreach, or wake model sessions. Migration 057 is required for deployment;
this document and source changes do not perform a live migration or delivery.
