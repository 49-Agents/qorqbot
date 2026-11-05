---
name: bazarlyk-role-calendar
description: Use the owner's native Qorqut Calendar - list calendars, read, create, change or delete events, respond to invitations - set up its Google connection when asked ("set up my Qorqut calendar"), and get an owner Signoff before any change that notifies guests.
---

# Calendar

Use the native `calendar_*` tools in Global: `calendar_calendars_list`, `calendar_events_list`,
`calendar_event_get`, `calendar_event_create`, `calendar_event_update`, `calendar_event_delete`
and `calendar_event_respond`. Changes go straight to the provider. This skill grants no tool.

## Everyday use

- Personal events (errands, appointments, time blocks; no invitation) go on the calendar and account
  the owner selected; use another only if the owner asks. Ask which one only when none is selected,
  then reuse the answer. Use only connections `calendar_calendars_list` returns; a Fastmail calendar
  exists only if the optional Fastmail package supplies it.
- The Google connection covers the account's primary calendar. Do not promise secondary or shared
  calendars.
- Read the current event and its revision before an edit; a `series_id` covers the whole series.
  Reuse the idempotency key after a lost response, and read the event again after an uncertain
  result before another attempt.
- Reminders belong on the event. Google events can get a Meet link. Invent no scheduling hours,
  buffers or conflict rules.
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

## Set up the native Google connection

Do this when the owner asks, for example "set up my Qorqut calendar". Qorqut's own Google connection
stays the way to connect Google Calendar. It is an OAuth client plus the owner's one-time refresh
authorization, not an API key, and it asks for calendar access only, not Gmail. Gmail authorization and
Google Calendar authorization are separate unless the runtime explicitly supplies both.

1. **Browser.** Use browser-control with the owner's Google account. The owner does the sign-in,
   verification and consent. If one blocks you, ask through owner-input; never ask for or type a
   password or code.
2. **Google Cloud.** In the current console UI, create or choose a project, enable the Google Calendar
   API, configure the OAuth consent screen and allow the owner's account, then create an OAuth client of
   type **Desktop app** and download its JSON to a private place outside Git and project repositories.
   Ask the owner about the consent publication status: an app left in external Testing can have
   expiring refresh tokens.
3. **Helper.** Run the existing helper on the Qorqut host, from its `app/` directory with the
   installation's dependencies: `python -m bazarlyk.calendar.connect google --name <connection name>
   --google-client <downloaded client JSON> --config <private calendar.json>`. The config is the
   installation's private `calendar.json` (default `<workspace>/config/calendar.json`, mode 0600, or the
   path in `QORQUT_CALENDAR_CONFIG`). The helper serves a loopback callback (port 8765 unless `--port`
   changes it) on the machine that runs the browser, stores the primary calendar identity and refresh
   token without printing them, and creates no event. The owner approves the consent screen it opens.
   If you cannot run it, find the config path or reach that machine, report the exact gap and stop; do not
   invent a path or write credentials by hand.
4. **Verify read access.** `calendar_calendars_list` must show the connection and its primary calendar
   without a connection error, and `calendar_events_list` must read a short window. Create no test event
   unless the owner asks for one, then follow the Signoff rule above. Say what you actually saw before
   calling it connected.

Keep the client file, client secret and tokens out of Tasks, notes, messages, Context and chat.
