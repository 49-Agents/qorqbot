# Operations engine design and source map

The engine was extracted from the earlier host core (called "v1" below). This document records the source map, the
deliberate differences from that code, the ports a host supplies, table ownership and the retained literals. It
documents contracts, not release status.

After the v1.32 merge: the host's `platform_actions` and `domains.email` that this document names as host-owned were retired with core
email. Their two Signoff ports (`platform`, `email`) remain optional declarations a host may leave `ABSENT`, which the Bazarlyk host does,
and `ActionOperations` is the one Action writer the host composes; `ActionWriteback` stays here as a library capability with no host caller.

## Source map

| v1 (`bazarlyk/…`) | Here (`qorqut_operations/…`) | Change |
|---|---|---|
| `contracts.py` | `contracts.py` | verbatim |
| `local_time.py`, `business_lifecycle.py` | same names | import path only |
| `application/registry.py`, `batches.py` | `application/registry.py`, `batches.py` | `batches` verbatim; registry adds `freeze()` and closes `declare`/`register` once frozen |
| `application/engine.py` | `application/engine.py`, `delegation.py`, `review.py` | see "Engine changes" |
| `application/ports.py` | `application/ports.py` | adds `DelegationProvider` (the former `SignoffStager.authorize_mail_control` was removed in v1.32) |
| `application/catalog.py` (BUILTIN_CATALOG data, `builtin_registry`) | `application/catalog.py` | the engine ships no operation names: `CatalogContribution` + `compose_catalog` (frozen registry; `operations_batch` registered by the engine) |
| `domains/common.py` (generic part) | `application/plans.py` | `PlanBuilder`, `apply_records`, `ref`, `record_key`, `rows`, `page*`, `require_revision`, `prepare_job`; Story/Action/limit branches moved behind `RecordEffectHooks` |
| `storage/database.py` | `storage/database.py` | see "Storage changes" |
| `storage/repositories.py` | `storage/repositories.py`, `specs.py` | catalog from the assembly, not a module-global `TABLES` |
| `storage/receipts.py` | `storage/receipts.py` | document availability/history behind `ResourceLifecycle` / `ReceiptHistory` |
| `storage/files.py` (`FileApplier`, `RegisteredFileTarget`) | `storage/files.py` | explicit `LockPaths` |
| `signoffs/execution.py` (`PreparedFileExecutor`, `application_claim`) | `storage/executor.py` | the file-execution part only, with explicit `LockPaths`; Signoff execution proper is in `signoffs/service.py` (see "Signoff, routine and quiescence") |
| `storage/safe_io.py` | `storage/safe_io.py` | unchanged on disk: `TEMPORARY_PREFIX = ".bazarlyk-"` is the retained v1 temporary-file prefix that recovery sweeps match (`storage/recovery.py`); the constant stays, there is no second prefix |
| `storage/recovery.py` admission classes | `storage/admission.py` | `InstallationRecoveryLock` takes an explicit path; `RecoveryAdmission` unchanged |
| `policy/resolver.py` | `policy/rules.py`, `resolver.py` | hard rules here, domain rules contributed |
| migrations 001–113 (SQL) | `storage/core_schema.py` | engine tables only; v1.3 maintains it directly (the derivation tool is removed, see "Retired product surface" below) |

## Precise differences from deployed v1

This package makes no blanket "no behavioural change" claim. The differences are exactly these:

1. **Fail-closed corrections (two).** A delegated call whose assignment row is missing is `forbidden` (v1 raised an
   internal `TypeError`), and a review-scope exception whose bound argument is absent never matches (v1 compared
   `None == None` for a composite ID).
2. **Explicit interfaces replace discovered globals**: the frozen assembly (table catalog, hooks), `LockPaths`
   (no installation-root discovery), `PolicyRules`, the typed ports (`SignoffStager`, `FileExecutor`,
   `DelegationProvider`, `AgentAuthority`) and `ReviewScopeException` data. These are authorized interfaces; the
   decisions made through them are the v1 ones.
3. **Storage model**: inert `Database`, explicit `initialize_target`/`open_existing`, a new `application_id`, the
   target identity table and the real-schema inventory validation below; no migration chain or older-format reader.
4. **Post-commit publication split**: a change-feed publish failure after `COMMIT` is logged (`change_publish_failed`)
   and can neither fail nor undo the committed unit (v1's feed had no such split).
5. **Structure only**: `platform_actions`/`_email_submission` are not carried (they became the `PlatformActions` port of
   the Signoff extraction), domain rules and tables are contributed by the host, and the removal of
   `Database.for_root`/`initialize(through=…)`.

Requiring `manage_delegated_assignments` in the engine's delegation check is preserved v1 behaviour
(`application/engine.py`), not a hardening.

## Engine changes (each one deliberate)

* `Application(database, registry, …, delegation=None, review_exceptions=(), delegated_creation_groups=())`. `platform_actions` and
  `_email_submission` (Signoff/email domain) are not carried; they are the optional `PlatformActions` port
  (`staging_values`/`staged`/`transition`/`replaceable_after_cancel`/`view`/`is_email_submission`).
* Delegation: the v1 checks are unchanged but live in `application/delegation.py` over a `DelegationProvider`
  (bindings, assignments, live authentication, role support, required capabilities, selector vocabulary, thread
  lookup, committed-action recording). With no provider every delegated call is `forbidden`. The engine itself
  requires `manage_delegated_assignments`, as v1 did, even if the provider's `required_capabilities` omits it. The one
  correction is that a missing assignment row is `forbidden` (v1 raised an internal `TypeError`).
* `delegated_creation_groups`: a tuple of typed `DelegatedCreationGroup(operation_name, resource_types)` values made by trusted
  assembly (a name and at least two distinct resource types; malformed values, non-typed values and two rules for one operation
  are refused when the `Application` is built; the empty default exempts nothing). It is the only addition to the delegated
  checks: every other delegated check (live binding, parent, assignment, generation, lease, role and capabilities, the
  operation allowance, the owner/external exclusions and the business boundaries) runs first and is unchanged. A prepared change
  qualifies for the record-ID membership exemption only when the actual descriptor name equals the group's operation and the
  whole expected-target list is exactly one typed `ExpectedTarget` per declared resource type, each expected absent with no
  revision or hash, all sharing one canonical new UUID, in the admission's own business (or Global) scope, with a resource
  namespace that is absent or exactly the canonical admission namespace. Existing rows, extra, duplicate or mismatched targets,
  bare `ResourceRef` values, another operation or another scope keep the assignment's record boundary. The targets stay in the
  prepared plan, so `check_targets` (expected absence) and the apply transaction still guard them.
* `_review_scope`: the three v1 exceptions become data (`ReviewScopeException`/`BoundResource`), supplied by
  trusted composition and checked exactly as before; the unlisted case still fails with `invalid_plan`. The host
  registers these three:
  `({"routines_run","routines_stop"}, {"jobs","routine_runs"})`,
  `({"agents_create","agents_update","agents_deactivate"}, {"business_memberships"})`, and
  `({"notes_update","notes_delete"}, bound_resources={"documents": BoundResource("document_id"),
  "managed_document_files": BoundResource("document_id","document_id")}, principal_kinds={"human"}, allow_delegated=False)`.
  A bound resource whose argument is absent never matches (v1 compared `None == None` for a composite ID).
* `TABLES` lookups (`_scope_plan`, `resolve_target`, `check_targets`, `PlanBuilder`, `ref`, `record_key`) read the
  frozen assembly of the database/unit; `PlanBuilder`/`ref`/`record_key` keep their v1 call signatures by using the
  running unit's catalog (or an explicit `tables=`).
* `apply_records`: `RecordEffectHooks.apply_effect` is offered every record/job effect first (Story events,
  collection limits and the pre-101 Action insert live behind it); `force_revision` replaces the per-table
  forced-revision conditions; `record_applied`/`tracked_tables` replace `history.record_applied`/`TRACKED_TABLES`.

## Storage changes

* `Database(path, *, assembly)` is inert. `initialize_target()` (explicit, empty stores only) creates the identity
  table, the composed DDL, `user_version`, a new `application_id` (`'QOPS'`, so a v1 database is never accepted) and
  the target name/version/digest; `open_existing()` validates exactly that without creating, converting or scanning.
  `Database.for_root`, `initialize(through=…)`, the migration chain and Python migration hooks are removed.
* **Schema validation** (`storage/inventory.py`). Only `initialize_target`, `open_existing` and `validate_snapshot`
  compile the expected inventory: the composed trusted DDL plus the identity table is executed in a disposable
  in-memory SQLite database and `sqlite_master` (type, name, owner table, SQLite's stored definition; `sqlite_*`
  excluded; no home-grown SQL parsing) is read back. The real store's catalog, opened read-only, must hold exactly
  those objects (missing, unexpected and changed objects fail by name, never with definitions or data), in addition
  to the application ID, `user_version` and identity row/digest checks, and `initialize_target` checks what it just
  created the same way. Every table spec's key, scope, revision and immutable column names must exist in the compiled
  schema or composition fails with `AssemblyError`. No DDL is ever executed against an existing target, no row is
  scanned and nothing is repaired. `change_sequences` (engine-owned, seeded by the core DDL) and `target_identity` are
  reserved names: a contribution cannot declare them as tables and DDL that redefines them fails to compile.
* **SQL helpers.** `storage/sqlfunctions.py:install_sql_functions` registers `inbox_actor`, `san_juan_date` and
  `san_juan_hour` (the v1 definitions) and is the single helper used by operational connections, the read-only
  `open_existing`/`validate_snapshot` connections (so expression indexes and triggers that call them can be read and
  integrity-checked) and the disposable inventory connection. Registering a function changes only that connection.
* Units run: body → (writers) publisher `capture` → contributed finalizers in registration order → `COMMIT` →
  publisher `publish_commit`. A capture/finalizer failure rolls the whole unit back; a publish failure is logged
  (`change_publish_failed`) and can neither fail nor undo the committed unit (v1's feed had no such split).
* `validate_snapshot` and `restore` are instance methods (they need the composition); `restore` returns an opened
  `Database`. `FileApplier`, `application_claim` and `InstallationRecoveryLock` require explicit absolute paths
  (`LockPaths`); nothing discovers an installation root.
* `ApplyReceipts` calls `ResourceLifecycle` (`applying`/`conflicted`/`applied`/`released`) by resource type and the
  required `ReceiptHistory.capture_receipt` in the finalising transaction, at the points v1 updated document
  availability and shared history. A receipt cannot be finished without a verified file result. The same call
  sequence is kept; the engine hard-codes no resource name.
* **Required lifecycles.** `StorageContribution.required_lifecycles` (frozen set of resource-type names, default
  empty) is merged into the frozen `Assembly`, and `AssemblyBuilder.freeze()` raises `Required hook missing: resource
  lifecycle for <names>` when a required name has no `FileEffectContribution` lifecycle, so a composition missing one
  is rejected before any `Database` can be initialized or opened. A resource type nobody declared required has no
  lifecycle callbacks (the lookup is optional by design; generic file resources need no meaningless lifecycle).
* `PolicyResolver(registry, agent_resolver, *, rules, …)` and `resolve_operation_policy(…, *, rules)` require the
  explicit `PolicyRules`. The agent's stored profile, role and plugin packages arrive through the `AgentAuthority`
  port (`refresh_invocation_context`, `policy_basis`, `plugin_declares`). Precedence is documented in `policy/rules.py`.

## What the host still contributes (not in this package)

* Domain tables, indexes, triggers and sequence seeds (`StorageContribution`); `artifacts`, delegation, Context,
  Task/Action/thread, inbox and history tables are all host/later-step ownership (the Signoff, `jobs` and routine
  tables moved into the engine's core schema; see "Signoff, routine and quiescence").
* The operation catalog data (`BUILTIN_CATALOG`, v1 `_READS`/`_MUTATIONS`/…) and installed descriptors.
* The v1 domain permission rules (Telegram, tables, products, email aliases, inbox, milestones, workspace, …) as
  `PolicyRules` in the three phases; `focus_move`'s `focus_order_owner_only` provenance is a before-override rule,
  so it is reported as `owner_only` only if a descriptor is also marked owner-only.
* `ChangePublisher` (change feed), `ReceiptHistory`, `RecordEffectHooks`, and the `documents` `ResourceLifecycle`.
  The host's documents `StorageContribution` must declare `required_lifecycles=frozenset({"documents"})`, because v1
  tracked document availability beside every file receipt; the synthetic checks' contribution
  (`tests/support.py`) declares exactly that requirement and the engine itself does not know the name.
* The `SignoffPorts`, the `ProgramRegistry` contents and `JobObserver`, the file target resolver,
  `DelegationProvider`, `AgentAuthority`. Worker profiles are derived by the engine's `BackgroundAuthority`.
* SQL helper functions: the engine registers `inbox_actor`, `san_juan_date` and `san_juan_hour` on every connection
  (operational, read-only validation, disposable inventory) because retained-domain DDL (inbox/email triggers,
  expression indexes) from v1 references them. Moving them behind a storage contribution is a candidate for the
  next step; there is no plugin framework for them.

## Signoff, routine and quiescence

Scope: the Signoff service/inputs/notes/rejection/notifications, the registered-program
background (programs, authority, serial job service, routine scheduler), the generic routine operations and reads, and
the generic recovery quiescence. The code is the deployed v1 code moved mechanically: SQL, literals, ordering,
transaction boundaries and error codes are kept, and every host reach (import, `getattr` on the application, lazy
import) became one constructor-injected typed port. Nothing here adds v2.2 grants, Signoff self-relays, script routines
or scheduling freedoms; approval still applies the saved v1 proposal automatically, input responses authorize nothing.

### Source map

| v1 (`bazarlyk/…`) | Here (`qorqut_operations/…`) | Change |
|---|---|---|
| `signoffs/service.py` | `signoffs/service.py` | host reaches → `SignoffPorts`; `SignoffService(database, ports, *, locks, file_applier=None, clock, id_factory)` + one `bind(application)` |
| `signoffs/inputs.py`, `notes.py`, `rejection.py`, `notifications.py` | same names | ports only; `NOTE_REFS` derives from `ReferenceVocabulary.targets` |
| `signoffs/platform_review.py` (`PlatformApproval`) | `signoffs/platform_approval.py` | renamed; only the shared exact-decision orchestration; the concrete Reddit/Email approvals (`signoffs_reddit_approve`, `signoffs_email_approve`) are host subclasses |
| `background/programs.py`, `authority.py`, `service.py`, `routines.py`, `schedule.py` | `background/…` | `ProgramRegistry.freeze()/bind_authority`; `BackgroundService(…, locks, job_observer, scheduler)`; `RoutineScheduler(operations, database)` |
| `domains/routines/operations.py`, `reads.py` | `routines/operations.py`, `reads.py` | `RoutineOperations(programs, authority, *, clock)` is application-free; catalog entries + `contribution()` |
| `sandbox/quiescence.py` (generic part), `sandbox/recovery_gate.py` | `storage/quiescence.py`, `storage/recovery_gate.py` | `RecoveryQuiescer`, `QuiescenceProof`, `BoundedWriter`, `KnownZeroWriters`, `DatabaseMaintenanceGate`; Docker freezers/ownership stay host |
| `roles.py` ids, `domains/conversations.py`, `domains/team_channel.py` | same names | pure helpers the extracted code compares against; the department registry (`departments.py`) is removed in v1.3 |
| thread names `bazarlyk-signoffs`, `bazarlyk-background` | `operations-signoffs`, `operations-background` | the only identifier changes that are not import paths |

### Precise differences from the extracted code (Signoff, routine, quiescence)

1. `SignoffService`, `RoutineOperations`, `RoutineScheduler` and `BackgroundService` no longer take the whole
   `Application` as their only dependency: the database, typed ports and `LockPaths` are explicit constructor inputs
   (`Application` is still needed to authorize and apply, so `SignoffService.bind(application)` is called once;
   `BackgroundService` takes it directly). `application_claim` uses the explicit `LockPaths`.
2. The v1 `getattr(self.app, "incident_reporter", None)` in the job service became the `JobObserver` port
   (`job_result(row, error)`, same call points, same swallow-on-error); `Application.incident_reporter` is unchanged.
3. `ProgramRegistry` registration ends at `freeze()`; `BackgroundAuthority` binds itself to the registry once
   (`bind_authority`, v1 assigned `programs.authority = self`). The job values, definition digest, worker identities and claims are unchanged (golden digests
   from the deployed module are asserted in `tests/test_background.py`).
4. Optional domains must be declared (`ABSENT`) and the operations that need an absent domain return
   `operation_unavailable` (the delegated input-answer descriptor is not installed without `management`; the `focus`
   argument does not exist without `focus`; platform staging fails with `operation_unavailable`). Where v1 treated the domain as optional the behaviour is v1's.
5. `FileExecutor.recover_pending` is now part of the `FileExecutor` Protocol (v1 already called it on
   `app.file_executor` from the Signoff worker's file recovery pass).
6. `StorageContribution.required_tables` now also fails `freeze()` for a required table that no
   contribution specifies, and `compile_target` fails for one that is absent from the composed schema.
7. Everything else in the moved modules is the v1 text with import paths changed.

### Port call map (v1 source → typed port)

`SignoffPorts(execution, peers, tasks, references, platform, email, management, focus)`; the first four are
required (missing or incomplete fails when `SignoffPorts` is built, i.e. at assembly), the rest must be declared with an
implementation or `ABSENT`. A `None` for an optional port is also an `AssemblyError`.

| Port (required?) | Method / attribute | Replaces (v1) | Engine call site | Returns |
|---|---|---|---|---|
| `execution` (required) | `execution_states` | `platform_actions.proposals.EXECUTION_STATES` | `signoffs_list` schema enum | ordered names |
| | `execution_state(row, uow=None)` | `proposals.execution_state` | `_dto` | one of those names |
| | `execution_sql("s")` | `proposals.execution_sql` | `signoffs_list` filter | fixed SQL over alias `s`, only embedded |
| `peers` (required) | `configured_ordubashy(uow, *, require_codex)` | `management.identity` | `authorize_mail_control`, `_validate` | coordinator principal id or `None` |
| `tasks` (required) | `open_assigned_count(uow, business_id, principal_id, now)` | `tasks.models.DEFAULT_CONFIG` + `tasks.snooze.ACTIVE_SQL` inline SQL | `notifications.overview` | `int` |
| `references` (required) | `targets` (`{kind: ReferenceTarget(operation, argument)}`) | `tasks.operations.NOTE_REFS` and the kind table | note schema; `notes.references` (engine authorizes the read op, then calls its read handler) | ordered mapping |
| | `task_namespace(ctx, args)` | `tasks.operations.task_namespace` | delegated answer namespace | namespace string |
| `platform` (optional) | `staging_values(ctx, plan, old, uow)` | `platform_actions` staging | `stage`, before the row insert | `{"id", "execution_job_id"}` |
| | `staged(ctx, row, plan, old, uow)` | same | `stage`, after the insert, same transaction | `None` |
| | `transition(row, updated, uow)` | same | `_update` for `platform_action` rows | `None` |
| | `replaceable_after_cancel(old, uow)` | same | supersession check | `bool` |
| | `view(row, uow=None)` | `proposals.platform_view` | `_dto`, `PlatformApproval.target` | view mapping with `submission` |
| | `is_email_submission(plan)` | `Application._email_submission` | `stage` classification | `bool` |
| `email` (optional) | `visibility(ctx, uow)` | `domains.email.decisions` | `_visibility` (appended verbatim: a nonempty string carries its own leading ` AND `, empty is allowed; can only narrow) | `(sql, params)` |
| | `email_context(row, uow)` | same | `_dto` of input requests | mapping or `None` |
| `management` (optional) | `managed_input_sql` | `management.service.MANAGED_INPUT_SQL` | `signoffs_list`, `notifications._rows` | fixed SQL over `s` |
| | `coordinator(uow)` | `ManagementService._coordinator` | delegated answers (raises `management_unavailable`) | principal id |
| | `auto_route_input(ctx, row, uow)` | `ManagementService.auto_route_input` | input creation, same transaction | `None` |
| | `routing_for(uow, signoff_id)` | direct `management_routing` query | delegated answers | row mapping or `None` |
| `focus` (optional) | `fields` | `inbox.attention.FIELDS` | `signoffs_input_create` schema | property schema |
| | `creation_access(application, ctx, focus, uow)` / `present_created(application, ctx, focus, source, uow)` | `inbox.attention.*` | input creation prepare / apply | `None` |
| | `release_signoff(row, updated, uow)` | `AttentionBoard.release_signoff` | `_update` | `None` |
| `narrowing` (optional, new; default `ABSENT`, no v1 counterpart) | `narrow(ctx, uow)` | host-owned (WhatsApp Signoff privacy) | `_visibility`, last, so every reader (`signoffs_get`/`list`/notes/decisions/owner attention) applies it | `(sql, params)`: empty or exactly one ` AND (<predicate>)` over `s`; `checked_narrowing` refuses anything else, so it can only remove proposals from a reader's view |
| `JobObserver` (optional) | `job_result(row, error=None)` | `app.incident_reporter` | `BackgroundService._observe` | `None` |
| `FileExecutor` (existing) | `recover_pending(app, *, limit)` | same | `SignoffService._recover_pending_files` | receipts |
| Quiescence controllers | `drain(*, timeout)`/`resume()`, `inventory_and_freeze`, `freeze`/`thaw` | `sandbox.quiescence` | `RecoveryQuiescer.quiesce` | `QuiescenceProof` |

Engine-side authorization kept: the visibility predicate, owner/delegated decision paths, standing-authority re-checks,
reference-read authorization, the rejection-message rule, receipts/idempotency. No port can grant
authority, rewrite a prepared plan, or replace the visibility predicate.

### Operations-owned operation IDs (catalog contributions)

* `SignoffService.contribution()` (owner `signoffs`): reads `signoffs_list`, `signoffs_get`, `signoffs_notes_list`;
  records `signoffs_comment`, `signoffs_reject`, `signoffs_input_create`, `signoffs_input_cancel`,
  `signoffs_attention_set`, `signoffs_input_answer_delegated` (installed only with the `management` port);
  owner-only dashboard/worker `signoffs_review`, `signoffs_retry_apply` (metadata only, as in v1: the owner HTTP
  routes call `review`/`retry`; no agent descriptor exists).
* `RoutineOperations.contribution()` (owner `routines`): reads `routines_list|get|preview|runs|costs`; records
  `routines_create|update|delete|pause|resume|run|stop`. The host still supplies the review-scope exception for
  `routines_run`/`routines_stop` (see "Engine changes").
* Host-owned and not moved: `signoffs_platform_submit`, `signoffs_reddit_approve`, `signoffs_email_approve` (the last two concrete
  `PlatformApproval` subclasses). `signoffs_review`, `signoffs_retry_apply` and `signoffs_respond` are also the
  idempotency-receipt operation names of `review`, `retry` and `respond`; `respond` has no catalog entry in v1.

### Ownership of tables, indexes, triggers and hooks

Engine-owned (`storage/core_schema.py`, maintained directly): `principals`, `businesses`,
`business_memberships`, `installation`, `agent_profiles`, `signoffs`, `signoff_notes`, `signoff_attention`,
`signoff_notifications`, `signoff_notification_schedule`, `idempotency_receipts`, `apply_receipts`, `resource_claims`,
`maintenance_state`, `change_sequences` (seed `signoffs`), `jobs`, `routines`, `routine_runs`, `routine_slots`.
Newly engine-owned here: `signoff_notes`, `signoff_attention`, `signoff_notifications`,
`signoff_notification_schedule`, `jobs`, `routines`, `routine_runs`, `routine_slots`.

Repository specs (`storage/specs.py` `CORE_TABLES`, with the v1 keys, scope and revision columns) exist for the tables
v1 also had specs for: `installation`, `principals`, `businesses`, `business_memberships`, `agent_profiles`, `signoffs`,
`idempotency_receipts`, `apply_receipts`, `resource_claims`, `maintenance_state`, `routines`, `routine_runs`,
`routine_slots`, `jobs` (and, with "Neutral items and relationships" below, `relationships`, `context_reviews`,
`context_review_states`). The four Signoff adjunct tables `signoff_notes`, `signoff_attention`, `signoff_notifications`
and `signoff_notification_schedule` have **no** repository spec, exactly as in v1: the Signoff code reads and writes
them with direct SQL on the unit's connection, and `change_sequences` is read and advanced by the engine's own SQL.
They are engine-owned tables (DDL, indexes and triggers here) that are not reachable through `uow.repository(...)`.

* Indexes kept: `agent_profile_active_names`, `agent_profiles_one_active_role_per_home`, `signoffs_category_queue`,
  `signoffs_execution_job`, `signoffs_open_request_key`, `signoffs_queue`, `signoffs_updates`,
  `signoffs_working_attention`, `signoff_notes_thread`, `signoff_notifications_owner`, `jobs_business_kind_created`,
  `jobs_ready`, `routine_runs_business_started`, `routine_runs_namespace_started`, `routine_runs_state_started`.
* Triggers kept: `signoffs_immutable_payload|platform_binding|request_key|response`, `idempotency_receipts_no_update`,
  `apply_receipts_immutable_payload`, `signoff_notes_no_delete|no_update`, `jobs_immutable_bindings|dispatch_intent|
  external_receipt|outputs`.
* **Left to host contributions, exactly one owner each** (omitted from `core_schema.py`; the engine neither creates
  nor assumes them): triggers `inbox_capture_signoffs_insert|update|delete` and
  `inbox_capture_signoff_attention_insert|update|delete` (change-feed/inbox capture contribution),
  `email_history_signoff_insert|update` (email-history contribution), and index `jobs_business_kind_upload`
  (Context contribution: it keys Context uploads in `jobs`).
* Hooks: no new engine hooks. The Signoff service is the `Application.signoffs` stager; `FileExecutor.recover_pending`
  and the `JobObserver`/controller ports above are constructor-injected; host `ReceiptHistory`/`ResourceLifecycle`
  contributions are unchanged. Operation tags written for the host change feed: `signoffs_notification_tick|dismiss|
  snooze`, `jobs_routines_tick`, `jobs_run`.
* Newly owned `jobs` immutability: `storage/specs.py` `CORE_IMMUTABLE_FIELDS["jobs"]` = `business_id`,
  `target_namespace`, `kind`, `principal_id`, `initiated_by_principal_id`, `arguments_json`, `definition_ref_json`,
  `dedupe_key`.

### Host tables each port/query requires

Declared through `StorageContribution(required_tables=…)` and checked at freeze/initialize/validate:

* `signoffs.storage_contribution()` → `inbox_changes` (`source_type`, `action`, `actor_id`, `before_json`,
  `after_json`; insert-only from `notes.append`, which feeds the host change feed with `signoff_notes` events).
* `background.storage_contribution()` → `documents` (`id`, `kind`, `business_id`; read by `BackgroundAuthority.validate_plan`
  to keep automatic work off mandatory instruction documents).

Behind ports (not required of the engine, required of the host's port implementation): `OwnerTaskAttention` → v1 read
`business_configs` (`tasks` config), `tasks`, `relationships` (assignee role) and the snooze predicate;
`ManagementQueries` → `management_routing` (+ the `inbox_events` write of `auto_route_input`); `PlatformActions`,
`EmailDecisions`, `FocusAttention`, `PeerIdentities` → the platform-action, email, Focus/attention and agent-roster
(`agents.global_peers`) state of the host. The engine's other SQL reads only engine-owned tables.

### Retained domain literals (verbatim from v1, in engine code)

* Role ids (`roles.py`, plus inline `'ordubashy'` in `signoffs/inputs.py`, `rejection.py`, the agent-visibility SQL in
  `signoffs/service.py`): `ordubashy` (plus the global `global-worker` and `learning-applier` ids); runtime `'codex'` and
  scope `'ecosystem'` in the Ordubashy visibility SQL (the former `account_email_%`/`email_%` operation-prefix clause was removed in v1.32).
* Standing authority and operation set in `signoffs/service.py`: `business_archive` attention in `stage`. The engine
  issues no standing authority (the v1 `ordubashy_mail_control` and `MAIL_OPERATIONS` were removed in v1.32). A proposal
  that records any authority (the removed `marketing_business_execution` or `ordubashy_mail_control`) is refused as
  `forbidden` and never executed; capability names `read_global_context`, `manage_global_routines`.
* `routines/operations.py`: `learnings.extract` / `learnings.extract_global` (supersession-mode owner rule);
  no department ids or labels exist. The former `learnings.extract` each-business target that also included
  the Global workspace is removed (an each-business routine never includes the Global workspace): an each-business routine targets businesses only and a root routine
  targets Global only, and the scheduler refuses a saved target the program's scope kinds do not support.
* `background/authority.py`: protected document kinds `pillar`, `shared_instruction(s)`, `mandatory_brief`,
  `agent_instructions`; `signoffs/notifications.py`: `America/New_York` 08:00 summary and hourly urgent cadence;
  `team_channel.py`: `owner_ordubashy` / `owner_conversation_send`; `storage/recovery_gate.py` maintenance message
  `Installation recovery in progress`; `RecoveryQuiescer` slot names (`background`, `signoffs`,
  `telegram`, `browser_writers`, `container_writers`, `host_writers`).
These literals are retained as they are in the engine; they are not behavior changes and not open decisions. The same
holds for the pure `conversations` and `team_channel` helpers, the role ids, the learning program ids,
the protected document-kind literals and the Signoff timing: they stay in the engine unless a later change
specifies otherwise.

### Remaining host and neutral-item dependencies

1. Host composition (`qorqut-code`): the `SignoffPorts` implementations, the concrete `Program` set and
   `ProgramRegistry.freeze()`, `JobObserver`, `PlatformApproval` subclasses, quiescence controllers (Docker, terminal
   inventory, browser/sandbox leases) and the `ReviewScopeException`s for `routines_run|stop`.
2. Host tables and triggers listed above (`inbox_changes`, `documents`, the omitted capture/email-history triggers and
   the Context upload index), and the SQL helper functions they call.
3. Host implementations of ports whose contracts are fixed: `OwnerTaskAttention`/`ReferenceVocabulary` over the new
   Task/Action/thread model. The retained literals above, `conversations`, `team_channel`, role ids,
   learning program ids, protected document kinds and Signoff timing are retained in the engine and are not
   unresolved decisions.
4. Telegram/notification bridge: `SignoffNotifications.deliveries_in` is the engine API a bridge would call; the
   bridge itself is host.

## Neutral items and relationships

Scope: the shared item identity contract, the generic relationship operations, and the
explicit context reviews, moved from the deployed v1 `domains/relationships/*` into the engine behind typed,
required host ports. The code is the v1 flow with its operation IDs, authority metadata, descriptions, revisions,
cursor binding, error codes and result shapes unchanged. What v1 knew about the other domains (which item kinds
exist, how each is read and fingerprinted, the role vocabulary, Material/archive adapters, navigation counts and which operations a review may compose) is now supplied by the host. Nothing here adds
access: registering a kind grants none, enumeration grants none, a port cannot replace application policy.

### Source map

| v1 (`bazarlyk/…`) | Here (`qorqut_operations/…`) | Change |
|---|---|---|
| `domains/relationships/items.py` (`ContextItems`) | `items.py` | a class that held the Story service and every domain → `ItemContribution`, `ItemLookup`, `ItemCandidates`, `ItemResolver`; `authorize` → `authorize_item_read`; `item_revision`; `UNAVAILABLE` |
| `domains/relationships/operations.py` | `relationships/operations.py`, `edges.py`, `ports.py` | `RelationshipOperations(items, ports, *, clock)`; `ROLES`/`WORKFLOW_ROLES`/`protected_edge`/Material/archive/navigation → `RelationshipPorts`; helpers `endpoints`, `edge_view`, `edge_values`, `related_rows`, `create_edge` |
| `domains/relationships/reviews.py` | `relationships/reviews.py` | `ContextReviews`; the `EDITS` table → `ReviewEdit` data in `RelationshipPorts.review_edits` |
| `domains/stories/reviews.py` | not moved | Story-specific review behavior is retained until ordinary parent Tasks replace Stories; the shared related-context ownership moves to the host; the generic reviews are this module's |
| `application/catalog.py` relationship lines | `relationships/operations.py` `RELATIONSHIP_ENTRIES` | the sixteen v1 catalog lines, unchanged |
| migrations 014–019, 026, 035, 046, 056, 111 (`relationships`), 016 (`context_reviews`, `context_review_states`) | `storage/core_schema.py` | the three tables, the six indexes and two triggers below |
| `storage/repositories.py` `TABLES` lines | `storage/specs.py` `CORE_TABLES` | the three v1 specs, byte-for-byte |
| `domains/common.py` `apply_records` review branch | `application/plans.py` `apply_records` | the engine-owned `append_context_review` effect |

### Exact interfaces

`qorqut_operations.items`

* `ItemContribution(owner, kinds, lookup)`: frozen, validated at construction (`AssemblyError`): `owner` a lowercase
  name, `kinds` a **nonempty tuple** of lowercase snake_case names, no duplicate kind inside it, `lookup` an object
  with callable `resource`, `resolve`, `relationship_item` and `fingerprint`.
* `ItemLookup` (Protocol; `resource`, `resolve(ctx, item, uow, builder=None, *, allow_busy=False)`,
  `relationship_item(ctx, item, uow)`, `fingerprint(ctx, item, uow, builder=None)`) and `ItemCandidates.candidates(ctx,
  uow, kind=None)`. The lookup keeps its domain's private-source, availability and membership rules and authorizes its
  domain read through `authorize_item_read`.
* `ItemResolver(application, contributions, *, candidates)`: immutable (`__slots__`, read-only properties). A
  duplicate owner, a duplicate kind across owners, an empty or untyped contribution list, a missing `candidates`
  port, or an `Application` without its policy is an `AssemblyError`. Exposes `application`, `contributions`, the
  ordered `kinds`, `item_schema` (the item JSON schema derived from the registered kinds; a fresh copy per call),
  `authorize(ctx, operation, uow)`, `resource`, `resolve`, `relationship_item`, `fingerprint` and `candidates`. An
  unregistered kind is `not_found` ("Context item does not exist in this business"), as for a missing item.
* `authorize_item_read(application, ctx, operation, uow)`, `UNAVAILABLE` (v1's set of lookup failures that mean "not
  available to this caller now": skipped in lists, the graph and review status, every other error propagates) and
  `item_revision(row)` (`revision`, else `status_revision`, else `record_revision`, else 1).
  The refusal text is `ContextItems.authorize`'s ("The item's read operation is not permitted"). v1's
  `StoryOperations.related` used its own text ("The linked item's read operation is not permitted"); that Story-side
  copy is not carried (the host removes the shared `StoryOperations.related` ownership now; see "Remaining host work").

`qorqut_operations.relationships`

* `RelationshipOperations(items, ports, *, clock=utc_clock)`; `.contribution()` is a `CatalogContribution` (owner
  `relationships`) of the sixteen v1 entries `relationship_roles_list`, `relationships_list`, `relationships_get`,
  `context_items_list`, `context_items_get`, `context_navigation_get`, `context_graph_get`, `context_reviews_list`,
  `context_reviews_history` (reads) and `relationships_create`, `relationships_update`, `relationships_remove`,
  `context_items_archive`, `materials_update`, `context_review`, `context_review_reopen` (records, Signoff-capable),
  all exposed to `mcp`, `dashboard` and `worker`, none owner-only. `.reviews` is the `ContextReviews` helper. Review
  edits naming an item kind that the resolver does not register fail at construction.
* `RelationshipPorts(roles, workflow_roles, materials, archive, navigation, review_edits)`: frozen;
  every field is required and validated when the value is built, `roles` (ordered `role -> (label, inverse)`),
  `workflow_roles` (`role -> dedicated tool text`, a subset of `roles`) and `review_edits` are copied into read-only
  mappings. The ports are `MaterialClassification` (`filters`, `material_types`, `tags`, `matches`, `prepare`,
  `apply`: the existing Material mutation), `ItemArchive` (`prepare`, `apply`), `ContextNavigation.read(ctx, args,
  uow)`. v1 also had a business-setup `ProtectedEdges` port; it was removed with business setup (see below), so no stored
  edge is protected by its role name and the ordinary revision, workflow-role and policy checks are the whole contract.
* `ReviewEdit(item_kind, argument, resource_type=None, apply_through_domain=False)`: one operation a review may
  compose, keyed by operation name. `argument` must equal the reviewed item's id, `item_kind` (or, when it is `None`,
  the stored `resource_type` of the item) must match, otherwise `validation_error`. The nested operation is looked up,
  its arguments bound, prepared and target-validated through the one application registry, so it is judged by its own
  operation policy (a nested `deny` is `policy_denied`, a nested Signoff floor is honored) and its changes keep their
  `operation_name`. `apply_through_domain` applies the composed plan through the nested operation's own `apply`
  (v1 table updates and row writes) instead of as plain prepared records.
* Helpers: `endpoints(edge)`, `edge_view(edge, roles)` (**signature change**: v1 read the module-level `ROLES`; the
  predefined flag is `edge["role"] in roles`), `edge_values`, `related_rows`, `create_edge`.
* Directional DTOs (`source`/`target`, `source_item`/`target_item`, `predefined`, `revision`), filters (`item` with
  `direction`, `source`/`target`, `source_type`/`target_type`, `role`, `query`, `include_archived`), newest-first
  ordering and the cursor binding (`page`/`page_position`) are v1's. A Message-like endpoint is stored as the
  conversation its lookup's `relationship_item` names.

### Composition order (settled)

`ItemResolver(application, contributions, *, candidates)` is the specified and settled constructor; there is no bind
API and no second registry. The host constructs the `Application` over its one open `OperationRegistry`, then the
`ItemResolver` and `RelationshipOperations`, registers every trusted contribution into that registry (`declare` every
entry, then `register` every descriptor, as `compose_catalog` does), `freeze()`s it, and calls
`SignoffService.bind(application)`, all before traffic. The resolver only uses the `Application` when one of its methods
runs, so building it before the registry is populated is sound and nothing is reachable before `freeze()`. The checks
and the installed-wheel slice compose it exactly this way.

### Tables, indexes, triggers

Engine-owned in `storage/core_schema.py` with the v1 DDL (regenerated by the tool, which reproduced the previous file
before it was extended):

* `relationships` (PK `(business_id, id)`; `business_id` nullable for Global edges; `created_by` → `principals`).
  Its `CHECK` is the v1 one with the retired group item type replaced by Tasks (see "Ordinary parent Tasks"): `business_id IS
  NOT NULL` permits every Business row outright (the constraint restricts no Business shape). A Global row
  (`business_id IS NULL`) must match one of: a `task` target with `concerns` (anything related to a group Task), a
  `task` source with `concerns`; a `task` source with `assigned_to` an `agent` or `person`, or `blocked_by` a `task`;
  or a `material`, `topic` or `person` source with `part_of` a `topic`, `concerns` or `mentions` a `topic` or `person`,
  or `extracted_from` a `material`. Hierarchy is the Task's `parent_id`, never a `part_of` edge, and a Global agent
  `participates_in` edge no longer exists (assignees say who works). The Global alternatives therefore carry Context
  (material/topic/person) shapes as well as Task vocabulary. This is the one engine definition that differs from the
  deployed v1 final schema; `derive_host_schema.py` names that revision and checks the exact replacement.
* `context_reviews` (append-only; `mutable=False`, no revision column) and `context_review_states` (one mutable row
  per `(business_id, relationship_id, item_type, item_id)`, FK to `relationships ON DELETE CASCADE` and to
  `context_reviews`). Removing a relationship therefore removes its review state and keeps the decisions.
* Indexes kept: `relationships_from`, `relationships_global_key`, `relationships_memory_observe`,
  `relationships_role`, `relationships_to`, `context_reviews_history`. Triggers kept: `context_reviews_no_update`,
  `context_reviews_no_delete`.
* Specs (`storage/specs.py`): `relationships` `(("business_id","id"), "business_id")`, `context_reviews`
  `(("business_id","id"), "business_id", False, None)`, `context_review_states`
  `(("business_id","relationship_id","item_type","item_id"), "business_id")`: the v1 lines.
* **Capture/history hooks that stay with the host** (omitted from `core_schema.py`; exactly one owner each):
  triggers `inbox_capture_relationships_insert|update|delete` (change-feed capture of `relationships`; they call the
  `inbox_actor` helper the engine already registers). The engine records no shared-history event for review decisions
  (v1 did not either) and offers `relationships`/`context_review_states` changes to the host `RecordEffectHooks`
  exactly as any other record effect.

### The `append_context_review` effect

`application/plans.py:apply_records` handles `action == "append_context_review"` on `context_reviews` itself: the
decision's content, identity, actor and source token are frozen in the prepared plan; `created_at` is allocated only
in the applying transaction (so a staged proposal's decision time is the approval/application time, later than its
`prepared_at`), the row is inserted through the repository and returned. The effect is never offered to
`RecordEffectHooks.apply_effect`, and `record_applied` is not called for it. The same action on any other table is an
`invalid_plan`. The host removes only its duplicate branch for this action.

### Remaining host work

1. `ItemContribution`s and lookups for each item kind the host keeps (documents/topics/people/materials, tasks, tables,
   products, mailbox messages …) with their private-source and availability rules, the `ItemCandidates` port, and the
   `RelationshipPorts` adapters (roles and workflow tools, Material/archive/navigation,
   the `ReviewEdit` table of the v1 `EDITS`).
2. The host triggers listed above, the review-edit operations themselves (`tasks_update`, `docs_update`, …), and
   domain-built plans that call `create_edge`.
3. The shared resolver left the group service; ordinary parent Tasks removed the group service itself
   (see "Ordinary parent Tasks").

## Operational records and the durable queue

The engine owns the shared Task/Action tag catalog, the canonical item discussion and inbox persistence, Tasks,
Actions with immutable revisions (each stored revision is handed to the host-supplied `on_revision` admission; the Context input queue itself is Context's, see "Queue, filing and graph edits moved to Context"). Immutable revisions, thread/follow rules, snooze
reconciliation and queue transitions run on the one `Application`/`InvocationContext`/`PlanBuilder`/unit of work; there
is no second Database, Actor or policy evaluator, no tag-derived ownership and no second Signoff-grant or routine model.

### Packages and composition order

`StorageContribution` order (each later package may `required_tables` an earlier one):
`tags` -> `discussion` -> `tasks` -> `actions` -> `signoffs` (a host splices Context's `context-queue`, `context-filing`,
`context-graph-edits` and `context-usage` contributions after `actions`). Host composition exposes each
package's `storage_contribution()` and one `*Operations(...).contribution()` catalog contribution.

| Package | Owns | Constructor / entry |
|---|---|---|
| `tags` | `tags`, `item_tags`; `tags_list`, `tags_create` | `TagOperations(clock=)` |
| `discussion` | `inbox_events`, `inbox_deliveries`, `inbox_subscriptions`, `thread_messages`, `thread_follows`; `emit_event`, `auto_follow`, `set_follow`, relay/attribution, system entries | functions only (no operations) |
| `tasks` | `tasks`, `task_completions`, `task_snooze_wakes`, `task_moves`; `tasks_list/get/thread_get/notes_list/config_get/overview`, `assignees_list`, `tasks_create/update/assign/comment/complete/config_update/follow` | `TaskOperations(application, ItemResolver, TaskPorts, clock=)`; `TaskOperations.reconcile_snoozes(uow, now=, limit=)`; `TaskOperations.note_references()` |
| `actions` | `action_logs`, `action_businesses`, `action_revisions`; `actions_list/get/config_get/thread_get/record/correct/withdraw/comment/follow`; writer `new_action`/`change_action`/`settle` | `ActionOperations(application, references, *, on_revision, clock=)` where `references` is `tasks.note_references()` (build the Task service first) |

The engine imports no host module. What a host must supply comes in as `TaskPorts` (assignee rules,
Focus, incident link, note references, owner-work routing) and the neutral `ItemResolver`.

### Task contract (frozen matrix, unchanged v1 authority)

States `open`, `working`, `waiting`, `done`, `cancelled` (done/cancelled are closed). One shared `snoozed_until`
(UTC timestamp) with an idempotent set/clear and one reconciler wake per snooze generation; a snooze never changes the
state. Multiple assignees, parent Tasks (same owning namespace, no cycle, independent child/parent state) and
informational dependencies (`blocked_by`, same namespace, no cycle) never gate state changes. Tags are catalog labels
only (ordinary labels: no alias or department canonicalization); tags grant no access and never select an owner. Writes keep the
retained v1 gates: policy decisions per operation, Signoff-capable mutations, `expected_revision`, owner-request and
channel rules, contact/peer rules (ports), configured fields. `tasks_follow` is a policy-gated personal preference.

### Discussion and inbox rules

* One writer: every Task note, system entry and relay is `thread_messages` written through `discussion.threads`;
  the only item types are `task` and `action` (a trigger refuses anything else, so a private DM is never a thread).
* A responsibility event (assignment, a state/detail change, a removed assignee, a blocker finishing for dependent
  work, host-required routes) is a **required** delivery whatever the follow state. Comments and Action events are
  **optional**: they reach only current followers. `required` wins over `optional` for one recipient and the actor is
  never notified. Recipients must be active agents allowed in the item's owning scope.
* Unfollow is sticky: `auto_follow` (creator, assignee, commenter) never changes an explicit row, including a
  tombstone; only `set_follow(..., True)` re-follows.
* Relay attribution (`explicit_owner_statement`, ...) is coordinator-only and recorded with the recorder.
* The comment write is `discussion.plan_comment(...)` for Tasks and Actions alike (summary/reference/relay/blank
  validation, owner team-channel guard hook, attribution); Actions resolve typed note references through
  `tasks.note_references()` (`DiscussionReferences`), so there is no second message implementation.

### Actions

An Action has one owning storage namespace (a business, or Global) fixed at creation. `action_businesses` are
read-visibility links only. Every record, correction and withdrawal is an immutable `action_revisions` row
(1 = `add`, then `edit`/`remove`); the current `action_logs` row is a projection. Correction and withdrawal belong to
the original recorder in the owning scope, or the owner, plus the normal per-operation grant; withdrawal is final.
Writers call `new_action`/`change_action` on a `PlanBuilder`, then `apply_records` and **`settle(ctx, plan, uow, *, on_revision)`**:
`settle` hands the exact stored revision to the required `on_revision` callable (Context's queue admission in a Context host) and announces, inside the applying transaction.

**Tag envelope.** Each revision freezes `snapshot_json.tag_snapshots = [{id, name, business_id}]`, sorted by
(`name` casefolded, `id`): `id` is the stable catalog id, `name` the catalog's immutable display label, and
`business_id` is ALWAYS null (ordinary tags are shared labels; no business tag is synthesized and the owning scope is
never inferred from a tag). The queued payload's `tags` is exactly that list, so it satisfies Context
`input_modes.tag_snapshots` (`{id,name,business_id}` exactly) and never consults today's catalog or tag associations.
Label-list DTOs are derived from the frozen snapshot (`actions_get {revision}`) or the live tags (current reads).
`tags.tag_snapshots(uow, business_id, item_type, item_id)` reads an item's current snapshots; `plan_item_tags(...)`
returns the resulting `tags` snapshots for the plan being built.

**Discussion.** `actions_thread_get {action_id, limit?, cursor?}` -> `{thread_id, action_id, participants, messages,
next_cursor}` is authorized exactly like `actions_get` (reader scope, retained business read links; unauthorized or
unknown = `not_found`), reads the one `thread_messages` table (`item_type='action'`) and never follows anything.
`actions_comment {action_id, text, summary?, note_refs?, relay?, rationale?}` is a policy-gated, Signoff-capable
mutation with NO automatic grant. It requires the Action to be readable AND the caller to be an active participant of
the Action's owning namespace (`ctx.business_id == action.business_id`); a read link is never a write. It records one
`thread_messages` row (actual recorder and attributed speaker, relay as for Tasks), auto-follows the commenter and, for
a relay, the represented speaker (`creator`/`commenter`/`represented`; an explicit unfollow stays), and emits ONE
optional `note_added` event scoped to the owning namespace (`source_type actions`, `source_id` the action key, revision
= the current revision) for the Action's current followers. It writes no `action_logs`/`action_revisions` row and no
`context_inputs` row. The installation owner is refused by the retained team-channel rule (`channel_required`, use the
private owner conversation) exactly as for other shared discussion writes. A withdrawn Action's discussion stays open
(as a closed Task's does). Returns `{action_id, thread_id, message}`.

### Queue, filing and graph edits moved to Context

The durable Context input queue (`context_queue`), source ownership and filing (`context_filing`) and controlled graph edits
(`context_graph_edits`) are no longer Operations packages. They live in `qorqut_context.integration` (the `integration` extra of
`qorqut-context`), described in that repository's `INTEGRATION.md`. Operations keeps only the Action-side contract: 
`settle(ctx, plan, uow, *, on_revision)` hands each stored revision to the required `on_revision(ctx, exact_revision, uow) -> input_id`
callable, and `ActionOperations(application, references, *, on_revision, clock=)` stores the same callable. Operations never
imports Context.

### Host inputs

* Compose the `storage_contribution()`s in the order above before `signoffs`, with the listed `required_tables`
  satisfied by host tables (`businesses`, `principals`, `business_configs`, the item tables). Context's four contributions are
  spliced after `actions` by the host.
* The required `business_configs` table holds two Task domains the engine itself writes: `tasks` (field definitions, via
  `tasks_config_update`) and `task_customer_states` (the scoped Customer-state vocabulary, appended inside the Task plan that
  introduces a label; see the README's "Customers"). Neither needs a schema change or a new operation.
* Provide `TaskPorts`, an `ItemResolver`, a delegation provider on the `Application`, and the registration of
  `reconcile_snoozes` in the host lifecycle: one periodic write unit calling `tasks.reconcile_snoozes(uow)` (the
  engine reconciler; scheduling and the lifetime wiring belong to the host lifecycle, not to this package).
* Snooze responsibility is computed once: every actual assignee, otherwise the creator (never the creator while
  somebody else is assigned). Each responsible principal is classified from the principal registry, not from the edge
  type. Agents get a required `snooze_expired` inbox delivery through the event writer's eligibility filter. Human
  responsibility is not an inbox row and ALWAYS goes through the typed port
  `Routing.snooze_routes(uow, business_id, task, human_ids) -> agent ids` (declared and validated in `TaskPorts`;
  `Routing.routes` keeps its ordinary-event semantics), creator fallback included. The host answers with the Focus
  coordinator and the owner-work coordinators when the installation owner is among `human_ids` and applies its other
  mandatory routes; optional related-work/follower routes are not reminder responsibility. The result is filtered to agents
  eligible in the Task's scope by the event writer. A wake nobody is eligible for is still recorded (one per
  business/task/generation), a done/cancelled Task is never woken, and a wake never starts work.
* Retire the replaced host tables/triggers (`tasks`, `task_notes`, `task_changes`, `task_completions`, `action_*`,
  `inbox_*`, `inbox_capture_*`, `task_first_done_*`) in the generated host DDL; callers move to the engine operations.
* Platform writebacks that record Actions call `new_action` then `settle` with the composition's one `on_revision`.
* Routing for owner-work, `finalize_events` replacement and the `context_inputs` inbox visibility are host adapters
  over `discussion.emit_event`/`tasks.events.routes`.

## Ordinary parent Tasks

The engine holds no group service; a work group is an ordinary Task with a title, a description and its canonical
thread, children are Tasks with `parent_id`, and any other association is an ordinary typed relationship to a Task.

* `storage/core_schema.py`: the Global `CHECK` of `relationships` names Tasks where v1 named the retired group type
  (see "relationships" above); nothing else in the core schema changed.
* `tasks.Campaigns` was a required `TaskPorts` member here; v1.3 removes it (see "Retired product surface").
* `team_channel`: the owner's business Task discussion exception stays (`tasks_comment`, `focus_reply`); the retired
  group note operation is no longer named.
* Action evidence: the `story` evidence key is `task`.

## Task relocation (Move to project)

A Task keeps its identity when it changes owning scope. `tasks/relocation.py` (`TaskRelocation`, reached as
`TaskOperations.relocation`) is the one helper that does it; the host registers the operations that expose it.

* `inspect(ctx, uow, source, destination, task_ids)` reads only. It expands the selection to its descendants (overlapping
  selections move once; at most 100 given ids and 1000 expanded Tasks), checks everything that travels (discussion, follows,
  completion, snooze history, tags, assignments and internal relationships, CRM memberships and observations, subscriptions,
  project links), returns the counts, concrete `blockers` and a fingerprint of that exact state and caller. A missing Task, a
  missing or archived destination and an oversized selection are errors, not blockers.
* `apply(...)` recomputes the inspection inside the write transaction, refuses any difference (`revision_conflict`) or blocker
  (`move_blocked`), then arms `UnitOfWork.relocating(source, destination, ids)`: a unit-local guard behind the SQL function
  `relocation_permits(old, new, task_id)`, which answers `0` everywhere else. The triggers that make scope immutable
  (`tasks_scope_identity`, `thread_messages_immutable_update`, `task_snooze_wakes_immutable`, `crm_entries_identity`,
  `crm_activity_events_immutable_update`) recognize it for the scope column alone; every other identity, author, timestamp and
  content protection still applies. Foreign keys are deferred only inside the block and checked before it ends. Capture rows the
  rewrite wrote to `inbox_changes` are removed again: relocation announces nothing.
* `RelocationAdapter` objects supply the references only the host owns (their counts, blockers, fingerprint material and
  application run in the same guard and transaction).
* `task_moves` is the append-only audit. `moved_location(uow, business_id, task_id)` replays it to find where the Task a stale
  scoped reference named is now - bounded, unambiguous (a Task is identified across moves by id and creation time), current
  rows first. Global is a real place a Task leaves (`None` is the scope, not "no answer"), so a Global -> Project -> Project
  journey resolves from either old scope. `TaskOperations` answers a read or write against the old scope with `record_moved`
  (`details.destination`, `details.task`) only to a caller who may read the destination; otherwise `not_found`. A pending inbox
  event recorded under the old scope is delivered if the recipient may read the Task where it is now.
* Historical references. A note stores `{type, id}` relative to the scope it was written in and never changes. The nullable
  `thread_messages.reference_namespace` (`global` or `business:<id>`) keeps that scope once the note's Task has moved: the
  first relocation pins the exact old namespace (a later one keeps it, and a note written after a move is pinned to its own
  scope by the next one); `NULL` means the note still lives where it was written. The immutable-message trigger lets only a
  guarded relocation fill a `NULL` pin with the exact old namespace of that row; nothing else can set, change or clear it.
  The display path (`tasks/history.py`, used by `TaskOperations.message_refs` for Task, Action and CRM discussions, and by
  `SignoffNotes.references(strict=False)` for Signoff and Focus notes) is read-only and opt-in: a record at the exact original
  scope wins; for a Task that is gone from there the relocation resolver names its current scope and the caller's own read
  authorization on that exact target decides whether any label or place is shown. A readable reference of a scope-qualified
  kind (Task, topic, material, table) always carries `business_id` (an explicit `null` is Global), `business_slug` and
  `business_name`, also when it resolved in the reader's own scope; other kinds carry them only outside it and an unavailable
  reference none. `HistoricalReferences.target` gives consumers that open the Task themselves (the host's CRM linked activity)
  the same answer: the pinned origin while a Task with that exact identity is there, else the authorized current place, else
  `ABSENT`. A destination record that only shares the bare id is never the target. Writes and strict validation never follow a moved target.
  `journey_spans(uow, business_id, task_id)` gives the stays of one Task (by id and creation time) with their times; CRM
  activity uses it to find the membership developments, whose immutable payload names the scope the record had, without
  borrowing an unrelated Task's that reuses the id.
* Not moved, by decision: a Task with an Email binding, a relationship that would cross the move (including `blocked_by`), a
  Task outside the move whose stored link names a moved Task, a Context-reviewed relationship, a destination that already has
  the same ids, a destination that rejects the Task's typed fields or assignees. Each is a named blocker.

## Global work relationships

Deployed Stories grouped and linked work in a business or in Global under `read_global_context`. Ordinary parent Tasks keep
that: exactly eight neutral operations now resolve their owning namespace like a Task operation does.

* `relationship_roles_list`, `relationships_list`, `relationships_get`, `relationships_create`, `relationships_update`,
  `relationships_remove`, `context_items_list` and `context_items_get` take the optional `scope` input
  (`business` | `global`) and declare `scope_kinds={business, global}`, `global_capability="read_global_context"` and
  `resolve_namespace=tasks.scope.task_namespace`. Their handlers (read, prepare, validate-prepared, apply) first call
  `tasks.scope.task_context`, the Task check: a Global call needs the installation owner or an active ecosystem agent
  holding the capability, and a business-restricted agent is refused. There is no second permission evaluator.
* A Global call runs with `business_id=None`: reads and writes use the Global partition only, a business row never becomes
  visible by supplying its id, the plan's namespace and affected scope are Global, and both endpoints are re-resolved in
  that one namespace (the host's lookups already honor an unselected business), so a cross-business endpoint is
  `not_found`. The relationships `CHECK` still restricts a Global edge to a Task `concerns` link.
* Unchanged: the per-operation policy and revision checks, prepared Signoff and fresh target validation, canonical
  conversation identities, and every other relationship operation (`context_navigation_get`, `context_graph_get`,
  `context_items_archive`, `materials_update`, the review operations), which stay business-only and have no `scope`.

## Context model-call usage ledger

Moved to Context (`qorqut_context.integration.usage`, owner `context-usage`); see that repository's `INTEGRATION.md`.

## Setup ports removed (project transition)

Business setup is gone from the product, so the two ports that existed only to serve it are gone from the engine; they
are removed, not made optional or inert, and nothing replaces them:

* `tasks.SetupGuards` and the `setup` member of `TaskPorts`: the Task prepare-time and apply-time guard, the
  configuration guard of `tasks_config_update`, and the `setup` field of `tasks_get` (a Task read no longer carries
  `setup`). A Task write prepares and applies exactly the records it did, so no
  prepared plan changes; no operation version moves (reads carry no plan version).
* `relationships.ProtectedEdges` and the `protected_edges` member of `RelationshipPorts`: the check on an existing stored
  edge at preparation and in `validate_prepared`.

Everything else of those operations is unchanged: scope and policy, revision checks, assignee protections
(`AssigneeRules`), workflow-role refusal, parent independence, review composition and archive
behavior. `TaskPorts(assignees, focus, incidents, references, routing)` and `RelationshipPorts(roles,
workflow_roles, materials, archive, navigation, review_edits)` are the complete constructors; a host that still passes
`setup=` or `protected_edges=` fails with `TypeError`. Receipts of earlier applications stay exact.

## Installed-wheel smoke

`tools/check_installed_wheel.py` runs four scripts from the installed wheel, with no `bazarlyk` importable:

* the foundation probe,
* the Signoff and registered-program slice. Its proposer is an active global agent (a supported role, an ecosystem scope,
  no home project, no membership row): its project access is the active project directory, exactly as the engine requires
  of every agent. A project-home agent is refused with `forbidden` ("Only active global agents may invoke operations"). The slice asserts the Signoff, stale and forbidden
  decisions, single application, replay, principal attribution, routine and golden digest,
* the neutral-item/relationship slice, which uses no protected-edge check,
* a `create_directory_tree` slice: the installed `storage.directories` module publishes an empty layout once through the
  installed `Application` and prepared file executor, the same key replays, and an occupied path or an identical empty tree
  made by someone else conflicts and is never adopted.

## Retired product surface

The v1.3 engine keeps only the general platform. The following are removed outright, not made optional, inert or
flag-gated, and nothing replaces them in the engine:

* **Campaigns Task port.** `tasks.Campaigns`, the `campaigns` member of `TaskPorts` (now `assignees, focus, incidents,
  references, routing`), the `plan(...)` callback of every Task label write and the `campaign` field of `tasks_get`.
  A Task label write is the shared tag plan only. The Signoff `campaigns` port, `CampaignFilter` and the
  `signoffs_list` `campaign_id` filter are removed with it.
* **Department registry.** `departments.py` (ids, labels, aliases, colors, role-to-department defaults) and its use by
  tags (`canonical_labels`), conversation presentation and routine definitions (the `departments` property, DTO field,
  run-record definition and the `routines.departments_json` column). Tags are ordinary labels: one entry per
  normalized key, no aliasing. Capabilities, permissions, UUID identity and worker scopes are unchanged and nothing
  derives authority from a tag.
* **Specialist role rules.** `roles.py` keeps `global-worker`, `learning-applier` and `ordubashy`; the supported global
  roles are exactly those three. The `marketing_business_execution` standing authority, `MARKETING_PLATFORM_TOOLS`,
  `proposer_department`, `_marketing_platform_eligible`, `standing_authority_current`, the `marketing_peer` peer lookup
  and the `PlatformActions.enqueue_binding` member (only that rule called it) are removed. `ordubashy_mail_control`
  is the only standing authority; a stored proposal that records any other authority is refused as `forbidden` and
  never executed.
* **Schema.** The only engine-owned definition that changed is the `routines.departments_json` column. The engine
  declares no finance, metrics, subscription, campaign routing/attribution, daily-report, milestone, setup or
  standalone-product table; those lived with the host. `tools/derive_core_schema.py` is removed because regenerating
  from the v1 migrations would reintroduce the column: `storage/core_schema.py` is the maintained target schema.
* **No transition machinery.** `Database.initialize_target`/`open_existing` already refuse a store whose identity or
  schema differs and convert nothing; a store with `routines.departments_json` is refused the same way
  (`tests/test_storage.py`). A host raises its `target_version` and creates an empty store.

Host follow-ups (the engine does not mask them): drop the `campaigns` member from `TaskPorts(...)` and
`SignoffPorts(...)`, the `marketing_peer` method of `PeerIdentities`, `enqueue_binding` of its `PlatformActions`, and
any use of `departments`, `canonical_labels`, `CampaignFilter`, `proposer_department` or the routine `departments`
field; raise `target_version` for the new schema. A host profile whose role is `global-marketing` is no longer a
supported current global (it stays readable in history, like the retired Founder), so the host must not seed or
publish it as an active global.

Paper/outreach and content-session storage contributions are untouched. The engine's storage declares no Paper,
outreach or content-session table, and the quiescence controllers have no content-session slot: an installed
extension registers its own named writer (`RecoveryQuiescer.register_writer`) and names it in `required`. The only
remains are the "outreach handoff email link" wording in the `NoteReferences.derived` docstring (`tasks/ports.py`).

## v1.32: mail leaves the engine

The Operations engine no longer carries mail-specific behavior; the optional Fastmail extension owns it. Removed:
`SignoffStager.authorize_mail_control` and the Signoff standing mail authority (`ordubashy_mail_control`,
`MAIL_OPERATIONS`), the `account_email_%`/`email_%` visibility clause, `OperationDescriptor.account_policy` (a Global caller
is judged by the descriptor as declared) and the `email-management` plugin-gate special case, and the `email_history`
recovery-writer slot. `OperationDescriptor.default_mode` is new: a provider package declares the standing mode of its own
operations (applied only after every other gate and only when neither an override nor a role default names them).
The optional `SignoffPorts.platform` and `SignoffPorts.email` ports remain, generic and `ABSENT` for a host without them.
An existing Signoff row approved under `ordubashy_mail_control` is refused when executed, like any authority this engine no
longer issues. The Email Task type and `email_conversation_id` binding remain only for historical Task rows: a new Email
Task cannot name a conversation because no conversation resource exists.
