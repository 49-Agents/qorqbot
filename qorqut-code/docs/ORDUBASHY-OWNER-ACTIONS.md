# Ordubashy owner assistance

Ordubashy uses existing project-scoped task operations for the owner's tasks:
identify the owner with `assignees_list`, filter `tasks_list` by that human's ID,
read the task and configured statuses, then use `tasks_update` or `tasks_complete`
with its current revision. Personal taskbox entries remain project-owned tasks.
A full task ID alone finds the task's project; pass `project_id` only when the
answer lists several, and keep full task IDs, never shortened ones, in notes
([details](GLOBAL-TASKS.md#finding-a-tasks-scope-from-its-id)).
The built-in [inbox](AGENT-INBOX.md#ordubashy-follows-the-owners-tasks) automatically
follows current owner-assigned tasks without historical backfill or manual
subscription churn. This does not assign those tasks to Ordubashy.

`signoffs_reject` is a separate, rejection-only MCP operation for active ecosystem
Ordubashy profiles. The role grant and current scope/policy are checked normally;
an override cannot enable another role to use it. It requires the proposal ID,
proposal revision, payload hash and status revision returned by `signoffs_get`.
Optional `feedback` stores the rejection notes with the actual rejecting agent's
identity. It cannot modify an existing decision, reject historical/superseded
proposals, answer input requests, approve, or retry execution. Existing platform
rejection transitions and transactional/idempotency protections are retained.

Approval, execution retry and owner-input answers remain human-only. Ordubashy
must not impersonate the human or use dashboard credentials/browser control to
bypass that boundary. Rejection never authorizes a replacement action.
