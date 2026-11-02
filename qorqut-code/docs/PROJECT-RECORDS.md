# Project records

A Project record is one **existing** Project (`businesses` row), not a Task subtype, a shadow Story or a second copy.
It is a normal neutral item and a private-thread source, keyed by the actual Project UUID. This page documents the
backend contract: the six operations, the bounded inventory, explicit links, the private conversation and the storage
change. It does not create, rename, archive or delete a Project.

## Authority

The six operations belong to the installation owner and the exact active configured Ordubashy
(`installation.management_coordinator_id`, `kind='agent'`, `status='active'`, role `ordubashy`). One nonrecursive
Project read guard protects every route: it refreshes the caller's current identity, checks Project reach (active
membership or supported active global reach), refuses an archived Project unless `include_archived` is explicitly
requested, and applies the ordinary `project_record_get` policy including explicit deny in that Project's own scope.
The same guard authorizes `project_record_get`, the inventory and candidates, typed Project resolution and
fingerprints, Focus source/reference projection, the private Project source and directory view, and every Project
target read in a link. An ordinary member, another agent, a historical identity or a delegated worker is refused even
when it can read the raw `businesses` row; a denied record never emits its title, count or association.

From the root route name the Project and pass `scope: "global"`; the engine switches this invocation to the Project's
own scope. A business route must agree with `project_id` (`forbidden`) even when the JSON body says
`scope: "global"`. The same wire arguments work through MCP: `split_options` moves `business_id`,
`expected_revision` and `idempotency_key` into the invocation options, so `project_items_unlink` receives its
`expected_revision` exactly as the dashboard HTTP route does. Reads and links never send notifications or start
product agents.

The narrow **Project reminder read** `owner_reminders_list` uses the existing owner-reminder decision and reads only
the owner's already stored shared reminders (see [shared reminders](REMINDERS.md)) of exactly one scope: `scope: "global"` is Global only, `scope: "business"` is the selected
Project only. It never schedules, updates or delivers a reminder. It is registered on the narrow root surface with
the six Project operations; a business route uses the same existing reminder read.

`business_create` and the rest of Project lifecycle policy are untouched. The standing defaults only name
`project_record_get`, `project_items_list`, `project_items_candidates`, `project_item_get`, `project_items_link` and
`project_items_unlink`; they sit after explicit deny overrides and grant no other audience. A `project` neutral item
read is authorized by `project_record_get` in the item's own scope, so it needs no new policy rule.

## Canonical reference

```json
{"type": "project", "id": "<PROJECT_UUID>", "business_id": "<PROJECT_UUID>"}
```

A **public** Project reference (an operation argument, a Focus source/reference, a private-thread source, a link
target, or a Project item) must name the same real Project in both fields. A null or omitted `business_id` is refused;
it is never inferred from the route. Internal two-field neutral refs used only for fan-out (a stored relationship or
Focus reference that already carries its owning business separately) may canonicalize, because the caller is not
supplying the scope. Name and slug are read from the current `businesses` row, so a rename keeps the conversation,
links and Focus identity. The private channel key is `source:<PROJECT_UUID>:project:<PROJECT_UUID>`. A Project card
shows the real Project name; a custom Focus display name is refused for a Project source.

## Operations

| Operation | Inputs | Output |
| --- | --- | --- |
| `project_record_get` | `project_id` | `{project: {id, name, slug, summary, revision, archived, source}}` |
| `project_items_list` | `project_id`, `section`, `root_tasks_only`, `include_closed`, `include_archived`, `cursor`, `limit` | `{items, next_cursor}` |
| `project_items_candidates` | `project_id`, `source_business_id` (UUID or `null` for Global), `query`, `type`, `cursor`, `limit` | `{items, next_cursor}` |
| `project_item_get` | `project_id`, `item: {type, id, business_id}` | `{item, record}` |
| `project_items_link` | `project_id`, `item: {type, id, business_id}` | `{item, link, already_present}` |
| `project_items_unlink` | `project_id`, `link_id`, `expected_revision` | `{removed: true}` |

- `summary` is the existing Project `config_json.one_liner` (whitespace-normalized), never generated text.
- Sections are `all`, `work`, `signoffs`, `actions`, `documents`, `tables`, `automation`, `other`. Selecting one
  section enumerates only its families.
- `limit` defaults to 25 (max 100). Items are ordered by their real updated (or created) instant descending, then by
  `(type, true owning scope, canonical id)` ascending. The cursor is opaque and bound to the Project, the selected
  section/readers and the filters; it is not a total count. There is **no maximum-total cap** and no full-collection
  load: SQL enumerates only typed identity and sort metadata in bounded keyset windows, and each canonical reader
  resolves its own item. Installed extension kinds use the bounded `project_inventory_window` metadata port: only
  the selected kind/section is enumerated, its rows carry the source updated/created timestamp and true scope, and
  records explicitly linked to the Project are included by the same source key rather than the link timestamp. A
  kind without that port is an explicit `operation_unavailable` for the selected family, never a silent omission or
  a full content scan. Hidden, closed, archived and denied rows are carried through with the reader's real
  frontier; every canonical-resolution attempt (including denied rows) charges the fixed per-request budget, so an
  empty visible page still carries a cursor when a reader has more rows. Owned and linked rows with the same canonical scoped identity merge into one item with both
  associations. The page is full only when the requested limit is met or every reader is exhausted; a cursor is the
  only continuation signal. A linked row is ordered by its target's real source time, never the link creation time.
- `record` is the appropriate existing domain read: `tasks_get`, `signoffs_get`, `actions_get`, `tables_get`,
  `routines_get` (Project or `scope:"global"` routine), `dev_tickets_get` for the scoped Ordubashy or the owner's
  `dev_tickets_list(details=true)` queue with its exact optional `ticket_id` selector for the owner, `owner_reminders_list`, `notes_get`/`content_get`,
  `assets_get` (`asset_id`), `data_uploads_get` (`upload_id`), `context_get`/`context_people_get` (`document_id`),
  `plans_get` (`agent_id`), `assignees_list` for a person, `project_record_get` for a Project, or the extension item's
  authorized neutral read. It is never a raw database dump and never a mutation: `context_upload` staging rows have
  no read handler and are never inventory.

A normalized item is
`{type, id, business_id, business_slug, business_name, label, revision, state?, task_type?, parent_id?, summary?,
resource_type, can_thread, association}`, where `association` is `{owned, linked, link_id?, link_revision?}`.
`can_thread` is true only for an already-supported private source (`task`, `signoff`, `table`, Focus `reference`) or `project`.

## Inventory

Families:

- `work`: Tasks of every type (including Task types other than plain work). The status is read through the authorized
  `tasks_get` record for every represented Task, not a neutral summary; closed and archived are opt-in. With
  `root_tasks_only=true`, an item is a root when it has **no represented parent after the lifecycle and archive
  filters**. A linked foreign/Global child whose parent is not linked is a root; a linked child with a represented
  parent is not. A hidden or closed parent is not represented, so its open child is a root until closed history is
  requested. Owned and linked parents/children use their real scopes, so equal IDs in unrelated scopes never collide.
  If the fixed request budget cannot complete a required represented-parent read, the candidate is deferred: it is
  not emitted, the cursor stays at the prior frontier and the next request examines it with a fresh budget.
- `signoffs`: the Project's review scope (pending/answered open by default; closed history with `include_closed`);
- `actions`: Actions whose `action_businesses` membership includes the Project, plus explicit links. A Global-stored
  Action's canonical `action_key` is its identity; a bare storage row ID is accepted only where the existing neutral
  resolver accepts it for that Project's own business-stored legacy row, never as an alias that widens a Global
  Action. The prepared plan pins the true storage row; replay re-resolves and refuses a retargeted alias.
- `documents`: Project notes, materials, people, topics and other readable documents through the neutral resolver,
  keeping journal, private-document and Context-exclusion rules. The authoritative journal kinds (`global_note`,
  `business_note`, `journal`) are excluded even when the owner can read the source, on owned enumeration, candidates,
  explicit link targets and exact item reads; genuine `business_owned_note`, agent notes, Context documents, assets
  and uploads stay available. Context topics, notes and people use their own canonical reads; assets and data uploads
  use `assets_get`/`data_uploads_get`; plans use their own agent contract; `context_upload` is never a readable item.
- `tables`: data tables through `tables_get` (rows are not enumerated here);
- `automation`: Project routines through `routines_list` and, because Global routines are link targets too, Global
  routines through `routines_list(scope="global")`/`namespace="global"`. Routine executions stay in
  `routines_runs(items)` under the routine's exact id and scope; a routine's job reference navigates through the
  existing canonical job refs. Listing a routine never starts it. The pane reads those existing parent/detail
  operations through the real owner paths:
  `POST /api/biz/<slug>/operations/routines_runs` (body `{routine_id, limit?, cursor?}`) for a Project routine and
  `POST /api/biz/<slug>/operations/jobs_get` (body `{job_id}`) for a Project job, or the corresponding
  `POST /api/root/operations/...` with `scope: "global"` for a Global source. The root route refuses a missing/non-
  Global scope and the business route refuses `scope:"global"`; job IDs must come from structured authorized data,
  never arbitrary strings. There is no job browser or job enumeration.
- `other`: Project-owned Dev reports (owner queue or scoped Ordubashy reader), stored Project reminders
  (`owner_reminders_list`), linked external references and installed extension kinds. The owner's `ticket_target`
  verifies that `row.business_id` agrees with the supplied scope; the scoped agent reads through
  `dev_tickets_get` and receives its details/history envelope; the owner queue uses its exact `ticket_id` selector so a
  report beyond the first reader page is still reachable without materializing the whole queue.

A family the caller may not read is omitted; each denied candidate is skipped before its label or count is emitted.
No internal receipts, outbox destinations, leases, sessions or credentials are items.

Ownership is derived, never backfilled: a record whose own scope is the Project (or an Action with a Project
membership) is owned. An explicit link adds the second association in any readable scope (another Project, Global, or
a foreign Project's record) without changing ownership, copying content, granting access or writing into the source.
An owned record is a harmless no-op link; a self-Project link is refused. Unlink removes the explicit association
only; the owned record remains, and the target is untouched. A caller who can read a Global-stored Action may
explicitly link it into another Project; that adds only the association and never `action_businesses` membership or
another principal's source access. A bare business-stored row ID is accepted only in its own source scope; the
prepared arguments are canonicalized to the Action's `action_key` and its true storage identity is pinned.
Replaying a link re-runs the Project guard, the identity
and target resolution, the canonical target/scope check and the target's current revision before returning a cached
result; a revoked membership, archived Project, retargeted alias or removed association refuses the replay.

## Private conversation, Focus and Threads

A Project uses the existing private owner–Ordubashy participants, message controller and delivery. The source schema
accepts `project`; `owner_conversations.source_key`, ids, peer bindings, message sequences and read cursors are
unchanged for every existing channel. A Project has only that one channel: it has no team discussion, no
`focus_notes` shadow thread. A legacy team-reply request for a Project returns `channel_required`; a Project card
reports `note_count: 0`. Reading, pinning, linking, related records and directory listing create no message; opening
an empty channel does not create a Threads entry. The directory orders by the latest real owner/coordinator message
only, and `owner_conversation_related` for a Project shares the authorized inventory/deduplication identity with its
own independent `offset`/`limit` envelope. Revocation or archiving removes the Project from the directory/Focus
projection without deleting the stored conversation or link rows.

Registry integration: `project` is one `ItemContribution` in the single neutral resolver and candidate enumeration;
it is a Focus source (and reference target) through the existing schemas, guarded by the same Project read policy;
the post-commit catalog has entries for the six operations and `owner_reminders_list`; the item kinds and sources are
additive, so an extension cannot replace `project`.

## Storage and conversion

`project_item_links` is owned by Operations (`qorqut_operations.projects.links`) and contributed by the host
assembly. Columns: `id`, `business_id` (Project UUID), `target_type`, `target_id`, `target_business_id` (nullable
Global), normalized non-null `target_namespace`, `revision`, `created_by`, `created_at`, `updated_at`; unique
`(business_id, target_type, target_namespace, target_id)`. Creation is idempotent under the unique key; removal is an
exact revision CAS. Private conversation content stays out of operational Git exports; the link table is exported
only under the existing non-private allowlist (`host.project_links` history contribution, `BUSINESS_TABLES`).

`app/bazarlyk/migration/project_sources.py` is the narrow, explicitly callable offline conversion. It requires an
explicit existing target path and takes the before and after composed `Assembly` as arguments; the test fixture uses
the immutable pre-feature host `ceb46263` with Operations `6ff63572` and Context `8ba00f6b` and verifies the before
Assembly's exact target digest (`a8e1f553…`) and 509-object inventory (`be828d9f…`), not a reverse-derived fixture
alone. `_check_scope` proves the schema difference is exactly the `owner_conversations` source CHECK widening plus
the new `project_item_links` table and its indexes; any other rebuilt table, index or trigger is refused. The
conversion rebuilds `owner_conversations` with the widened CHECK, creates the link table and its indexes, rewrites
the target identity digest, and verifies the after inventory, foreign keys, and byte-identical private evidence:
multiple conversations, message sequence gaps, peer bindings, unread positions, deliveries, change sequences and
unrelated rows. The whole conversion is one transaction and rolls back on any difference; a clean post-upgrade
validation must pass. Startup, services, release workers and deployment never invoke it; later integration work
qualifies the combined artifact.

Focused coverage:
`app/tests/test_project_records.py` (authority and denied callers, inventory union, cross-scope links and idempotent
retries, owned no-op, unlink revisions, candidates, private channel, rename, reminders, schema);
`app/tests/test_project_transport.py` (all six through the real root dashboard HTTP route, business-route mismatch,
MCP `split_options`, reminder root read);
`app/tests/test_project_replay_and_actions.py` (replay reauthorization, archived/revoked refusal, removed association,
Action bare/canonical scope, duplicate convergence);
`app/tests/test_project_pagination.py` (beyond the old scan cap, Global scope cursor chains, equal-timestamp scoped
ties, linked source order, filtered windows, Story/Customer container and Task hierarchy, independent related pages);
`app/tests/test_project_families.py` (Global routine and retired routine history, owner/agent Dev reports, exact owner
Dev selector, note/asset/upload/table/entity records, journal exclusion and staging-row exclusion);
`app/tests/test_project_authority.py` (direct resolver/fingerprint denial, Focus, private Threads, Project target
scope, stored rows after revocation);
`app/tests/test_manager_project_remaining.py` (the Manager's eight remaining behavioral probes: Global cursors,
source order, Story state, owner Dev paging/selector, readable Global Action linking, bounded filtered window,
distinct-timestamp cursor, journal boundary);
`app/tests/test_manager_project_extension_edges.py` (the Manager's 205-record continuation/order/budget extension
probes, plus the synthetic extension link/source-time and core-agent metadata regressions);
`app/tests/test_project_extension_items.py` (an installed extension item kind);
`app/tests/test_migration_project_sources.py` (before/after conversion, immutable pre-feature evidence, refusal,
rollback). Engine persistence: `qorqut-operations/tests/test_project_links.py`.
