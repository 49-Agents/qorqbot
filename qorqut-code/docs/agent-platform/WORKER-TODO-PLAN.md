# Worker Todo implementation plan

Status: first implementation pass complete on branch `codex/worker-todo`, from base
`b91d455` on `origin/main`. The design below is unchanged; see Implementation status
for what is done, what is verified and what remains outstanding.

## Implementation status

Implemented and verified in this branch (local commit, not published):

1. **Board identity and access** — `qorqut-code/app/bazarlyk/agents/attention.py`
   resolves the acting principal separately from the selected board, reuses
   `is_worker`/`require_worker_manager`, keeps revisions, cursors and idempotency
   attached to the selected board, permits owner/Ordubashy reads and membership
   changes on worker boards, leaves native focus changes to the board's own agent,
   and authorizes referenced Tasks for both the caller and the target worker. The
   existing `agent_attention` row is the enrollment marker (empty reads enroll
   nobody; the first explicit addition creates it through the existing writer;
   removing the last item keeps it). `task_recipients` routes a Task change to every
   authorized board that contains it.
2. **Task events and reminders** — `agents/attention_delivery.py` reconciles and
   delivers per enrolled board, `agents/attention.py` rechecks eligibility per board
   (active registration, current binding, pause/stop, grants), and
   `agents/activity.py`, `native_chat/captures.py`, `native_chat/arbiter.py` and
   `native_chat/nudges.py` use the same board predicate. The pure Agentic timer
   policy is untouched. Focused workers still receive manager messages and related
   task changes.
3. **Current work and instructions** — an adopted worker's current work is projected
   from its Todo focus and Intent (`agents/activity.py`); `agent_work_update`
   returns an actionable Todo/focus instruction; completion checks read the same
   source with the same predicate (`agents/checkpoints.py`) and no longer demand a
   repeated maintenance write. The global-worker role grants, `BRIEF.md`, the shared
   role procedure and `/role` skill suppliers, and the runtime/tool instruction
   entries now describe the adopted board; `qorqut-code/docs/AGENT-WORK-COMPACT.md`
   documents the projection.
4. **Shared dashboard** — `biz-dashboard/js/agentpane.js` offers the existing Todo
   view to permanent workers and mounts the shared board for them;
   `js/attention-board.js` restores the shared worker control on Todo rows (an empty
   assignment stays visible on interaction) and sends the displayed board's
   `agent_id` on membership removal; `css/agent-focus.css` restores that control's
   row styling.

Verification (all synthetic, isolated; no live installation, session, preview
service or task record was touched). Private evidence is kept outside this
publication repository and is available to the Manager:

- 30 synthetic Python acceptance checks over the real attention schema and the real
  service/clock code (`test_board_access.py` 19, `test_delivery_and_activity.py` 11).
- 6 instruction/grant consistency checks (`catalog_check.py`).
- Browser check of the shared board in an isolated fixture (`browser_check.js`,
  screenshot `todo-worker-board.png`) and the agent-pane wiring check
  (`agentpane_check.js`); the published `attention-board-owner-toggle.test.js`
  (2 tests after restoring the shared control) passes.
- Existing focused checks: `app/tests/test_ordubashy_any_runtime.py` 7 tests,
  `app/tests` 261 tests with only pre-existing environment failures (missing
  `psutil`, a real `claude` binary installed), and the related dashboard suites
  (`focus-attention` 18, `signoff-attention-pill` 5, `focus-expansion` 12).
- Exact commands and outputs, plus the reproduction transcript, are in that
  private report.

Outstanding prerequisite — **not** satisfied by this pass and not claimed as
recovery: a board reminder whose automatic chat attempt ends `uncertain` with
`paste_unverified` stays quarantined (correctly, without a blind retry) and its
reminder sequence never advances, so no later board reminder is produced for that
focus generation. Reproduced synthetically with the unmodified base code and again
on this branch (`repro_reminder_uncertainty.py`); see the report for the observed
receipts. No automatic recovery, replay or manual live-input handling is added here.
Recovery remains a separate focused change; the owner requested that the Manager
make the post-first-pass fixes. **Do not treat this prerequisite as met when
deciding on a rollout.**

Adoption is separate from this implementation: publishing, merging, refreshing a
running instance's instruction catalog and role policy, and starting an isolated
preview are not performed by this branch.

## Design

Give permanent workers the existing Todo board, one current focus with intent,
and the existing progress nudges. Ordubashy manages their commitments through
the same operations. Reuse the stored tasks, board membership, native sessions,
inbox delivery and dashboard component.

## First version

Eligible agents are the configured Ordubashy and active `global-worker` profiles.
Learning Applier, retired roles and temporary delegated identities retain their
existing work mechanisms. Existing tool grants and explicit denies still apply.

The owner and Ordubashy can inspect and curate a worker's board. Workers can read
and maintain only their own board and declare their own focus. A worker chooses
one actionable item from the work Ordubashy has authorized; questions and verified
completion reports use the existing messages to Ordubashy.

The same task may appear on Ordubashy's board and a worker's board. Status,
Workers and task history are shared; focus and intent belong to each agent.
Separate deliverables use existing child tasks. Completing a child does not
complete its parent. Removing board membership releases only that board's
commitment and matching focus, with existing protected-removal rules retained.

Queue assignment remains explicit: Ordubashy adds an existing task to the target
board and sends the assignment through existing messaging. Changing Workers alone
does not dispatch work or synchronize board membership. This version adds no
queue priority field, automatic reassignment, parent completion calculation,
new scheduler, or worker workflow synchronization. Worker focus initially targets
the task itself. Ordubashy's broader focus types continue to work.

## Reuse and implementation order

### 1 Board identity and access

Reuse `agents/worker_access.py:is_worker` and `require_worker_manager` rather than
introducing another worker-management permission model. In `agents/attention.py`,
resolve the acting principal separately from the board's target agent.

- A worker's omitted `agent_id` selects itself; it cannot select another agent.
- Ordubashy's omitted target selects his own board; an explicit worker target
  is authorized through the existing manager helper.
- Preserve the owner's existing omitted-target default of Ordubashy.
- Permit manager reads and membership changes on worker boards. Native focus
  changes remain declarations by the board's own agent in this first version.
- Check the actor's native registration and session as the actor. Management
  access must not require pretending to be the worker or a running worker session.
- Authorize both the caller and the target worker for referenced tasks. Board
  membership grants no additional task, project or conversation access.
- Keep revisions, pagination cursors and idempotency attached to the selected
  board. Preserve `attention_*` operations and their `todo_*` aliases.

Existing tables already include `agent_id`, including the membership uniqueness
indexes. No new table or schema migration is expected for this scope.

Use a worker's existing `agent_attention` row as the enrollment marker: the first
explicit board addition creates it through the existing writer. An empty-board
read does not enroll a worker. Activity sampling and reminder reconciliation must
not create state for workers who have never enrolled. Removing the last item
keeps the state row and the worker's adopted Todo behavior.

### 2 Task events and reminders

In `agents/attention.py:task_recipients`, route task changes to authorized agents
whose boards contain that task, using the existing inbox event and deduplication
machinery. Keep private owner conversations private.

Generalize `agents/attention_delivery.py` reconciliation and eligibility to each
eligible board. The existing native nudge pass already visits agent profiles;
reuse it and the current hook/chat claim and receipt handling.

Use the unchanged pure timer policy in Agentic's `behavior/attention_clock.py`:
10-minute focused checks, 15-minute unfocused checks for open/working tasks,
four-hour reassessment for blocked/waiting/input-only boards, and the existing
30-minute owner-message buffer. Preserve the current hook-to-chat fallback.
Workflows and their detachment timing remain Ordubashy's existing behavior.

Update the coordinator-specific activity gates in `agents/activity.py`,
`native_chat/captures.py` and `native_chat/arbiter.py`. Only genuine activity and
actual owner messages advance those watermarks. Ordinary tool use does not reset
the focused deadline. Reuse `native_chat/nudges.py` focus-related inbox filtering;
manager assignments and decisions must still reach a focused worker. An explicit
switch or pause takes precedence; unrelated updates do not silently change focus.

Every delivery must validate the target's current native session and binding.
Paused, stopped, inactive or unauthorized agents receive no nudge. A reminder
does not launch a CLI. Each agent's sequence and claims remain independent.

### 3 Current work and instructions

Workers currently maintain `agent_current_work`, which is also read by
`agents/activity.py` and `agents/checkpoints.py`. For workers adopting Todo, derive
the current-work display from their focus, intent and authorized task instead of
requiring a second authored list. Update completion checks to review that source
and meaningful task progress without forcing repeated maintenance turns.

Preserve runtime presence as observed state: a stored focus does not mean the
agent is currently executing. Workers without an attention state row continue
using their existing current-work behavior. Once enrolled, activity reads project
Todo focus and legacy `agent_work_update` writes return an actionable instruction
to use Todo and focus operations. Retain the old record for recovery; clearing
focus must not fall back to displaying that old assignment. Completion checks
select the same source using the same enrollment predicate.

Update Agentic's global-worker `role.yaml` and `BRIEF.md`, the active shared role
procedure/workflow suppliers under `operations/source-instructions`, and relevant
runtime/tool instruction resources. Use the existing instruction catalog and
preparation paths. Workers need the board and task-focus grants; avoid granting
unrelated coordinator capabilities or workflow tools.

### 4 Shared dashboard

Enable the existing Todo view for permanent workers in `biz-dashboard/js/agentpane.js`.
Reuse `attention-board.js`, its pagination, cache, owner filter, task-state controls
and record panel. Include the selected board's `agent_id` on membership mutations;
the current remove request omits it and would otherwise select the owner's default
Ordubashy board.

Restore `taskWorkerMount` in the Todo row renderer. The current API already returns
`workers` and `worker_ids`, but the renderer omits the mount call. Keep the existing
worker picker and make empty assignment visible. Verify the record's private chat
still opens with Ordubashy even when entered from a worker's Todo.

Owners can open each worker's existing agent pane to inspect its Todo. A dedicated
team overview or nested-board UI is outside this first version.

## Acceptance checks

Use synthetic tasks, agents and sessions. Extend focused tests around these
observable behaviors:

1. Two workers have independent boards, focus, revisions and reminder sequences.
   An owner or Ordubashy can curate either board while workers cannot inspect or
   mutate peers. Explicit denies and source-access revocation still take effect.
2. Adding one task to both manager and worker boards creates no duplicate task.
   Its status updates in both views. Removing one membership preserves the other
   membership, task and history. Child completion leaves the parent independent.
3. Owner UI removal targets the displayed worker board. Worker identities render
   on assigned rows, empty assignment is visible, and the owner filter still works.
   Opening a task preserves its canonical record and private conversation.
4. Synthetic time exercises focused, idle, blocked-only and empty boards, owner
   message buffers, intent edits and switching focus. Activity on one worker never
   postpones another worker's reminder.
5. Claude Code and Codex delivery use the correct agent/session. Stale bindings,
   pause/stop, role removal and permission revocation prevent delivery. Repeated
   passes and receipt retries do not duplicate an occurrence.
6. Focused workers receive relevant task changes and manager directives. Unrelated
   deferred events remain available when focus clears. Private owner messages are
   not exposed through worker-board membership.
7. Adopted worker activity and completion review use Todo focus without demanding
   a second `agent_work_update`. Existing coordinator behavior and unadopted-client
   compatibility remain covered.

The existing coordinator identity/runtime suite passed on the base: seven tests.
That is baseline evidence only; it does not validate the proposed worker behavior.
Use the repository's existing build checks and isolated runtime/browser fixtures
for the changed components, preserving its publication rules for private fixtures.

## Reminder delivery prerequisite

The running preview exposed a current board reminder in `uncertain` after
`paste_unverified`. Reconciliation preserves that occurrence while normal sending
selects only `pending` rows. Reproduce this with synthetic delivery receipts before
extending the rollout. Fix recovery as a separate focused change, or incorporate
an existing verified fix; never turn an uncertain input into a blind retry.
The worker-board work should not expand into a general native transport rewrite.

## Adoption

Build and verify an isolated preview first. Prepare matching worker role grants
and instructions through the canonical catalog/preparation path. A first worker
can be seeded explicitly with its existing authorized task, preserving its prior
current-work record for recovery. Text-only legacy assignments need reconciliation
into a real task before they become task-focused work.

Changing source alone does not activate new worker behavior. Adoption must verify
the loaded instructions, tool grants, native hook delivery and current-work
projection together. Publishing, merging and changing the running installations
are separate from this implementation preparation.
