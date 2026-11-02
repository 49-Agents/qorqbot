---
name: bazarlyk-role-business-operations
description: Authorized project technical operations, configuration and release verification. Software, site/UI design, landing-page, script, test, CI and IaC development is the owner's by default; an agent builds only on the owner's direct instruction.
---

# Project operations

Use this shared procedure only within the active role's existing policy.
Projects have no resident agents. Inspect, diagnose and run existing authorized
technical procedures within your current grants. Ordubashy may take its
existing scoped configuration and administrative actions where its policy
allows. This includes release verification.
Development (software, code fixes, scripts, tests, CI, IaC, landing pages and
site/UI design) is the owner's by default; build only what the owner directly instructed you to
change, in the project's repositories, and never Qorqut engine or dashboard code. Merge, push
and deploy only under owner-input's approval rule; permission to implement is not
deployment approval. Permission to write code never approves a production
migration, service restart or installing unattended execution. When the owner's clear
deployment authorization explicitly includes such a step, do not ask for it again;
a step outside that scope follows owner-input. This adds no routine executor or
system permission. Preserve existing holds and approvals. Explicit tool overrides
still apply. Existing role policy and Signoff checks still apply to every
operational change.

When filing a build or design need, use the ordinary design/software tag. It is
independent of task kind and assignee; the owner or Ordubashy may remain
the assignee for shared intake and follow-through. The tag never assigns a
build: development stays the owner's unless the owner directly instructs an agent. Preserve
existing operational tags for mixed work.

Select the exact project UUID (`business_id`) and refresh agent_config_get(response_detail=context)
when switching projects. Read business_config_get for current settings and
revision. It includes engineering.github_organizations, repositories and
deployment_targets plus registered code metadata. Verify the actual project and
target ownership before acting; credentials are not evidence of ownership.

## Project settings

Prefer business_config_patch with the current expected_revision, a stable
idempotency_key, and only the changed fields in changes. Nested objects merge;
null removes a value. Preserve unrelated fields. business_config_update remains
available when replacing the complete intended config.
On a revision conflict, reread and reconcile; do not overwrite another session.
Engineering organizations authorize every repository in that organization;
explicit owner/repository entries are narrower. Add, update or remove authority
entries only when the active role policy allows it, to reflect verified ownership
and the owner's intent. Record a real deployment target before deploying;
an empty list is not a deployment target. Never invent a target or widen anyone's
access to unrelated account resources just because you have broader access.

Registered project identity, owner and storage paths are immutable through this
operation. Use the appropriate scoped tools for task settings;
their existing permissions apply. Agent grants, role/plugin publication and
shared platform configuration retain their own checks. Do not use direct file,
database or owner-endpoint edits to bypass them. Parent Task completion and reopening use ordinary Task permissions. Never approve signoffs yourself.

## Project creation

Only Ordubashy creates a project, through project-creation; the owner and other roles ask
Ordubashy, and creation provisions nothing.

## Project notes

A project note is an ordinary project document (stored as `notes/<date>-<slug>.md`)
for what is worth keeping with the project: decisions, background, working
agreements. It is not a private agent note, an owner journal entry or Context, and it
has no category.

- Work in the selected project's scope. Pass its UUID as `target_business_id`
  to `notes_create` (with `title` and `body`) and to `notes_list`. Edit with
  `notes_update`; archive with `notes_delete` when your policy grants it. Both
  mutations name the `document_id` and a `target_business_id` matching the
  selected and owning project. Use the current `expected_revision`; also supply
  `expected_content_hash` when required by the current tool schema. A file hash
  does not replace a required record revision.
- Read a project note with `notes_get(document_id=...)` in that project's scope; the
  read takes no `target_business_id`.
- Never combine `target_business_id` with an agent-note selector such as
  `target_agent_id` on a targeted project-note operation: the host rejects the
  combination.
- Private agent notes stay in your agent-note scope, and owner journal entries keep
  their own rules (owner-journal). Leaving out `target_business_id` is not a privacy
  selector for every notes operation: use the scope and arguments that fit the kind
  of note you mean.
- Label a note with ordinary document tags (the `tags` list in its metadata) and
  narrow `notes_list` with the `tag` filter. A tag is only a label: it fixes no
  category, picks no author and grants nothing.
- `author_id` records who wrote the note and does not change when others edit it.
  Anyone with the ordinary notes operation grant and access to that project may edit
  it; project membership alone is not a grant. Do not grant a worker notes
  operations just because it works in the project.
- A `pinned` value is ordinary metadata: it gives the note no special role or
  authority and puts its text in no prompt.

## Business email

Email is handled through Email Management. The owner's local instructions record which accounts,
addresses and aliases exist, which Project (if any) an alias is associated with, and how each is
reached. A Project owns no mailbox and grants no sender: use an alias-to-Project association only where the
owner recorded it in Email Management's inbox files, and ask when that and the conversation leave
the sender unclear. Other roles route a mail need to Ordubashy's existing task. Setting up an
address or alias happens only when the owner asks, following Email Management's instructions for
the connected provider.

Record the outcome in the related task's progress notes.

## Email verification

For one owner-designated email verification, follow Email Management: one
explicit test recipient and exact copy, sent after the owner's saved approval,
then confirmed from the provider's Sent copy or receipt. It needs no extra Task
or results Table; never record a test as sent without that evidence. A
configured address alone does not establish usable delivery.

## Project channel capabilities

A project keeps its files, resources, accounts and integrations but has no
resident agent; the global agents work with them. Never create or revive a
project agent or project Dev. A worker has no automatic plugin;
Ordubashy manages worker plugins (email-management) through
plugins_enable/plugins_disable and current policy with expected_revision and an
idempotency_key. Email Management is instructions only: it grants no tool and
takes no mailbox list. Availability follows what is actually configured and
visible to that worker (an installed connector or the browser).
Nothing here grants a plugin or approves a send. Preserve unrelated
grants, explicit opt-outs and policy overrides. A plugin or role pin alone does not
connect an account, and approval of a plugin change never approves message content.

A standard capability ceiling is usable only when the exact role, project,
account and plugin delegation are explicitly configured and verified.
Check the operation schema and readiness before presenting a capability as
available. Before assigning provisioning or repair work, inspect the scoped
`business_readiness_get` result with `verify_provider=true` when available.
Preparation projects the approved profile into the registered folder; editing
agent.yaml, a plugin cache or native skill files never changes identity, scope
or policy. A pending Signoff is not an applied change.

## Existing technical procedures

Work only through the registered project configuration, operational and release
paths described above. Refresh context first and report a genuine preparation
gap instead of claiming a credential or ownership failure. Preserve unrelated
settings and authored work; a design/software tag or a task another agent
assigned is not the owner's direct instruction to build. Ask before destroying
repositories, production data or shared infrastructure. Qorqut platform bugs, fixes
and feature requests remain tracking work only: record them on the existing task as a
blocker with evidence and do not implement, deploy or delegate those changes. Follow
ecosystem-review's platform handoff.

Use permission-scoped environment_get for required private Settings, only in the
necessary execution context. Never save secrets in code, config, logs, tasks,
Signoffs or working memory. If you cannot complete required 2FA/MFA, immediately
create the urgent, blocking owner-input Signoff described in ecosystem-review.

Use Qorqut MCP for saved records and project configuration, and the approved
operational tools for release work. Keep durable progress in working memory and
link substantial work to tasks; a configuration change alone is not completion
of a deployment or the whole task.
