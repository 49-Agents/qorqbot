# Persistent workflows

`layer.workflows` implements explicit progress through an authored workflow.
It calls no model, infers no values and schedules no background work. A caller
starts ready steps and submits completion evidence. The host retains identity,
authorization, confirmation and execution policy.

Use the public, lazy cached facade on an explicitly opened layer:

```python
from qorqut_agentic import AgenticLayer

layer = AgenticLayer.open("/absolute/scratch/authored", "/absolute/scratch/runtime")
workflows = layer.workflows
```

Every method below is also a JSON CLI operation prefixed with `workflows.`.
For example, after explicitly importing the synthetic example package:

```sh
printf '%s\n' '{"operation":"workflows.start","arguments":{"ref":"example-project-workflow/landing-page","actor":{"actor_id":"example-owner","actor_kind":"owner"},"operation_id":"example-start"}}' |
  qorqut-agentic --root /absolute/scratch/authored --state /absolute/scratch/runtime
```

Supply `--runtime-config /absolute/scratch/runtime.json` to register a trusted
standalone runtime when a step needs verification; see [interfaces](INTERFACES.md).
Each command opens the same durable store. `workflows.get` retrieves progress
across processes, and retrying an operation ID uses its saved result.

## Public calls

```python
start(ref, actor, arguments=None, selection=None, operation_id=None)
get(run_id)
list(state=None, limit=100, offset=0)
supply_arguments(run_id, arguments, actor, operation_id)
start_step(run_id, step_id, actor, operation_id)
complete_step(run_id, step_id, actor, operation_id,
              completion_note="", output=None)
pause(run_id, actor, operation_id)
resume(run_id, actor, operation_id)
cancel(run_id, actor, operation_id)
```

`actor` has the tool API's `{actor_id, actor_kind}` shape. Successful mutators return
`{run, operation_id}`, and `get` returns `{run}`. `list` returns `{runs, total}`;
its state filter accepts the states below. `limit` is 1–1000 and `offset` is
nonnegative. Start operation IDs are generated when omitted. Supply an explicit
ID when start retry protection is needed. Start IDs have a store-wide namespace;
other operation IDs share a namespace within their run.

Each operation fingerprints its name, actor and request. Reusing an ID for a
different request fails with `operation_conflict`. Matching retries return the
stored result, which is a view at that operation's completion, not the latest
state. Use `get` for current progress. A concurrent retry while verification is
still in flight returns its current view without dispatching a duplicate tool.

## Arguments and readiness

Supplied arguments map names to records:

```json
{
  "product_name": {"value": "Example", "source": "owner-reply"},
  "audience": {"value": "Designers", "source": "owner-reply", "confirmed": true},
  "primary_keyword": {"value": "example", "source": "brief", "note": "Derived from the accepted brief"}
}
```

Unknown fields and argument names are rejected. Values must match their authored
JSON Schemas. Sources are nonempty attribution strings, not authorization.

| Acquisition | Behavior |
| --- | --- |
| `default` | Fill the validated default unless explicitly supplied; source is `default`, confirmation is false. |
| `ask_before_start` | Request the missing value before any step is ready. |
| `ask_at_step` | Request the value when the owning step's dependencies complete. |
| `confirm_default` | Propose the authored default; supplied records require literal `confirmed: true`. |
| `derive` | Require a supplied value with a nonempty derivation note. No inference runs inside the library. |

Missing global values gate all steps. Missing local values gate their owning
step, leaving independent ready steps available. `ask_before_start` always gates
initial progress even when its definition names an owning step. Global requests
appear immediately; local requests appear when the step's dependencies are done.

Starting a step or reserving its first completion attempt atomically locks all
resolved global arguments and that step's local values. Arguments owned by other
steps remain editable until consumed. A changed locked value is `state_conflict`.
An identical canonical JSON value is a no-op preserving its original provenance,
confirmation and timestamp. JSON booleans differ from numbers. Failed verification
does not unlock consumed input; correcting it requires a new run.

The library records explicit confirmation but does not verify who supplied it.
The host must establish whether a response actually came from the owner.

## Run views, snapshots and controls

A view contains `run_id`, `workflow_ref`, `workflow_revision`, `state`, `actor`,
timestamps, argument records, `steps`, `ready_steps`, `argument_requests`, and
ordered durable `events`. Each ready step includes `step_id`, frozen instructions
and current argument values. A step exposes pending/active/completed state,
verification state, instructions and reference revisions, completion note/output,
start/completion attribution and all attempts.

| Run state | Meaning |
| --- | --- |
| `waiting_arguments` | Global or before-start values are unresolved. |
| `running` | Explicit step progress is allowed. Some local arguments may still be missing. |
| `paused` | No new arguments, steps or completions are accepted. |
| `completed` | Every step completed. |
| `cancelled` | Terminal cancellation; further progress is rejected. |

A run snapshots its workflow definition and referenced instruction bodies and
revisions at start. Later catalog edits affect future runs. Verifier definition
and code/resource revisions are pinned too: changed verification code yields a
blocked attempt with `revision_conflict`, rather than silently running new code.
Invalid dependencies, cycles and references prevent start. SQLite transactions
serialize competing argument changes and step progress; all state survives reopen.

`start_step` requires pending state and readiness. `complete_step` accepts a
ready pending or active step. Dependencies are satisfied only by completed steps.
Ready steps can proceed independently. `pause` requires a running/waiting run,
`resume` requires pause, and `cancel` accepts running/waiting/paused runs.

## Completion and verification

With no configured verifier, completion is **declared**, never verified. With a
verifier, an attempt is persisted before calling the tool with:

```json
{"run_id":"...", "step_id":"...", "arguments":{}, "output":{}, "completion_note":"..."}
```

Only `{passed: true, feedback: string}` can complete a verified step. A false
verdict stores feedback and leaves the step incomplete. Tool/schema errors,
missing tools/runtimes, revision conflicts, unusable/deep output, crashes and
timeouts leave it blocked. Pending authoring recovery prevents new verifier
dispatch and is recorded as the `recovery_required` underlying cause.
No failure falls back to declared completion. A blocked completion first commits
the incomplete step and its attempt, then raises `VerificationBlockedError`
(`code: verification_blocked`) outside the state transaction. Its details are:

```text
{run: <persisted run view>, operation_id: <caller ID>,
 reason: <underlying code>, attempt_recorded: true}
```

The underlying cause and receipt remain in attempt `status`, `feedback`,
`block_reason` and `details`. A valid false verdict returns an ordinary result
with failed verification; it is not an infrastructure error. The JSON CLI can
use its ordinary error envelope for `verification_blocked`.

Attempts contain their operation and invocation IDs, actor, submitted note and
output, timestamps, verdict and bounded receipt diagnostics. The deterministic
invocation ID lets an interrupted completion reuse its stored tool receipt after
reopen. In-flight receipts remain pending; indeterminate or reconciled receipts
cannot imply successful verification. Reconcile effects through the tool API,
then explicitly use a new completion operation ID for another attempt. Retrying
the old finalized operation raises the same saved exception, code and details.

A pause or cancel during verification is detected using the recorded control-event
generation, including a pause followed by resume before the tool returns. The
actual verdict and receipt remain in the attempt, but it is blocked with
`control_changed`, the step stays incomplete, and `verification_blocked` is raised
with that underlying reason. Current control state is never
overwritten. After resume, a new completion ID can verify again. Cancellation is
terminal. A pause cannot undo a verifier's already executed external effects.

## Plan runs

A plan run has no catalog entry. The host hands over an ordered list of step
instructions and the run is stored like any other, with `workflow_ref` set to
`task-plan` (no slash, so it can never name a catalog entry):

- `start_plan(steps, actor, origin, operation_id)` starts it. Steps get the IDs
  `s1`, `s2`, ... and each depends on the one before. `origin` names the host
  record the plan belongs to and is only stored. The operation ID is the host's
  key for the run: the same steps under the same ID return the same run.
- `plan_run(operation_id)` returns that run as it stands now, or `None` before
  it was started. It only reads.
- `add_step(run_id, instructions, actor, operation_id, after=None)` adds one
  step to a running plan, right after `after` (default: after the last step).
  The step that followed `after` then follows the new one, so it must still be
  pending. Step IDs are never reused.
- `skip_step(run_id, step_id, actor, operation_id, reason)` closes an unfinished
  step without doing it. The step's state becomes `skipped`, with the reason as
  its completion note and the actor as who closed it.

A saved workflow can serve as a template for a plan. `plan_template(ref, selection=None)`
returns its step instructions in an order that respects their dependencies, as
`{ref, revision, steps}`. It only reads: the host copies the steps into its own
record and starts a plan run from them, so the plan no longer depends on the
workflow. A workflow that takes arguments or has a step with a verifier is
refused, because a plan carries neither.

A skipped step counts as finished: steps that depend on it become ready, and a
run whose steps are all completed or skipped is completed. `add_step` and
`skip_step` are refused for a run started from a catalog entry, and for a run
that is not running. A plan has at most 50 steps of at most 4000 characters.
These five calls are not part of the JSON CLI; the host decides who may use them.

In Qorqut a delegated Task carries its plan and the host starts the plan run on
the first step action: see `qorqut-code/app/qorqut/agents/task_plans.py`.

See the synthetic [example walkthrough](../examples/packages/example-project-workflow/README.md)
for a complete sequence. The standalone facade and JSON CLI exercise this same
runner. The authorized Qorqut execution adapter remains separate host work.
