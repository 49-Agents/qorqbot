# Global tasks and delegated signoff attention

Global tasks use the ordinary task operations with `scope: "global"`. They have
no business ID and are not a cross-business task collection. An invocation with
no selected business defaults to Global; `scope: "business"` still requires an
explicit business. Explicit Global selection clears the selected business for
the whole task plan, its comments, dependencies and assignment edges.

Only the installation owner or an active ecosystem agent with
`read_global_context` and the relevant task operation grant can access them.
Business agents and system principals cannot opt into Global tasks, even with a
task-policy override. Existing role grants continue to apply; this is not an
implicit grant to all global roles. Assignments accept the installation owner
and active global agents, never business agents. Agents still cannot assign
Learning Applier; the owner can.

The same create/get/list/update/assign/comment/complete/config operations,
revision checks and idempotency receipts apply. Global configuration is separate
from every business. Dependencies must be in the same namespace; Global tasks
cannot link to business Stories. There is no task scope-move operation. Keep
business information in business tasks, not Global task bodies or comments.

Global task assignment and owner-task-following use the existing agent inbox;
read-time permissions filter every notification. Global session context includes
the same bounded task attention and same-scope working-memory source references.
No terminal is started, resumed, or messaged by a task change.

[Task snoozing](TASK-SNOOZING.md) applies in this namespace too: use the same task
operations, revision checks and active/snoozed/all filters. Migration 047 follows
046; no task lifecycle, priority system or agent wake is introduced.

The owner dashboard exposes `/api/root/operations/tasks_*` and
`/api/root/operations/assignees_list`. These endpoints reject a selected business.
The Global task drawer tab and global-agent pane use these typed operations.
The existing `/api/me/tasks` remains a separate aggregate of owner-assigned
**business** tasks; business task lists and summaries do not inherit Global work.

Migration 046 rebuilds the task, note, relationship and task-configuration tables
transactionally, preserving business rows, indexes, capture triggers and foreign
keys. Partial unique indexes enforce NULL-scope identities; Global note-parent
guards cover the NULL foreign-key case. Only task configuration and task-owned
assignment/dependency relationships can use Global scope. SQLite stays the
operational authority; normal backups include Global tasks. This application
policy is not an OS isolation boundary against processes sharing a user account.

## Cross-scope task overview

`tasks_overview({})` gives Ordubashy a brief index of tasks assigned to the
installation owner, the calling Ordubashy, or both. It reads Global and all of
that agent's currently authorized businesses in one invocation, regardless of
the selected business. It does not copy business tasks into Global storage.

Defaults are `assignee: "both"`, `snooze_filter: "all"`, `include_closed: false`
and `limit: 100` (maximum 200). Use `assignee: "owner"` or `"self"`, and optionally
`snooze_filter: "active"` or `"snoozed"`. Archived tasks are always excluded;
closed-state classification comes from each scope's task configuration. Snooze
expiry is evaluated once per call. Paused/blocked unfinished states remain visible.

Results have `assignees` (the authoritative owner/self IDs), `counts` of matching
active/snoozed tasks, `scopes` grouped by business ID/name (null means Global),
and `next_cursor`. Each scope separates `active` and `snoozed` entries. Entries
contain only ID, title, status, due date, owner/self assignee labels and, when
currently deferred, snooze metadata. They also contain `tags`, the complete
normalized ordinary tag list (or `[]` when untagged). Shared owner/self
assignments appear once. The configured `tags.list` projection includes the
built-in `design/software` tag without rewriting saved scope configuration; it
is independent of assignee and task kind.
Titles longer than 180 characters are shortened and marked `title_truncated`.
Notes, custom fields, dependencies, execution details and revisions are not loaded.
Read `tasks_get` in the returned scope for details and a fresh revision before
editing. This operation changes no task, signoff or session state.

The page limit applies across all scopes, with active work before snoozed work,
then earliest due date and stable scope/ID ordering. Counts include all matches,
not just this page. Follow `next_cursor` before claiming the list is complete.
Continuation is bound to the caller, current owner, readable scopes and filters;
it is a fresh read, not a frozen snapshot. A safety bound rejects more than
20,000 matches rather than returning a misleading partial overview.

Only an active ecosystem Ordubashy with `read_global_context`, `tasks_overview`
and `tasks_list` permissions can use it. Current scope and active business
memberships are rechecked, including the owner's membership for owner-assigned
business work. Other humans marked owner and other Ordubashy agents are not the
selected assignees. Denying task listing also denies the aggregate; granting this
tool to another role does not bypass its Ordubashy-only boundary. No new global
capability, schema migration, dashboard route or bootstrap payload is introduced.

Deploy the companion core operation, then publish/adopt Ordubashy 1.19.0 and
reprepare its native projections explicitly. Source changes alone do not upgrade
live agents. Existing sessions may need refreshed tool metadata/instructions.

## Signoff urgency

`signoffs_attention_set` additionally lets active ecosystem Ordubashy update
other agents' accessible pending signoffs. The owner/proposer rules otherwise
remain unchanged. The server checks current role, target visibility and pending
state again at application. Existing policy overrides still apply.

This operation sets the complete attention object: `notify`, `urgent`,
`blocking`, `reason`, and `deadline`. Omitted fields take their default values;
read first and preserve any flags/deadline not intentionally changed. Urgent or
blocking flags require a reason. Owner snoozes survive reprioritization.
Each write retains normal actor-attributed application receipts.

This does not widen access to the signoff, alter its proposed action, approve it,
answer owner input, cancel someone else's input request or retry execution.
Ordubashy 1.18.0 documents Global tasks and attention triage, retaining Focus and its 2FA
escalation rule. Publish and repin that role explicitly after the core release;
source edits alone do not update installed agents or running conversations.

## Execution synchronization versus retry

A signoff's decision and its execution result are separate records. Approval
authorizes the saved action; execution records whether it actually succeeded.
Synchronization reconciles recorded execution outcomes (for example, a saved
provider receipt after an interrupted response). It does not mean marking an
ordinary task done. A retry attempts execution again and can cause an external
effect. These execution controls and their existing safety gates are unchanged.
