---
name: bazarlyk-role-action-recording
description: Record each atomic completed result once in the canonical action log with actions_record, in at most 20 words, with the correct scope and installation timezone.
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
  "Published release notes with install steps". Longer text is rejected;
  shorten it rather than splitting one result into several entries.
- Add evidence when available (`url`, `commit`, `pull_request`, record IDs) and
  `task_id` when the result belongs to a task in the same workspace.
- Ordubashy records the owner's reported completed Actions on their behalf: pass
  `performed_by` as the owner ID and an `explicit_owner_statement` relay containing
  the reported result. For another registered agent use `agent_report`. The server
  preserves Ordubashy as recorder and validates the performer. An `owner_intent`
  interpretation is not evidence of completed work. Other workers record their own
  work. Check for an existing Action before relaying a result.
- The legacy `delegation_assignment_id` path keeps an unknown performer and the
  verified assignment as provenance. Do not combine it with a performer relay.
  Corrections and withdrawals preserve the immutable original performer.

Scope:

- Always choose `category`. Use `global-operational` for shared
  operations not attributed to particular projects, or
  `business-operational` with `business_ids` naming every currently authorized
  project the work was for. One inaccessible project rejects the whole call.
  `global-systems` and Unclassified are owner-only.
- A multi-project action's text is shared with each selected project. Keep it
  free of details that one project should not see.

Time: omit `real_time` for work completed now. For earlier work, read the
installation timezone from `actions_config_get`, then send that local date
(`YYYY-MM-DD`) or date and hour (`YYYY-MM-DDTHH`, 24-hour), never minutes,
offsets or future times. A nonexistent DST hour is rejected; a repeated hour
means its first occurrence. Schedule and provider zones do not apply to actions.

Use `actions_config_get` for your allowed categories, projects and current local
date/hour, and `actions_list`/`actions_get` to check what is already recorded
before backfilling. Historical records keep their original date, precision and
zone; changing an installation setting does not reinterpret old actions.
