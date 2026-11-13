# Google Calendar connection

The extension supplies the `google` connection type to the host's generic `calendar_*` operations (`calendar_calendars_list`,
`calendar_events_list`, `calendar_event_get`, `calendar_event_create`, `calendar_event_update`, `calendar_event_delete`,
`calendar_event_respond`). The host keeps the operations, their permissions, the validation and the native screen; it asks this
package only for the `google` adapter, through the host's calendar-provider seam. Without the extension selected, a `google`
connection is reported as `provider_unavailable`.

## Install and select

Prepare the host with the extension's wheel (`tools/prepare-local.py --with-google-calendar`, see the host's installation
guide). That installs the package into the host environment and nothing else: no account is connected, no service is configured
and no credential is created. Then name the factory in the trusted installation configuration (the `extensions` list that is
passed to the initializer as `--extensions-config`):

```json
{"extensions": [{"factory": "qorqut_google_calendar.extension:create", "config": {}}]}
```

The configuration is empty: connections belong to the private calendar configuration below, never to the extension setting, and
credentials placed in the extension configuration are refused.

## Connect once

Enable the Google Calendar API in a Google Cloud project, configure its OAuth consent screen, and download an OAuth client of
type **Desktop app**. Allow the Google account you want to connect to authorize that application. The helper uses the normal
[installed application OAuth flow](https://developers.google.com/identity/protocols/oauth2/native-app), with PKCE and a loopback
callback. It requests calendar event editing and calendar metadata reading, not Gmail access.

Run it outside Git, on the owner's machine, only as part of an authorized installation configuration:

```sh
python -m qorqut_google_calendar.connect --name main \
  --google-client /private/google-desktop-client.json \
  --config /private/calendar.json
```

Authorize the account in the browser. The helper stores the actual primary calendar identity and the refresh token in
`calendar.json` (mode 0600, written atomically) without printing secrets, and creates no event. Run it on the same computer as
the browser, or forward its loopback port with SSH first (`ssh -L 8765:127.0.0.1:8765 <calendar-host>`; `--port` changes the
port). Securely place the resulting file at the host's configured calendar path (`<workspace>/config/calendar.json`, or the path
in `QORQUT_CALENDAR_CONFIG`). The file is read on each tool call, so credential changes need no restart. A Google OAuth app left
in external Testing mode can have expiring refresh tokens; use the appropriate consent publication status for continuing use.

The connection is an entry of the `connections` object, keyed by the name you chose:

```json
{
  "connections": {
    "main": {
      "provider": "google", "email": "owner@example.com",
      "client_id": "...", "client_secret": "...", "refresh_token": "..."
    }
  }
}
```

## Behavior

- A connection is the account's **primary** calendar: `calendar_calendars_list` returns one calendar with `id` `primary`, and any
  other `calendar_id` is refused.
- Creation uses the native request key as the Google event ID, with a stored request fingerprint, so a retried request with the
  same key and payload returns the same event, and a different payload under that key is an `idempotency_conflict`.
- Edits, deletes and responses carry the event's `etag` as `revision` (`If-Match`); a stale revision is a `revision_conflict`.
  Fields that are not supplied are preserved, including provider fields this adapter does not model.
- Invitations, changes, cancellations and RSVPs are delivered by Google (`sendUpdates=all`). A response is made as the connected
  account's own address; invitations use the connected account as organizer.
- `google_meet: true` creates a Google Meet conference when the event has none; conference creation can briefly stay pending, so
  reread for its link. Recurrence is Google-native iCalendar lines; a recurring series is edited through its master event, and
  single occurrences through their own event IDs.
- Provider failures are mapped to the host's calendar error codes (`calendar_auth_required`, `forbidden`, `not_found`,
  `revision_conflict`, `provider_unavailable`, `calendar_provider_error`). A failure is not proof that a timed-out change did not
  happen: read the event before sending a new request.

The same rules apply as for any provider (reads accept `manage_calendar` or `read_global_context`, changes need `manage_calendar`); nothing here widens calendar access.
