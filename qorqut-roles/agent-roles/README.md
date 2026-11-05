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
| `roles/ordubashy/` | 1.73.0 | The general coordinator. Owner dialogue (Telegram text, and one authorized file or photo with `telegram_send_file`), plans and escalations; the only project creator, which the owner and workers ask him to do (`project-creation`); operations, provider work on request (`service-provisioning`), email through the Email Management package, the native calendar (`calendar`) and Cloudflare; the `automations_*` tools when the optional automation extension is installed (script authoring still goes through owner-approved agentic changes); manages the permanent workers and selects their assignments. |
| `roles/global-worker/` | 1.6.0 | The general, repeatable permanent worker: one atomic assignment at a time, accurate shared status and Task, questions and completion to Ordubashy. Grants only its own work and Context reads; Ordubashy adds anything more. |
| `roles/learning-applier/` | 1.16.0 | Overseer extraction, owner-invoked review and application, with retained legacy learning history. |

Learning Applier 1.16.0 grants none of the ten retired procedural-learning operations (the eight it still allowed are removed) and directs historical reads through the paginated read-only inventory and bounded review pages; its identity, Overseer grants and Context authority are unchanged. This source update does not publish or adopt the role.

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
| `business-operations`, `business-tables`, `context-relationships`, `ecosystem-review`, `operations-delegation`, `project-creation`, `service-provisioning` | Ordubashy |
| `chat-references`, `owner-focus`, `owner-journal`, `owner-reminders`, `telegram-owner-communication` | Ordubashy |
| `owner-input` | Ordubashy, Learning Applier |
| `managed-browser-control` (the `browser-control` skill) | Ordubashy, `global-worker` |
| `ordubashy-browser-control` (the `browser-control` skill) | Learning Applier |
| `learning-application` | Learning Applier |

Loading a skill grants no tool. Content-creation production guidance is not part of this
repository: it lives in the separate `qorqut-content` extension and is installed with it.

## Email and Calendar

Email guidance is not a role skill. The brief tells Ordubashy to consult the Email Management
package (`dependencies/email-management`) for all email work; that package holds the owner's
editable local instructions for accounts, Gmail and Fastmail access, senders and outgoing-mail
approval. The role no longer carries the Fastmail account, business-mailbox setup or mailbox-route
procedures or the `email_*`, `business_email_*`, `signoffs_email_approve` and `signoffs_platform_submit` defaults. Role text
grants nothing: the host's plugin defaults decide who holds Email Management and which tools exist.

The `calendar` skill covers the native `calendar_*` tools (their policies are unchanged), the
browser-assisted Google connection through the existing setup helper, and the owner Signoff
(`signoffs_input_create`, read back with `signoffs_get`) before any change that notifies guests. The
Signoff is an instruction to the agent, not a technical interception.

## Owner communication and files

Telegram: Ordubashy grants `telegram_send`, `telegram_history`, `telegram_delivery_get` and
`telegram_send_file`; no other role holds any of them. The tool sends one authorized file to
the owner's own Telegram topic as a downloadable `document` or a displayed `photo` (static
JPEG or PNG). The shared `skills/telegram-owner-communication/` skill documents the closed
contract: `source` is a workspace file (relative path under `exports/` or `downloads/`) or a
stored document with an explicit `business_id` (`null` for Global), `expected_sha256` is
required, the framework `idempotency_key` makes retries safe, `caption` is plain text of at
most 1024 characters and 1024 UTF-16 code units, and `reply_to` may name a message in the
agent's own topic; there is no chat, topic, token, URL, bytes or file-id argument. Receiving
media is unchanged: an owner attachment still reaches the agent as its caption plus an
undelivered-attachment marker. The host allows only the currently configured, active Global
Ordubashy bound to the owner's topic to call it (a role name or an added permission on
another agent is not enough) and enforces every source, size, format and delivery gate; the
skill grants nothing.

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
The owner and workers ask Ordubashy. Independently useful provider procedures live in
`service-provisioning` (Cloudflare, Railway, Resend, OpenRouter), used only on explicit
request under the real permissions of each provider operation.

## Checks

```text
python3 -m unittest discover -s tests
```

The tests are source-only: they read manifests, briefs and skills and need neither a host
nor a live agent. Earlier release notes, the retired roles and their packages remain in Git
history.
