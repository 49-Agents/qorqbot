---
name: qorqut-role-owner-reminders
description: Create, change, snooze, dismiss or cancel a one-off or recurring reminder for yourself or, for Ordubashy, the owner or another agent; also handle a due reminder notice. Use when the owner says "remind me", "remind me to", "remind me about", or asks to be reminded later, and when a reminder notice reaches you.
---

# Reminders

One shared reminder service serves the owner, Ordubashy and every other agent through
the `reminders_*` operations. The service fires due reminders automatically, even when
your session is offline. No delivery setup or agent wake-up is needed.

A reminder has a stable `reminder_id` and a `revision`, text, a recipient, a creator, an
optional time-critical flag and an optional linked item. Each due occurrence has its own
`occurrence_id`. States: `scheduled`, `due`, `dismissed`, `cancelled`.

## Who can be reminded

- Omit `recipient_id` to remind yourself. Other agents may only create, read, change,
  snooze, dismiss and cancel reminders addressed to themselves, whoever created them.
- The owner and Ordubashy may manage reminders for any recipient. When the owner asks to
  be reminded, set `recipient_id` to the owner's `id` (the `is_owner` person in
  `assignees_list`). Ordubashy may pass `all_recipients: true` to `reminders_list` to
  see other recipients' reminders.
- The service enforces this on every call. Another agent that needs to remind the owner or
  a colleague asks Ordubashy; do not look for a workaround.
- The owner sees only reminders addressed to the owner, in the Focus and Todo reminder strips
  and behind the Reminders button. Your self-reminders never appear there.

## Operations

- `reminders_create`: `text`, then either `at` (one-off; ISO date/time with timezone
  offset) or `recurrence: {schedule, timezone}` (five-field cron and an IANA timezone,
  e.g. `{"schedule": "0 9 * * 1-5", "timezone": "<owner's timezone>"}`). Optional
  `recipient_id`, `time_critical` (default false) and `linked_item: {type, id,
  business_id}`. A recurring reminder starts at its first future slot; omit `at`. Both
  you and the recipient must be able to read the linked item.
- `reminders_get`: `reminder_id`. Returns the current `revision`, `occurrence_id`, `due_at`
  and `next_at`.
- `reminders_list`: your own by default; optional `states`, `all_scopes`, `cursor` and
  `limit` (at most 100). Cancelled and finished one-off reminders are omitted by default.
- `reminders_update`: `reminder_id`, `revision` and any changed `text`, `recipient_id`,
  `at`, `recurrence`, `time_critical` or `linked_item`. The `reminder_id` stays the same.
  `recurrence: null` makes it a one-off and needs `at`. A text-only edit does not
  notify again; a schedule or recipient change withdraws the pending notice.
- `reminders_snooze`: `reminder_id`, `revision`, `at` (a future time with offset). It
  clears the current due occurrence, suppresses repeats until then, and a recurring
  reminder then resumes its original calendar schedule. Works on a scheduled or due
  reminder, not a cancelled one.
- `reminders_dismiss`: `reminder_id`, `revision`, `occurrence_id` of the current due
  occurrence. It clears only that occurrence; a recurring reminder keeps repeating. It
  never completes or changes the linked item.
- `reminders_cancel`: `reminder_id`, `revision`. It ends the whole reminder or series
  and withdraws any pending notice.

Use the normal operation idempotency key on writes and retain it when retrying the same
call. If a write reports a stale revision or occurrence, read `reminders_get` and
retry only if the change is still right; a newer occurrence may now be due.

Write the text as one short, clear action, at most 240 characters: "Pay the accountant
today." Include just enough context to understand it later. Resolve relative times from
the current time and the owner's known timezone; never assume the host's timezone. Ask
only if the intended time is missing or ambiguous. Confirm the actual time returned by
the tool; do not promise a reminder after an error.

## Recurring reminders

A recurring reminder is one reminder with one card. Each scheduled occurrence notifies
once, even if the previous one is still due. After downtime only the latest missed
occurrence is surfaced, never a backlog. Dismiss handles the current occurrence; cancel
stops the series. Cron here is minute-resolution calendar scheduling with named
timezones, the same as Routines. Do not claim completion-relative or other repeat rules.

## Due reminders addressed to you

A due reminder reaches you as an ordinary inbox event naming its `reminder_id` and
`occurrence_id`. Notification is not a promise of action at the exact time: delivery can
wait for your session, direct inputs and the normal inbox pacing.

- Reading or acknowledging the inbox event does not dismiss the reminder. It stays
  `due` until you explicitly dismiss, snooze or cancel it.
- Dismiss it once its action is captured as tracked work: reuse the matching Task or
  create one. Ordubashy puts an accepted commitment on Todo (`todo_add`) before
  executing it. You do not have to finish the work first. Do not dismiss before the
  capture, or merely to clear the notice. If you cannot capture it now, snooze.
- A reminder is not permission to do the remembered action, and dismissing it never
  completes a task. Reminders addressed to the owner stay in the owner's queue until the
  owner (or, on their request, Ordubashy) dismisses, snoozes or cancels them.
- Ordubashy only: while Focused, ordinary unrelated reminders wait. One linked to the
  focused or current work is announced, and so is one the creator marked
  `time_critical: true`. Set `time_critical` only when the requester says it is urgent
  enough to interrupt unrelated work; a timestamp or an urgent-sounding word alone does
  not. A time-critical notice is only a notice. It does not switch focus, start work or
  authorize anything: keep your current focus, capture it, and change focus deliberately.
  Other agents receive reminders through the normal inbox path. An offline or stopped
  agent keeps its due reminders; none is started to deliver one.

Use a self-reminder to resurface work later, not as a substitute for a Task, Todo
item or Signoff. Never invent recurring follow-ups the requester did not ask for.

## Legacy `owner_reminder` (Ordubashy)

`owner_reminder` still works and always reminds the owner through the same service:
`action: "schedule"` (`text`, `at`), `"list"`, `"update"` (`reminder_id`, `revision`,
replacement `text` and `at`) and `"cancel"` (`reminder_id`, `revision`). An update now
keeps the same `reminder_id`. Prefer `reminders_*` for recurrence, snooze, dismissal or
another recipient.
