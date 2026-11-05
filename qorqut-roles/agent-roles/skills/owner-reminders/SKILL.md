---
name: bazarlyk-role-owner-reminders
description: Schedule or manage a reminder for the owner when they say "remind me", "remind me to", "remind me about", or ask to be reminded later.
---

# Remind the owner

Use `owner_reminder` when the owner asks to be reminded. The service sends the
reminder automatically at the chosen time, even when your session is offline.
No delivery setup or agent wake-up is needed.

- Schedule: `action: "schedule"`, `text`, `at` (ISO date/time with timezone offset).
- List your reminders: `action: "list"`.
- Change a waiting reminder: `action: "update"`, its `reminder_id` and `revision`
  from the list, plus the replacement `text` and `at`. Use the returned new ID.
- Cancel: `action: "cancel"`, `reminder_id`, `revision`.

Use the normal operation idempotency key; retain it when retrying the same call.
Write the message as one short, clear action, at most 240 characters: "Pay the
accountant today." Include just enough context to understand it later.
Resolve relative times from the current time and the owner's known timezone.
Ask only if the intended time is missing or ambiguous. Confirm the actual time
returned by the tool; do not promise a reminder after an error.

This tool schedules one occurrence. Do not claim a recurring reminder was set,
or invent recurring follow-ups. Reminder requests are not agent self-reminders,
tasks, Signoffs, or permission to do the action being remembered.
