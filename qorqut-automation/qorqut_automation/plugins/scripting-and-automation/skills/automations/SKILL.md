---
name: scripting-and-automation-automations
description: Operate durable Python automations - create, author, schedule, run, pause, answer their questions, grant authority and read their failures and output.
---

# Automations

An automation is a stable, scoped record (`automations_*` tools) bound to one authored Python script. A run is a background job that
executes the script against your *current* authority. The script holds no credentials: every call it makes is one of your own tools,
decided by the same policy, with its own approval requirements. Only you (the configured Ordubashy) and the owner manage automations;
other agents may read an automation's published output and history, never its script, steps or pending questions.

Scope: pass `scope: "global"` or `scope: "business"` (the active Project). A Project automation sees only that Project's records.

## Create and author

- `automations_create` with `title`, optional `slug`, `description`, `arguments` (bounded input the script reads as `run.input`), saved
  `phases` and `restrictions`, plus **exactly one** of `script_source` (the complete Python source of this automation's own script) or
  `script_ref` (an existing Python tool entry of this plugin). You follow its output once; unsubscribing is respected.
- `automations_script_write` replaces or first-writes the script of an existing automation through Agentic authoring. Replacing needs
  `expected_code_sha256` from `automations_script_get`. A run that is already waiting or running keeps the exact version it was admitted
  with; a queued run has that version kept for it first (repeat the edit if it asks you to), and later runs use the edit. Each automation
  owns its script entry exclusively; `script_ref` can only name an entry no other automation uses. This is the only authoring path for
  these scripts; do not use `agentic_change_propose` for them. Read the writing guide first (`script-authoring`).
- `automations_update` edits the record; a run in flight keeps the script and input it was admitted with.

## Schedule and run

- `automations_schedule_set` (`schedule`, `timezone`, optional `status`) creates or changes the automation's own routine in one step; it is
  the only scheduler. Do not create routines for `automation.run` yourself. Pausing, archiving or an exhausted automation pauses it too.
- `automations_run` queues a manual run. An automation has at most **one** unresolved run; asking again returns the existing run.
  `automations_run_cancel` requests cancellation: no more script calls run, but the run is not finished (and the automation starts no
  new run) until every action it already queued or started is settled from its recorded Signoff, job and receipt - an action already
  dispatched is never undone, an unanswered question or undecided proposal is simply closed for that run, and an outcome that cannot be
  confirmed stays a visible recovery item you will be told about. Read the run with `automations_get` to see what it still waits for.
- `automations_pause` / `automations_resume` / `automations_archive` stop or restore only that automation and its schedule. A failed run
  pauses it; read the failure, fix the cause, then resume.

## Read

- `automations_list`, `automations_get` (schedule, waiting steps and pending questions for you), `automations_history` (`view`:
  `runs`, `outputs`, or with `run_id` the durable `steps`; paged), `automations_script_get` (script source and revisions).
- Subscribing to an automation (`source: "automation"`) delivers its published output; a subscription is not an access grant.

## Questions

A script can ask the owner (`ask_owner`) or ask you (`ask_agent`). A question for you arrives as a required inbox notice naming the
Signoff, its `proposal_revision`, `payload_hash` and `status_revision`; answer it with `automations_input_answer` using exactly those
values. A question the owner must answer cannot be answered by you or by the script.

## Authority and approvals

- Sending, replying, forwarding email and WhatsApp always need the owner's Signoff of the exact action. The run waits for the real
  outcome (never sends twice) and resumes after it; there is no grant that skips it, and the script cannot approve anything.
- Other approval-required operations can be pre-approved with `automations_grant_add` (persistent, one operation, optional capability
  and one resource - only for a call that names a single record by one required `<type>_id` argument, which the grant then covers
  exactly: a mutation touching anything more is not covered, and other shapes are refused) or `automations_run_with_grants` (an already-waiting run only). These are Signoffs for the exact grant; propose them,
  never assume them. `automations_grant_revoke` ends a grant at the script's next call. A script can ask for the operation's own
  full approval with `approval="required"` even when a grant would apply.
- A script cannot call `automations_*`, answer or approve Signoffs, change instructions or post to the owner's private conversation.
- `restrictions` narrow a script further: `allowed_operations` (an explicit empty list means none), `denied_operations`, `max_calls`,
  `max_run_seconds`. Names must be operations installed here.

## Failures

Every failed run and every failed step attempt reaches you as a required inbox event, even if you did not subscribe; it does not reach
the owner by itself. You decide what the owner needs to hear and when. Safe failures retry at most twice automatically; an unknown outcome
is rechecked (at most five times) and then waits visibly for your recovery decision. Read `automations_get` / `automations_history`,
do not rerun a send blindly, and tell the owner plainly when it matters.
