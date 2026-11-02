# Signoff host integration

The Signoff service, input requests, notes, rejection, notifications and the exact-decision orchestration of the
platform approvals are the Operations engine's (`qorqut_operations.signoffs`; its ledger is `docs/EXTRACTION.md` at the
pinned commit). This package is the host's side of that contract:

* `ports.py` implements the engine's typed `SignoffPorts` over the retained v1 domains (execution vocabulary, peer
  identities, owner task attention, conversation references, management routing, the Focus attention board and personal
  selected extensions). A port supplies vocabulary, an identity lookup or an extra *narrowing* filter and can grant no authority.
  Every retained optional integration is real. There is no mail port: the host keeps no email decision, approval or send
  Signoff machinery, and the optional Fastmail provider's account actions follow the owner-input model
  (`supports_signoff=False`) rather than staging platform approvals.
* `execution.py` is the generic execution-state vocabulary of a stored Signoff and its SQL mirror; no provider adapter
  contributes to it.

This package does not own HTTP, authentication, domain preparation, terminal transport, or external job dispatch. It
uses the shared application engine and storage receipts; domain handlers never implement a second file writer.

## Assembly

`bazarlyk.service.runtime.build_runtime` composes, in this order:

```python
file_applier = FileApplier(database, sealed_lazy_file_targets, locks=locks)
signoffs = SignoffService(database, signoff_ports(signoff_domains), locks=locks, file_applier=file_applier)
app = Application(database, registry, policy=policy, signoffs=signoffs,
                  context_resolver=policy.refresh_context, writer_admission=admission, ...)
signoffs.bind(app)                       # exactly once; the service is the Application's stager
app.file_executor = PreparedFileExecutor(database, file_applier, writer_admission=admission)
install_contribution(registry, signoffs.contribution())
...                                      # domains, Focus
signoff_domains.bind(management=..., focus_board=...).require()
```

The Signoff service is constructed before the Application and the retained domains are constructed from that
Application, so the domain-backed ports resolve their domain through `SignoffDomains` at call time. Using a port
before assembly bound its domain is a composition error. The operation registry and the `ProgramRegistry` are frozen
once by `Runtime.freeze_registry` before traffic.

The target resolver returns storage `RegisteredFileTarget` instances from registered
resource IDs. It must not accept a caller path. Use the profile module's target validators for
pinned packages and mandatory authored source dependencies. Global capabilities
apply to the engine-resolved global namespace, not every business invocation of
a mixed-scope descriptor.

`registered_worker_profiles` is a trusted mapping, or callable
`(principal_id, uow) -> WorkerProfile`. Missing grants deny. Its business IDs are
intersected with active database memberships. Increment its positive revision
whenever the program/grant definition changes. A system label or `source` field
is not a grant. Effective profile configuration and runtime decisions both use
`resolve_operation_policy`; do not introduce another role-default table.

The job verifier receives `(context, descriptor, targets, uow)` and returns
`None` for discretionary work or a stable mapping containing `job_id` and a
SHA-256 `definition_digest` for a verified declared consequence. It must verify
the persisted job, worker, business, frozen inputs and permitted outputs; the
job ID alone is insufficient. The result's job ID must match trusted
`InvocationContext.job_id`. Only internal system-worker contexts may carry that
field, and transports must reject it as caller input. The engine persists it
in `policy_binding`, and recovery restores it from that immutable binding.
Without a verified consequence, an agent initiator's current scope and policy
are intersected with the worker's grants. Historical human attribution never
grants the human's interactive permissions.

Human global capabilities come from the trusted authentication/provider layer,
not HTTP fields. Configure `human_global_capabilities` as a mapping or callable
`(principal_id, uow) -> capabilities` for unattended recovery of human-authored
global file changes. A custom `PreparedFileExecutor(context_loader=...)` may
instead rebuild current trusted contexts from `(plan, uow)`.

## Reads and Decisions

`signoffs.contribution()` supplies the executable `signoffs_list` and `signoffs_get` descriptors (installed in the
registry by assembly; `Application.signoffs` is the service). Both use `app.invoke` and the canonical result
envelope. The convenience methods are:

```python
signoffs.list(context, states=["pending"], limit=50)
signoffs.get(context, signoff_id)
signoffs.review(context, signoff_id, decision="approve", **binding)
signoffs.review(context, signoff_id, decision="reject", feedback=text, **binding)
signoffs.retry(context, signoff_id, **binding)
```

`binding` contains `proposal_revision`, `payload_hash`,
`expected_status_revision`, and `idempotency_key`. Owner-only HTTP routes call
`review` or `retry` directly; these commands own their transactions and must
not be wrapped in another UoW or routed through an agent permission gate. They
require a current human principal and active owner membership in the selected
business. Approve and reject keep nonblank feedback verbatim,
up to 8000 characters; whitespace-only feedback becomes null.

The context's selected business is the review route. A mismatching proposal ID
is `not_found` even for callers authorized in another business. Global rows,
counts and detail require their global capabilities and affected-resource scope.
List/detail DTOs include hydrated `proposer_display_name` and optional
`initiated_by_display_name` and `job_id`; a worker's `proposer_agent_id` is null.

List supports `states`, `proposer_principal_id`, `initiated_by_principal_id`,
`limit`, and mutually exclusive `cursor` or `updated_after`. Use `updated_after=0`
for initial catch-up. `updated_cursor` is bound to the caller, selected business
and filters; it is not authorization. `has_more` indicates additional matching
rows. Counts cover all visible states independently of the selected filters.

Return canonical envelopes with `Cache-Control: no-store`. `accepted` means a
durable approval awaiting application, not success. Use the specification's HTTP
status mapping, including `409` for stale decisions and `503` for bounded busy
errors. Never add business logs, journal entries, notifications, tasks or terminal
wakes for signoff transitions.

## Lifecycle and Recovery

Constructors perform no background work. Service startup invokes bounded
`app.file_executor.recover_pending(app, limit=50)` for unfinished allowed file
applications and starts `signoffs.start()` for durable approved proposals.
`signoffs.stop(timeout=5)` joins its current application on shutdown.
`signoffs.run_pending(limit=50)` and `run_one(id)` are also available to a trusted
lifecycle caller. `run_pending` returns candidates visited, not a success count.
Recovery results and retained claims must be checked before enabling conflicting
work; do not spin tightly on another application's busy claim.

The application engine calls the file executor's `pending` hook with its own
resolved namespace before preparing a retry. A receipt is bound to the canonical
operation, principal, namespace, idempotency key, normalized input and exact
stored plan. Both signoff and allowed-file execution retain an outer process
lock through preflight, file work and final database commit. Storage file replacement
has its own separate lock; do not nest the same file lock around `apply`.

Every restart uses the stored plan, current authority and expected revisions.
File recovery accepts only exact before/after hashes; unexpected bytes are
preserved. Claims can be released only after the file applier verifies all
original before hashes. Partial or uncertain effects remain failed with claims
retained. Safe owner retry reuses the approval and plan, never an LLM or a new
preparation. Metadata receipts and the signoff sequence are internal recovery
state, not an operator-facing event log.

## Verification

The engine's own Signoff cases are `tests/test_signoffs.py` in `qorqut-operations`; the host's installed proof is
`tests/test_host_retained_operations.py`.

Tests use temporary databases and files, real immutable role packages, profile
resolution, the core engine, and storage receipts. Coverage includes exact automatic
application, feedback, duplicate/racing decisions, supersession and cursors,
global visibility, revoked authority, worker attribution/job replay, DB rollback,
mixed-file crash recovery, raw-file conflicts and allowed-file restart recovery.
Service tests cover real HTTP/MCP integration; background tests cover job-consequence verification.

General input requests, saved responses and in-app notification scheduling are described in [Owner input and notifications](../../../docs/SIGNOFF-INPUT-NOTIFICATIONS.md).
