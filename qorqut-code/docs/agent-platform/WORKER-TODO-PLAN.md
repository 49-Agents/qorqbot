# Worker Todo

Implemented on `codex/worker-todo`, based on `b91d455`. The existing Todo service,
state tables, native nudge loop and dashboard renderer now serve permanent workers.
No schema migration or separate worker scheduler is required.

## Implemented behavior

- Owner and Ordubashy can read and curate worker boards. Workers manage their own
  board and declare one exact Task focus with Intent. Actor and target permissions
  are checked separately; shared membership never grants source access.
- The first explicit Task addition enrolls a worker. Empty reads, activity and
  native hooks do not enroll it. Enrolled current work comes from Todo focus;
  unenrolled workers retain their existing one-item assignment.
- Existing timers and focus filtering apply independently to enrolled boards.
  Manager directives continue to arrive while a worker is focused.
- Worker agent panes use the shared Todo view. Rows display task assignees and a
  visible empty-assignment control. Removing a row targets the displayed board.
- Exact archived native receipts recover uncertain chat reminders without replay.
  The original transport history remains uncertain; only proven arrival advances
  the reminder sequence. Inputs without matching evidence remain quarantined.

## Verification

Synthetic checks cover board isolation, enrollment, current-work projection,
permissions and revocation, focus, timer independence, delivery and instructions.
Manager regressions cover exact Task focus, native-hook enrollment, target-specific
owner reads, source redaction, and marked transcript recovery on Claude Code and
Codex, including duplicate, wrong-session and incremental-capture cases.

Focused Python suites, dashboard checks and an isolated browser fixture pass.
The Agentic wheel builds with the updated role and instruction resources.
Private tests and execution evidence are retained outside the publication tree.
The broad first-pass app run also reported unrelated environment failures (missing
optional `psutil` and an installed `claude` affecting a missing-binary fixture).

Publishing, merging, live adoption and changes to running sessions are separate.
The design and adoption requirements follow.

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

Use the shared pure timer policy in Agentic's `behavior/attention_clock.py`:
5-minute focused checks, 3-minute unfocused checks for open/working tasks,
30-minute reassessment for blocked/waiting/input-only boards, and the existing
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

## Reminder delivery recovery

An uncertain chat attempt is never retyped. The existing bounded transcript scan
now also checks Todo reminder notices. Recovery requires the exact system marker
and complete body in the same agent, provider, conversation and transcript, with
a timestamp within the native session's lifetime and after the input was admitted.
The original uncertain transport receipt is retained, its body is scrubbed after
confirmation, and the attention occurrence consumes the receipt once. Existing
clock rules preserve later owner-message/activity deadlines and reject receipts
for an obsolete focus generation or sequence.

When no exact receipt exists, uncertainty remains quarantined. A paste still held
in the terminal requires the existing owner-authorized recovery procedure; source
changes do not submit it or fabricate proof of delivery. This behavior applies to
both Ordubashy and enrolled worker boards.

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
