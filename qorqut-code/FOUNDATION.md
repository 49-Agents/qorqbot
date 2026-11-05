# Shared Foundation Contract

This is the foundation contract of the shared service. The contract types, application engine and storage
live in the Operations engine (`qorqut_operations`, in the `qorqut-operations` repository); the host composes
them.
Database initialization, authentication, concrete business handlers, background
admission, policy/signoff installation, and transport startup are separate
composition steps. No module performs live imports or initializes a database at
import time. There is no CLI application-logic adapter.

## Install and Test

Use Python 3.12 or later in a project-local environment. `app/pyproject.toml` declares
the host's runtime dependencies; the `service` extra adds the maintained MCP SDK,
Starlette, and Uvicorn. Run focused tests as described in the [README](README.md#running-tests).
Foundation tests use temporary roots and never access operational business data.

## Values and Handler Interfaces

`qorqut_operations.contracts` is the single shared DTO owner. Constructors are immutable;
JSON values are defensively copied/frozen, not references to caller-owned dicts.

- `Principal(id, kind, display_name='', active=True)`: canonical UUID; kind is
  `human`, `agent`, or `system`. An owner remains a human with membership rights.
- `InvocationContext(principal, ...)`: selected and home business UUIDs, allowed
  businesses, global capabilities, profile/policy revisions, pinned role/plugin
  refs, source, request/session/initiator identity, and review business. Optional
  `job_id` is a trusted claimed-job UUID, allowed only for system worker contexts.
  It is persisted in the policy binding when present. Adapters
  construct this from authenticated registrations. None of these authority facts
  are supplied by an operation argument or trusted because they appeared in JSON.
- `InvocationOptions`: `expected_revision`, `idempotency_key`, `business_id`,
  `review_business_id`, `supersedes_signoff_id`, and
  `expected_superseded_status_revision`. The last two must appear together.
  Review context chooses a queue, never grants target scope.
- `ResourceRef(resource_type, resource_id, business_id=None, namespace=None)`;
  `ExpectedTarget(resource, revision=None, content_hash=None, exists=True)`.
- `PreparedChange(kind, resource, action, values, operation_name=None)`:
  `kind` is `record`, `file`, or `job`. `operation_name` adds a composed permission
  check; it does not run a second independently prepared operation.
- `ChangeSet(operation_name, operation_version, principal_id, target_namespace,
  arguments, affected_scope, expected_targets, changes, preview, ...)` stores
  authoritative canonical UTF-8 JSON bytes once. `canonical` is those bytes;
  `payload_hash` is their SHA-256 hex digest. Persist both, not a second serializer.
  `ChangeSet.from_bytes(bytes, expected_hash=...)` rejects noncanonical or changed
  bytes, unknown fields, and mismatched hashes.
- `OperationResult` has `ok`, `signoff_required`, `accepted`, or `error` status.
  `data`, `signoff`, and `job` are optional JSON members. Accepted owner review
  may carry `data.signoff` rather than a job. `DomainError` carries `code`,
  `message`, `retryable`, optional field errors/current revision/details.

Preview types are `ChangePreview`, `PreviewResource`, `FieldChange`,
`StructuredPreview`, and `TextPreview`. Their serialized shapes are the frozen UI
contract: title, rationale, affected scope, resources, changes, and effects.
Structured fields are ordered by path. `ChangeSet.to_dict()` returns a mutable
copy for serialization, never mutable proposal authority.

`OperationDescriptor` callback signatures are:

```python
read(context, arguments, options, uow) -> JSON | OperationResult
prepare(context, arguments, options, uow) -> ChangeSet
apply(context, prepared_change_set, uow) -> JSON | OperationResult
validate_targets(context, prepared_change_set, uow) -> None  # optional
resolve_namespace(context, arguments) -> str                 # optional
```

Reads and preparation receive a query-only SQLite unit of work. Apply receives a
short write transaction. Handlers must not start subprocess logic, mutate global
cwd/environment, dispatch network work, or wait for models within a transaction.
Describe background work as exact durable job effects; workers perform it later.
All writes performed by apply, including compound identity/membership/config
writes, must be represented in the prepared change set and preview.

## Registry and Invocation

`builtin_registry()` declares the complete operation metadata catalog.
`registry.register(descriptor)` installs real code. `metadata`, `entries`, and
`known` can describe a known but uninstalled operation; `get` refuses to pretend
one has a working handler. Canonical snake_case names and aliases share one policy
and idempotency key. `alias_bindings` are descriptor-owned constant JSON arguments;
conflicting caller values are rejected before canonical validation. `schema_for`
removes those fixed fields from alias discovery, and `required_plugin_for` supplies
the same alias grant for discovery and pre-replay authorization. Installation cannot expose a previously private operation
to a broader source. Input/output JSON Schemas are validated with the maintained
JSON Schema implementation, including format checks and unknown input rejection.

```python
app = Application(database, registry,
    policy=policy_resolver,
    signoffs=signoff_stager,
    context_resolver=refresh_context,
    file_executor=durable_file_executor)
result = app.invoke(context, operation_name, arguments, options)
```

Ports in `qorqut_operations/application/ports.py` are deliberately injected, not allow-all stubs:

- Policy: `resolve(context, descriptor, expected_targets, uow) -> PolicyDecision`.
  Missing policy denies ordinary operations. Owner-only operations independently
  verify active human owner membership and are not agent-exposed.
  Optional `validate_plan(context, descriptor, change_set, uow)` binds a claimed
  worker's exact arguments to frozen job outputs before staging or applying.
- Signoff: `stage(context, descriptor, change_set, options, uow) -> OperationResult`.
  It must persist the immutable proposal and return `signoff_required`, without
  domain effects. The engine saves the invocation receipt in that same transaction.
- Context refresh: `refresh(context, uow) -> InvocationContext`. The engine also
  checks live principal activity, current agent profile revision, and indexed
  scope. The profile resolver supplies full artifact resolution; this is not a second resolver.
- Files: `execute(app, context, descriptor, change_set, options, input_hash)
  -> OperationResult`. The signoff module owns durable orchestration and stores its idempotent
  result together with the final domain transaction.
  `pending(context, descriptor, options, input_hash, uow, namespace=...)
  -> ChangeSet | None` resumes an exact unfinished allow-mode receipt before any
  domain preparation, so a crashed file mutation is never prepared twice.

Namespace spelling is `business:<uuid>`, `global`, or `agent:<agent_uuid>`.
Descriptors use `business`, `global`, and `agent_workspace` in `scope_kinds`.
Global targets require a declared `global_capability` and the current grant.
For mixed-scope descriptors the capability is conditional on resolving to global;
the engine passes a descriptor copy without that capability to business/agent
policy checks. Ecosystem agent administration uses `manage_ecosystem_agents`.
Root global mutation with signoff still requires a permitted review business.

Prepared business effects cannot target another business. Read dependencies may
include registered shared global context or other currently authorized businesses.
Every effect needs an expected target; file effects are unique per resource.
Composed permissions are checked both before proposal creation and application.
Changed policy bindings conflict rather than authorizing a proposal with its own
proposed new grants. The database maintenance gate blocks new application writes;
the separate legacy-writer barrier is owned by migration code.

## Repositories and Migrations

`qorqut_operations.storage.database.Database(path, assembly=...)` receives an explicit database path and composed schema. The host resolves `operations/.runtime/operations.sqlite3` through `InstancePaths`. Empty target construction uses `initialize_target()` explicitly; ordinary startup uses `open_existing()`, never an implicit migration. Units of work exclusively borrow pooled connections with foreign keys, bounded busy timeout, full synchronous durability, and WAL. This is a single-host local
SQLite authority, not a database file to share over a network filesystem.

```python
with database.transaction(business_id=business_id) as uow:
    tasks = uow.repository('tasks')
    current = tasks.get(task_id, required=True)
    tasks.update(task_id, {'title': title}, expected_revision=current['revision'])
```

`read` uses query-only transactions; `transaction` uses `BEGIN IMMEDIATE` and
rolls back all handler/receipt writes on failure. Unit-of-work scopes cannot be
overridden by repository calls. An unscoped internal UOW must pass explicit
business IDs to scoped repositories. `None` explicitly selects a global/null
business namespace, not every business. Global tables still require domain-level
authorization for their identity and target relationships.

Repositories expose `get`, bounded `list`, `insert`, revision-CAS `update`, and
revision-CAS `delete` over the fixed `TABLES` registry, not arbitrary SQL exposed
to a caller. Internal repository/domain code can use the supplied connection for
typed joins, compound constraints, and updates that are represented in its plan.
`*_json` columns accept/return JSON values; repositories canonicalize storage.
Created/updated timestamps and initial revisions are filled on insert. A semantic
no-op update keeps its revision. Composite keys use mappings or tuples; resource
references use a canonical JSON object key unless a custom resolver is installed.

Custom targets register via
`app.register_target_resolver(resource_type, callback(context, ref, uow))`.
The callback returns a row/mapping with revision/content_hash or `None`. This
supports immutable composite artifacts, filesystem snapshots, and uniqueness
expectations without introducing generic SQL tools.

Schema ownership remains with storage. New modules request required indexed fields/tables
and submit their desired semantics before changes. Append the next numbered SQL
migration; never modify an applied migration. Startup checks migration checksums,
contiguous history, application ID, and unsupported future versions. Tables cover
identities/profiles/registrations, configuration, tasks, document metadata, logs,
requests, learning, routines/runs/slots, jobs, signoffs,
idempotency/application receipts, resource claims, imports, backups, and maintenance.
Published artifacts are immutable; proposal/application binding
bytes also have SQLite update triggers protecting them from internal mistakes.

## Durable Apply and Recovery

`validate_prepared(context, plan, uow, approval=None, receipt_id=None,
allow_unverified_files=False, require_approval=True)` returns refreshed context
and installed descriptor without executing a domain handler. Approval inspection
may set `require_approval=False` to inspect current before targets while pending.
This never authorizes an apply. `apply_prepared(...)` always revalidates and
requires `ApprovalBinding(signoff_id, payload_hash)` when policy requires signoff;
it verifies the authoritative stored proposal is approved and byte-identical.

`ApplyReceipts(uow).create(plan, receipt_id=None, signoff_id=None)` persists exact
bytes. `claim_resources(id)` obtains durable namespace/resource claims and marks
existing documents unavailable. Claims from another unfinished apply return busy.
`FileApplier(database, resolve_target).apply(receipt_id)` uses a bounded per-receipt
cross-process lock, then performs file I/O outside SQLite write transactions.

The trusted file resolver maps a `ResourceRef` to
`RegisteredFileTarget(root, relative_path)`. It must choose roots and paths from
registered domain identities, never arbitrary request paths. Resolution rejects
absolute/traversing paths and symlink components. `file_values(bytes_or_none,
before_hash=...)` creates the exact base64 body and before/after hashes; actions
are `write` and `delete`. Generic registered artifact files may be absent before
their metadata row exists.

Replacement uses a local temporary file, fsync, atomic rename, and directory
fsync. Recovery accepts before bytes (write them), after bytes (verify them), or
unexpected bytes (preserve them and conflict). A crash after rename but before
progress recording therefore does not blindly replay a write. It never blindly
retries an external effect. `allow_unverified_files=True` is only for preflight
recovery against a matching durable receipt; actual apply requires verified file
progress plus unchanged DB revisions.

After files are verified, the worker calls `apply_prepared(...receipt_id=id)` and
`ApplyReceipts.finish(id, result)` in the same transaction as domain changes,
idempotency completion, and signoff finalization. Finish releases claims and
restores document readiness. If authority changes with no file effects,
`FileApplier.release_unchanged(id, error)` proves all before bytes still exist
before releasing claims. Empty progress is not proof: a crash may already have
replaced a file. Unknown/partial effects retain claims for reconciliation.

## Backups

`database.backup(destination)` uses SQLite's online backup API, validates integrity
and foreign keys, records schema/count metadata and a SHA-256 checksum, fsyncs,
and publishes to a new path without overwrite. `Database.restore(snapshot,
new_destination, expected_checksum=...)` validates before creating a separate
database; it never overwrites the running authority. Service/migration code must
drain writers, coordinate the legacy barrier and maintenance gate, switch the
registered database path, and reconcile authored-file snapshots. A database-only
backup does not include authored bytes or establish a DB/files atomic snapshot.
