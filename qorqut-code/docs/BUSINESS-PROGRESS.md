# Business progress

The Progress page calls owner-only `business_progress_get` with 30,
90 or 365 days. Each series uses dates in the installation timezone; totals cover all recorded history.

- Emails: captured outgoing messages, including replies/follow-ups and deleted
  synchronized messages. RFC message IDs deduplicate Sent/BCC copies within a
  business; mailbox/provider identity is the fallback. Unsynced messages and
  unknown provider outcomes are not inferred. Uses existing outgoing attribution.
- Tasks: distinct business tasks first entering the literal `done` state;
  reopening/completing again counts once. `dropped` is excluded. Existing done
  tasks enter the total with an unknown date; they are not placed on the chart.
  Custom closed state names are not assumed to mean successful completion.
- Actions: persisted business activity logs, using their occurrence dates;
  this is not a count of tool calls or internal mutations.
- Signoffs approved: saved approve decisions, using decision time, regardless
  of subsequent execution success. Rejections and question answers are excluded;
  application retries do not add approvals.

Missing or invalid timestamps contribute to totals and explicit undated counts.
No historic task completion time is inferred from mutable `updated_at`.
The first-done task records come from SQLite migration 050 and are included in normal SQLite backups. The separate
milestone feature that migration 049 introduced is retired in v1.3 without a replacement or a conversion; its tables are not part
of the target store. No routine or background polling is added. No live records change until an explicitly approved deployment.
