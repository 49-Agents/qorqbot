# Inbox ownership implementation

Manager decision D37, 2026-10-08. Implements D36's ownership rule in the
existing host application. It can proceed independently of the manual-creation
review, recorded-shell work and adapter corrections. This is the ownership and
observation boundary; startup-hook transport and delivery composition follow
the three reviewed implementations in one integration assignment.

## Scope and interface

Add `qorqut_core.runtime` with typed models, `SessionOwners`, and a small
`SessionMonitor`. Assemble `Application.session_owners`. Own host migration 6,
adding a typed `session_owners` history table in the existing database, and the
read operation `runtime_get(agent_id)`. There will be 15 public operations and
the same five job handlers. Do not expose connect/release as model-writable
operations or add a service, socket, broker, generic plugin or launcher.

The trusted internal input `OwnerCandidate` contains agent UUID, terminal UUID,
recovery UUID, environment generation, provider (`claude` or `codex`), exact CLI
version, native-session UUID, and the existing `ProcessFingerprint` for the
actual native process. It does not use an HTTP connection, model label, cwd,
newest file, heartbeat expiry or native-session UUID alone as the owner key.
The future startup bridge supplies these already-associated facts; this module
does not claim to establish native-file or hook-to-process association.

`SessionOwners(application, *, observe_process=...)` supplies:

- `connect(candidate)`: checks installation epoch, agent and supported provider
  profile, verifies the candidate process, and atomically obtains/reuses the
  owner. Returns `OwnerConnection` with outcome `connected` or `reconnected`,
  owner UUID and per-agent monotonically increasing run generation. The owner
  UUID is also this connected native execution's run UUID. Unknown evidence
  returns an unavailable result, never a conflicting-session diagnosis.
- `release(owner_id, process)`: only releases that exact owner's process after
  observing its exit. A live or unknown process retains the claim. Stale IDs or
  process identities cannot revoke a successor. A native Stop/SessionEnd hook
  alone is not exit evidence.
- `reconcile()`: checks a finite snapshot of current owners, then applies each
  observation only if the same owner/process/epoch still holds. No external I/O
  occurs inside a host SQL transaction. The monitor calls this every second;
  shutdown stops and joins the monitor before application storage closes.

These are internal callables, not a new competing operation-receipt mechanism.
`connect` is naturally idempotent by the complete candidate binding. It does
not accept a mutation idempotency key that could replay an obsolete live claim.
Network startup registration will use this identity in the subsequent bridge.

The default process observer supports the local onbash target only. It returns
`live`, `exited`, or `unknown` with an observation timestamp. Reuse existing
fingerprints; distinguish a missing process, zombie or changed boot/start
identity (exited original process) from permission, malformed /proc data or I/O
errors (unknown). A brief observation failure cannot release ownership.
Ordinary VM agents return `unsupported_capability` from this local binding path;
do not inspect a guest PID on the host or silently run a VM agent locally.
Injecting a fake observer is a test seam, not VM acceptance.

## Transactions and records

Preserve migrations 1-5 byte-for-byte. Migration 6 records owner UUID, agent and
conversation binding, terminal UUID, recovery/environment/boot identity, run
generation, provider/version/native UUID, canonical process fingerprint,
`active|closed|retired` state, `live|unknown|exited` observation, connection and
last-observation times, and optional close time/reason. Use foreign keys and a
partial unique index allowing one active owner per agent; generation is unique
per agent. Connection identity and its attribution cannot be rewritten.

Before a claim transaction, observe the candidate and any saved competing owner.
Inside the transaction recheck the exact prior owner and installation epoch. If
they changed while observing, retry a bounded three times then return not-ready;
never silently act on stale observations. All new owner records and owner
transition events commit together. Use the registered agent actor for trusted
session observations. A second live owner raises an internal `SessionConflict`
subclass of CoreError, with code `invalid_transition` and fixed Python attribute
`reason="session_conflict"`, preserving the incumbent. Export this type for the
later hook bridge; do not change the shared public error envelope in this lane.
The text says: "Another session is connected to this agent. Tell the user and
wait until that session is closed." An unknown incumbent returns `unavailable`
and explains that its connection could not be verified; it also keeps ownership.

An identical active candidate reconnects with the same owner/run generation.
A different native session, terminal, process start or provider is a competing
candidate, including two processes resuming the same native UUID. A conclusively
exited incumbent can be closed and a new verified candidate connected atomically.
Reconnecting an already closed historical process must fail its live observation.
Do not kill either process or transfer a live claim. No launcher exclusivity
scheme is added.

Append `session.connected`, `session.closed`, `session.retired`, and changed
`session.observation` events with exact IDs and reasons. Ordinary live refreshes
and identical reconnects need no duplicate event. Conflicts have a returned
typed reason; do not add an unbounded event per polling request. Recovery
activation retires active old-epoch owners in the existing activation transaction,
with attributed events. It never adopts them into a restored epoch or alters
messages, existing receipt bytes or transcript lineage.

`runtime_get` is a shared HTTP/MCP/operator read, with agent UUID, status
`disconnected|connected|unknown`, nullable current owner (safe identity/provider/
version/run metadata, not credential or socket paths), `observed_at`, and
`input_ready: false`. With an active owner, only a live observation from the last
five seconds can project connected; older/unknown evidence projects unknown
without expiring the claim. A missing/closed/retired owner is disconnected.
The read neither probes processes nor changes state. Connected is not capture,
delivery, authentication or composer readiness. Preserve current AgentView and
workspace provisioning fields; integration will join those views separately.

Wire `SessionMonitor` into the existing server lifespan alongside JobWorker.
Use one bounded sequential thread, no thread per agent and no model calls. Make
uncaught monitor failure visible to `/health/ready`, retaining claims; stop/join
it during normal or partial-startup shutdown. Process-inspection failures become
unknown observations rather than crashing the whole server. Application.open
and imports do not spawn monitors or processes.

## Ownership, checks and next integration

Own `runtime/`, migration 6, narrow app assembly/recovery and server-lifespan
changes, `tests/runtime/`, and mechanical schema/operation-count updates in
existing tests. Do not modify agents creation/models/workspace, adapters,
runner, recorder, messaging delivery, UI, dependencies or other workers' files.
Update only `docs/evidence/inbox-ownership-r1.md` beyond these owned paths.

Use compact grouped tests: racing two claims, same-process reconnect and
different-process resume, live/unknown retention, real owned-child exit and PID
identity change, stale release after replacement, reopen/recovery, monitor
shutdown/failure, and shared runtime_get HTTP/MCP parity. Build/install and run
one representative race/reopen/exit flow from the wheel. Preserve prior schema
checksums and prove the migration retains existing messages/receipts. Do not
duplicate other suites; report unchanged failures separately. No native provider
turns, VM or live Qorqut data are needed.

After independent review, the integration worker joins this owner record to the
recorded shell and startup hooks, adds conflict context and actual Chat routing,
and tests the combined running system. No startup hook, inbox consumption,
assistant capture, UI Terminal or VM readiness is claimed by this increment.
