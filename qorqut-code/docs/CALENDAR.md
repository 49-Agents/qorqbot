# Calendar tools

Ordubashy and the installation owner can list calendars, list/get events,
create/update/delete events and accept/decline/tentatively respond to invitations.
Changes go straight to the connected provider. Core ships no provider adapter: each connection type is supplied by an
optional extension, Google Calendar by `qorqut-google-calendar` (`extensions/google-calendar/` in this repository) and Fastmail
CalDAV by `qorqut-fastmail`. A connection whose extension is not selected is reported as `provider_unavailable`. Which account
an event belongs to is the owner's choice of connection and calendar. Apple Calendar and Qorqut display the same provider
events. There is no local event database, background sync worker, scheduling policy, conflict checker or additional approval
workflow.

## Connect accounts once

On v1.2, source commands run from `qorqut-code/app/` with the composed host's
dependencies installed. The native calendar domain remains in the host and uses
the extracted Operations engine's registry, contracts and invocation pipeline.
There is no change to the Operations engine or its schema.

Install the core `service` dependencies. Keep connection
credentials outside Git in `<workspace>/config/calendar.json`, mode 0600. The
service can override this path with `QORQUT_CALENDAR_CONFIG`. The file is read on
each tool call, so credential changes do not require a calendar worker restart.
Only run connection setup as part of an authorized installation configuration.

Core saves a connection with the provider-neutral `qorqut.calendar.connect.save_connection`; each provider package supplies
its own one-time setup helper that calls it. Neither provider is part of the core, and a connection works only while its
extension is selected.

### Google (optional extension)

The optional `qorqut-google-calendar` package (`qorqut_google_calendar.extension:create`, selected through the host's extensions
setting; prepared with `tools/prepare-local.py --with-google-calendar`) supplies the `google` connection type to these same
`calendar_*` operations. Enable the Google Calendar API in a Google Cloud project, configure its OAuth consent screen, download
an OAuth client of type **Desktop app**, then run its helper outside Git:

```sh
python -m qorqut_google_calendar.connect --name main \
  --google-client /private/google-desktop-client.json \
  --config /private/calendar.json
```

The helper uses the installed-application OAuth flow with PKCE and a loopback callback, requests calendar event editing and
calendar metadata reading (not Gmail access), and stores the actual primary calendar identity and refresh token without printing
secrets. The OAuth setup details, the selection entry and the adapter's behavior are in the package's
[docs/CALENDAR.md](../extensions/google-calendar/docs/CALENDAR.md).

### Fastmail (optional extension)

Fastmail CalDAV is not part of the core. The optional `qorqut-fastmail` package (selected through the host's extensions
setting) supplies the `fastmail` connection type to these same `calendar_*` operations, and its `icalendar` dependency; its setup
helper (`python -m qorqut_fastmail.calendar_connect`) and the connection details are in that repository's `docs/CALENDAR.md`.

Without an extension selected, its connections in `calendar.json` are reported by `calendar_calendars_list` as
`provider_unavailable`, and the other providers' connections keep working unchanged.

The configuration contains a `connections` object keyed by the names you choose (each entry applies only with its provider's
extension):

```json
{
  "connections": {
    "main": {
      "provider": "google", "email": "owner@example.com",
      "client_id": "...", "client_secret": "...", "refresh_token": "..."
    },
    "work": {
      "provider": "fastmail", "username": "owner@example.com",
      "app_password": "...", "identities": ["owner@example.com", "owner@work.example"]
    }
  }
}
```

## Use the tools

All operations are Global. The owner has the calendar capability; the configured
ecosystem Ordubashy additionally needs `manage_calendar` in its profile and the
companion Ordubashy 1.57.1 role's tool grants. Other roles and delegated workers
do not gain private calendar access from email permissions. Publish/adopt the
role, add the one capability and refresh preparation through supported APIs
after the matching core release. Source changes alone do not activate grants.

| Tool | Inputs |
| --- | --- |
| `calendar_calendars_list` | None; returns connection/calendar IDs, provider and organizer choices, plus connection errors. |
| `calendar_events_list` | `connection_id`, `calendar_id`, `start`, `end`; optional `limit`, `cursor`. |
| `calendar_event_get` | Connection/calendar IDs and `event_id`; returns current `revision`. |
| `calendar_event_create` | Connection/calendar IDs, `title`, `start`, `end`; optional organizer, attendees, location, description, timezone, recurrence, reminders, Google Meet. |
| `calendar_event_update` | Event target, current `revision`, and only the supplied `changes`. |
| `calendar_event_delete` | Event target and current `revision`. |
| `calendar_event_respond` | Event target, current `revision`, your `attendee` address and accepted/declined/tentative `response`. |

Mutations use the normal native `idempotency_key`. Reuse it with the exact payload
after a lost response. Creation uses a stable provider ID to prevent duplicate
events. Reads return provider revisions; reread after a revision conflict. A
provider error is not proof that a timed-out change failed; inspect the current
event before forming another request. No extra Signoff is created by these tools.

Times are ISO date-times with an offset. All-day events use ISO dates with an
exclusive end date. Timed recurring events also name an IANA timezone. Recurrence
is provider-native iCalendar lines, for example `["RRULE:FREQ=WEEKLY;COUNT=8"]`.
Use `series_id` to get/edit/delete a whole recurring series. Google occurrence IDs
also support single-occurrence changes (with the Google extension); Fastmail tools edit the series resource.

`reminders: [10, 60]` adds popup alarms that calendar clients deliver; `[]` removes
them. These are calendar alarms, separate from the shared `reminders_*`
operations ([shared reminders](REMINDERS.md)). `google_meet: true` creates a Meet conference on a **Google** event;
conference creation can briefly remain pending, so reread for its link. Fastmail
events can use a supplied meeting link in `location`.

Invitations, changes, cancellations and RSVPs are delivered by the provider.
Do not send a second email invitation. Reading a pending invitation does not
accept it. The role asks the owner before a new acceptance or independent commitment,
but acts directly on the owner's explicit requests. Already accepted provider updates
appear on the next read; no new scheduler or automatic invitation watcher exists.

## Apple Calendar and Qorqut

Add the connected Google and Fastmail CalDAV accounts to Apple Calendar and enable
their calendars. Use account synchronization, not a read-only ICS subscription.
Qorqut's **Global → Calendar** page displays both calendars on a time grid with
Day, 3 days and Week views, plus basic event and invitation controls.
Refresh to read changes made in Apple or the
provider apps; Qorqut has no separate event copy to reconcile.

Before declaring the installation connected, verify each connected account can be read,
then use an explicitly agreed real test appointment to check the organizer, guest delivery,
recurrence, alerts and Apple edits. Fixture tests cover
API behavior, not live account authorization or external delivery.

Focused checks: `app/tests/test_calendar_tools.py` (the provider seam and the private-calendar boundary, with an in-memory
adapter and no provider package installed), `app/tests/test_calendar_statuses.py` (personal status store and route), the
companion dashboard's `tests/test_calendar_browser.js` and, for the Google adapter, its connection helper and its selection,
`extensions/google-calendar/tests`. They use an empty temporary v1.2 instance,
the composed Operations/Context engines and fake calendar providers; no live
configuration, service workers or agent sessions are started. Set
`QORQUT_TEST_ROLE_ROOT` to the paired role source for these checks.
No migration. Merge, release and profile adoption
remain independent owner-authorized steps.

## Personal event status

The owner can label an event in the Focus pane or Calendar drawer as `pending`
(the default, including events already in the past), `completed`, `cancelled`
or `rescheduled`. The label is a private dashboard note. It never writes to
Google or Fastmail, notifies guests, changes an RSVP, deletes an event or moves
its dates, and the provider stays authoritative for the appointment. Edit,
Delete and invitation response keep their provider behavior, and provider-
cancelled events are filtered as before.

A label belongs to `connection_id` + `calendar_id` + `event_id` +
`occurrence_start`. The last is empty for a nonrecurring event, so changing its
date keeps its label. A recurring event (one with a `series_id` or non-empty
`recurrence`) is labelled per clicked occurrence: its start as a UTC ISO
millisecond instant, or an ISO date for an all-day occurrence. The server
accepts an empty value, `YYYY-MM-DD` or an offset-aware ISO date-time and stores
it canonically; naive times are refused.

Access is the installation owner's dashboard only: `GET /api/root/calendar-statuses`
returns `{items: [{connection_id, calendar_id, event_id, occurrence_start,
status, revision}]}` and `POST` saves one item. There is no MCP operation or
agent grant, and any `Authorization` header is refused. The owner comes from the
authenticated installation, never from the body or URL. A `POST` carries exactly
the target, `status`, `expected_revision` (0 for a first label) and a stable
`request_id` and returns `{item}`. A stale revision fails with
`revision_conflict` (409, with `current_revision`) and never overwrites a newer
edit. Retrying the same `request_id` with the same content replays the saved
item; different content with that ID is `idempotency_conflict`. Only each item's
latest request is kept, so replaying an older request after a later change
conflicts. Responses are `Cache-Control: private, no-store`.

Labels are stored in
`operations/.runtime/dashboard-preferences/<sha256(owner id)>.calendar-statuses.json`,
mode 0600 beside the other dashboard preferences. They are outside the
operational database, schema, operational Git history, Context and provider data.
Saves hold a per-owner cross-process lock, atomically replace the file and flush
it and its directory. The store holds at most 10,000 items: new entries are then
refused with `validation_error`, updates still work and nothing is evicted. A
missing file is an empty set. An unreadable, unsafe or corrupt file is an
explicit `operation_failed` error and is never treated as empty or overwritten;
repair or restore it deliberately.
If flushing the directory fails after a save was swapped in, the retryable error
says the save may have succeeded; reload, or retry with the same `request_id`,
which replays the saved item rather than applying it twice.
