# Completed actions

One canonical log records atomic completed results: a finished artifact, an
independently complete setup step, a sent or published item, or a finished
investigation with a saved finding. It is not a task list, a plan or an audit
trail of every tool call.

## Data model

`action_logs` stays the single table. Its `business_id` is an immutable
**storage namespace**: the owning business for business rows,
`NULL` for Global-stored rows (owner and ecosystem agents). Storage never grants
scope. Each row has:

- `action_key`: `business/<business-uuid>/<id>` or `global/<id>`, unique. The
  public `action_id` of every `actions_*` operation.
- `category`: `global-systems`, `global-operational`, `business-operational`, or
  `NULL` (Unclassified, owner capture only).
- `action_text` (the result, trimmed, 1–2000 characters), `performed_by`,
  `recorded_by`, `origin` (`manual`, `agent`, `automatic`, `legacy`).
- San Juan occurrence: `occurred_date`, `occurred_hour`, `time_precision`
  (`instant`, `hour`, `day`), `time_zone`; `occurred_at` is a UTC instant or the
  start of the supplied local day/hour (an index anchor only, never displayed as
  an exact time). `created_at` is the immutable server recording instant.
- `revision` and `updated_at` for owner edits.

`action_businesses(action_key, business_id)` is the scope: every
business-operational action has one or more memberships, and no other category
has any. Business views join through memberships, so a shared action appears
once in each linked business. Aggregates count distinct `action_key`.

Triggers keep agent, automatic and legacy rows append-only and prevent any
deletion. Identity, origin, attribution and recording time never change. Only
manual rows can change text, classification, memberships or occurrence, and only
through `actions_update`.

## Operations

| Operation | Callers | Notes |
|---|---|---|
| `actions_record` | owner, business agents, ecosystem agents with the grant | Mutation, idempotent by `idempotency_key`. |
| `actions_list` | same | Newest occurrence first; `since`/`until` (San Juan dates), `business`, `performer`, `categories` (incl. `unclassified`), `origins`, `limit`/`cursor`. Cursors bind the caller, scope and filters. |
| `actions_get` | same | Unauthorized and unknown actions are both `not_found`. |
| `actions_config_get` | same | Categories, limits, allowed/default scope, business directory, San Juan time now. |
| `actions_update` | installation owner only, dashboard exposure | Own manual actions; `expected_revision` required. |

`actions_record` accepts `text`, optional `category`, `business_ids`,
`real_time`, `task_id` and typed `evidence` (`url`, `commit`, `pull_request`,
`signoff`, `story`, `document`, `request`, `conversation`, `upload`).

- Agents are limited to 20 words; the owner may write up to 2000 characters.
  Blank text is rejected. No task, title, tag or time is required.
- Business agents send text only. The action is business-operational for their
  home business and stored there; Global and other businesses are forbidden.
- Ecosystem agents must choose `global-operational` or `business-operational`.
  Business-operational needs `business_ids`, all currently authorized and
  active; one inaccessible business rejects the whole call and writes nothing.
  `global-systems` and Unclassified are owner-only.
- Authorization and business state are rechecked immediately before apply.
- Performer and recorder are the caller for its own work; no caller can name a
  performer. A bound delegated worker has no principal of its own: its records
  have an unknown performer, the parent as recorder and the assignment and
  generation as `delegation_assignment`/`delegation_generation` evidence. A
  parent records a delegated result with `delegation_assignment_id`, verified at
  prepare and apply: the parent owns the assignment, it completed with a result
  summary and the businesses are within its scope. Each assignment's result is
  recorded once, and not when its worker already recorded actions.

Scoped reads: a business agent sees actions linked to its home business; an
ecosystem agent sees global-operational actions and business-operational actions
linked to a business it may access; the owner sees everything. A shared action
shows only the reader's authorized memberships. Storage is not evidence
authorization: evidence (refs, detail) and the legacy identity are shown to the
owner, to readers of the business that stores the action, and for a
Global-stored action only to a reader who may access every linked business.
Otherwise the reader sees the shared text and its own businesses only.
`logs_list`/`logs_get` apply the same rule in a business view.

`actions_list` applies authorization, filters and the cursor position in SQL and
fetches one bounded page, so it keeps working as history grows.

`actions_update` changes `text`, `category`, `business_ids` and `real_time`
(`null` restores the recording instant, not the edit time). Agent, automatic and
legacy actions, other principals' actions and business-stored actions are not
editable.

The dashboard root route `/api/root/operations/<op>` serves all five operations
to the owner. MCP exposes the four agent operations; `actions_update` is not an
MCP tool.

## Legacy and automatic writers

`logs_add` is the same domain: the result is a business-operational action in
the selected business, and agent callers keep the 20-word and home-business
rules. `logs_list`/`logs_get` read through memberships, so shared actions appear
in each linked business's Activity feed and existing `(business_id, id)`
references still open.

Provider-confirmed email sends record one automatic
action with the confirmed receipt, from a deterministic source identity
(`signoff:<id>:email`); a retried application finds the existing
action instead of adding another. Their text is a bounded summary of the
receipt. Task transitions, approvals, filing suggestions and internal messages
are never logged automatically.

Background programs keep their frozen definitions: an `action_logs` declaration
covers the membership rows its action creates.

## Time: San Juan

Operational human time is America/Puerto_Rico (UTC-04:00 all year, no DST),
labelled "San Juan time". `bazarlyk/local_time.py` and the dashboard's
`js/san-juan-time.js` are the only conversions. Stored and wire instants stay
UTC.

`real_time` is `YYYY-MM-DD` or `YYYY-MM-DDTHH` (24-hour) in San Juan time. No
minutes, offsets or natural language. Invalid dates and future dates/hours are
rejected. Omitted `real_time` records the server instant. Listings, filters and
day counts use the San Juan occurrence date, so a backfilled action counts on
the day it happened. Day-only entries sort after that day's timed entries.

Business progress, Global progress, the dashboard summary and the Global
activity chart bucket timestamps by San Juan day. Dashboard task, comment,
signoff, story, email, fleet and snooze times display San Juan time. Cron
schedules, routine timezones, per-metric measurement zones, provider wire
formats, filing-cost billing ranges and receipts keep their own explicit zones
and are not action time.

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
- Story and related-item lookups accept a canonical `action_id` or a legacy bare
  ID from the business's own storage. A Global-stored shared action can be
  linked from each linked business and is not found elsewhere. Recording an
  action creates no business history event.
- Nightly exports include Global-stored actions and all memberships in the root
  export; business exports contain business-stored rows.
