# P3 host side: operational records and the durable Context queue

Branch `v1.2/operational-model`, based on the host foundation `7679f287`. The engine half is Operations
`v1.2/operational-model` at `24f1ea2` (tags, canonical discussion, Tasks, Actions, `context_queue`; `2055fda` plus the
2058 correction round). This host branch is built and tested against an installed wheel of exactly that commit
(`git archive 24f1ea2`, `pip wheel --no-deps`); the checked-in Operations gitlink and the retired private foundation receipt
still name the foundation pin `138f40c`, so the receipt case of `test_host_foundation_engine` fails until integration
advances the pin to a commit that contains `24f1ea2`. Nothing here changes the receipt or the gitlink.

## What moved into the engine and what the host keeps

| Concern | Owner now | Host contribution |
| --- | --- | --- |
| Task states, fields, parents, informational dependencies, shared `snoozed_until`, assignees, protected `request_to_owner` | `qorqut_operations.tasks` | `domains/tasks/ports.py` answers the typed `TaskPorts` (assignee rules, setup guards, Focus presentation, incident links, note references, owner-work routing) |
| Shared Task/Action tag catalog (`tags`, `item_tags`) | `qorqut_operations.tags` | none; a tag is a label and grants nothing |
| Item threads, follows, the one event writer (`emit_event`), required vs optional deliveries | `qorqut_operations.discussion` | `inbox/service.py` routes `inbox_subscribe` of a Task or Action to the same follow state; `inbox/events.py` no longer writes Task or Action events |
| Actions with immutable revisions (`action_logs`, `action_revisions`, `action_businesses`) | `qorqut_operations.actions` | `domains/activity` is now `ActionWriteback` (one-source-one-Action key) plus `linked_action` for item links |
| Context input queue (`context_inputs`), `enqueue_action`, transitions, attempts, controls | `qorqut_operations.context_queue` | none |

`domains/__init__.py` builds `TagOperations`, `TaskOperations`, `ActionOperations` and `ContextQueueOperations` and installs
each `contribution()`; their storage comes first in `composition/schema.py::storage_contributions()` and the host owners
that sit on those tables follow. The host schema owner formerly named `tasks` is `requests`; `task_notes`, `task_changes`
and `action_log_labels` are retired with their v1 behaviour. (The schema owner `requests` itself was removed in v1.3.)

## Cross-domain Task writes

Project setup, reply routing and handoff repair record Task rows in their own prepared plans.
They apply those plans through `domains/tasks/writes.py::TaskWrites`, which snapshots the Task before and after
`apply_records` and hands the difference to `TaskOperations.announce` in the same transaction, so every Task write gets its
follows, thread entry and one event from the engine and the host finalizer writes none. Action writebacks
(`platform_actions/delivery.py`) append through `ActionWriteback.append_to_plan` and the applying operation calls the
engine's `settle`, which enqueues the exact revision and announces it in that transaction.

## Change feed

`composition/capture.py` is a host-owned storage contribution (owner `operational_feed`, last in order) that attaches the
`inbox_changes` capture triggers to the engine tables `tasks`, `thread_messages`, `thread_follows`, `item_tags`,
`action_logs` and `action_businesses`. Triggers copy only scope and identity. `service/changes.py` maps them to the
`tasks` and `action_logs` kinds (a thread entry, follow or tag by its `item_type`), and `inbox/events.py::targets` ignores
these sources, so the capture never produces a second event.

## Catalog, policy and program definitions

* `composition/catalog.py`: the engine entries are `ENGINE_ENTRIES`; `logs_list`, `logs_get`, `logs_add`,
  `logs_labels_update` and `actions_update` are retired metadata only (replaced by `actions_list/get/record/correct/withdraw`,
  `tags_*`).
* `composition/policy.py` fallback defaults, read grants only: `tasks_notes_list`, `tasks_thread_get` and `tasks_follow`
  resolve as `tasks_get`; `actions_follow` and `actions_thread_get` as `actions_get`; `tags_list` as `tasks_get`.
  The new mutations have NO fallback: `actions_correct`, `actions_withdraw`, `actions_comment` and `tags_create`, like the
  `context_queue_*` operations, are denied for a registered agent until a role default or an explicit override names
  them (P8 owns the source role grants; no existing role was changed). Inline label creation inside an authorized Task or
  Action write is unaffected. The installation owner keeps the engine's owner authority for correction and withdrawal and
  is refused `actions_comment` by the retained team-channel rule (`channel_required`).
* Frozen background programs whose permissions or resources changed are version 2: `context.file`, `platform.dispatch`,
  `platform.reconcile` (`logs_add` became `actions_record`; `task_notes` became `thread_messages`; Action revisions and
  tags are declared). Jobs queued under the v1 digests are refused rather than run under a changed definition.
* The agent guidance for backlog work names `snoozed_until`; `snooze` / `indefinite` no longer exist.

## Checks

`tests/test_host_operational_contracts.py` (4 cases, one composed host with two businesses, two business agents and a
registered coordinator): the default denial of the new mutations, an explicit override enabling correction and its
revocation before a Signoff-staged correction is applied (the approval answers `stale_signoff`, the proposal is
`conflicted`, no revision or Context input appears); the queued create/correction/withdrawal payloads handed to the exact
Context `begin_action` API of the pinned Context (`24f3744`) in a disposable initialized Context with no model, proving the
nonempty `{id,name,business_id}` tag envelope is accepted, the retired label list is rejected and an earlier queued
revision never changes; the Action discussion (default denial, explicit grant, read with the Action's authorization, a
coordinator's owner relay following both parties, one optional event, sticky unfollow, no Action revision, Context input
or private-conversation row); and the snooze wake (human-only assignment and an owner-created unassigned non-Focus Task
both routed through `HostRouting.snooze_routes` to the registered coordinator with exactly one required wake, no human
inbox row and nothing on repeated reconciliation; no creator notification while someone else is assigned; an
agent-created unassigned Task reaching its creator; an assigned agent's inbox row; a closed Task never woken).
`tests/test_host_operational_records.py` (10 cases, one composed host, two businesses, registered agents): states,
snooze idempotency, parent and dependency validation; agent defaults and the protected owner request; tags that grant
nothing across businesses; one discussion writer with required events independent of an unfollow; private conversations
never becoming item threads; Action record, correction and withdrawal each queued as the exact immutable revision;
the host Action writeback recording once per confirmed source; change-feed capture; the setup guard answering through the
Task port; `TaskWrites` announcing a Task written inside another domain's plan once.
`tests/test_host_neutral_items.py` was adjusted: the engine Task service holds no Story reference.

## Inputs for other pieces and known limits

* H1 (`service/runtime.py`, `service/lifetime.py`, `composition/assembly.py`): register a periodic write unit calling
  `runtime.domains.tasks.reconcile_snoozes(uow)` (the engine `TaskOperations.reconcile_snoozes`, which uses the Task
  `Routing` port: the engine classifies the responsible principals once (every assignee, else the creator), delivers to
  responsible agents through the eligible inbox filter and hands the human ones to
  `HostRouting.snooze_routes(uow, business_id, task, human_ids)`, which returns agent ids: the mandatory Focus-coordinator
  route and, when the installation owner is among those humans, the Ordubashy owner-work recipients (`ordubashies(uow)`,
  no assigned-to-owner edge required, so an owner-created unassigned Task is covered); other humans keep whichever
  mandatory route applies; optional Story routes are not responsibility and no human inbox/notification store is
  invented); snooze scheduling stays with the host lifecycle and was not edited here. Drive `context_queue` transitions (the driver is P4/P5's); the `HandoffRepair(domains.tasks)` call at
  `runtime.py` already receives the engine service.
* P4/P5 and the role source (P8 for role decisions): give `context_queue_*`, `tags_create`, `actions_correct`,
  `actions_withdraw`, `actions_comment` and `tasks_follow` explicit role defaults; today they are denied by default.
* Transport allowlists do not yet carry the new Action reads/writes: `owner_sessions.READ_OPERATIONS`, the
  `root/operations` route list in `transport/http.py` and `transport/mcp_presentation.py` name `actions_thread_get`
  nowhere. Add them with the Chat/UI integration (the owner cannot `actions_comment`).
* Generated schema: the `global_chat` tables (`composition/schema_ddl.py`, group `global-chat`, and
  `qorqut/global_chat`, `reply_notifications.py`) are intentionally NOT removed on this isolated branch; their exact
  deletion belongs to the approved combined feature integration after P2. Full Story removal is P6.
* `ContextQueueOperations` is built without a content cap (`max_content_bytes=None`); the Context piece supplies its limit.
* Dashboard repository: `biz-dashboard/js/actions.js` still calls `actions_update` and `task-snooze.js` reads
  `task.snooze.indefinite`; both need the new read models (`actions_correct`, `snoozed_until`). The compatibility
  transport (`/log`, `/log/<id>/labels`) already maps onto the Action operations.
* The "Filed context upload" Action producer in `domains/documents/operations.py` is removed; Context filing is P4/P5's.
* Story `record_task` events are no longer produced by the Task flow (full Story deletion is P6); setup keeps its
  `part_of` edges and has no `parent_id` migration.
* `composition/tables.py` is hand-maintained; the retired specs were removed by hand.
* `maintenance/single_agent` remains a conversion tool for the v1 schema (data migration is a later phase); it only lost
  its import of the removed Task configuration.
* About 399 retained legacy host tests are not migrated (they import `qorqut.contracts`); they were not run.
