# Signoff notification tick: read first

`SignoffNotifications.tick()` runs about every 15 seconds from the Signoff worker. A write unit (`BEGIN IMMEDIATE`, operation
`signoffs_notification_tick`) takes the single SQLite writer lock, so an idle installation must not open one when no owner has
anything due.

Each run therefore first asks a read-only question, `_may_have_due_work(current, eastern)`, in one `database.read()` unit. Only when the
answer is yes does it enter the authoritative transaction. Once that transaction
is entered, the maintenance check (`ensure_writes_open`), owner selection, visibility and management filtering, per-owner
contexts, delivery and its dismissals, deduplication keys (`delivery_key`, a hash of owner and signoff and due time or owner and
date), the `INSERT OR IGNORE`, the schedule upsert, the hourly `next_notify_at` advance and the operation tag all run exactly as
before.

A tick therefore does not always run every one of those steps:

* **Preflight says no.** The tick returns after the read unit. It opens no write unit and does not call `ensure_writes_open`, so it
  does not reach the maintenance write gate. While the maintenance state is not open, an idle tick does not raise the
  maintenance refusal.
* **Preflight says yes.** The tick enters the write unit, which begins with `ensure_writes_open`. While the maintenance
  state is not open that raises the maintenance refusal.

## What the preflight may say yes to

The preflight is conservative: a false positive only enters the authoritative path, a false negative would lose a notification and must
not happen. It therefore consults only stored facts that the authoritative path also consults, and none of the filters that can only narrow
it (visibility, management routing, an owner's scopes):

* **Urgent or blocking alerts.** Any `signoff_attention` row with `notify=1` joined to a signoff that is `pending` and not
  superseded, that is not snoozed (`snoozed_until` empty or not after now) and whose `next_notify_at` is set and not after now.
  Timestamps are compared with the module's own `instant()` parser, never as strings, with the same truthiness tests as the tick.
* **The daily summary.** From 08:00 `America/New_York` (the Eastern clock, so the daylight-saving days keep their instants), any
  owner of the exact selection the tick uses (`ACTIVE_OWNERS_SQL`, shared by both) without a `signoff_notification_schedule`
  row for today's Eastern date, or with an older one. This does not look for ordinary notifications: the tick also records the
  schedule entry when there are none, so an owner without today's entry is work.
* **Anything unreadable.** A timestamp the parser rejects counts as possible work, so the authoritative path sees the same data.

Ordinary (non-urgent) alerts cannot make a tick due by themselves; they are delivered by the summary, which the schedule test covers.

## Persistent false positives: a due alert that no owner can receive

The preflight deliberately does not run visibility, management routing or the owners' scopes (above). A notify-enabled, pending,
unsnoozed urgent or blocking Signoff whose `next_notify_at` is due therefore makes the preflight answer yes even when the
authoritative tick later filters it out for every owner. An example is a management-routed question. The host's managed-input
predicate (`bazarlyk.management.service.MANAGED_INPUT_SQL`: a pending `input_request` question with `management_allowed` response
policy and a `management_routing` row that sends attention to management) is applied by the tick through
`ports.management.managed_input_sql`, so no owner's tick sees it, and no owner can receive it while that routing stands.

Only rows the tick actually delivers enter its `due` set, and only those get the hourly `next_notify_at` advance. A filtered-out
row is never delivered and never advanced, so its `next_notify_at` stays in the past. Every later tick, about every 15 seconds,
repeats the preflight, answers yes and enters the authoritative write transaction without delivering anything for that row. This
continues until relevant state changes: the Signoff stops being pending or is superseded, its notification settings or snooze
change, or the routing, visibility or owner-scope facts change so that some owner can receive it (it is then delivered by the next
tick).

This is a conservative false positive, not a correctness problem. While such a row exists a tick opens the write unit and adds
the preflight's read unit. It is not removed by filtering on permissions in the preflight, by advancing `next_notify_at` for an undelivered item, or by marking
anything delivered: those would risk a false negative or change authority and delivery state. No test in
`tests/test_signoff_idle_tick.py` exercises this filtered-out case; the description follows from `notifications.py`.

## Timing of newly arriving work

The decision is taken from the clock and state read at the start of the tick. An alert created just after an empty preflight is
handled on the next tick. There is no wake-up, queue or cache.

## Tests

`tests/test_signoff_idle_tick.py` counts the write units of the real service: ticks for which the preflight finds no due candidate
open none; the first 08:00 Eastern tick of each date opens one; urgent and blocking alerts are delivered at once, then hourly; a snooze keeps the tick idle until
it expires; muted, answered and superseded alerts stay idle; work created after an empty tick is delivered by the next one; four
concurrent ticks deliver a due alert once; unreadable timestamps count as work; and a differential case forces the original path
at every 20-minute instant across the 2026 and 2027 daylight-saving changes and fails if the original path changed anything while
the preflight said no.

```
python -m pytest -q tests/test_signoff_idle_tick.py tests/test_signoffs.py::test_owner_attention_is_durable_and_follows_the_eastern_summary_and_snooze_rules
```
