# Global ownership and review destinations

Global review and the personal taskbox are part of the application.

New global proposals use Global Signoffs. Existing proposals retain their
original saved review destination.
Global is an installation-level workspace, not a synthetic business.

## Product outcome

Global routines belong in Global and their protected changes are reviewed there.
A business routine belongs to its business and is reviewed there. The owner no
longer selects “Review in 49” when managing a global routine.

Ordubashy can propose global work when its registered ecosystem profile grants
the relevant capability. Business agents cannot create or
move routines into Global, even if they request it. The backend enforces this.

Keep the interface simple: the pinned Global workspace uses the existing main
navigation and familiar routine/signoff cards. No additional navigation layer or
destination chooser. Overview remains the compact business gallery; aggregate
counts there do not change where the underlying records belong.

## Ownership, review, and execution

These are separate values:

| Concept | Meaning | Example |
| --- | --- | --- |
| Ownership | The namespace responsible for the record | Global routine definition |
| Review destination | The owner queue for a protected proposal | Global signoffs |
| Execution target | Where a run operates | Root, or each authorized business |

Use an explicit descriptor at new API boundaries:

```json
{"kind": "global"}
```

```json
{"kind": "business", "business_id": "<canonical UUID>"}
```

The business variant requires a registered UUID; the global variant forbids a
business ID. Missing scope does not mean Global. Do not put the string `global`
into UUID fields or create a Global business row.

New records accept an explicit ownership scope, subject to authorization. Existing
records supply their stored scope; updates cannot change it. A namespace and record
ID identify the target together. Never search another namespace on a lookup miss.

The server derives `review_scope` from the operation's authoritative target
ownership and freezes it in the prepared proposal. It is not an agent-controlled
routing option. A supplied conflicting review destination is rejected.

| Proposed change | Review destination |
| --- | --- |
| Create/update/pause/delete a global routine | Global |
| Change a business routine, even when proposed by Ordubashy | That business |
| Change shared instructions or another registered global resource | Global |
| Change a business document or task | That business |
| Promote a business learning into shared guidance | Global, for the new shared change |

Policy still determines whether review is needed at all. Global scope does not
turn `signoff` into `allow`, and a signoff cannot overcome `deny`.

For new discretionary proposals, mixed ownership must be separated into independently
reviewable changes. Reject an inseparable mixed-ownership proposal before staging;
do not choose its destination from the caller's current workspace. A global
routine's authorized fanout is an execution target list, not mixed ownership of
the routine definition. Preserve existing legacy multi-target proposals intact.

## Who can decide and approve

An agent can select the intended scope of a new record only within its registered
authority. Role names, prompts, dashboard selection, paths, and request arguments
are not evidence of authority.

| Actor | Business work | Global routines |
| --- | --- | --- |
| Business agent | Its registered business, subject to policy | Denied |
| Ecosystem agent / Ordubashy | Explicitly allowed businesses, subject to policy | Requires `manage_global_routines` and operation policy |
| Installation owner | Current authorized business access | Owner's current global authority |
| Background system principal | Declared program authority and targets | Explicit program authority; no inherited unrestricted owner rights |

For agent global mutations require all of: active registered ecosystem scope,
the specific global capability, and the operation's effective policy. Check these
on preparation and application. Profile validation already excludes global grants
from business-scope profiles; invocation must also reject forged scope or stale
grants. An `allow` override or plugin assignment cannot enlarge scope.

Only the active installation owner can decide Global signoffs in this delivery,
through the authorized owner transport. Business ownership alone is insufficient.
Agents cannot approve their own proposals. Recheck proposer authority, current
target access, policy bindings, revisions, and hashes before applying exactly the
stored change. Preserve existing conflict, rejection, receipt, and recovery rules.

A business agent can propose an escalation through an authorized business-owned
task or existing communication mechanism. Ordubashy independently evaluates it and
creates a new global proposal under its own identity and rights. Escalation neither
grants permission nor wakes an agent. Do not overload external-action requests as
a generic agent messaging system.

Promotion creates a linked global proposal; it does not silently change the source
record's ownership. The owner approves the protected shared change.

## Routines And Signoffs

1. Introduce explicit review scope across contracts, policy, storage, signoff
   staging/decisions, and transport context. Route new protected global operations
   to Global, including shared resources that currently borrow a business queue.
2. Allow authorized root routine reads and mutations without selecting a review
   business. Global root routines work even when no business exists.
3. Show only global definitions in Global routines. Business routines retain their
   business ownership. Global execution history can show its business targets, but
   does not absorb unrelated business routine history.
4. Add Global's pending-only Signoffs page using existing review cards and behavior.
   Show explanation and affected scope, with the same approve/reject workflow.
5. Remove the global routine review-business selector and its query/body dependency.
   Keep routine creation, editing, pause/resume, manual run, stop, deletion, history,
   schedule previews, and status behavior.

Global routine definition changes are reviewed globally. A run's business-specific
effects retain business attribution and policy. If a program prepares a separate
protected business change, that proposal goes to that business; a shared change
goes to Global. Parent routine scope never grants authority over its targets.

Keep fixed registered programs, typed arguments, root/each-business targeting,
current target authorization, timezones/DST, scheduled-slot semantics, cost history,
and recovery unchanged. Do not add agent scheduling, arbitrary commands, or model
execution during approval. An each-business run with no eligible targets reports
no targets and does not fall back to a root run.

## Other Global Records

Use the same ownership descriptor when adding global requests and learning
records. This specification does not make their existing business-only tables or
APIs global-ready automatically.

- **Tasks:** remain business-owned. The personal taskbox aggregates existing tasks
  assigned to the current human across authorized businesses; no global task storage
  or global assignment grants are planned. See [Personal taskbox](../PERSONAL-TASKBOX.md).
- **Requests:** existing requests are typed capability/external-action records.
  Global requests require compatible definitions, explicit global capability and
  account grants, and executor support. Preserve outbound owner approval independently
  of a signoff on request creation. Global email is deferred.
- **Learnings:** distinguish ownership of a learning from the scope of the proposed
  application. Preserve business provenance when proposing shared guidance. Coordinate
  with the separate [Learning Applier](LEARNING-APPLIER-IMPLEMENTATION.md);
  review routing does not change the learning's business ownership.

Do not provision broad global request/learning grants or advertise unsupported
mutations in this first delivery. Analytics and Brief remain Coming soon.

## Contracts And Storage

The implementation covers these existing seams together:

| Area | Required change |
| --- | --- |
| `contracts.py` | Versioned prepared-plan encoding with explicit `review_scope`; retain legacy decoding |
| `application/engine.py`, `policy/resolver.py` | Derive review scope from target ownership; remove business fallback for global operations |
| `signoffs/service.py` and repository | Scope-aware queues, lookup, authorization, decisions, and proposer reconstruction |
| Storage migrations | Global review representation, business FK integrity, scoped indexes, immutable proposal protection |
| `transport/compatibility.py` | Root routines/signoffs without a borrowed business context; business routes remain explicit |
| Routine/background context | Preserve definition ownership, execution attribution, and derived output review scope |
| Bootstrap/MCP schemas | Expose effective scopes and requirements without granting new rights |
| Dashboard | Global queue and routine integration; remove review selector |

For signoff storage, add a required `review_scope_kind` (`business` or `global`)
and make `review_business_id` nullable only for global rows. A CHECK constraint
requires business kind with a business FK, or global kind with a null business ID.
Queue indexes include scope kind and business ID. Legacy rows are business-kind,
even when their affected resource is global. Preserve all existing identifiers and
proposal bytes. New API responses expose `review_scope`; legacy business responses
can retain their UUID field during transition.

Expose root signoff list/get/decision routes parallel to existing business routes,
using the same service and immutable IDs. Global list/count operations return the
Global queue only. Business list/count operations return that business's queue only.
Queue scope is checked on every get and decision, not just during list filtering.

New global routes reject a supplied `review_business_id`; they never ignore a
conflicting destination. Legacy business routes continue to serve existing business
records and legacy approvals. Legacy global-routine callers that supply a review
business receive an explicit migration error for new mutations, rather than silently
creating a proposal in a different queue. Update first-party callers together.

Use existing error conventions for invalid scope, denied scope, missing targets,
and stale/conflicting proposals. Errors must not disclose inaccessible records.

## Preservation And Recovery

Older proposals retain their original business destination, serialized changes,
hashes, decisions and receipts. They finish in their saved queue, including when
the affected resource is global; they are not duplicated into Global. Old
prepared-plan decoding and application/recovery remain supported. Routine
ownership, schedules and run history are independent of review routing.

Recovery requires compatible application code and a verified database/file
generation. Account for later writes before restoring a backup; do not install
an older reader against a newer database. See [Recovery](../../app/qorqut/storage/RECOVERY.md).

## Acceptance checks

Use temporary databases, isolated services, and injected programs/providers.

| Scenario | Expected result |
| --- | --- |
| Business agent creates a global routine, directly or through a root/alias route | Denied before staging; no record or signoff |
| Business agent spoofs ecosystem scope, global grant, or review destination | Registered identity wins; denied |
| Business agent tries to move a business routine to Global | Rejected; original unchanged |
| Ordubashy has ecosystem scope but lacks routine capability | Global routine mutation denied |
| Authorized Ordubashy proposes a global routine under signoff policy | One pending Global signoff; no business copy |
| Ordubashy changes a business routine | That business's policy and queue |
| Caller supplies mismatched review scope | Rejected, never silently rerouted |
| Business-only owner attempts a Global decision | Denied |
| Owner approves with proposer offline | Exact stored change applies; no model call |
| Proposer rights revoked or targets changed after staging | Existing policy/conflict protections prevent unauthorized application |
| Global routine executes across businesses | Authorized targets only; attributed runs and correctly scoped protected outputs |
| Global root routine with no businesses | Reads, creation, review, and authorized execution work |
| Global each-business routine with no eligible targets | No target jobs and explicit no-target result |
| Same local record ID exists in two namespaces | No cross-scope lookup, update, or decision |
| Old pending global proposal after migration | Original business queue, bytes/hash intact, approval/recovery still work |
| New proposal decision is retried or application interrupted | Existing idempotency and receipt recovery hold |
| Global dashboard routines and signoffs | No review-business selector, no inner nav, no unrelated business rows |
| Existing business workflows and outbound approvals | Remain functional and retain their separate permission requirements |

## Implementation and verification notes

- Signoff storage enforces review scope, business foreign keys and immutable
  proposal payloads. IDs, decisions and application receipts remain stable.
- Prepared-plan encoding 2 adds immutable `review_scope`; encoding 1 remains readable
  with exactly its original canonical bytes and hash. Legacy owner policy bindings
  continue to use their saved business context during application.
- Existing routine inputs retain their `scope: business | global` contract. New
  signoff DTOs expose the explicit review descriptor. MCP signoff reads accept an
  explicit `scope: global`; absence of a business is not a scope grant.
- New global proposals cannot mutate unrelated business content. Routine run/stop
  execution records and ecosystem profile membership grants remain effects of their
  globally owned definition or profile; they retain target authorization checks.
- Business routine history keeps its existing authorized global-run summaries.
  The Global routine endpoint returns only global definitions and executions.
- `tests/test_global_scope.py` covers global permissions, separate queues, offline
  application, grant revocation, migration/restore, legacy approval, and root work
  without businesses. `tests/test_service_routines.py` covers authorized fanout and
  targeted stop. Dashboard `tests/test_global_routines_browser.js` exercises routine
  management plus Global approve/reject against an isolated real service.
