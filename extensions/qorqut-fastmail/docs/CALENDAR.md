# Fastmail calendar connection

The extension supplies the `fastmail` CalDAV connection type to the host's generic `calendar_*` operations
(`calendar_calendars_list`, `calendar_events_list`, `calendar_event_get`, `calendar_event_create`, `calendar_event_update`,
`calendar_event_delete`, `calendar_event_respond`). The host keeps the operations, the Google adapter, the native screen, the
OAuth helper and the primary-calendar semantics; it asks this package only for the `fastmail` adapter, through the host's narrow
calendar-provider seam. Without this extension selected, a `fastmail` connection is reported as `provider_unavailable`.

## Connect once

Create a Fastmail [app password](https://www.fastmail.help/hc/en-us/articles/360058752854-App-passwords) with calendar access,
then run (outside Git, on the owner's machine, only as part of an authorized installation configuration):

```sh
python -m qorqut_fastmail.calendar_connect --name business --config /private/calendar.json
```

The helper asks on the terminal for the Fastmail login email, the app password and the owner's actual invitation addresses
(not wildcards or agent addresses), and saves a `fastmail` connection into the same private, mode 0600 `calendar.json` the core
reads (`QORQUT_CALENDAR_CONFIG` overrides its path). It creates no event and prints no credential. The connection talks to
Fastmail's CalDAV service at `https://caldav.fastmail.com`; run `calendar_calendars_list` to discover its calendars. No new
Fastmail user or mailbox is needed for each business address.

```json
{"connections": {"business": {"provider": "fastmail", "username": "owner@example.com", "app_password": "...",
                              "identities": ["owner@example.com", "owner@business.example"]}}}
```

## Behavior

Fastmail tools edit a recurring series as a whole resource (single-occurrence changes are a Google capability). The same owner and
`manage_calendar` grants apply as for Google; the Fastmail mail operations' `manage_fastmail` grant does not confer calendar access.
