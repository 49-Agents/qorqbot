---
name: bazarlyk-role-action-recording
description: Record each atomic completed result once in the canonical action log with actions_record, in at most 20 words, with the correct scope and San Juan time.
---

# Action recording

After each atomic completed result, call `actions_record` once. A task is
optional; the larger task may still be open.

- Record: a finished artifact, an independently complete setup step, a sent or
  published item you performed, or a completed investigation with a concrete
  saved finding.
- Do not record: plans, proposals or Signoffs awaiting approval, task status
  changes, routine reads, internal messages, individual tool calls, failed or
  unfinished attempts, or anything already recorded by a system receipt or
  worker (for example provider-confirmed sends or filing). Never record only to
  satisfy a completion reminder.
- Text is the result, not the task description: at most 20 words, for example
  "Published pricing page with annual plan toggle". Longer text is rejected;
  shorten it rather than splitting one result into several entries.
- Add evidence when available (`url`, `commit`, `pull_request`, record IDs) and
  `task_id` when the result belongs to a task in the same workspace.
- Attribution is server-derived. Record only your own work. To record the
  result of your own completed delegated assignment, pass
  `delegation_assignment_id`: the server verifies it, records you as recorder
  with an unknown performer and keeps the assignment as provenance. Skip it
  when the worker already recorded its actions; never create a second copy.

Scope:

- Always choose `category`. Use `global-operational` for shared
  operations not attributed to particular projects, or
  `business-operational` with `business_ids` naming every currently authorized
  project the work was for. One inaccessible project rejects the whole call.
  `global-systems` and Unclassified are owner-only.
- A multi-project action's text is shared with each selected project. Keep it
  free of details that one project should not see.

Time: omit `real_time` for work completed now. For earlier work, send its San
Juan date (`YYYY-MM-DD`) or date and hour (`YYYY-MM-DDTHH`, 24-hour), never
minutes, offsets or future times. Qorqut operational time is America/Puerto_Rico
(UTC-04:00 all year); schedule and provider zones do not apply to actions.

Use `actions_config_get` for your allowed categories, projects and the current
San Juan date/hour, and `actions_list`/`actions_get` to check what is already
recorded before backfilling.
