# Authenticated native toolkit

The host integration registers `AgenticAccess(resolver, view)` in the existing
Operations registry. Its `agentic_access` read checks the current profile,
machine registration and credential, current project scope, and ordinary tool
policy. It returns the canonical actor, explicit eligible package selection,
opened module root/state/instance identity, profile revision and registration.
It performs no toolkit mutation and runs no user-authored program in the service.

The prepared `.qorqut/agentic` launcher uses the existing installation helper
to run `qorqut.agentic.client --registration /absolute/prepared/registration.json`.
Preparation owns launcher generation. The client follows MCP initialization and
calls only `agentic_access` as an operation through `ReconnectBridge`. It then
opens the module locally and dispatches one request:

```json
{"operation":"tools.invoke","arguments":{"ref":"example/tool","arguments":{},"invocation_id":"original-id"},"context":{"project_id":"00000000-0000-0000-0000-000000000001","work_type":"review"}}
```

`context` is optional. Only `project_id` and `work_type` are accepted. Actor,
selection, root/state, registration and runtime configuration cannot be supplied
in arguments. The workflow list's ordinary `state` filter remains supported.
For attributed management operations, supply `attribution` with only `reason`
and optional `source_refs`; the helper inserts the authenticated actor.
`changes.scan` accepts no attribution: observed filesystem changes retain the
module's `external/unknown` attribution. A scan does not establish their writer.

Ordinary full native agents can read eligible catalog content, invoke/test their
tools, inspect/reconcile their own receipts and manage their own workflow runs.
Delegated assignment credentials cannot use this helper. Management operations
require both `manage_agentic_layer` and the current configured Ordubashy or
eligible global Learning Applier identity. Fresh initialization grants that
capability to those two profiles; existing profiles are not upgraded. Worker
creation/update cannot grant it.

Workflow history is filtered by creator; optional context filters match each
run's saved selection before pagination. An agent may read its own historical
runs and inspect/reconcile its own old tool receipts, and pause/cancel its own
runs, after package/project access is revoked. Progress and resume recheck the
original project, the workflow's package, and any verifier of the affected step
against current access. An explicit context that contradicts a run's saved
project/work type is rejected. Frozen instruction bodies remain frozen.
The access lookup precedes local execution; it is not an atomic transaction
between the service's current grants and the native process.

Python tools use `LocalProcessRuntime` with the helper's current interpreter,
the fixed alias `python`, an explicitly empty environment and the module's
bounded runtime limits. This retains the native terminal's OS authority; it is
not an OS security sandbox. Tool code can access files and networking allowed
by that OS account. Native plugin activation and operational grants still use
the host's existing permission system.

The helper emits the module `{ok,result}` or `{ok:false,error}` JSON envelope
and never retries local authoring, Git, tool or workflow effects automatically.
A successful access lookup proves only authorization. An unexpected local
failure or an oversized result can follow a completed effect. Inspect history
and change/tool receipts before retrying; retain original invocation/operation
IDs. Tool and workflow idempotency/reconciliation semantics are unchanged.

Service wiring, installation/binding persistence and generated launcher
integration are separate integration work. Constructors do not discover,
initialize or migrate a module store.
