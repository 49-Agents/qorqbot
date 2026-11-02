# Operational timezone

Set `QORQUT_TIMEZONE` to an IANA zone such as `Europe/Berlin` before starting the
application. If it is absent, the default is `UTC`. Invalid or empty values stop
startup with a configuration error. The setting is read once per process; restart
after changing it. Each installation runs its own process and may select a
different zone. Required timezone data must be available in the runtime.

The native launcher records the installation's chosen setting separately from
its replaceable application runtime. A fresh installation may use the system
zone, with UTC as the fallback. Upgrading a legacy installation must preserve its
previous operational zone (`America/Puerto_Rico`) unless a new zone is explicitly
chosen. Do not infer a new timezone from the build machine during an update.

The owner session returns `operational_timezone`. `actions_config_get` returns
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
