# Agentic changes in the existing inbox

This is an explicit, composable connection, not automatic production wiring.
After a later installation/startup integration supplies an initialized or opened
`AgenticLayer`, it calls:

```python
from qorqut.agentic.inbox import attach_agentic_inbox

bridge = attach_agentic_inbox(runtime, layer, runtime.inbox)
```

The helper binds the existing host inbox adapter and composes the existing
`runtime.inbox_nudges.reconcile` callback. Its original Task snooze reconciliation
runs exactly once per call, then the bridge drains the module feed. The existing
nudge backend owns admission and the Operations write transaction. Attachment
does not start it or any other background worker. Reattaching the same layer
object is a no-op; a different layer, inbox binding, backend, or replaced callback
is a configuration error. `service/runtime.py` does not call this helper yet.

`InstancePaths.agentic_root` resolves `operations/agentic`; `agentic_runtime`
resolves `operations/.runtime/agentic`. They use the usual confinement checks,
remain separate from Context and each other, and create/open nothing. No required
source component or installation dependency is added by this change. The injected
module exposes a read-only `instance_id` from its existing initialized metadata.

## Transaction and identity contract

`AgenticInboxBridge(layer).reconcile(uow, limit=100)` requires the caller's admitted
write UOW; `limit` is an integer in `1..1000`. It reads the cursor in that transaction
and processes only finalized module changes, in sequence order, with bounded feed
pages. `status(uow)` reads the same state without creating a binding or changing
either store. Construction and imports open no store and register no tool runtime.

The host contribution `agentic-inbox` owns only `agentic_inbox_cursor`, keyed by
`consumer='overseer'`. Its fields are `instance_id`, `last_sequence` (initially 0)
and `updated_at`. The contribution explicitly requires the existing `principals`,
`agent_profiles`, `inbox_events` and `inbox_deliveries` tables. The cursor is
technical metadata, explicitly excluded from operational Git snapshots. It is
not a copied history or another delivery database.

The first reconciliation binds the cursor to the supplied module identity even
if the recipient is unavailable. Identity changes, deletion of the binding and
cursor regression are refused. Replacing the store at the same path requires a
future explicit reconciliation procedure; it never silently resets delivery.
Both `status` and `reconcile` compare the saved cursor with the returned module
feed head before writing, including when the recipient is unavailable. A cursor
beyond that head raises `agentic_feed_rewound`: an older snapshot can retain the
same module identity, so identity alone cannot prove the feed has not rewound.
There is no automatic reset, replay or rebinding. Existing inbox references remain
readable under their usual identity and recipient checks.

Each finalized non-self change appends through Operations'
`qorqut_operations.discussion.events.emit_event` (the current shared discussion
writer). Event and cursor updates use the **same transaction**. A pre-commit crash,
failed append, or append returning no event rolls back both; after commit, replay
or reopen reads the advanced cursor and creates no duplicate. Module consumer
cursors are never advanced. A later undeliverable event cannot be skipped.

Direct `bridge.reconcile` calls continue to raise on failure; callers must let the
transaction roll back. The attached nudge callback first runs the original Task
snooze callback, then encloses only the agentic reconciliation in the named
`agentic_inbox_drain` SQLite savepoint on that same UOW connection. An ordinary
agentic exception rolls back and releases that savepoint, allowing successful
Task snooze work to commit. Partial agentic events and cursor changes roll back
together and remain pending for the next pass. Successful reconciliation releases
the savepoint and commits with the outer transaction. Exceptions from the prior
callback, writer-admission failures, and `BaseException`/process crashes are not
swallowed by the agentic handler. No second connection or transaction owner is
introduced.

`bridge.last_callback_failure` is a bounded in-memory observation containing
`code`, `error_type` and `observed_at`, or `None`. It omits exception messages and
other content, returns a defensive copy, and clears after a successful attached
callback. Direct reconciliation/status calls do not alter it. The same bounded
code/type is logged for diagnosis. This observation is not a persistent delivery
record; the durable cursor is the retry authority and survives process restart.

## Recipient and event

The recipient is the existing active, supported global agent whose saved role ID
is `learning-applier`, validated with Operations' shared global-agent predicates
and global inbox eligibility. Its UUID is retained regardless of display name.
No recipient leaves the cursor unchanged and returns `recipient_unavailable`;
an ambiguous active roster fails closed. Nothing creates an agent, changes its
capabilities or touches a native session. Offline/disabled/busy native delivery
does not prevent the ordinary durable inbox append.

The reserved core source is `agentic_changes`; selected extensions cannot shadow
it. Events have `business_id=None`, `source_id=change_id`,
`source_revision=sequence`, `event_type='agentic_layer_changed'` and exactly the
existing recipient as a required delivery. A registered change actor is retained
in `actor_id`; an unknown actor is represented as null in the host and keeps its
original attribution in the module. A change authored by the recipient advances
the cursor without an inbox event, retaining the module history.

Visibility metadata contains only `instance_id`, `change_id`, `sequence` and
`recipient_id`. The summary contains the change reference and changed-file count,
never file bodies, source references or the free-text reason. Visibility requires
the current addressed Learning Applier, matching module identity and cursor
binding, valid event metadata, and a non-delegated invocation. The existing inbox
delivery join, operations and policy remain authoritative. The decorated
`agentic_change` value is `{instance_id, change_id, sequence}` for a later
change-read operation; visibility does not depend on authored files still existing.

Both methods return `{instance_id, cursor, recipient_id, delivered, self_skipped,
pending, state}`. Read-only status reports zero delivery counts. `pending` is a
boolean observation of finalized work beyond the cursor; `state` is `idle`,
`pending`, or `recipient_unavailable`. Counts describe durable host inbox appends
and intentional self skips, never native-agent execution or completion.

## Focused verification

`app/tests/test_agentic_inbox.py` exercises synthetic initialized host and module
stores, multi-file apply before a Git commit, ordinary inbox visibility and
decoration, replay/reopen, concurrent drains, pre-commit and append failures,
recipient loss/restoration/ambiguity, self changes, offline nudge visibility,
confinement, store mismatch and rewind, bounds, cursor ownership, and attachment
composition. Callback failure tests verify that Task snooze work commits once,
partial agentic work rolls back, and a healthy retry delivers each change once;
prior callback errors, admission refusal and process-crash simulations still escape.
The module's `tests/test_instance_identity.py` checks distinct/stable identities,
read-only access and invalid metadata. No real provider or native process is used.

Host operations, change-read authorization, native execution, text projection,
installation dependencies/orchestration, and the eventual startup call site are
separate assignments.
