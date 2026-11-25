# Operational timezone

The owner's timezone setting (Settings > Timezone, or Ordubashy's `settings_set` `timezone`) is the one clock of the
installation: calendar, Focus chips, the reminders form, operational time labels and agent-facing time text. Precedence:
the owner's setting, then `QORQUT_TIMEZONE` (an IANA zone such as `Europe/Berlin`), then `UTC`. The owner's value is
stored in `dashboard-preferences/operational-timezone.json` under the runtime folder, applied at startup and at the
moment it is saved, with no restart; code reads it through `local_time.current_zone()`, `current_timezone()` and
`current_label()`. `QORQUT_TIMEZONE` is still read once per process and an invalid value stops startup; an invalid owner
value is refused when saved. Required timezone data must be available in the runtime. The experience workflows use
`QORQUT_EXPERIENCE_TIMEZONE`, then the owner's setting, then `QORQUT_TIMEZONE`, `TZ` and the system zone.

The owner session reports `operational_timezone` and `operational_timezone_source` (`owner`, `environment` or
`default`). When the source is not `owner`, the dashboard saves the browser's zone once, through
`POST /api/root/operational-timezone`, and never again overrides a later choice.

The native launcher records the installation's chosen setting separately from
its replaceable application runtime. A fresh installation may use the system
zone, with UTC as the fallback. Upgrading a legacy installation must preserve its
previous operational zone (`America/Puerto_Rico`) unless a new zone is explicitly
chosen. Do not infer a new timezone from the build machine during an update.

`actions_config_get` returns
the timezone, current local date/hour, UTC server instant and current offset.
Progress APIs also report their timezone. The dashboard consumes these values;
its browser's local zone does not replace the operational setting.

Actions preserve their stored local date, hour, precision, timezone and UTC
instant. Changing the setting changes new records and current reporting windows,
not old records. Action filters and counts use the stored occurrence dates. The
UI formats an instant-precision action in that action's recorded zone so its date
and time agree. A text-only correction leaves its time unchanged. An explicit
`real_time` correction uses the current installation zone, updating all occurrence
fields together while retaining previous immutable revisions.

Backfilled hours mean the start of the specified local hour. An hour that does
not exist during a DST transition is rejected; a repeated hour selects its first
occurrence. Day-only entries have unknown time and use the first valid minute of
that date only as an index anchor. A wholly skipped local date is rejected.
Recorded exact instants distinguish both occurrences of a repeated hour.

Task completions, inbox and learning timestamps are bucketed at read time in the
configured zone. Calendar events, reminder/routine schedules and other explicitly
zoned provider records retain their independent settings. Historical SQL helpers
named `san_juan_date` and `san_juan_hour` retain their original meaning for old
expression indexes; current summaries use the configured helper directly.

Fresh `action_logs` DDL uses a UTC default. Existing schemas require the normal
offline update path before opening with the new application; changing the DDL
default must copy existing values unchanged. Setting an environment variable is
not a database migration and does not authorize rewriting historical rows.
