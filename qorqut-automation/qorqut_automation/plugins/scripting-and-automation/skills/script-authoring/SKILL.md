---
name: scripting-and-automation-script-authoring
description: Write an automation script with the run SDK - durable step keys, phases, questions, invocation limits and published output.
---

# Writing an automation script

Write the script with `automations_script_write` (or `script_source` on `automations_create`); start from the `script-template`.
It runs in a separate process with the standard library plus `qorqut_automation_sdk`, which the host puts on its path. Include an
`input_schema` when `arguments` has a required shape, and keep the source small and readable: the owner may read it.

```python
import qorqut_automation_sdk as sdk

def work(run):
    run.phase("scan")                                    # a phase the automation declares
    found = run.call("tasks_list", {"limit": 50}, key="read-%s" % run.saved.get("cursor", "start"))
    tasks = found.get("tasks", [])
    for task in tasks:
        run.publish("task-%s" % task["id"], "Task: %s" % task.get("title", ""), data={"id": task["id"]})
    if tasks:
        run.save({"cursor": tasks[-1]["id"]})        # after everything it covers was published
    return {"seen": len(tasks)}

sdk.main(work)
```

## Rules that keep a run safe

1. **Deterministic step keys.** The script restarts from the top after a wait, a continuation or a crash; finished keys return their
   recorded result. Derive keys from saved cursors and indexes, never from time or randomness. A different request under an existing key
   is refused.
2. **Publish, then advance.** `publish(key, summary, advance=...)` writes the event and the cursor in one step. Do not advance a cursor
   with `save` for output that has not been published; advance it only after everything it covers has been published.
3. **Quiet when nothing changed.** Publish only when there is something new; an unchanged check ends without an event.
4. **Waits.** `ask_owner(key, form)`, `ask_agent(key, form)`, any approval-required call and any call that queues a provider action (an
   email change that does not send included) suspend the run until the answer or the real action outcome; do not poll or sleep for it. `form` needs `title`, `explanation`, `selection_mode` and, for choices, `choices` with
   `id`, `label`, `explanation`. A question for the owner is `owner_only` unless you set `response_policy: "management_allowed"`.
5. **Actions.** A send, reply or forward call returns only after the owner approved the exact action and it reported its outcome. Never
   work around that, never repeat it with a new key to "try again", and use `approval="required"` when an action must get a fresh
   approval even though a grant exists.
6. **Invocation ceiling.** One invocation lasts `run.invocation_seconds` (60 by default). Check `run.remaining()`; for longer work call
   `run.continue_run({...})` with bounded JSON and read it back as `run.progress`. Waiting time does not count.
7. **Failures.** Let an `AutomationError` end the run unless you can truly continue; the failure reaches the managing agent and pauses
   the automation. Do not catch and hide a refused call.
8. **Bounded work.** Page large results across steps (stable keys per page), keep saved state and step results small, and never
   embed secrets: the script has no credentials and every call uses the managing agent's policy.
9. **Declare limits.** The script entry is written with an explicit timeout, so long work needs `continue_run`, not a longer wait.

## Reading a result

`run.call` returns the recorded result of the operation. `run.input` is the frozen `arguments` of this run, `run.saved` the state
carried between runs, `run.progress` the continuation state of this run, `run.phase_name` the current phase.
