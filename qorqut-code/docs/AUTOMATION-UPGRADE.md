# Scripting and automation: selection, offline upgrade and release notes

Scripting and automation is the optional `qorqut-automation` distribution. Selecting it is a source-only, trusted-installation
decision; no request, agent or role package can enable it.

## Select the extension

Add one entry to the installation's `extensions` list and install the distribution into the host environment:

```json
{"factory": "qorqut_automation.extension:create",
 "config": {"python": "/absolute/path/to/python3", "state_dir": "/absolute/private/automation-state",
            "invocation_seconds": 60, "max_run_seconds": 3600, "max_calls": 2000, "max_step_bytes": 262144}}
```

`python` is the interpreter that runs scripts; `state_dir` holds the immutable run snapshots and the retained script copies and is owned by the service user. Every key is
optional. `invocation_seconds` bounds one script invocation (waits excluded; longer work continues with `run.continue_run`),
`max_run_seconds` the run's total active time, and the other limits are defaults that a single automation may only tighten with its
own restrictions. Prepare the distribution with `tools/prepare-local.py --with-automation` (it only installs the package into the host
environment; its authored plugin ships inside the wheel as `qorqut_automation/plugins/scripting-and-automation/`).

The extension depends on the Operations engine worker's lane contract (`Program` admission, polling and cancellation settlement through
the optional `Program.cancel`, the `automation` job checkpoint) and on the `Extension.background_tick()` hook; both must be present in the release that selects it.

## Offline schema upgrade

The extension owns exactly two tables, `automations` and `automation_steps`, with their indexes (the upgrade creates whatever
`qorqut_automation.schema.SCHEMA` declares: nullable `business_id` on both for Project scope, the frozen `admitted_json`, the `grant`
step kind, `log_expired_at` and the retention index). A store prepared without them is
refused when serving with the extension selected; the runtime never migrates. The core composition also widens the `source_type`
CHECK of `owner_conversations` to accept `automation` (the owner-Ordubashy discussion of an Automation), exactly as it once did for
`project`; the same command rebuilds that one table with every row kept. Upgrade a **stopped** store into a **new** file:

```
python -m bazarlyk.migration.automation_upgrade --source OLD.sqlite3 --destination NEW.sqlite3 \
    --installation-config CONFIG.json [--dry-run]
```

The command follows the same discipline as the CRM upgrade ([CRM upgrade](CRM-UPGRADE.md)): the source must have no `-wal`,
`-shm` or hot journal and must equal the composed target of `CONFIG.json` without the automation entry; the source is copied,
the two tables and the new identity are added (and `owner_conversations` rebuilt when it still has the previous CHECK) in one
transaction, every pre-existing row is compared in both directions, the new tables must be empty, foreign keys and integrity are checked, the engine opens the result, and the file is published by an
exclusive hard link so an existing path is never replaced. The source is hashed before and after. Nothing is run by the service
and nothing was run on any installation when this source was prepared. Recovery is the owner's retained source file; the tool
never restores it.

## Release steps (owner decisions, in order)

1. Stop the app; keep the old store. Run the upgrade with `--dry-run`, then for real into a new path; select the new store and the
   new release together.
2. Add `manage_automations` to Ordubashy's profile (the role package's `default_tool_policy` names the tools; the capability is
   an explicit Global grant and is barred from worker sessions). No other role receives it.
3. The authored plugin (`qorqut_automation/plugins/scripting-and-automation/`: skills `automations` and `script-authoring`, and the
   script template) is seeded into the Agentic store by the extension the first time an automation script is written, and only
   when absent; an existing, possibly edited plugin is never overwritten. Ordubashy writes each automation's own Python entry with
   `automations_script_write` (its narrow Agentic authoring path; it never uses `agentic_change_propose` for these scripts).
4. After start, Ordubashy creates automations with `automations_create`, schedules them with `automations_schedule_set` (an ordinary
   routine bound to the automation in the same step), and receives every distinct failure as a durable required inbox event even when
   unsubscribed (the owner is not notified by the extension; Ordubashy decides). Persistent grants and run-only grants are owner
   Signoffs.
5. The background tick settles the unresolved external steps of ended runs against their canonical records and clears closed ordinary
   step debug logs older than 30 days, recording the expiry. Retained script copies, run snapshots, scripts, results, errors and wait
   history are kept; nothing retained is pruned automatically.
