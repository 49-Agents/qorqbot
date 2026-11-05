# Short browser handles

`browser_turn` version 2 keeps reservation credentials on the server. This is the
MCP path used by Ordubashy, explicit native Codex helpers and durable workers;
the business agent's shell launcher is unchanged.

A native caller acquires with its session UUID once:

```json
{"action":"acquire","protocol":"handles","consumer_id":"<session-UUID>"}
```

Native handles name the caller's own lease-free configured browser session. They
are derived from the agent and session, stored nowhere, never expire and are never
superseded; see [browser command recovery](BROWSER-COMMAND-RECOVERY.md). The
reservation rules below apply to durable workers.

Durable workers use `{"action":"acquire","protocol":"handles"}`; their authenticated assignment
already fixes the browser scope. An owning response returns a short opaque
`handle`, such as `b0123456789ab`. A waiting response returns queue state and no
handle. Durable waiting/resumption remains dispatcher-driven, without parent
model polling. After resumption, acquire in the new generation.

```json
{"action":"command","handle":"b0123456789ab","action_id":"<unique-action-UUID>","command":["observe","overview"]}
{"action":"release","handle":"b0123456789ab"}
```

All follow-ups use `handle`: status, command, checkpoint, renew, artifact,
reconcile and release. No consumer UUID, turn ID, lease ID, capability token or
browser target needs copying after acquire. Repeating an acquire supersedes its
previous handle; it does not authorize replaying an uncertain action.

Routine command replies contain the command output, exit code, action ID/state,
and nonempty artifacts/errors. They do not echo the handle, turn/lease IDs,
target, expiry or checkpoint. If command completion changes ownership, the new
state remains visible. Acquire/status expose relevant queue/checkpoint state;
renew returns the expiry; checkpoint/release return state. Full reservation and
action records remain on the server. An action ID is a retry/reconciliation
reference, not browser ownership bookkeeping.

## Explicit business selection (configured Ordubashy only)

Ordubashy alone may add `business_id` (an authorized business UUID) to a handles
`acquire` to lease that business's shared browser; omitting it keeps Ordubashy's own
configured browser. The lease uses the business's exclusive `browser_turns` queue
(`business:<id>`), `control.lock`/`lease.json` and a `browser_handles` record that stores
the business, consumer, turn, lease, generation and browser instance. A busy browser is
refused, never taken. Follow-ups still use only the handle; identity, membership, scope,
the browser-control skill and the browser instance are rechecked on every call, and
release, reconcile, expiry, replacement or a restarted browser invalidate it. An
uncertain action holds the lease until the same handle reconciles it. See
[isolated business browser](BROWSER-ISOLATED.md#explicit-ordubashy-selection). This needs
no migration.

## Ownership and recovery

Migration 088 adds private `browser_handles` records linking the short ID to
its exact original turn, lease, token, authenticated identity and worker
generation. They survive process restarts; they grant no new authority and are
not portable between caller registrations/credentials. Native helpers sharing
parent credentials remain within that parent's trust boundary, as before.

Every ordinary use checks the saved binding and current reservation before the
existing browser operation rechecks scope and ownership. Release, expiry,
cancellation, lease replacement and worker replacement invalidate execution.
Never resolve a stale handle to the latest lease or silently retry a command.
The old lower-level `worker` method remains an internal execution adapter; MCP
uses `tool`, which resolves the handle before calling it.

Only reconciliation has a deliberate generation exception: the authenticated
current generation of the same assignment may use its old handle to settle an
uncertain action. The existing reconciliation rules still check assignment
ownership and running state. This permits no command/renew/release with an old
handle. Native reconciliation is a no-op; configured browsers keep no fence.
Action intents, idempotency, FIFO handoff, uncertain-command fences and artifact
access remain in the existing backend. Handles retain recovery references;
released records are history, not active browser owners or running sessions.

## Release boundary

Core may deploy before role publication/adoption. The compatibility default
retains v1 acquire responses and raw follow-up inputs for existing instructions.
New callers explicitly acquire with `protocol: "handles"`; follow-up calls need
only the handle. Raw fields remain labeled legacy in discovery during transition.
A handle never falls back to the legacy path. Ordubashy 1.37.1 selects the compact
protocol when separately published/adopted and prepared. Do not replay an existing
v1 action through a fresh handle. Preserve configured
Chrome, its tabs/profile, operational data and agent terminals. No provider trial,
role adoption, merge or deployment is performed by this source change.

The reviewed release manifest must include the exact commits and migration 088.
A pre-088 binary refuses the newer schema; recovery requires a reviewed compatible
forward release or separately approved database recovery, not an automatic
switch to the old binary or an automatic database restore.

## Focused evidence

`tests/test_browser_short_handles.py` adds ten focused cases for handle-only
execution/MCP schema, database reopen, action replay, caller/selector isolation,
lease expiry/replacement, durable waiting/resumption/cancellation, private
business gateway tokens, artifact delivery, migration from 087, cross-generation
uncertainty reconciliation, delayed lease replacement, and compact receipts.
All ten passed in targeted runs; only failed new fixtures were corrected and
retried. Previously accepted tests were not rerun. Existing transport doubles
were adjusted to the new entry point without rerunning their suites.

These checks use a real temporary SQLite database, queue and browser adapter,
with the external browser command runner stubbed. MCP coverage exercises real
binding/dispatch with a fixture caller/application authorization layer. They are
not a new live Chrome/provider trial or a measurement of model token savings.

An additional compatibility check passed for unrefreshed v1 acquisition, command
and release alongside explicit handle opt-in, including rejection of stale-handle
downgrades. The ten previously accepted checks were not rerun.
