# Business Domain Integration

`register_business_domains(registry, application, paths, *, catalog, agent_resolver, clock=utc_clock, job_programs=None, filing=None, graph_edits=None, queue=None)` composes the retained domains with Operations and shared Context. Read the exact current signature in [domain composition](app/qorqut/domains/__init__.py); optional integration arguments evolve there. `paths` is explicit `InstancePaths`, not a businesses-root string. The `BusinessDomains` dataclass includes Tasks, Actions, tags, queue, shared Context, neutral items and retained domain services. Stories and the former Requests domain are not members.
`job_programs` is the shared `ProgramRegistry`, not an ad hoc task map.
Missing required programs fail explicitly. No handler starts agent sessions.

Compose `BusinessDomains.resolve_file_target` for resource type
`managed_document_files` with the catalog/profile registered-file resolvers.
Use `PreparedFileExecutor` for allow-mode writes and the same `FileApplier` for
signoffs. Document files and immutable proposal bytes are never rewritten by a
transport serializer. All mutation options require idempotency; existing mutable
records additionally require `expected_revision`.

## Retained domain reference

The source schemas are authoritative. The authored-document and relationship interfaces below are separate from the published Context graph, whose current integration is [documented here](docs/v1.2/CONTEXT_COMPOSITION.md). Retired Context-base details are historical and must not be used to restore removed tools.

## Adapter Results

Results are inside the canonical `OperationResult.data` envelope:

| Domain | Collection | Detail/mutation |
| --- | --- | --- |
| Tasks | `tasks`, `next_cursor` | `task`; config: `config`, `revision`; assignees: `assignees` |
| Activity | `entries`, `next_cursor` | `entry`, including `labels` |
| Tables | `items`, `next_cursor`, `purposes` | `table`; typed rows and purpose-specific view constraints |
| Documents | `items`, `next_cursor` | `document`; mutations also `documents`, `deleted` |
| Assets | `items`, `next_cursor`, `groups` | `asset` or `deleted` |
| Signoffs | `items`, `next_cursor` | `signoff` |
| Relationships | `items`, `next_cursor` | `relationship`; typed endpoints, role, context and timestamps |
| Context items | `items`, `next_cursor` | `item`; graph: `nodes`, `edges` |
| Context reviews | `items`, `next_cursor`, `item_revision` | `item`, `review_ids`; history: `reviews`, `next_cursor` |

Domain `operations.py` input schemas are the executable adapter contract. Request
IDs retain historical string identities; new IDs are UUIDs. Document/asset IDs
are registered UUIDs, not request-supplied paths. Document views include
`metadata`, `source_refs`, `agent_id`, `body`, `content_hash`, and `file_resource`.

Business Materials additionally expose `material_type`, `origin`, and `tags`.
`context_items_list` filters these across documents;
`context_list` and `context_search` accept the same filters for the text library
(use `tag` for one label). Published content is included. `materials_update`
accepts `material_id`, `changes: {material_type?, tags?}`, and an expected revision.
It edits classification metadata only, never source bytes or permission policy.
Tags replace the whole list. Raw uploads and published content
have fixed types; origin is immutable. Notes/plans retain their dedicated tools.
Asset bytes use the registered file target after scoped authorization. `notes_list`
defaults to the agent's own notes or the human's business notes; `agent_id` selects
same-business teammate reads. Agents may update only their own plans/notes.
`notes_create.target_agent_id` is a worker-only, claimed-job output selector.

Out-of-band authored edits are readable with `metadata_stale: true`. An update
must then supply the observed `expected_content_hash` as well as its indexed
revision. This acknowledges the exact current bytes without a read-side mutation.
Protected business documents use `context_document_update`; registered global
instructions use `shared_instructions_update`. Topic moves cannot relocate them.
Context mutations regenerate a flat file inventory in `INDEX.md` as a registered
`context_index` within the same exact file plan. The read-only
`document_index_sources` target hashes scoped document revisions, so concurrent
new documents cannot silently make a prepared index stale. Context and request
worker programs include that explicit resource type in their declared bounds.
The inventory is not a hierarchy. Topic membership, parent relationships, people
mentions, Story links, task assignments and blockers all use `relationships`.
Task mutations take `assignee_ids`; the list filter `assignee_id` selects one
assignee's shared queue. Assignment never launches an agent.

`context_review` explicitly acknowledges selected relationship/source versions
for one item. An optional same-item domain edit is atomic and retains its normal
permissions and signoff requirements. Reading and filing never acknowledge
evidence. See [Context Relativity](docs/CONTEXT-RELATIVITY.md) for the shared model,
archive behavior, source fidelity and review semantics.

MCP `context_list`, `context_search` and `context_get` use bounded library
projections, excluding private Notes, agent plans and raw inbox/transcript files.
Dashboard and worker document reads retain their full projections. Accepted
context-bucket learnings enter the existing filer atomically with owner review;
delta-bucket learnings use `learnings_work_*` state and note operations. See
[learning and context contracts](docs/agent-platform/LEARNING-APPLIER-IMPLEMENTATION.md)
for scope, cursors, recovery, completion targets and preparation.

## Platform Signoffs

The host registers no platform adapter: there are no `signoffs_platform_*` operations and no `platform.dispatch`,
`platform.reconcile` or `platform.cancel` programs. The one channel that used them, ordinary project email (`email.send`), is
retired from core, and the optional Fastmail provider's `fastmail_*` account actions follow the owner-input model instead of
staging a platform Signoff. A stored platform-action Signoff keeps its recorded evidence and execution state, stays readable
through `signoffs_list` / `signoffs_get`, and cannot execute, retry or cancel. Generic Signoffs (internal changes and input
requests) are unchanged. See [Email ownership](docs/EMAIL-OWNERSHIP.md).

Filing operations accept exact frozen outputs and optional
`expected_documents[{document_id,revision,content_hash}]`. The source snapshot
API is `documents.snapshot_for_job(ctx, kind, arguments, uow)`; it returns source,
topic/person rosters, mandatory briefs read-only, and related source documents.
Provider calls run outside UoWs. All job creation uses the shared ProgramRegistry.
The worker atomically revalidates current authority
and persists the dispatch intent before sending. Owner changes check the same
job revision and refuse already-started dispatch, closing the rejection/send race.

## Storage

`businesses.config_json` is the sole general business configuration. Indexed
name/stage/timezone update atomically; legacy config `id` is not the business UUID.
This is ordinary agent-visible data; secrets belong in provider/connection
configuration, referenced by stable IDs, not in business settings or proposals.
`business_configs.domain` names the domain of an operational setting (for example `tasks`). Document namespaces remain raw business UUIDs
(or `global`), independently of application target namespace `business:<UUID>`.
`data_tables` and `data_rows` hold general tables. Specialized domain tools wrap the same
table mutation, validation and Git-versioning path as the dashboard grid. Purpose fixes the
required columns and domain rules; tags only group tables.

Request creator attribution, installation ownership and job identity/output
bindings are explicit. Dispatch intent and external receipts freeze once
populated. Only the shared schema owner appends numbered migrations; no generic
SQL/file mutation API is exposed.

## Notes Context snapshots

`notes_upload(document_id, expected_content_hash?)` submits a selected business
note using the displayed revision. It creates a `context_upload` with a source
reference and a `context.file` job, and records the upload ID and snapshot hash on
the original note in the same prepared application. Unchanged snapshots cannot be
submitted again; `context_retry` retries failed filing. Changed snapshots create
new linked uploads, preserving prior versions.

The `notes-v1` snapshot is escaped XML-like text: a `<note format="1" title="…">`
contains `<text>`, `<item status="todo|white|in_progress|done|blocked|cancelled">`
and `<divider />` blocks. Bold blocks carry `bold="true"`. The authored body uses
LF between independent blocks and U+2028 for a soft break within a block; export
converts soft breaks to LF inside the enclosing tag. Existing raw LF boundaries
are never guessed to be continuations. Status keys use trimmed display text,
including soft breaks rendered as LF. The filer treats the complete snapshot as
one paragraph unit to preserve all block boundaries and original escaped bytes.

`notes_create.date` optionally assigns an entry date for the activity board.
Journal handlers are retired. Metadata-only catalog entries remain solely so
immutable published roles with old Journal grants can still validate; no Journal
operation is installed or callable. Notes reads retain legacy `journal` records
and explicit updates adopt `business_note` without changing the registered path.

Former Journal entries use `content_format: plain_text` in registered metadata.
The Notes editor treats their whole body as literal free text; Context export
wraps it in one `<text>` block without parsing dashes, dividers or bold tokens.
A metadata-only `notes_update` on a Journal record adopts that format and kind
without changing any source file bytes. Normal subsequent edits preserve the
format and use the regular revision/hash checks.

To adopt current entries across all registered businesses, inspect first, then
apply the same explicit migration command with `--apply`:

```sh
python -I -m qorqut.migration.journal_notes --root /path/to/businesses --workspace /path/to/workspace
```

The migration uses the installation owner's normal application policy, checks
current revisions/hashes, privately backs up every original file and record,
verifies byte identity afterward, and is safe to rerun. It does not start a
service or background jobs. There is no startup migration or implicit import.

## Call And Meeting Uploads

`context_upload` accepts an optional `origin` (`call` or `meeting`), an optional
`occurred_on` date (`YYYY-MM-DD`) and up to 20 `participants`, each
`{person_id?, name}`. Participants and dates require an `origin`. A `person_id`
must name an active Person of the selected business and is bound to the prepared
plan with its revision, so a missing, archived, cross-business or Global Person
fails before anything is written. Names are stripped, kept as chosen (never replaced
by the Person's stored name) and must be distinct. The details live in the upload's
metadata, survive `context_retry` and the move to `context_dump`, and need no
migration. The dashboard sends them to `POST /api/biz/<slug>/context/upload` and
finds Persons for its participant typeahead with `context_people_list` through the
generic operation route.

Only for these uploads, the `context.file` program adds an `encounter` block to its
model input, with a stable `participant-N` key per participant. Linkage is enforced in
code after the model answers rather than requested from it: every derived note becomes
`material_type: conversation`, carries the upload's `origin` and `occurred_on`, and
links every participant. These notes are always new materials: a `material_id` from
the model is ignored, because appending would keep the older material's own type and
origin. A picked Person is linked by ID. A typed name the model matches to an existing
Person links that Person and adds the typed spelling as one more `aka` entry, with
`filer_evidence` as the only other change. When the model links a typed name to no
existing Person, whether it gives no entry or proposes a new Person for it, the filer
links the one existing Person whose name or alias equals it, ignoring case (nothing is
recorded on them, since the spelling is already theirs, and a proposed new Person is
dropped), and creates a new Person named as typed when no Person or several Persons
match. A picked Person archived
or merged after the upload fails the filing, leaving the upload failed with
"Participant is no longer a Person in this business"; restore them and use
`context_retry`. `context_items_list`, `context_list` and `context_search` filter the
derived notes with `origin: "call"` or `"meeting"`. `context_items_list` is business
scope only; see Global Context Upload below for `context_list`/`context_search`.

For every upload, not only these, a filing answer that lists one existing Person in two
`people` entries is refused with a diagnostic and gets the usual second attempt, rather
than failing once the plan is built and leaving the upload pending.

## Global Context Upload

`context_upload`, `context_people_list` and `context_people_get` are also usable at
Global (`business_id` `None`), under the existing `read_global_context` capability;
no new capability was added. `context_upload` additionally requires the installation
owner at Global (the `context_retry` pattern), so a teammate or an ecosystem agent
holding `read_global_context` cannot upload there even if their tool policy allows the
operation. `story_ids` is rejected at Global with `validation_error` "Stories are per
business"; an empty list is accepted. Everything above (encounter fields, participant
linkage, archived-participant failure, `context_retry`) applies the same way, against
Global Persons and Global topics under the businesses root rather than a business's
`hq/context`. A business Person ID is not a Global participant and fails with
`not_found`, and `context_people_list`/`context_people_get` never cross the business/Global
boundary in either direction.

Global documents are classified like business ones: `classification()` in
`materials.py` admits a row when `business_id` is set or `namespace` is `"global"`, so a
filed Global `context_note` carries `material_type`/`origin`/`tags`, and
`context_list`/`context_search` with `scope: "shared"` and an `origin`/`material_type`/`tag`
filter return Global materials (a business-scope call still never does). The dashboard
sends Global uploads to `POST /api/root/context/upload` and finds Global Persons with
`POST /api/root/context/people`, mirroring the business shapes above; neither is on the
generic `/api/root/operations/` allowlist, since Global context traffic stays on
hand-written compatibility routes.
