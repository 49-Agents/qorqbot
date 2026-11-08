# qorqut-google-calendar

Optional Google Calendar provider for a Qorqbot host. The host core keeps the calendar screen, the shared `calendar_*`
operations, their permissions and validation, and the common provider helpers; it has no Google import, credential or network
call. Everything Google-specific lives here and is switched on only by selecting the extension in the trusted installation
configuration. This directory is ordinary source inside the host repository, not a submodule and not a dependency of the core.

- `qorqut_google_calendar.extension:create` is the factory selected through the host's `qorqut.extensions` mechanism
  (extension id `google_calendar`). It takes an empty configuration, reads no credential, contacts no Google service and starts
  nothing; it contributes one `CalendarProvider("google", GoogleCalendar)`.
- The adapter keeps the established `google` connection type: one connection is the account's primary Google calendar, with
  provider-native recurrence, reminders, invitations and responses, optional Google Meet links, stable creation identities and
  revision (`etag`) checks on every change.
- Account settings (OAuth client, refresh token, email) stay in each connection of the host's private calendar configuration.
  `python -m qorqut_google_calendar.connect` is the one-time local helper that adds a connection to it.

See [docs/CALENDAR.md](docs/CALENDAR.md) for installing, selecting and connecting.

## What it does not do

It owns no database table, worker, job or schema and adds nothing to the host's history. It does not read Gmail, share
calendars between accounts, keep a local copy of events or choose which events belong to which account. Without the
extension selected, a `google` connection is reported by `calendar_calendars_list` as `provider_unavailable`; there is no
fallback inside the core.

## Checks

```sh
python -m pytest tests          # from this directory, with the host and its Operations engine importable
```

The tests use a synthetic OAuth and Calendar transport and disposable data; the host-composed cases take the host's own test
helpers from `QORQUT_HOST_TESTS` or, by default, `../../app/tests`. No network, Google account or credential is used.
