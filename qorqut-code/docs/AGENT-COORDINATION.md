# Agent activity and task progress

Coordination reuses tasks, their append-only notes, relationships and the inbox.
Stories keep their existing flexible links and timeline; there is no new hierarchy
or separate progress-event system. Private working memory remains private.

## Presence and current work

Successful native bootstrap registers an activity session. Prepared Claude/Codex
hooks write a tiny local observation on lifecycle and tool events. One shared
service thread samples registered sessions every 60 seconds and writes a single
SQLite batch. No model call, HTTP request, per-agent daemon or inbox event is needed
for a heartbeat. Action hooks execute a standalone stdlib script rather than
importing the application. Reads do not acknowledge messages or resume terminals.

Running means a native session is handling a turn; Idle means that session has
finished or awaits approval. Native process start identity and boot identity
prevent an open shell or reused PID from implying a running agent. Codex app-server
sessions additionally require the exact thread writer lock. Container PID namespaces
are translated through the host's process inventory when observable. Unsupported,
unreadable or stale observations are explicitly unknown; an observed end is offline.
Freshness expires after three minutes without a verified observation. It is not a
measurement of CPU usage or proof of successful progress.

Claude process detection supports native `claude` and npm's `claude.exe` builds,
including processes retained after an executable update. Existing agents need
their prepared `.bazarlyk/activity-hook.py` refreshed through preparation after
deploying a hook fix; a service restart alone does not replace those local copies.

`agent_work_update` replaces only the calling agent's selected session list, up to
ten simultaneous work items. Each has a brief summary, explicit business UUID
(null for Global), optional task ID and optional `blocked_reason`. Native session ID
is supplied by bootstrap. Read the `work_revision` from `agents_activity_list` before
updating; heartbeat writes do not change that revision. Cleared/finished work should
be removed from the list. A blocked task does not make an entire agent blocked.

`agents_activity_list` returns currently authorized agents, their Claude/Codex
sessions, work items, source task state and blockers, last-seen/work timestamps,
and a continuation cursor. It defaults to the selected business; Global aggregates
only authorized businesses and Global agents. Source permissions are rechecked,
including for authored descriptions about a task. Filters include `agent_id` and
`role_id`; pages default to 100, maximum 200. Up to 20 recent sessions per agent are
returned; old offline sessions are pruned after seven days, retaining the latest.

The installation owner's `GET /api/me/agents/activity` uses this same scoped
operation. Dashboard Global → Agent fleet and agent panes refresh every minute
while visible and offer manual Refresh. The UI labels stale work after one hour,
separately from the much shorter presence timeout. Latest work on an idle, ended
or unobserved session is labeled as last work, not active execution.

## Task progress and dependencies

`tasks_comment` appends a brief note with server timestamp and actor attribution.
Use it for meaningful findings, changed blockers, handoffs and verified completion;
do not repeat unchanged waiting. `note_refs` can link up to ten directly related
signoffs, stories or tasks in the task's scope. Targets are checked when preparing
and applying writes and again on reads. Inaccessible references reveal no identity
or label. References are context, not approvals or implicit task dependencies.

`tasks_get` includes the latest 20 notes, `note_count` and `notes_next_cursor`.
`tasks_notes_list` pages older notes (each page chronological); cursors bind task,
scope and principal. `tasks_list` omits history and includes its count, so a growing
timeline does not enlarge routine lists. Existing exports preserve all notes.

Existing `blocked_by` edges remain authoritative. Task details also expose `blocks`
for unfinished dependent tasks. Adding/changing an edge informs relevant assignees;
later prerequisite changes notify dependent-task assignees via existing inbox
delivery, independently of manual subscriptions. Direct signoff references follow
answers and execution outcomes for task assignees. Current read access still gates
delivery, with existing deduplication and no self-generated wakeups. Existing task
state configuration determines whether a prerequisite is closed; an agent must
check the recorded outcome before treating closure as successful fulfillment.

## Startup and rollout

Shared role instructions require current-work updates at meaningful checkpoints.
Where a client's completion checkpoint protocol is off, the local hook can remind
an agent after actions at most once per 15 minutes; recent work updates suppress
the reminder. With protocol 1 the Stop checkpoint replaces it with at most one
reminder per substantive turn ([management coordination](AGENT-MANAGEMENT-COORDINATION.md)). The business agent receives a bounded business
team summary; Ordubashy receives authorized business agents. Managers finish continuation
pages through `agents_activity_list` when there are more than 20 agents. Unblocking
others is prioritized within owner instructions and existing authority.
Team summaries travel in the existing `work_briefing.rendered` field so currently
running container helpers can consume them without an image replacement.

Deploy core and dashboard together after approval, applying migration 055 through
normal service initialization. Reprepare agents to install the new hooks and role
procedure. No role republishing or profile repinning is required. Existing sessions
without the new hooks cannot be retroactively classified; show unobserved presence
until their next successful prepared bootstrap. Preserve terminals and credentials;
do not restart or prompt real agents just to populate the fleet.

The collector shares the existing recovery write-admission barrier and never
persists tool arguments, outputs, transcript text or credential values. Activity
rows are replaceable observations excluded from inbox capture and operational Git
exports; durable progress stays in task notes.

## Validation

Isolated integration checks cover simultaneous sessions, revoked source access,
outdated work revisions, stale/dead process observations, paged note references,
dependency/signoff delivery and throttled reminders. Existing task, bootstrap,
inbox and role-preparation checks run against the installation's immutable role
and plugin fixtures, without opening a real agent or modifying live records.

Very frequent tool calls consume transient CPU through hooks, and durable note storage grows with progress.
