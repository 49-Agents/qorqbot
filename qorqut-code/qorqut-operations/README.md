# Qorqut Operations

The portable Operations engine of Qorqut: the one authority, application and storage contract that the host
composes with its own domains. It is a library (Python 3.12, four runtime dependencies: `jsonschema`, `markdown-it-py`, `PyYAML` and the first-party
`qorqut-business-docs-versioning` document-history library) with no
authentication service, transports, native-session code or import-time database access. It imports nothing from the
deployed v1 core or from any host. It does own two service endpoints (`SignoffService` and `BackgroundService`, each
with explicit `start()`/`stop()` and `run_one()`); they start no thread and touch no store until the host invokes
them, and the engine never starts one on import, construction, assembly or `bind`.

The contracts are the deployed v1 ones: canonical `ChangeSet` bytes and payload hashes, exact `ExpectedTarget`
revision/content checks, `InvocationContext`/`InvocationOptions`, idempotency receipts, Signoff-or-apply routing from
the policy decisions, durable file `ApplyReceipts` with claims and recovery, and the writer-admission/maintenance
gates. This is not a claim of line-for-line equivalence: `docs/EXTRACTION.md` lists the precise differences (two
fail-closed corrections, the explicit interfaces that replace discovered globals, the post-commit publication split
and the target identity/validation model), the source map and what the host still contributes.

| Module | Responsibility |
|---|---|
| `contracts` | Values and errors: `InvocationContext`, `ChangeSet`, `PolicyDecision`, `OperationResult`, `DomainError` |
| `application.engine` | `Application`: the one authenticated invocation path, prepared-plan validation and application |
| `application.registry`, `.catalog`, `.review` | Operation metadata/handlers; trusted catalog composition; review-scope exceptions |
| `application.plans`, `.batches` | `PlanBuilder`, `apply_records`, paging/revision helpers; the `operations_batch` discovery contract |
| `application.ports`, `.delegation` | Typed injected ports (`SignoffStager`, `FileExecutor`, `DelegationProvider`) and the delegated-worker checks |
| `policy.rules`, `.resolver` | Mode precedence with contributed `PolicyRules`; principal-aware `PolicyResolver` over an `AgentAuthority` port |
| `storage.assembly`, `.specs`, `.core_schema` | Trusted schema/hook assembly with a freeze boundary; the engine-owned tables and DDL |
| `storage.database`, `.repositories` | Explicit SQLite units of work, scoped repositories, revision CAS, snapshots |
| `storage.receipts`, `.files`, `.executor`, `.safe_io` | Idempotency and apply receipts; hash-checked file effects and prepared-file execution |
| `storage.directories`, `.directory_layout` | The `create_directory_tree` file effect: a registered directory target, private receipt-bound staging, an exact inode identity proof in the receipt and no-replace publication (`docs/DIRECTORY_EFFECT.md`) |
| `storage.locks`, `.admission` | Explicit lock locations; in-process writer admission and the installation recovery lock |
| `signoffs` | The retained Signoff service (staging, owner decisions, exactly-once application, input requests, notes, delegated rejection, notifications) behind typed `SignoffPorts` |
| `storage.structured`, `.paths` | The one safe YAML parser (`parse_yaml`: duplicate and non-string keys refused, dates stay strings) and `registered_path`, the resolver of a path under a trusted root |
| `documents` | Operational source documents: the registered-file `store`, the source `library` (selection, parsing, cache, search, cursors; `markdown-it-py` for headings), the source read and mutation `operations`, the owner `journal` reads, the Context people/topics/notes helpers and the `documents` table DDL and spec (`schema`; `CHANGE_CAPTURE` is spliced by the host beside its change feed). Host behavior arrives through constructor callables and one presentation callback |
| `document_history` | Exact document history over the `qorqut-business-docs-versioning` library: generic `text` versions and events, file-receipt capture with per-kind byte-capture callbacks (`files`), the document availability `lifecycle`, Task/Action/document record history with host-supplied SQL projections and fact callbacks (`records`), the owner-only `reads` and the history table DDL (`schema`). A missing hook fails composition |
| `documents.serialization` | Source document framing: `split_document`, `document_bytes`, `slugify` and the `SLUG`/`BODY`/`HASH`/`MAX_TEXT_BYTES`/`BINARY_KINDS` bounds |
| `assets`, `assets.validation` | Generic assets (`AssetOperations`, over the host's source store and a host `additional_references` source of `used_by` entries) and the asset and upload file-name normalizer (`filename`) |
| `tables` | Operational tables (general and `crm` kinds), table versions and original data uploads: `TableOperations`/`TableHistory` (explicit `operations_root` and `project_hq`), `DataUploadOperations`, the feature-local DDL, specs, immutable fields, history-coverage declarations, the Global-table policy rule and the table revision hook |
| `crm` | CRM tables (`CRMOperations`, `docs/CRM.md`): memberships of existing Task records in a `crm` Table (`crm_entries_*`), the Table discussion (`tables_thread_get`/`tables_comment`/`tables_follow`), append-only recorded email/message/development observations (`crm_activity_record`), the `local_activity` and `resolve_record` composition methods, the DDL, immutable fields and history classifications. Bound to the Table service with `bind_crm`; the host owns the unified activity page |
| `records.vocabulary`, `.associations` | The neutral record vocabulary (`ITEM_TYPES`, `RESOURCE_TYPES`, `RAW_KINDS`, `document_type`) and Task associations of a new resource (`TASK_IDS`, `link_new_resource` with an optional host `item_resolver`) |
| `items` | The neutral item contract: typed `ItemContribution`/`ItemLookup`, the frozen `ItemResolver`, `authorize_item_read`, `item_revision`, `UNAVAILABLE` |
| `relationships` | Directional relationships and explicit, append-only context reviews (`RelationshipOperations`, `ContextReviews`) behind required `RelationshipPorts`/`ReviewEdit` |
| `tags` | The shared Task/Action label catalog (`TagOperations`): labels only, no access, no counts |
| `discussion` | Canonical item threads for Tasks, Actions and Tables, follows (sticky unfollow), relay attribution and the required/optional inbox events (`emit_event`) |
| `tasks` | Tasks (`TaskOperations`, `TaskPorts`): fixed states, one snooze, assignees, parents, informational dependencies, snooze reconciler |
| `actions` | Actions with immutable revisions (`ActionOperations(..., on_revision=)`, `new_action`/`change_action`/`settle(..., on_revision=)`; the admission callable is required and supplied by the host) |
| `tasks.writes` | `TaskWrites`: Tasks written inside another module's plan are announced once (follows, thread entry, inbox event) in the writing transaction; import the submodule |
| `actions.writeback` | `ActionWriteback`, `linked_action`, `linked_action_items`: the automatic one-per-source Action written inside an applying transaction, and the workspace-visible Action item links; import the submodule (the `actions` initializer does not load it) |
| `projects` | Project lifecycle bookkeeping behind host ports: `creation` (`BusinessCreationOperations`, `COORDINATION_CAPABILITY`; the host supplies the roots, the path functions and `creator_actor`), `archive` (`BusinessArchive`, optional late-bound `reset_terminals`, the owner-approval `POLICY_RULES`), `settings` (`merge_settings`) and `summary` (the Task/Action counting readers for the progress views) |
| `background`, `routines` | Registered programs and their worker authority, the serial job service, the cron/DST routine scheduler, and the generic routine operations/reads |
| `inbox_nudges` | The durable inbox-nudge need, immutable attempts and exact receipts and the event-based eligibility rule (each event announced at most once per native conversation), behind a typed `Visibility` port (the host supplies the binding and authorization; see `docs/INBOX_NUDGES.md`) |
| `inbox` | Direct agent messages (`Messages`, over a required `MessageHost` adapter), broadcasts and their delivery engine (`Broadcasts`, `BroadcastDispatch`, over host-supplied global-agent facts, authority and terminal-input calls), the pure related-work routing (`related`), the generic read/acknowledge/follow/subscribe service (`InboxService`, over a required `InboxHost` adapter of nine methods and the host's explicit `SourceHandlers`) and the one before-commit event finalizer (`EventFinalizer`, over a required eleven-method `EventRouting` adapter), and the verbatim DDL, capture triggers, specs, history export, catalog entries, classifications and standing policy of those tables (data for the host's assembly, not a `StorageContribution` of its own) |
| `storage.quiescence`, `.recovery_gate` | `RecoveryQuiescer`, `QuiescenceProof`, `BoundedWriter`, `KnownZeroWriters`, `DatabaseMaintenanceGate` over host-supplied writer controllers |
| `roles`, `conversations`, `team_channel` | Pure vocabulary the extracted code compares stored profiles and notes against (global role ids only; there is no department registry — ordinary tags are the grouping model) |

## Not in this package

The Context input queue, source filing, controlled graph edits and Context usage are not Operations packages: they live in
`qorqut_context.integration` (the `integration` extra of `qorqut-context`; its `INTEGRATION.md` holds the old-to-new path map).
Operations imports nothing from Context, the host or an extension. An Action's revision reaches the Context queue only through the
required `on_revision` callable that the host composes. Ownership across Operations, Context and the host, the remaining host
adapters and the installation entry points are in the host repository's `docs/v1.3/MODULE-MAP.md`.

## Composition is trusted and explicit

```python
from qorqut_operations.storage import (AssemblyBuilder, Database, FileEffectContribution, RecordEffectContribution,
                                       StorageContribution, TransactionContribution)

assembly = (AssemblyBuilder(target_name="my-host", target_version=1)
    .add(StorageContribution("tasks", tables, immutable_fields, schema_statements))
    .add(TransactionContribution("tasks", finalizers, change_publisher))   # same-transaction vs post-commit
    .add(FileEffectContribution("documents", lifecycles, receipt_history))   # providers for any required lifecycle
    .add(RecordEffectContribution("tasks", record_hooks))
    .freeze())                          # registration ends; a missing required hook fails here

database = Database(path, assembly=assembly)      # inert: touches no file
database.initialize_target()                      # explicit: only for an isolated empty instance or a test
database.open_existing()                          # validates the stored target identity and schema; never creates or converts
database.enable_wal()                             # host-owned startup only: concurrent readers/writers after an offline copy
```

A storage contribution may name `required_lifecycles` (resource types whose availability bookkeeping it cannot do
without); `freeze()` fails unless a lifecycle provider is registered for each, so such a composition never reaches a
`Database`. A file resource with no declared requirement simply has no lifecycle callbacks.

Validation is against the real schema, not just the identity row: the composed trusted DDL (plus the identity table)
is executed in a disposable in-memory SQLite database, and an existing store is accepted only if its `sqlite_master`
holds exactly those tables, indexes and triggers with the same owner tables and stored definitions
(`sqlite_*` objects excluded). This runs only in `initialize_target()`, `open_existing()` and
`validate_snapshot()`; it never executes DDL against an existing store, reads no rows and repairs nothing.
`change_sequences` and `target_identity` are engine-owned names a contribution cannot declare or redefine.

A contribution is a frozen dataclass made by trusted assembly code; mappings, JSON or any other caller-supplied
value are refused, duplicates fail, and nothing registers after `freeze()`. The same applies to the operation catalog
(`compose_catalog`). There is no migration chain and no reader for an older format: a store whose target
name/version/digest differs from the composition is refused.

## Typed host ports

The Signoff, program and quiescence code reaches the host only through constructor-injected, typed ports
(`SignoffPorts`, `JobObserver`, `FileExecutor`, the quiescence controllers); there is no `Runtime`/attribute bag and no
dynamic loader. The four required `SignoffPorts` (`execution`, `peers`, `tasks`, `references`) must be present and
complete when the ports are built; each optional domain (`platform`, `email`, `management`, `focus`)
must be declared with an implementation or `ABSENT`, and then the operations that need it are `operation_unavailable`
(never an invented default). A service reads host tables only through those ports, plus the two it declares with
`StorageContribution(required_tables=…)` (`inbox_changes`, `documents`). The service is bound to its `Application`
exactly once. See `docs/EXTRACTION.md` for the source-to-port call map, table/trigger ownership, the retained domain
literals and the remaining host dependencies.

## Neutral items and relationships

The item kinds are the host's, not the engine's. A trusted host builds frozen `ItemContribution(owner, kinds, lookup)`
values (a nonempty tuple of kinds and a typed lookup that keeps its own private-source, availability and membership
rules), one candidates port and an `ItemResolver(application, contributions, *, candidates)`, then
`RelationshipOperations(items, RelationshipPorts(roles, workflow_roles, materials, archive, navigation,
review_edits), clock=…)`. Every port is required; a duplicate owner or kind, a missing port or an
unregistered review-edit kind fails at assembly. Registering a kind, enumerating candidates or supplying a port grants
no access: every read is authorized through the item's own read operation, a review decision is explicit and
revision-bound (reading or filing never acknowledges), and a review may commit a same-item edit in the same
transaction under that edit's own operation policy. The `relationships`, `context_reviews` and
`context_review_states` tables, their indexes and the append-only trigger are engine-owned; the host keeps the
change-feed capture triggers. The settled composition order (the `Application` over its one open registry, then the
resolver and operations, then registration and `freeze()`) and the exact interfaces are in `docs/EXTRACTION.md`.

## Checks

```
pip install -e '.[test]' && pytest                                    # synthetic, temporary paths only
QORQUT_OPERATIONS_WHEEL_CHECK=1 pytest tests/test_packaging.py        # builds and installs a wheel in a throwaway venv
python tools/check_installed_wheel.py                                 # installed wheel: foundation + Signoff + registered-program + neutral-item + directory-effect slices
```

The test composition (`tests/support.py`) is a tiny fake domain; it never reads an instance database.

## Task types

`tasks_create` and `tasks_update` accept `task_type: task | story | customer | email`,
`overview` (up to 20,000 characters), and nullable `email_conversation_id`.
All are existing Tasks with the same states, parents, permissions, revisions,
Focus, discussion and related items. `kind: request_to_owner` remains an independent
assignment contract for work the human owner must perform. Ordinary agent work, including
work requested by the owner, uses `ordinary`. The old `owner_request` input remains
a compatibility alias; stored rows, historical plans and receipts retain that spelling.
Public Task responses use `request_to_owner`. A Story holds an ongoing relationship or work group's current
overview; parent/child links organize its next steps. There is no separate Story or
Customer table.

An Email Task must select a readable whole email conversation in the Task's owning
business/global scope. Binding/rebinding validates the source through the host's
existing item resolver and pins its revision in a prepared plan. Reading the email
still requires the email reader's permissions. It does not send mail, grant approval,
copy messages, mark mail read, or synchronize Task and mailbox state. Creation is
explicit; incoming emails never create Tasks automatically. Clear the conversation
binding when changing to a non-email type. `tasks_list` can filter by `task_type`
and `email_conversation_id`; `tasks_overview` includes the type without its overview.

These built-in attributes occupy the reserved `$task` object in the existing
`fields_json` payload. Public `fields` contains custom fields only; replacing those
preserves the built-in attributes, and callers cannot inject the reserved key.
Old Tasks have type `task`, empty overview and no email binding without rewriting
records or changing the storage schema. Updates use the usual atomic Task plan,
events and table history; there is no second writer or new operational authority.

### Customers

A Customer is a Task whose title is the customer's name. Three more typed attributes
(`tasks_create`, `tasks_update.changes`, `tasks_get`, `tasks_list`) live in the same
reserved `$task` payload, survive custom-field replacement and cannot be written
through `fields`:

| Field | Contract |
|---|---|
| `customer_kind` | `person` or `company` for a Customer, else `null`. A Customer created or converted without one is a `person`; an explicit `null` on a Customer is refused. |
| `customer_state` | Free-text label, at most 120 characters after trimming and collapsing whitespace; `""` clears it. |
| `customer_profile` | Replaced as a whole. Only `company`, `contact_name`, `email`, `phone`, `website` (strings up to 500 characters) and `notes` (up to 20,000); default `{}`. |

All three belong to the Customer type. Changing the type away requires the same
update to clear them explicitly (`customer_kind: null`, `customer_state: ""`,
`customer_profile: {}`); nothing is erased silently, and a partial clear is refused.
Nested custom `fields` and these typed top-level fields are distinct namespaces, as with
`task_type` and `overview`: an existing custom field definition that happens to use one of
these names stays legal, and writing it never sets the typed attribute or adds a choice.
The label is only a label: it
never changes `status`, `archived`, children, snooze, assignees or routines, and starts
no automation. Archiving is the existing `archived` flag. Compact projections
(related-Task chips) carry `customer_kind` and `customer_state`; the profile and its
notes appear only in the authorized `tasks_get`/`tasks_list` detail rows, never in
`tasks_overview`, links, children or the discussion's system entries.

**State vocabulary.** The choices offered are the labels ever used in the owning scope
(each business, and Global, separately). They are stored in the existing scoped
`business_configs` table, domain `task_customer_states`, as `{"v": 1, "states": [...]}`.
No new table is introduced, but a Global vocabulary row has a NULL business, so the host
must admit `task_customer_states` as the second permitted NULL-business
`business_configs` domain (beside `tasks`). An existing store needs a separately reviewed, explicit offline schema transition before this code opens it; that transition belongs to the host installer, not to this engine. Business choices and Global choices stay
separate. `tasks_config_get` returns them as the top-level
`customer_states` array beside `config` and `revision`. Labels compare after Unicode
(NFKC) normalization and case folding; a variant of an existing label is stored with the
first-introduced spelling and adds nothing. A label stays offered after every Customer
using it has moved away, been archived or been converted; none is ever removed, and
there is no count cap. `tasks_config_update` owns only the `tasks` row, so it can
neither clear nor write the vocabulary, and the vocabulary's growth does not change its
revision.

**Authority and atomicity.** A Customer create or state update that introduces a new
label appends it in the same prepared plan as the Task row. This is an engine-maintained
consequence of an authorized `tasks_create`/`tasks_update`, governed by that Task write's
own policy: it declares no `tasks_config_update` effect, grants no configuration
authority and creates no separate approval flow. The plan pins the vocabulary row's
current revision, or its absence, so two writers introducing labels cannot lose each
other's (the later one gets the ordinary revision conflict, and a retry reuses the
winner's spelling). A failed, denied, stale or rolled-back write adds nothing, and an
idempotent replay returns the saved result without appending again. Prepared-plan
validation accepts a `business_configs` effect in a Task plan only as one exact record
append of the label the Task introduces, in its first spelling: this scope's
`task_customer_states` row, inserted with exactly `domain` and `config_json` while absent
or updated with exactly `config_json` while present, no named operation, pinned to the
row's absence or revision, and not duplicated. Any other `business_configs` effect
(another domain, extra columns, another scope or row) is refused before application, and
a write that introduces no label may carry none. Delegation is unchanged: a constrained assignment
that does not name the `task_customer_states` row fails closed on a new label (it may
still reuse an existing one) and is not widened.

### Links between Tasks, Stories, Customers and Email Tasks

`tasks_update.changes.related_task` accepts `{business_id: UUID|null, task_id,
linked: boolean}`. A Task can link to multiple other Task records across business
and Global scopes. Every type uses the same identity and operation; linking does
not move either item, set a parent, propagate state, or subscribe participants.
Plain tags remain independent labels. Self-links are rejected; existing links,
including links created from the other end, are not duplicated.

A link is stored once in its source Task's engine-owned `$task_links` metadata.
It uses that Task's revision, prepared policy/Signoff flow and history. Custom
field replacement preserves this metadata and cannot inject it. Up to 100 links
may originate from one Task; incoming links are not limited by that count.
The target's exact revision and current read permission are checked at prepare
and application time. No new SQL schema or parallel relationship writer exists.

`tasks_get` returns `related_tasks` in both directions, resolving current titles,
types and scope names only after current access checks. Each entry contains
`link_source` with its scope, identity and revision: unlink by updating that
source (with its normal write permission). Reads paginate 50 links using
`related_tasks_next_cursor`, passed back as `related_cursor` to `tasks_get`.
Other Task reads remain lightweight. Existing same-scope generic relationships,
parents and informational dependencies retain their own contracts.
