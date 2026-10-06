# Live local recorder observation: first runtime connection

Manager design, 2026-10-08. This bounded increment follows the reviewed
[runtime composition evidence](evidence/runtime-composition-r1.md). It adds a
fresh local observation of the recorder's terminal capture loop. It does not
implement managed launch, native-source capture, provider readiness, automatic
input, cgroups or host archival. Those independent requirements remain open.

## Ownership and purpose

The recorder's SQLite `healthy` value is historical and can survive normal exit
or SIGKILL. Its actual `admission_ready` value is process-local. Add one optional
Linux Unix-domain datagram query handled by the recorder's existing event loop,
plus a small client. No thread, daemon, network service, new database or polling
heartbeat. A client obtains a new nonce-bound response from the exact expected
recorder process; an old row or cached response cannot substitute for it.

Owned production paths are `transcripts/recorder.py`, a new
`transcripts/probe.py`, and a minimal read-only binding accessor in `spool.py` if
needed. Reuse the reviewed `runner.process` fingerprint functions and
`runner.models.ProcessFingerprint`; do not duplicate Linux process identity
logic or change runner input behavior. Imports remain passive. No host schema,
agent application assembly, provider adapter or public HTTP/MCP changes.

## Local protocol and client

Use AF_UNIX/SOCK_DGRAM, Linux abstract addresses and nonblocking sockets. The
configured server name is 1–80 ASCII letters/digits/underscore/hyphen/dot, mapped
to `NUL + "qorqut-recorder-" + name`. It is explicit caller configuration, not
discovered by PID/name scanning. Binding an occupied name fails; do not take over
or remove another endpoint. Abstract names need no stale filesystem deletion.
The client uses its own random abstract return address for one query.

Protocol version 1 has one observation request only. Use strict bounded models
with no extra fields. Request fields are `version`, a fresh UUID `nonce`, the
complete capture `RunBinding`, and `source_id`/`source_epoch`. Response repeats
those exact coordinates and adds `recorder: ProcessFingerprint`,
`child: ProcessFingerprint | null`, and strict boolean `terminal_active`.
No path, argv, environment, source bytes, credentials, native-session assertion,
input command or generic method dispatch belongs in this protocol.

Keep each datagram at most 8 KiB. Detect MSG_TRUNC/ancillary truncation; malformed,
oversized, unknown-version, extra-field or wrong-binding requests receive no
success response. They never reach terminal input or alter the spool. Never log
their body or echo arbitrary fields. The response contains only fixed validated
fields and the validated nonce. Normal unavailable/error outcomes are bounded.

The client callable is `probe_recorder(address, expected_binding, source_id,
source_epoch, expected_recorder, *, timeout_ms=200) -> ProbeResult`. The exact
argument types use the owning existing capture/fingerprint models. Validate the
address/coordinates before I/O; timeout is a strict integer from 1 through 1000.
Create one socket, send one request, and use a monotonic deadline. No automatic
retry, cache, fallback to stored health, or implicit process adoption.

Enable SO_PASSCRED on the receiving client socket and validate kernel
SCM_CREDENTIALS on the response. PID/UID must equal the expected recorder;
the returned fingerprint must equal that expectation too. Independently verify
its full live boot/PID/start/UID/PID-namespace fingerprint before the send and
after the matching reply. Check nonce, protocol and every source/run coordinate.
Bound handling of unsolicited/malformed replies to eight datagrams and the
original deadline; none may yield a positive result. Client and recorder are in
the same local PID namespace for this increment. Unsupported platform/socket
capabilities produce an explicit unsupported/unavailable result.

`ProbeResult` carries `verified` (strict bool), `terminal_active` (strict bool),
an optional validated observation, and a bounded reason from a fixed set:
`active`, `inactive`, `unavailable`, `target_changed`, `invalid_response`,
`unsupported`. Only a matching credential/fingerprint/nonce response is verified;
only a verified observation with active terminal capture yields `terminal_active=true`.
Local argument errors remain typed validation errors, not network success.
An authentic inactive response is verified but inactive. Failure/timeout is not
proof of death. Reason ordering for simultaneous invalid conditions need not
be globally specified; no mismatch may become a positive result.

## Recorder integration

Add optional constructor configuration `probe_address=None` and CLI flag
`--probe-address`. Existing invocations without the option preserve behavior.
The listener is created only during explicit `Recorder.run`, not at import or
construction. Validate/bind it before reserving the epoch or forking. If binding
fails, no child launches and no epoch is consumed. A subsequent startup failure
closes the owned socket through the existing complete cleanup/restoration path.

Before answering, use the stored exact run/source/epoch link, and verify that
its boot UUID matches the running recorder's observed boot. A small read-only
CaptureSpool binding accessor may retrieve those committed coordinates; it must
not create, migrate, rewrite or adopt anything. There are no invented runtime
coordinates or synthetic default identities in product code.

Add the listener descriptor to the recorder's existing select loop. Service at
most one ready datagram per iteration, after the existing capture/output work;
an idle child still permits queries. Query work never feeds stdin, ACKs/reclaims
chunks, changes status or rewrites health. Child close-range setup must close the
listener in the forked child before exec. Do not change the signal masking,
durable-before-display order, partial-write behavior or D27 cleanup guarantees.

Sample the response in that same loop; there is no helper thread that can
answer while the capture loop is stalled. `terminal_active` requires current
`Recorder.admission_ready`, the exact direct child observed at fork still live
at the recorded start identity, and no already observed child exit/PTY EOF.
Reuse the fingerprint primitive; a child observation failure produces inactive,
not another process selected by name. The child fingerprint is a direct-child
observation, not proof of successful exec, provider identity, authentication,
native session or readiness. Check readiness again after collecting those
observations before constructing a positive reply.

Server recv/send is nonblocking. A vanished/slow/malformed client cannot stop
capture, make a blocking send or kill the owned child. A fatal query-endpoint
error disables/closes that endpoint and leaves capture/drain running; clients
then report unavailable. Do not turn an optional observer failure into dropped
captured data. Binding/setup failure remains a prelaunch error as above.

Close the listener when the recorder exits or fails; record/attempt that cleanup
alongside all existing restorations without skipping later cleanup steps. Query
responses are fresh observations only. A process can still die immediately after
responding; do not claim atomic liveness or a lease. Socket waits use the stated
deadline; arbitrary kernel/filesystem stalls are not a hard real-time guarantee.

## Scope of a positive observation

This establishes live terminal capture evidence for the stated local process
and byte source only. Do not wire it directly as the complete production
`TerminalRunner.capture_ready` predicate: native source ingestion, supported
provider composer/paste evidence, machine holds, launch ownership, host backlog
policy and independent process supervision remain separate requirements.
Do not upgrade adapter capability flags or agent lifecycle because this probe
works. No public `runtime_start` or fake managed executor is introduced.

## Focused checks

Extend the existing transcript fixture support where useful. New probe tests
belong in the transcript suite; do not copy the runner suite or create another
generic process fixture framework. Use current installed/source import rules.

- One real nested-PTY recorder and a synthetic child: observe it while idle and
  after exact captured bytes, then exit normally. A saved healthy row after exit
  must not yield active. Assert probe traffic neither becomes terminal input nor
  changes byte cursors/ACKs by itself.
- Use the owned fixture's SIGSTOP/SIGCONT to prove that a stopped capture loop
  cannot answer via another thread or stale row; query timeout is bounded. The
  resumed same process can answer again. Reuse positive process ownership and
  explicit cleanup; no signals to names/unowned PIDs.
- Exercise stale/wrong nonce, source/run coordinates, boot/start fingerprints,
  missing/wrong sender credentials, and a different live responder. Use compact
  parameterized protocol checks; no malformed-response variant may be positive.
- Test occupied endpoint before epoch reservation, oversized/truncated/bad
  datagrams, vanished clients and socket cleanup. Capture continues through
  malformed traffic and observer failure. Cover inactive/blocked local admission
  without claiming that a persisted row by itself proves the local latch.
- Existing recorder/spool tests still pass, preserving bytes, startup signals,
  failure cleanup and terminal restoration. Run that owning suite once from
  source, then the focused live-probe flow from an installed wheel. Verify passive
  imports/unchanged pins with a small diagnostic; no whole-system suite repetition.

Use private temporary data/abstract sockets and positively owned child fixtures.
No provider/model turns, owner stores, services/cgroups/VM/network configuration,
global installs or browser material. No new dependency or usage examples.
Report code/evidence, exact tests/failures/cleanup, environment and remaining
limits in `docs/evidence/recorder-probe-r1.md`. Commit/push only the dedicated
branch, then stop edits for a fresh independent review. This does not advance
G0–G6 or establish managed-native-runtime readiness.
