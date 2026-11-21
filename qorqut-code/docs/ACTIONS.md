# Completed actions

One canonical log records atomic completed results: a finished artifact, an
independently complete setup step, a sent or published item, or a finished
investigation with a saved finding. It is not a task list, a plan or an audit
trail of every tool call.

## Data model

`action_logs` stays the single table. Its `project_id` is an immutable
**storage namespace**: the owning project for project rows,
`NULL` for Global-stored rows (owner and ecosystem agents). Storage never grants
scope. Each row has:

- `id`: a UUID, unique across all Actions. The public `action_id` of every
  `actions_*` operation. The scope that stores the Action is `project_id`
  beside it; the four operations that change an Action (`actions_correct`,
  `actions_withdraw`, `actions_comment`, `actions_follow`) find that scope from
  the id alone for a caller that selected no project.
- `category`: `global-systems`, `global-operational`, `project-operational`, or
  `NULL` (Unclassified, owner capture only).
- `action_text` (the result, trimmed, 1–2000 characters), `performed_by`,
  `recorded_by`, `origin` (`manual`, `agent`, `automatic`, `legacy`).
- Recorded local occurrence: `occurred_date`, `occurred_hour`, `time_precision`
  (`instant`, `hour`, `day`), `time_zone`; `occurred_at` is a UTC instant or the
  start of the supplied local day/hour (an index anchor only, never displayed as
  an exact time). `created_at` is the immutable server recording instant.
- `revision` and `updated_at` for owner edits.

`action_projects(action_id, project_id)` is the scope: every
project-operational action has one or more memberships, and no other category
has any. Project views join through memberships, so a shared action appears
once in each linked project. Aggregates count distinct `action_id`.

Triggers keep agent, automatic and legacy rows append-only and prevent any
deletion. Identity, origin, attribution and recording time never change. Only
manual rows can change text, classification, memberships or occurrence, and only
through `actions_update`.

## Operations

| Operation | Callers | Notes |
|---|---|---|
| `actions_record` | owner, project agents, ecosystem agents with the grant | Mutation, idempotent by `idempotency_key`. |
| `actions_list` | same | Newest occurrence first; `since`/`until` (stored occurrence dates), `project`, `performer`, `categories` (incl. `unclassified`), `origins`, `limit`/`cursor`. Cursors bind the caller, scope and filters. |
| `actions_get` | same | Unauthorized and unknown actions are both `not_found`. |
| `actions_config_get` | same | Categories, limits, allowed/default scope, project directory, configured operational time now. |
| `actions_update` | installation owner only, dashboard exposure | Own manual actions; `expected_revision` required. |

`actions_record` accepts `text`, optional `category`, `project_ids`,
`real_time`, `task_id` and typed `evidence` (`url`, `commit`, `pull_request`,
`signoff`, `story`, `document`, `request`, `conversation`, `upload`).

- Agents are limited to 20 words; the owner may write up to 2000 characters.
  Blank text is rejected. No task, title, tag or time is required.
- Project agents send text only. The action is project-operational for their
  home project and stored there; Global and other projects are forbidden.
- Ecosystem agents must choose `global-operational` or `project-operational`.
  Project-operational needs `project_ids`, all currently authorized and
  active; one inaccessible project rejects the whole call and writes nothing.
  `global-systems` and Unclassified are owner-only.
- `task_id` links one task. A caller that records in Global (the owner, an
  ecosystem agent) names it by id alone: it is found in Global or any project in
  the caller's reach where it may read tasks (the same reach as the task tools'
  own lookup, lower-casing an upper-case id), and a classification the caller
  left out is taken from it. With no `category`, a project task makes the action
  `project-operational` for that project and a Global task
  `global-operational`; `project-operational` without `project_ids` takes the
  task's project. Values the caller passes are never changed and are authorized
  as before. A task that does not exist or is outside that reach is a
  `validation_error` on `task_id`, with nothing disclosed. An id held in several
  reachable scopes (reused before ids were unique) needs the one meant named in
  `project_ids`, or `category: "global-operational"` for the Global one;
  otherwise the call is refused listing the candidates. A project agent and a
  delegated worker link a task of the action's own storage scope, as before. The
  linked task is looked up again when a staged record is validated and applied.
- Authorization and project state are rechecked immediately before apply.
- Performer and recorder are the caller for its own work; no caller can name a
  performer. A bound delegated worker has no principal of its own: its records
  have an unknown performer, the parent as recorder and the assignment and
  generation as `delegation_assignment`/`delegation_generation` evidence. A
  parent records a delegated result with `delegation_assignment_id`, verified at
  prepare and apply: the parent owns the assignment, it completed with a result
  summary and the projects are within its scope. Each assignment's result is
  recorded once, and not when its worker already recorded actions.

Scoped reads: a project agent sees actions linked to its home project; an
ecosystem agent sees global-operational actions and project-operational actions
linked to a project it may access; the owner sees everything. A shared action
shows only the reader's authorized memberships. Storage is not evidence
authorization: evidence (refs, detail) and the legacy identity are shown to the
owner, to readers of the project that stores the action, and for a
Global-stored action only to a reader who may access every linked project.
Otherwise the reader sees the shared text and its own projects only.
`logs_list`/`logs_get` apply the same rule in a project view.

`actions_list` applies authorization, filters and the cursor position in SQL and
fetches one bounded page, so it keeps working as history grows.

`actions_update` changes `text`, `category`, `project_ids` and `real_time`
(`null` restores the recording instant, not the edit time). Agent, automatic and
legacy actions, other principals' actions and project-stored actions are not
editable.

The dashboard root route `/api/root/operations/<op>` serves all five operations
to the owner. MCP exposes the four agent operations; `actions_update` is not an
MCP tool.

## Legacy and automatic writers

`logs_add` is the same domain: the result is a project-operational action in
the selected project, and agent callers keep the 20-word and home-project
rules. `logs_list`/`logs_get` read through memberships, so shared actions appear
in each linked project's Activity feed and existing `(project_id, id)`
references still open.

Provider-confirmed email sends record one automatic
action with the confirmed receipt, from a deterministic source identity
(`signoff:<id>:email`); a retried application finds the existing
action instead of adding another. Their text is a bounded summary of the
receipt.

A worker's `done` Task closure records one automatic action credited to that
worker, from a deterministic source identity (`task-closure:<project or
global>:<task id>`), so a Task reopened and closed again keeps the action of its
first closure. Its text is the closing result cut to 20 words, or the Task title
when the closure carried no result. Cancelled Tasks, children closed along with
their parent, and closures by the coordinator, the learning applier or a person
record none. Other Task transitions, approvals, filing suggestions and internal
messages are never logged automatically.

Background programs keep their frozen definitions: an `action_logs` declaration
covers the membership rows its action creates.

## Operational timezone

`QORQUT_TIMEZONE` selects an IANA timezone before the application process starts;
the default is UTC. `actions_config_get` reports the zone and current local date
and hour. The dashboard reads that setting from its session/API responses.
`qorqut_operations.local_time` owns the conversion; wire instants stay UTC.

`real_time` is `YYYY-MM-DD` or `YYYY-MM-DDTHH` in the installation zone. Future
values are rejected. A nonexistent DST hour is rejected; a repeated hour means
its first occurrence. A day whose midnight does not exist is anchored to its
first valid minute. Omitted `real_time` records the exact server instant.

Stored action dates, hours, precision, zone and UTC timestamps survive timezone
changes. Listings and counts use those stored dates; the dashboard renders each
action's exact time in its recorded zone. Correcting only text preserves its time.
Explicitly correcting `real_time` uses the current zone and updates the zone
alongside the date/hour/anchor. Previous immutable revisions retain the old values.

Progress windows and instant-based task/inbox/learning day buckets follow the
installation zone. Calendars, reminders, routines, measurement zones, billing
ranges and provider formats keep their independent explicit zones. See
[operational time](OPERATIONAL-TIME.md) for configuration and upgrade behavior.

## Agents

Agent bootstrap guidance, role workflow instructions and the shared
`action-recording` role skill describe when to record, what not to record, the
scope rules and the time convention. The completion checkpoint asks once per
substantive turn whether unrecorded completed work exists; `actions_record`,
`logs_add` and label saves are bookkeeping and never make the turn dirty again.

The Ordubashy and Learning Applier roles select the skill and grant `actions_record`, `actions_list`,
`actions_get` and `actions_config_get`.

## Release boundary

The action log is part of the host schema that the initializer creates; fresh local preparation and manual
startup are the supported path, and no numbered migration, backfill or automatic upgrade is supplied (see
[Release worker](release/README.md)). Existing agent sessions are not restarted by a source change.

## Limits and deferred work

- Daily and weekly reports, ranking and delivery are deferred until this
  foundation is reviewed and activated.
- Story and related-item lookups name an Action by its `action_id`. A
  Global-stored shared action can be linked from each linked project and is
  not found elsewhere. Recording an
  action creates no project history event.
- Nightly exports include Global-stored actions and all memberships in the root
  export; project exports contain project-stored rows.
