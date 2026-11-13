# Worker read parity

Workers read what Ordubashy reads; only writes need a grant.

Every active global worker (role `global-worker`) reads everything the configured Ordubashy's pinned role allows by
default, with no per-worker setup. Write access does not change. Source changes do not change live role pins, profiles or
deployment.

## The rule

`qorqut.agents.read_parity` computes a worker's effective defaults, and the agent resolver uses them in all three places
role defaults are read: the policy basis the engine enforces at call time, effective-configuration discovery
(`agent_config_get`, `permissions_get`, the MCP tool list) and orientation. Discovery and enforcement therefore cannot
disagree.

- Start from the worker role's own `default_tool_policy`.
- Add `allow` for each operation that the configured Ordubashy's pinned **role** allows, that the worker role does not name,
  whose `effect_kind` is `read` and that is not owner-only. Discovery reports these rows with
  `source: {"kind": "worker_read_parity"}`.
- Ordubashy's own profile overrides are not mirrored, and `PARITY_EXCLUDED` keeps four reads out on purpose:
  `environment_get` (returns secrets), `request_user_input` (asks the owner directly), `owner_conversation_get` (the private
  owner and Ordubashy conversation) and `attention_workflow_context` (signs Ordubashy's own session workflow binding).
- No write is added or changed. A few host rules decide a non-read from a read grant (`actions_follow` follows `actions_get`,
  `tables_follow` follows `tables_get`); each such operation keeps its previous mode, pinned as a worker default. If a
  worker's explicit override would let an added read lift a non-read, that read is left out for that worker.
- The result sits below the worker's explicit overrides, so an explicit `deny` still wins.
- With no configured, active Ordubashy (or its role artifact missing), nothing is added.
- Other roles are unchanged.

A worker's composed configuration depends on the Ordubashy role pin it mirrors: the pin is hashed and signed with the worker's
own artifacts, so adopting a new Ordubashy role refreshes the workers' configuration.

## Capability gates on reads

A Global read that needs a write-side capability declares `global_read_capability_alternatives={"read_global_context"}`, which
every worker holds by default. Mutations never accept an alternative.

| Reads | Primary capability (still required for changes) |
| --- | --- |
| `fastmail_*` reads | `manage_fastmail` |
| `calendar_calendars_list`, `calendar_events_list`, `calendar_event_get` | `manage_calendar` |
| `routines_list`, `routines_get`, `routines_runs`, `routines_costs`, `routines_preview`, `jobs_get` | `manage_global_routines` |

Mail and calendar handlers keep their identity rule: the installation owner, the configured Ordubashy or an active global
worker. Another role holding `read_global_context` (Learning Applier) is still refused, and discovery does not offer it these
tools.

A worker's Global table reads (`tables_list`, `tables_get`, `tables_rows_list`, `tables_history`, `crm_entries_list`,
`tables_thread_get`) follow the same decision as its business ones. Global table writes still need an explicit grant.

## Refusals name the missing grant

- A capability refusal from the engine is `forbidden` with the message `Global capability is not granted: <operation> requires
  <capability>` and `details.denial_reason = "capability_not_granted"`, `details.missing_capability`.
- Discovery reports `denial_reason: "capability_not_granted"` and `missing_capability` instead of `policy_denied` for an
  operation the engine would refuse for a missing capability (a Global-only operation, a private-account tool, or an agent
  administration tool).
- A profile edit (`agents_update`, `permissions_set`, `plugins_enable`, `plugins_disable`) is still accepted when it saves an
  `allow` or `signoff` override that a capability or plugin gate makes inert; its result lists each such override under
  `inert_overrides` with the missing capability or plugin.

## Security

Workers browse the web and read outside content. With parity, a hijacked worker can read the owner's private mail and
calendars and every Project's records. The owner accepted this. Unchanged: no write is widened, owner-only operations stay
refused, explicit `deny` overrides win, and the secret-bearing `environment_get` is not mirrored.
