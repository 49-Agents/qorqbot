# Project creation

`business_create` creates one empty local project at the owner's request. A project is a named
container for work: Tasks, documents, tables, assets, routines and its own Settings. Creating one
does not set anything else up. The operation keeps its stable identifier `business_create`; the
interface calls the result a project.

Ideas, `ideas_*`, `manage_ideas`, the business setup checklist and `manage_business_setup` are
retired.

## Who may call it

It is a Global operation (no project selected) for exactly one caller: the installation's configured,
active Ordubashy holding `coordinate_businesses`. The owner asks Ordubashy; there is no owner path, no
forwarding and no direct-application bypass.

Ordubashy is attributed as itself. Ordinary operation policy and Signoff apply on top: the capability
alone never skips either, and an explicit deny or a Signoff requirement is honored. When the owner
approves a Signoff that Ordubashy proposed, Ordubashy remains the creator of record; the owner's approval
never makes the owner the caller. The identity is rechecked at preparation, validation and application,
and again when an interrupted creation is recovered.

The installation owner (through the dashboard, the owner HTTP route or a direct invocation), every other
human, workers, the Learning Applier, system programs, business-home (historical)
identities, an inactive or unconfigured Ordubashy and any profile that only stores a retired capability name
(`manage_ideas`, `manage_business_setup`) are refused with `forbidden` and nothing is created (no row,
membership, folder or receipt), as is any context that still carries a project selection or a project
home. Hiding a control is not the enforcement; the operation is. The owner keeps the normal owner
membership of every project Ordubashy creates, with its read, edit and archive rights, and the owner's
authority over shared service helpers is unchanged.

## Input

| field | rule |
| --- | --- |
| `name` | required; trimmed, 1–200 characters |
| `slug` | optional; `[a-z][a-z0-9-]{0,63}`; the folder name below `projects/` |
| `summary` | optional; up to 2000 characters; stored as the project's one-line description |
| `rationale` | optional text kept with the review |

Nothing else is accepted. In particular there are no owner confirmation, core documents,
milestones or source references.

Without a `slug` the folder name is derived: normalized ASCII words joined by single hyphens, prefixed
with `project-` when the result would not start with a letter, trimmed to 64 characters and `project`
when nothing remains. A slug that is taken (a registered project, any existing folder or a case
variant of one) refuses the creation with `revision_conflict` and a `suggested_slug` (`slug-2`,
`slug-3`, …); the operation never adopts an existing folder and never picks a name for you.

The case-insensitive reservation is held until the project row commits, not only while the plan is
prepared: every validation (the first check, a Signoff's approval, the file application and the final
commit, and a recovery after a crash) refuses with the same `revision_conflict` and `suggested_slug` when
a folder that differs from the slug only in letter case has appeared since. That sibling is never adopted,
renamed, replaced or deleted. This creation's own published folder is recognized by its receipt, never by
its name. A refusal after the folder was published keeps the application receipt and its claims, and the
same idempotency key completes once the sibling is gone; a refusal before publication removes only the
proven staging tree.

## What it creates

One plan creates, atomically from the caller's view:

- the project row (stage `idea` and timezone `UTC` are inert storage defaults; no lifecycle form or
  workflow reads them) with `config_json = {one_liner, code: []}`;
- the owner's membership and the memberships of the already registered, project-scoped bounded system
  programs (no principal is created);
- the empty folders `projects/{slug}/hq` and `projects/{slug}/code`, made with the Operations
  `create_directory_tree` file effect (a receipt-bound private staging area published with a
  no-replace rename and recognized afterwards only by its recorded inode identity; see
  Operations `docs/DIRECTORY_EFFECT.md`). A folder that appears after preparation refuses the apply;
  a crash before the staged proof persists fails closed with `apply_incomplete` and keeps its claims.

It creates no documents, Context inputs, Tasks, routines, checklists, agents, sessions, sandboxes,
repositories, domains, mailboxes, Outreach registrations or other external effects. Every active
global agent reaches the new project at once. A later explicit request may add any of those.

## Result

`{business: {id, slug, name, summary, workspace_path, hq_path}, business_id, slug}`.

## Initial instructions and the absence of a template

Project creation uses no template repository, checkout or environment setting, and the v1.2 composition has no template
component. The three initial shared instructions (`foundation`, `ecosystem-agent`, `ecosystem`) are package resources of the
host, read with `importlib.resources` from `bazarlyk/bootstrap/instructions/` and registered once as ordinary global
`shared_instruction` documents under `operations/files/instructions/`. They say that only the configured active Ordubashy
creates projects, that strategy and review stay with Ordubashy and the owner, and that there is no Ideas or setup-checklist workflow.

Seeding never overwrites: an existing registered instruction keeps its identity, edited bytes, hash, revision and version
history when the seed runs again, and a file/record disagreement is refused until explicitly reconciled. The registered
document is looked up before any seed is materialized: only an unregistered document whose path is absent receives the packaged
bytes, and a registered document whose file is missing is refused with `document_conflict` with the path left absent and the
record, revision and history untouched (a missing file is never recreated from the seed, and nothing restores history or
refreshes an owner document automatically). Changing a packaged
seed or installing a new package therefore refreshes nothing; an eventual content refresh is an explicit registered-document
update (`shared_instructions_update`) that preserves any owner-authored change. No product, ICP, brand or positioning
document is seeded into a new project; existing project documents and ordinary document operations are unchanged.

## Layout

The project root is `{operations}/projects/{slug}` (`InstancePaths.business_root`); the runtime
tree stays under `.runtime`. Global material stays directly below `operations/`. A previous
`businesses/` layout is not probed or adopted; moving existing projects is a migration concern.
