# Explicit Context graph edits

Branch `v1.2/context-edit-host`, on top of the accepted Context completion checkpoint (host `c32abc6220af37dc3694065edfe2841249e20dd8`,
Operations `47f3d87c35ee31791f4ed63ddec5f383ceda5ab4`). The Operations half (the `context_graph_edits` binding beside the Context queue)
lives in `qorqut_operations/context_graph_edits/` and is described in the Operations `docs/EXTRACTION.md` section "Controlled graph
edits". This document is the host half: what `context_edit` now is, who may queue what, what the driver rechecks before it runs
anything, and what is deliberately left.

Dependencies, unchanged and read-only: the accepted Context library `32431e125b1982ea10fce4db1993ab22c6a241be`
(`qorqut-graph-edit-v1`, `Context.begin_edit`, `Pipeline.submit_edit`) and the committed role prerequisite
`fdd01b4a19346aab554f6c0babfdf67c3b592c05` (the role fixtures of the acceptance cases use its sources). No pin, receipt or role
source changed, and no usage ledger, UI, native or history code was touched.

## The path

```
context_edit {"edit": <qorqut-graph-edit-v1 plan>}
  -> prepare (read only):  head == expected_revision, library normalization, source-byte bound, object / endpoint existence,
                           frozen target ownership, authority of the caller in every namespace the edit needs
  -> apply (one transaction): authority and base rechecked, then the companion binding and ONE GENERAL queue input
                           (reserved five-character event ID) are inserted together; a conflict leaves neither row
  -> the Context queue driver, alone, in queue order:  recheck the ORIGINAL caller's current authority and the fixed base,
                           then Context.begin_edit (replay check) + Pipeline.submit_edit with the frozen plan, the original
                           submitted_by, the reserved event ID and the controlled record_context  ->  one real publication
  -> the queue records published / blocked / discarded exactly as for every other input
```

Nothing is published when `context_edit` returns, no model is ever started and no usage receipt exists for an edit. A model runner
that is called on this path is a bug.

## The operation

* Body exactly `{"edit": <plan>}`; there is no outer `rationale` (the plan carries its own). The schema is the closed D1 plan
  schema (`bazarlyk.shared_context.graph_edit.PLAN_SCHEMA`: `put_node`, `put_edge`, `remove_node`, `remove_edge`, full records,
  caller-supplied IDs, explanations, citations and `support_after`), and the library's own `normalize` runs on top of it. Exposures
  (`mcp`, `dashboard`, `worker`), `owner_only=False` and `supports_signoff=False` are unchanged; a policy that requires a Signoff
  for it cannot be configured (`permissions_set` refuses it).
* Result: `{"input": <queue DTO>, "execution": {"kind": "deterministic_graph_edit", "plan_digest", "expected_revision",
  "reserved_event_id"}}`. The same `execution` object is on `context_queue_list/get/submit` for that input (null for an ordinary
  input). An identical replay (same caller, key and arguments) returns the same input and event ID.
* `expected_revision` must be the current published head when the edit is prepared and again when it is applied; the plan is
  also bounded by `GraphEditPorts.source_bytes` (default 256 KiB, the Context library's default). The ports value must equal the
  `Pipeline`'s `config.source_bytes` at composition, or the library could refuse at dispatch what preparation accepted.
* Preparation never calls `begin_edit` / `submit_edit`, stages a draft, runs a model or publishes; the published files, events and
  pending job are byte-for-byte unchanged by `context_edit` (an acceptance case monkeypatches those calls to fail).
* A retained delegated worker may invoke it exactly as v1 allowed (an assignment that lists `context_edit`, a live compatible
  binding, lease and generation, and a parent that holds the permission). See "Delegated callers".

## Target ownership

An existing node or edge belongs to the scope in its first creation event's controlled `record_context.scope` in the published
history at the fixed base. Later support, attributes, links or tags (including a later event from another business that restates
the object) never transfer it. A recorded `{"kind": "global"}` scope or a `{"kind": "business", "business_id"}` of a registered
business is recognised; a missing, malformed or unresolved scope (no record context, an unregistered business ID, any other
shape, or a first event that is not an `add`) is protected: it is treated as Global/unknown and supplies no business grant. Objects
the plan itself creates belong to the admission namespace (the caller's selected scope).

The frozen evidence (`targets_json`) lists every object the plan updates, removes or creates, and every old and new endpoint of an
affected edge, each with its change, role (`target` / `endpoint`) and ownership (`recorded`, `new` or `unknown`). It is evidence the
driver re-derives and compares, never a permission. There is no hidden cascade: a node removal must account for every incident
edge in the same plan, and every edge of the resulting graph must have both endpoints.

## Authority

The retained one: the real resolver and `Application.authorize` for the `context_edit` descriptor, plus the preserved restrictions
of the old `_authorized` (now `GraphEditAuthority._role`). Nothing is added.

* A human must be the actual current installation owner (the owner keeps full authority over every namespace).
* An agent needs an active registration, a supported role (`INSTRUCT_ROLES`) and a current allow in the admission namespace *and* in
  the namespace of every target and endpoint (`policy.refresh_context` for that namespace, then `Application.authorize` with
  `namespace=`). A business agent therefore cannot change a Global, unknown-owned or foreign-business object, or point an edge at
  one. The Global namespace stays restricted to the coordinator role (`ordubashy`); having a Global agent kind or the shared
  graph-read capability is not enough, and an explicit deny override wins everywhere.
* A business agent invoked with no selected business is resolved to its home business by the retained resolver; it is never in the
  Global namespace, and a Global-owned object is refused for it.

## The driver gate

`ContextQueueDriver(..., graph_authority=GraphEdits.authority)` takes the typed port. For a bound input (resolved by input ID from
the companion row, never from the path, JSON shape or metadata):

1. A binding that is broken (a reserved path with no companion: `graph_edit_unbound`; a companion that no longer matches the
   payload, digest, submitter or base: `graph_edit_integrity`) blocks the head; nothing runs and the input is never shown as
   ordinary.
2. If the exact event is already published, that real result is reconciled first, whatever the caller's authority or the head
   is now and even when the authority port is unavailable (for example after a restart): reporting an existing effect is not
   authorizing a new one. The exact binding, source, marker, path, metadata, base, bytes, submitter, scope and provenance checks
   still apply before it is recorded.
3. An unpublished edit with missing wiring (`graph_authority=None`) blocks the head with `authority_unwired`.
4. Otherwise, before any fresh execution *or explicit retry* (including after a restart), `GraphEditAuthority.recheck` requires the
   head to equal the fixed base (`stale_base`), re-derives the target evidence from the stored plan and the fixed snapshot
   (`targets_changed`), and re-resolves the original caller's current registration, role and grants from the stored attribution, never the
   driver's system identity (`authority_revoked`). A delegated caller is re-resolved together with its frozen binding, assignment and
   generation (see "Delegated callers"). A refusal is the queue's preflight failure: the head blocks, no event ID is
   spent and nothing reaches Context.
5. A retry can recheck restored grants but cannot change the plan, base or event ID, and never rebases a stale edit; the caller
   discards it and submits a newly reviewed plan. A later input cannot pass a blocked head.
6. Dispatch is `Context.begin_edit` (the replay check only) and `Pipeline.submit_edit` with the frozen plan, the original
   `submitted_by`, the reserved event ID and the controlled `record_context` (the input's scope, plus provenance with the queue
   input and the original attribution). The published event is verified for the deterministic marker and plan digest, the reserved
   path, fixed metadata, base, canonical bytes, submitter, scope and provenance, and the explicit empty handoff before it is recorded.
7. An interruption with a partial draft is completed by the explicit retry for the same plan (the library refuses any other draft
   content); an interruption after publication and before terminal recording is reconciled on restart into one real result.

## Composition requirements (feature integration gate)

The real host runtime does not compose these yet; the stock runtime therefore cannot queue or run a graph edit, and no fixture-only
fallback is hidden behind that. The later integration must:

* add, in this order after the queue and filing contributions, `context_graph_edits.storage_contribution()` (it requires
  `context_inputs` and `context_input_attempts`);
* construct `ContextQueueOperations(..., executions=context_graph_edits.ExecutionFacts())`;
* give `GraphEditPorts(context, source_bytes=<the Pipeline's config.source_bytes>)` a published-head provider. It needs `snapshot()`
  returning the library snapshot (with `events` and `record_context`) and `pending()`; the `SharedContextReader` of the read path
  exposes no raw snapshot, so the writer-side `Context` is the natural provider;
* build `GraphEdits(application, ports, roles=INSTRUCT_ROLES)` where the application exists (the `register_domains` hook) and pass it
  as `graph_edits=` to `register_business_domains`; pass `graph_authority=edits.authority` to the driver. Without `graph_edits` the
  operation answers `operation_unavailable`; without `graph_authority` the driver refuses graph-edit inputs;
* build the `Application` with `delegated_creation_groups=HOST_DELEGATED_CREATION_GROUPS` (the stock `build_runtime` already does;
  it is the only place the `context_edit` group exists);
* replace the two stand-ins of the filing fixture (`action_revisions` and `inbox_deliveries.required`) as already planned.

`ContextBaseService.edit` and the other old-engine callers (listed in `CONTEXT_FILING.md`) are untouched; only the direct-edit
dependency of `context_edit` (`DirectEditRequest` / `service.edit`) is gone.

## Delegated callers

v1 let a live, compatible delegated assignment invoke `context_edit` when both the assignment and its parent allowed it; the edit
prepared no operational record, so nothing had to name an identity in advance. The queue input and its companion binding are two
new records with one randomly allocated ID, which an assignment's `record_ids` can never list. The ability is preserved, not widened:

* **One trusted creation group.** `composition/delegation.py` registers exactly `DelegatedCreationGroup("context_edit",
  {"context_graph_edits", "context_inputs"})` (`HOST_DELEGATED_CREATION_GROUPS`) and `service/runtime.py` hands it to the one
  `Application`. The Operations delegated check still runs every retained test first (live binding, parent, assignment, generation,
  lease, registration, role and capabilities, the operation allowance, the owner/external exclusions and the business boundaries of
  the admission and of each target). Only a *complete* expected-absent pair of those two types, with one new UUID and the admission's
  own scope, skips the assignment's record-ID membership; any other operation or table keeps the original record boundary. No
  caller argument, assignment JSON or tool policy can install or change the group.
* **The edit keeps its targets.** Both records stay ordinary prepared changes with expected absence, the existing transaction and
  CAS checks, and the recorded committed-action receipt of the admitted invocation. `GraphEdits.apply` requires the prepared effect
  set to be exactly the two new record inserts in order (binding, then input) sharing one ID and scope, with exactly those two
  expected-absent targets; an extra, missing, duplicated, reordered, updated, deleted, file, job, named or differently scoped effect
  is `invalid_plan` and writes neither row.
* **The delegation is frozen.** The binding's attribution carries a closed `delegation` value: `null` for a direct caller, otherwise
  exactly `{binding_id, assignment_id, generation}` (two canonical UUIDs and a positive integer, on an agent principal), copied from
  the authenticated context. It is validated at admission, by a database trigger and on every read, and it is repeated in the
  controlled `record_context` provenance of the published event. No credential, hash, lease or decision is ever stored.
* **Bound to the caller.** At prepare the attribution is built from the invoking context; at apply the frozen value must equal the
  invoking context's exactly. A different binding, assignment or generation, or a direct caller applying a delegated plan (or the
  reverse), is `forbidden` and writes neither row.
* **Rechecked as the same worker.** `GraphEditAuthority.recheck` rebuilds the original parent's current context with the retained
  resolver, attaches precisely the frozen triplet (a refreshed context can never carry another one) and authorizes in the admission
  and every target namespace through `Application.authorize`, passing the frozen edit as the arguments. The live provider check
  therefore repeats the assignment's operation grant, its exact business, the parent's profile and registration, and the binding,
  lease and generation. Nothing looks up a newer binding, clears the delegation or uses the unrestricted parent or the driver
  identity.
* **Expiry.** An expired, released, completed or revoked assignment, a new generation, or a changed registration blocks the head of
  an unpublished edit with the existing `authority_revoked` refusal, and the explicit retry repeats the same checks for the original
  triplet: it cannot renew a lease, start a worker, transfer ownership or adopt another generation. The caller discards it and
  submits a newly authorized request. Restored permission lets only the same plan run while the same binding is otherwise valid.
  An exact already-published edit is reconciled first even after the delegation ended or the authority port is unavailable.

`app/tests/test_context_edit_delegation.py` covers this with the real `OperationalDelegation` store and provider.

## Interpretations to review

* An unknown or unresolved owner needs the Global namespace's authority (it is never given to the admission business).
* The companion row carries no `business_id` or `created_at`: the queue input's immutable `business_id` and `created_at` are the
  single source of both.
* `targets_json` is a flat list of `{kind, id, change, roles, ownership}` subjects; the driver compares it for equality with its own
  re-derivation.
* The graph-edit storage is required, not optional: a database without `context_graph_edits` is a `composition_error` (raised by
  the Operations `load` / `reserved_event_ids` / `ExecutionFacts` reads and so by the driver and by `context_edit`), refused before any
  Context writer or model work. There is no schema probe, fallback to ordinary inputs or empty reservation set.

## Acceptance and fixture

`app/tests/context_edit_support.py`, `app/tests/test_context_edit_acceptance.py` and, for a retained delegated worker,
`app/tests/test_context_edit_delegation.py`: a disposable composed host (real resolver,
policy, application, `context_edit`, Operations queue, `context_filing`, `context_graph_edits`, the host driver wired with the
authority port, real Git Context and the accepted library `Pipeline`) with a model runner that fails the test if called. The role
sources come from the committed role prerequisite when `QORQUT_TEST_ROLES_SOURCE` names an export of it. The two stand-ins above
are the only schema stand-ins. The cases cover the owner and allowed business agent, replay, read-only preparation, forbidden
roles / non-owner human, ownership (Global, unknown, malformed, foreign, support from another business), old/new/newly created
endpoints, broader agents with and without grants and an explicit deny, revocation of a grant / registration / ownership after
enqueue, restoration, a stale base and queue order, missing wiring, spoofing of the reserved path and metadata, missing and
mismatched companions, changed prepared arguments, reserved-ID collision, interrupted staging with retry, interruption after
publication, discard, and ordinary GENERAL and upload inputs staying ordinary. Every controlled case also asserts that no model
started and that no published event has a usage receipt.
