# qorqut-automation

Optional Qorqut extension: Ordubashy-owned scripted automations that run as Ordubashy's *current* authority, with durable steps,
owner Signoffs for actions and questions, and failure delivery to Ordubashy.

Selected only by the trusted installation (see the host's `docs/EXTENSIONS.md` and `docs/AUTOMATION-UPGRADE.md`); prepared with the
host's `tools/prepare-local.py --with-automation`:

```json
{"factory": "qorqut_automation.extension:create",
 "config": {"python": "/abs/python3", "state_dir": "/abs/private/automation-state", "invocation_seconds": 60}}
```

## What it owns

- Two tables: `automations` (stable scoped record, saved phases, grants, restrictions, saved cross-run state, the frozen admission) and
  `automation_steps` (the durable step/attempt ledger). Both carry a nullable `business_id`: Global and Project automations share
  the tools, and a Project automation sees only its Project. Requires the offline upgrade `python -m bazarlyk.migration.automation_upgrade`.
- Reads (Global or Project scope, plain reads also open to other authorized agents with a safe projection): `automations_list`,
  `automations_get`, `automations_history` (runs, published outputs, and for the manager one run's steps; paged).
  Manager-only: `automations_script_get`.
- Management (`manage_automations`, Ordubashy and the owner): `automations_create`, `automations_update`, `automations_script_write`
  (the narrow Agentic authoring path for this plugin's per-automation Python entries), `automations_schedule_set` (an ordinary routine
  bound in the same step), `automations_pause/resume/archive`, `automations_run`, `automations_run_cancel`,
  `automations_input_answer` (answers a question the script asked the managing agent, bound to the exact proposal), and
  `automations_grant_revoke`. Owner Signoffs: `automations_grant_add`, `automations_run_with_grants`.
- Program `automation.run` (one durable job per logical run; routines are the schedule authority) and the `automation` inbox source.
- The run-bound Python SDK (`qorqut_automation_sdk`, stdlib only) and the host bridge. A script is an authored Agentic tool entry
  of the `scripting-and-automation` plugin, frozen into an immutable snapshot at admission and verified by digest on every start.
- A background tick (`Extension.background_tick`, run by the host's one background service; the extension starts no thread of its own)
  that reconciles missing failure deliveries, settles the unresolved external steps of ended runs against their canonical records and
  clears ordinary closed step debug logs older than 30 days, recording the expiry. Retained script copies, run snapshots, scripts,
  results, errors and wait history are kept: only ordinary closed debug logs ever expire.

## Authority model

A script call becomes an invocation of the *current* Ordubashy principal bound to one run (`InvocationContext.automation_run_id`).
The host policy gate (`Guard.gate`, bound once) denies forbidden operations (`automations_*`, `agentic_change_*`, `management_*`,
`permissions_*`, `profile_*`, owner-private sends), applies the automation's restrictions (an explicit empty `allowed_operations` means
none), forces a full action Signoff for action operations and only turns an eligible Signoff into an allow when an owner grant
(persistent, or run-only for an already-waiting run, bound to the exact Signoff and rechecked at every use) matches exactly. Email and
message approved-payload editing, no-second-send and Signoff supersession lineage are honoured: the run waits for the action's
real outcome and the Fastmail and WhatsApp send programs recheck the run's authority immediately before dispatch. The script can never
approve itself.

## Durability

Each `call` / `ask_owner` / `ask_agent` / `publish` / `phase` is a step keyed by `(run, step_key, attempt)`. Replays return finished
steps from the ledger; an ambiguous outcome is rechecked (at most five times, then a visible recovery-required wait and a notice to
Ordubashy) against durable effect evidence, never re-sent blindly; transient failures retry at most twice with attempt lineage;
`publish` writes the inbox event and advances the saved cursor in one transaction (publication-before-cursor), keeping a bounded
complete output in the step. A call is finished only by an actual completed result: one that queues a provider action - an explicit
accepted job, or the result of a queue-returning operation such as a non-send Fastmail mutation or a WhatsApp send, whose job carries the
operation's own run marker - waits on that exact job, and a proposal waits on its exact Signoff; the live bridge and the cancellation
recovery read the engine's committed receipt the same way. One invocation of a script is bounded (60 seconds by default); longer work saves JSON progress and
continues explicitly (`run.continue_run`). Every distinct failure becomes one required delivery to Ordubashy, who decides what the
owner needs to hear, even when unsubscribed, and pauses only the affected automation.

Cancellation settles effects before it frees the automation. `automations_run_cancel` makes the run `cancel_requested`: no further
script call is admitted and the child is stopped, but the run stays unresolved - so the automation admits no successor, manual or
scheduled - until the program's `cancel` callback (registered on `automation.run`, run by the engine's automation lane under the run's
claim, with no child, compute slot or new call) has settled every external step from its canonical Signoff, job and receipt record: a
decision nobody gave is closed for this run (the gate refuses to apply it now), a queued or dispatching provider action stays tied to
its exact original job and receipt (also when the process stopped after the engine committed that job or proposal and before the run
saved its wait: the committed receipt is read, never re-invoked), and an unconfirmed outcome keeps the existing visible recovery wait and
required notice. Only then does the run become `cancelled`. A closed run that still holds an unresolved external step also blocks the next
run, and a bounded pass of the background tick keeps settling such steps: five at a time, the least recently examined first, so steps that
stay unresolved take turns and a wait that is not yet due is not visited early.

Snapshots survive first runs, edits and crashes. The admitted script version of a queued run is copied (never consumed) into an
immutable retained copy, and the run's own snapshot is built from it in a temporary directory and renamed into place; retaining and
staging of one run are serialized by an installation-scoped application claim keyed on that run, and a crash before the snapshot is
recorded on the job rebuilds the same version even after the authored script changed. A script edit asks to be repeated while a queued
run has not yet kept its version. Nothing retained is ever pruned automatically.

## Authored plugin and example

`qorqut_automation/plugins/scripting-and-automation/` is the authored Agentic plugin shipped in the wheel (skills `automations` and
`script-authoring`, template `script-template`); the extension seeds it into the Agentic store only when absent.
`examples/fastmail-monitor/` is a source-only quiet-baseline Fastmail monitor; it is never installed, enabled or run by this package.

No tests, builds or smoke checks were run when this source was written.
