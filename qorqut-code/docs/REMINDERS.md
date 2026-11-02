# Shared reminders

One transport-independent reminder service in the host (`app/bazarlyk/reminders/`) serves the installation owner, the
configured Ordubashy and other registered agents. It replaces the Telegram-backed `owner_reminder` store. The service owns the
schema, permissions, recurrence, snooze, dismissal and the bounded due pass; it never sends anything itself. Delivery is the
separate `ReminderDelivery` adapter (`reminders/delivery.py`), which writes the existing agent inbox event or the owner's
Telegram outbox row inside the same transaction.

## Operations

All are ordinary operations (MCP, worker and the owner HTTP `POST /root/operations/<name>`; business routes use the existing
scoped dispatch). Writes take the standard idempotency key.

| Operation | Arguments | Result |
| --- | --- | --- |
| `reminders_create` | `text` (1-240), `recipient_id` (default caller), `at` **or** `recurrence {schedule, timezone}`, `time_critical`, `linked_item {type,id,business_id}` | `{reminder}` |
| `reminders_get` | `reminder_id` | `{reminder}` |
| `reminders_list` | `recipient_id`, `all_recipients`, `all_scopes`, `states[]` (scheduled/due/dismissed/cancelled), `cursor`, `limit` ≤ 100 | `{reminders, next_cursor}` |
| `reminders_update` | `reminder_id`, `revision`, any of `text`, `recipient_id`, `at`, `recurrence`, `time_critical`, `linked_item` | `{reminder}` |
| `reminders_snooze` | `reminder_id`, `revision`, `at` (future, with offset) | `{reminder}` |
| `reminders_dismiss` | `reminder_id`, `revision`, `occurrence_id` (the current due one) | `{reminder}` |
| `reminders_cancel` | `reminder_id`, `revision` | `{reminder}` |

The view carries `reminder_id, revision, text, recipient_id/recipient_name, creator_id/creator_name, business_id, at, state,
recurrence, time_critical, linked_item, link_available, occurrence_id, due_at, next_at, created_at, updated_at`. `at` is the due
time while due, otherwise the next scheduled time. `occurrence_id`/`due_at` are set only while `state` is `due`.

## Who may do what

* The **installation owner** and the **configured Ordubashy** manage every reminder and may name any valid recipient.
* **Any other registered agent** creates, reads, edits, snoozes, dismisses and cancels only reminders addressed to itself,
  whoever created them. It may not name another recipient, list `all_recipients`, or read another agent's reminder.
* Humans other than the owner, system principals and delegated identities cannot use the operations at all. The engine's
  normal tool policy applies on top (`reminders_*` follow the existing `owner_reminder` grant until a role names them; an
  explicit deny on either wins). Generic routines are not granted anything.
* A **valid recipient** is the active owner, or an active agent that can act in the reminder's scope (a Global reminder needs
  an active Global agent; a project reminder needs an agent allowed in that project).
* A `linked_item` must be readable by both the person making the change and the recipient when it is set. A read shows the
  reference and its label only to a viewer who can read the item right now; otherwise `linked_item` is null and
  `link_available` is false, so an unreadable reference is never presented as authorized.
* **Provenance and delivery authority.** `creator_id` is the immutable origin. A separate `authority_principal_id` (initially
  the creator; the creator for an imported legacy row) records who last authorized the schedule or recipient: a real
  schedule, recurrence or recipient edit, or a snooze, sets it to the authenticated actor. A text, link or `time_critical`
  edit leaves it alone and creates no new occurrence. `creator_id` is never relabelled, and readdressing a reminder gives the
  original creator no authority over the new recipient. This is internal provenance: no operation argument or result field
  carries it.
* **Delivery gate.** An occurrence is deliverable only while all of these hold: it is still the reminder's current due
  occurrence, the recipient is still a valid recipient, the authorizing principal is still the owner, Ordubashy, or the
  recipient agent itself and `reminders_create` is not denied for it, and the saved linked item (if any) is readable by both the
  authorizing principal and the recipient through the existing item resolver (failing closed when it cannot be resolved).
  `current_occurrence` (publish and Telegram admission) and `visible_occurrence` (reading the inbox event) apply the same rule.
  When it fails the due reminder and its pending delivery are kept, nothing is sent and nothing is dismissed or retried, and
  delivery resumes if access is restored; `visible_occurrence` returns nothing rather than a notice with the link removed.
  The owner can still see and cancel the reminder.
* **Replay.** A saved result of a reminder write is returned again only while the caller is still permitted the operation, still
  reaches the saved reminder (an agent no longer reaches one readdressed away from it) and can still read the linked item the
  saved view carried; otherwise the replay is refused. The same holds for the legacy `owner_reminder` replay, which also
  rechecks the owner/Ordubashy identity.

## Lifecycle

* A reminder is one row with a stable `reminder_id`, an optimistic `revision` and a durable `next_at` boundary. Every
  occurrence is an immutable row with its own `occurrence_id` and at most one delivery receipt.
* **One-off** (`at`, with offset, in the future) fires once. **Recurring** (`recurrence`) uses the existing Routines calendar:
  `background.schedule` parsing, `ZoneInfo` and `next_slot`, and the same latest-slot and repeated-wall-time (fold) rules as
  `RoutineScheduler.due`. There is no second cron parser. The first boundary is the first future slot at creation or edit.
* One card is kept per repeating reminder: if a new slot arrives while the previous occurrence is still due, the older one is
  superseded. After downtime only the **latest** eligible occurrence is created; older slots are skipped.
* **Snooze** defers to a future time, suppresses repeats before it, issues a new occurrence at that time and then resumes the
  original calendar after it. **Dismiss** clears the current occurrence only (a repeating reminder stays enabled; a one-off is
  finished) and never completes a linked task. **Cancel** stops the whole series. Reading, opening or acknowledging a notice
  never dismisses it.
* An edit that changes the time, recurrence or recipient discards the pending delivery (the old occurrence is closed and a new
  boundary is set; a recipient change on a due reminder re-delivers to the new recipient). A text, link or `time_critical` edit
  does not create a duplicate occurrence. A null `recurrence` makes a repeating reminder one-off and needs `at`; a stray `at`
  next to a recurrence is ignored. Use snooze, not `at`, to defer a repeating reminder. An update that sends only `text`,
  `time_critical` or `linked_item` keeps the active due occurrence; a client should omit unchanged schedule fields, because
  a time sent with an update is validated as a real reschedule and must be in the future.
* `revision` also changes when a reminder becomes due, so a stale tile gets `revision_conflict` instead of dismissing a newer
  repeat; dismiss additionally needs the current `occurrence_id`.

## The due pass

`ReminderService.reconcile(uow)` is called by the inbox reconcile hook (about every 3 seconds, in its write transaction, after
the Task snoozes). Idle cost is two indexed probes (`reminders_due`, `reminder_occurrences_pending`). A pass is bounded to 20
due boundaries, 5 publishes per recipient, 25 publishes and about 1.5 seconds. Each reminder and each publish runs in a
savepoint, so one failing row is deferred (10 minutes for a due boundary, 30 seconds doubling to 15 minutes for delivery) and
never aborts the others or the rest of the hook. Fair per-recipient batching keeps one unconfigured recipient from starving
the rest. `delivery.publish(uow, payload)` returning `None` keeps the occurrence pending (for example owner Telegram is not
configured); a receipt (`{kind:'inbox',event_id}` or `{kind:'telegram',outbox_id}`) is stored once and never replaced.

Known limit: the Routines slot search walks minute by minute, so a very sparse calendar (for example a yearly date) costs up
to a second or two of CPU under the writer when it is created, edited or fires. Results are memoized per minute.

## Legacy compatibility

`owner_reminder` (schedule/list/update/cancel) and `owner_reminders_list` keep their arguments and plain results but are thin
wrappers: the target is always the installation owner, mutation needs the owner or the configured Ordubashy, and an update keeps
its `reminder_id`. Each call is also authorized as the matching shared operation (`reminders_create`, `_list`, `_update`,
`_cancel`), so an explicit deny there is not bypassed; with no explicit grant that operation follows the `owner_reminder` grant. The Project inventory and reader project owner reminders from the new table. A legacy Telegram outbox row
that was imported is cancelled at admission; one that was never imported keeps the old creator recheck and sends once.

## Offline upgrade

The new tables change the composed target schema, so an existing store must be upgraded offline (normal startup never does
DDL). `python -m bazarlyk.reminders.upgrade inspect|apply --instance-root ROOT` recognises the exact predecessor schema, creates
the two tables, advances the stored target identity and imports only still-queued legacy owner reminders (same id, revision,
text, time, scope and creator, who is also the initial authorizing principal; the owner is the recipient), cancelling each
imported outbox row in the same transaction. Sent, failed and cancelled history is left alone and nothing is replayed.
In-flight (`dispatching`/`uncertain`) or malformed legacy rows refuse the upgrade until resolved through the existing Telegram
delivery resolution. It is idempotent and rolls back as a whole on any failure. See `reminders/upgrade.py` for the exact checks.
