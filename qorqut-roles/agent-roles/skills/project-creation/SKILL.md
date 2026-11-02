---
name: bazarlyk-role-project-creation
description: Create a Project, the container for Tasks, assets, documents, tables, code and routines, with business_create when the owner asks you for or agrees to a new one. Only the configured Ordubashy creates projects. It takes a name, an optional Description and a folder slug and makes only the record and empty hq/ and code/ folders, with no agents, setup, services or documents.
---

# Create a project

Use this when the owner asks you for a new project or agrees that a piece of work needs its own
container. A project holds the work it owns: Tasks, assets, documents, tables, code and
the routines that apply to it. Global stays available for work that belongs to no
project. Read your current profile (`agent_config_get`) for the projects you can reach
and use one that already fits; never create a second project for the same purpose.

## Authority

Only you, the configured Ordubashy, create projects. You hold `coordinate_businesses`
and `business_create` is `allow`; the host also checks your configured identity on every
step, so a similar name or role does not qualify. The owner has no creation tool: they ask you,
and you create the project for them. No worker, other human, the installation owner
included, retired role or system program can create one, whatever plugin, capability,
skill or old setup grant it holds, and reading this skill grants nothing. A worker that
needs a project asks you (`agents_message_send`); create it when the owner's intent covers it,
otherwise ask the owner.
An explicit deny or Signoff requirement on the operation still applies: report a denial
instead of working around it, and surface a required Signoff honestly.

## Call

Call `business_create` in Global (select no project) with:

- `name` (required): the display name, trimmed, 1 to 200 characters. A final brand or
  domain can come later, and a later rename never moves anything.
- `summary` (optional): shown as the Description. At most 2,000 characters, empty is
  fine. A short statement of what the project is for, not a working brief.
- `slug` (optional): the folder `projects/<slug>`. Lowercase ASCII, starting with a
  letter, then letters, digits or hyphens, at most 64 characters. When omitted it is
  derived from the name. It is fixed at creation: editing
  the name later never renames the folder. Agree it with the owner when it matters.
- a stable `idempotency_key`.

Nothing else is accepted. Do not pass confirmation text, core, sources or any setup
input; those do not exist.

After a lost response or a timeout, call again with the same key and identical arguments:
it returns the same project. Changed arguments need a new key, and changed arguments
under the old key conflict.

`revision_conflict` with `slug` and `suggested_slug` means that folder name is taken, or
collides by case or with an unsafe path. Nothing was adopted and the existing folder or
project is untouched. Never reuse or take over it. Ask the owner; if only a derived slug
collided and they have no preference, retry with `suggested_slug` under a new key.

## Result and what exists

The result carries `business_id`, `slug` and `business` (`id`, `slug`, `name`, `summary`,
`workspace_path`, `hq_path`). Select the returned `business_id` for later work.

Creation makes exactly the project record, the owner's membership (plus the memberships the
already registered system programs need) and an empty project folder holding empty `hq/`
and `code/` folders. It makes no agent (projects have no resident agents; never create,
request or revive one), no README, `business.yaml` or core document, no
checklist, Task or question, no routine, Context filing, external-service
registration, Git repository, domain, mailbox, account, credential or native session, and
it calls no provider. Do not describe it as more than that.

## After creation

The project is usable as it is. Create ordinary Tasks (`tasks_create` with its
`business_id`), upload assets, create documents and tables, and create routines
explicitly under their normal Signoff rules, all owned by that project. Project
documents and tags are ordinary and written when the owner wants them. A mailbox, domain
or other provider account starts only from an explicit request under its own procedure
(for a mailbox, Email Management); creation configures none.
