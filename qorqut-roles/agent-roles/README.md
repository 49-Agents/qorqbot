# Qorqut Agent Roles

Reusable role packages for Qorqut's Claude Code and Codex agents (v1.3 source). Projects
have no resident agents: global roles work with each project's files, resources, accounts
and integrations, across every current and future project without membership grants.
Project workspaces, operational ownership and history remain.

Development is the owner's by default, and the owner's direct instruction to a global is that global's
work; Qorqut engine and dashboard code are never edited by a role. See
[the v1.3 role sources](docs/V1.3-ROLES.md) for the full contract, what was removed and the
host dependencies.

| Role | Source version | Responsibilities |
|---|---|---|
| `roles/ordubashy/` | 1.74.0 | The general coordinator. Owner dialogue, plans and escalations; the only project creator, which the owner and workers ask the coordinator to do (`project-creation`); operations, email through the Email Management package and the native calendar (`calendar`); manages the permanent workers and selects their assignments. |
| `roles/global-worker/` | 1.7.1 | The general, repeatable permanent worker: one atomic assignment at a time, accurate shared status and Task, questions and completion to Ordubashy. Grants only its own work, its own reminders and Context reads; Ordubashy adds anything more. |
| `roles/learning-applier/` | 1.17.1 | Overseer extraction, owner-invoked review and application, with retained legacy learning history. |

Ordubashy 1.74.0, global-worker 1.7.1 and Learning Applier 1.17.1 remove the default guidance that depended on a specific provider or installation: the `service-provisioning` skill and its Cloudflare, Railway, Resend and OpenRouter references, the three `cloudflare_*` grants, the project IP, persona and proxy browser rules, and the Google-specific calendar setup. The Telegram owner-communication skill, its brief paragraph and the four `telegram_*` grants are no longer part of the default roles. The Telegram and Google calendar guidance is unchanged and kept under [`optional/`](optional/README.md), selected by no default role; a role manifest may grant only operations the host has registered, so the connector packages supply those grants. See [the publication cleanup](docs/V1.3-ROLES.md#publication-cleanup-ordubashy-1740-global-worker-171-learning-applier-1171). This source update does not publish or adopt any role.

Ordubashy 1.73.0, global-worker 1.7.0 and Learning Applier 1.17.0 select the one shared `owner-reminders` skill, now general reminder guidance (its path and id are kept for compatibility), and grant the host's `reminders_create`, `reminders_get`, `reminders_list`, `reminders_update`, `reminders_snooze`, `reminders_dismiss` and `reminders_cancel`. The grants are the same for every role: the host enforces that only the owner and the configured Ordubashy manage other recipients, while any other agent manages only reminders addressed to itself. Ordubashy keeps the legacy `owner_reminder` grant, an owner-targeted wrapper, and 1.73.0 builds on the current 1.72.3 source, retaining its browser-management grant (`agent_browser_manage`) and guidance. See [the reminder guidance](docs/V1.3-ROLES.md#shared-reminders-ordubashy-1730-global-worker-170-learning-applier-1170). This source update does not publish or adopt any role, and the host must register the operations first.

Learning Applier 1.16.0 grants none of the ten retired procedural-learning operations (the eight it still allowed are removed) and directs historical reads through the paginated read-only inventory and bounded review pages; its identity, Overseer grants and Context authority are unchanged. This source update does not publish or adopt the role.

Ordubashy 1.72.2 removes the retired System Dev report directions from the brief and `operations-delegation`: a technical blocker is recorded on the existing task with its concrete error, evidence and next actor, and `blocked`, `waiting` and Workers keep their existing meaning. No tool grants change; the removed agent-facing tools belong to the host's policy, not this repository. This source update does not publish or adopt the role.

Ordubashy 1.72.0 moves his commitments to the host's Attention board, separate from the owner's Pinned board: `attention_get`, `attention_add`, `attention_remove`, `attention_focus_set`, `attention_focus_clear` and `attention_workflow_update` are ordinary allows, `workers_list` reads the Worker candidates beside the existing `assignees_list`, and `attention_workflow_context` plus `attention_workflow_sync` let the native client keep exact workflow runs synchronized with the board as coordination reads and association writes only. The brief requires a board Task before work, keeps the precise focus and atomic Intent current, keeps Done visible until the owner removes it, and keeps Worker edits separate from delegation messages. Global workers and Learning Applier are unchanged. The host must register those operations before this source can be published; this source update does not publish or adopt the role, and it names no live adoption.

Ordubashy 1.70.0 and global-worker 1.6.0 add the host's CRM operations: Ordubashy holds `crm_entries_*`, `tables_thread_get`, `tables_comment`, `tables_follow`, `crm_activity_list` and `crm_activity_record`; the worker holds only `crm_activity_list` and `crm_activity_record`. Each is bounded by the existing Table or Task permission it builds on, so a missing or denied base grant still denies it. This source update does not publish or adopt either role.

Ordubashy 1.69.0 added scoped Context queue inspection/retry and a role-specific Context guide for source filing, queued graph edits and direct supervised edits. Global uploads require the companion host change that authorizes the active configured Ordubashy; existing policy and capability checks still apply. Direct edits preview the exact changes for owner approval. This source update does not publish or adopt the role.

Each role has a `role.yaml` manifest, a short `BRIEF.md` (at most 10,000 characters) and
selected procedures under `skills/`; manifest paths are relative to this repository, and
Qorqut publishes only the files a manifest selects. Changed package contents need a new
manifest version and explicit, owner-authorized publication and adoption; source edits
never change live pins, grants or sessions. Published versions are immutable: never
rebuild or republish an old role version from current source, whose shared skills have
changed in place.

## Skills

| Skill | Selected by |
|---|---|
| `action-recording` | Ordubashy, Learning Applier |
| `calendar` | Ordubashy |
| `business-operations`, `business-tables`, `context-relationships`, `ecosystem-review`, `operations-delegation`, `project-creation` | Ordubashy |
| `chat-references`, `owner-focus`, `owner-journal` | Ordubashy |
| `owner-reminders` (shared reminder guidance) | Ordubashy, `global-worker`, Learning Applier |
| `owner-input` | Ordubashy, Learning Applier |
| `managed-browser-control` (the `browser-control` skill) | Ordubashy, `global-worker` |
| `ordubashy-browser-control` (the `browser-control` skill) | Learning Applier |
| `learning-application` | Learning Applier |

Loading a skill grants no tool. Provider-specific guidance (Telegram, Google calendar setup) is
optional connector material under [`optional/`](optional/README.md) and is selected by no default
role. Content-creation production guidance is not part of this repository.

## Email and Calendar

Email guidance is not a role skill. The brief tells Ordubashy to consult the Email Management
package (`dependencies/email-management`) for all email work; that package holds the owner's
editable local instructions for accounts, provider access, senders and outgoing-mail
approval. The role no longer carries the Fastmail account, business-mailbox setup or mailbox-route
procedures or the `email_*`, `business_email_*`, `signoffs_email_approve` and `signoffs_platform_submit` defaults. Role text
grants nothing: the host's plugin defaults decide who holds Email Management and which tools exist.

The `calendar` skill covers the native `calendar_*` tools (their policies are unchanged) and the owner
Signoff (`signoffs_input_create`, read back with `signoffs_get`) before any change that notifies
guests, for any connected provider. The Signoff is an instruction to the agent, not a technical
interception. Provider connection setup is optional connector guidance (`optional/`).

## Owner communication and files

Telegram: the owner-communication skill, its brief paragraph and the `telegram_send`, `telegram_history`,
`telegram_delivery_get` and `telegram_send_file` grants (Ordubashy only) are optional connector
material in `optional/telegram/`. They are not part of the default roles and are not bundled; the
Telegram package supplies them with the operations.

Chat references: Ordubashy grants `request_user_input` (a message shown in the owner's Chat with
existing Signoffs, configured browsers, Tasks and registered agents below it; it returns at
once and creates no request, answer or approval) and documents it, with `browser_turn`
`action=list` and exact `browser_id` selection, in its own `skills/chat-references/` skill.
No permission, grant, approval, stop/pause or scheduling behavior changes.

Browser: the managed-browser skill documents native prompt cancellation and acceptance,
including the hyphenated commands supported by existing hosts. The workers' browser use is an
ordinary grant Ordubashy gives; the shared Signoff, Task and browser permission models are
unchanged.

## Projects

The organization concept is a Project and the paths are `operations/projects/...`;
identifiers (`business_id`, `business_*` operations, `coordinate_businesses`) and skill IDs
stay. Projects have no resident agents and no automatic creation flow. Only the configured
Ordubashy creates projects (the host refuses everyone else, the installation owner
included), through `project-creation`: with the owner's request or agreement Ordubashy calls the existing
`business_create` with a name, optional description (`summary`), optional slug and a stable
idempotency key, and the result is a project with empty `hq` and `code`, no provisioning and
no checklist; Tasks, assets, tables and routines are then created as ordinary owned work.
The owner and workers ask Ordubashy. Provider setup procedures are not part of the default roles.

## Checks

```text
python3 -m unittest discover -s tests
```

The tests are source-only: they read manifests, briefs and skills and need neither a host
nor a live agent. Earlier release notes, the retired roles and their packages remain in Git
history.
