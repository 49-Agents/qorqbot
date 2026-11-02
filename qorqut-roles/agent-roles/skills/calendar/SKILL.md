---
name: bazarlyk-role-calendar
description: Use the owner's native Qorqut Calendar - list calendars, read, create, change or delete events, respond to invitations - and get an owner Signoff before any change that notifies guests.
---

# Calendar

Use the native `calendar_*` tools in Global: `calendar_calendars_list`, `calendar_events_list`,
`calendar_event_get`, `calendar_event_create`, `calendar_event_update`, `calendar_event_delete`
and `calendar_event_respond`. Changes go straight to the provider. This skill grants no tool.

## Everyday use

- Personal events (errands, appointments, time blocks; no invitation) go on the calendar and account
  the owner selected; use another only if the owner asks. Ask which one only when none is selected,
  then reuse the answer. Use only connections `calendar_calendars_list` returns. If none is
  connected, say so; connecting a provider follows that connector's own instructions, and you invent
  no connection.
- Promise only what the connection and the tools report: do not assume secondary or shared
  calendars, video links or other provider features.
- Read the current event and its revision before an edit; a `series_id` covers the whole series.
  Reuse the idempotency key after a lost response, and read the event again after an uncertain
  result before another attempt.
- Reminders belong on the event. Invent no scheduling hours, buffers or conflict rules.
- A private change (no guests, nobody notified) may run when the owner asks for it.
- The provider delivers invitations, updates and RSVPs. Do not send a second invitation by email;
  which sender or inbox email uses is Email Management's concern.

## Signoff before guest-notifying changes

Get the owner's Signoff first for: creating an event with guests, adding or removing guests,
changing or cancelling an event that has guests, and `calendar_event_respond` (accept, decline,
tentative). Reading an invitation accepts nothing.

1. Create an owner-input request with `signoffs_input_create` (see owner-input) with
   `form.response_policy: owner_only`, `form.selection_mode: single` and choices `approve` and
   `decline`. The explanation names the connection and calendar, event title, exact time and timezone, the guests, what they will receive (invitation, update, cancellation or RSVP) and the
   event revision.
2. Read the saved response with `signoffs_get`. Act only when it selected `approve` and its notes ask
   for no change. The request executes nothing; you make the call afterward, exactly as approved.
3. Changed details or a new revision need a new request. A declined or unanswered one is not acted on.

This is an instruction to you. The calendar tools do not enforce it, so do not call them first. Only an
explicit owner instruction waives it, recorded in Email Management's local `instructions.md`; never
infer one.
