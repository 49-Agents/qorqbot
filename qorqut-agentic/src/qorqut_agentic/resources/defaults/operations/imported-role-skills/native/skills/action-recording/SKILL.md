---
name: qorqut-role-action-recording
description: Record each atomic completed result once in the canonical action log with actions_record, in at most 20 words, with the correct scope and installation timezone.
read_for: Recording a completed result in the action log (`actions_record`)
---

# Action recording

After each atomic completed result, call `actions_record` once. A task is
optional; the larger task may still be open.

- Record: a finished artifact, an independently complete setup step, a sent or
  published item you performed, or a completed investigation with a concrete
  saved finding.
- Do not record: plans, proposals, drafts or Signoffs awaiting approval, task status
  changes, Tasks closed because they were merged, duplicated or dropped, routine
  reads, internal messages, individual tool calls, failed or unfinished attempts,
  or anything the service already recorded (for example a worker's `done` Task
  closure, provider-confirmed sends or filing). A reminder, or a tool result saying
  no Action is recorded, is a reason to check, not a result: record a real
  completed result that is missing, never an invented one.
- Text is the result, not the task description: at most 20 words, for example
  "Published release notes with install steps". Longer text is rejected;
  shorten it rather than splitting one result into several entries.
- Add evidence when available (`url`, `commit`, `pull_request`, record IDs) and
  `task_id` when the result belongs to a Task. Pass `task_id` alone: the Task is
  found in any project you can reach, and a `category` and `project_ids` you
  leave out are taken from it.
- Ordubashy keeps the books of completed work: every real completed result appears
  once as an Action. The service records a worker's `done` Task closure itself,
  credited to that worker: the first 20 words of the closing result, or the Task
  title when none was given. Ordubashy never records it again; he corrects a wrong
  or unclear text with `actions_correct`. Nothing is recorded for a `cancelled`
  Task, for children closed along with their parent (the parent's Action covers
  them), or for a Task that Ordubashy or the owner closes.
- The result of `tasks_get` or `tasks_complete` on a `done` Task tells Ordubashy
  when its Action is recorded or missing. He relies on that instead of searching:
  `actions_list` cannot filter by Task.
- Ordubashy records his own completed results, including a Task he closes himself
  when the closure is a completed result, and relays the rest. For the owner's
  reported completed work pass `performed_by` as the owner ID and an
  `explicit_owner_statement` relay containing the reported result. For a worker's
  completed result that came without a Task closure pass the worker's ID and an
  `agent_report` relay. The server preserves Ordubashy as recorder and validates
  the performer. An `owner_intent` interpretation is not evidence of completed work.
- The legacy `delegation_assignment_id` path keeps an unknown performer and the
  verified assignment as provenance. Do not combine it with a performer relay.
  Corrections and withdrawals preserve the immutable original performer.

Scope:

- Always choose `category`. Use `global-operational` for shared
  operations not attributed to particular projects, or
  `project-operational` with `project_ids` naming every currently authorized
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
