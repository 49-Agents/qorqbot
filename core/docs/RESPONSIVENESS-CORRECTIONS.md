# Connected-flow responsiveness corrections

Manager decision D49, 2026-10-09. The accepted connected Codex evidence contains
ten `Operation capacity unavailable; call not admitted` responses. Its UI clears
the terminal view on a failed status read and thereby closes a healthy WebSocket.
The final private Terminal spool contains 3,320 chunks totaling 188,511 bytes;
1,147 retained chunks remain after cleanup. The existing four-chunk observation
budget is inadequate for these small PTY writes. These are three independent,
bounded corrections, not authorization for startup/browser/VM redesign.

All three components are accepted: [archive budget](evidence/terminal-archive-budget-manager-acceptance.md),
[request admission](evidence/dispatch-admission-manager-acceptance.md) and
[UI read recovery](https://github.com/qorqutbot/qorqutbot-ui/blob/main/docs/evidence/read-recovery-manager-acceptance.md).
The [reviewed combined real-core/UI run](evidence/responsiveness-integration-manager-acceptance.md)
passed 24 bounded burst reads and retained its
Terminal connection, but failed the 30-second archive catch-up gate. Its original
failed measurement is preserved. [D50](TERMINAL-TRANSFER-RANGES.md) supersedes
the one-host-object-per-original-chunk restriction below; other D49 scopes stay.
D50 now has [component acceptance](evidence/terminal-transfer-ranges-manager-acceptance.md)
and a passing smaller installed workload. The combined original workload remains
the integration gate, with its prior failure retained as evidence.

## Core request admission

Keep four executing Dispatcher calls. Permit up to twelve additional asynchronous
waiters for one second each, using the event loop rather than the executor queue.
Use the same admission helper for application invocations and native hooks.
Do not retry or execute a request twice. Full waiting capacity, timeout or closing
must refuse before any invocation/effect. Mark an application capacity refusal
retryable; an exception after invocation remains the existing possibly-executed
result. No business rules move into the transport.

Cancellation before execution withdraws the waiter. After execution starts, a
cancelled/disconnected caller still owns its running slot until the original
future completes. Shutdown stops admission, lets waiting calls resolve without
execution, and drains all executing calls before closing core. Never release a
running slot solely because its HTTP/MCP waiter vanished. Keep the executor queue
bounded, native hook outcomes unchanged, and original idempotency semantics.

Own `transports/server.py` and the existing dispatcher tests in
`tests/transports/test_transports.py`. Group meaningful checks for a short burst
that waits then succeeds, waiting overflow/timeout/cancellation without execution,
and running cancellation plus shutdown draining. Avoid a new service, dependency,
configuration knob or test suite. Record actual results in one short evidence file.

## UI status-read recovery

The separate UI lane follows `qorqutbot-ui/docs/READ-RECOVERY.md`: transient
runtime/terminal read failure is an observation error, not proof of a changed
terminal identity. Keep a healthy existing socket and its acknowledged control
state; do not establish a new socket from stale observations. Show the read error
and recover through the existing next poll/manual read. Positive identity changes,
malformed observations, authentication loss and actual socket failures still
invalidate the connection. No input/control/resize replay or automatic takeover.

## Small-chunk Terminal archival

Replace the fixed four-chunk cap in `runtime/terminal_archive.py` with three
simultaneous limits per existing observation: at most 128 original chunks,
at most 1 MiB, and a 200 ms monotonic scheduling budget. Always allow one complete
pending chunk, then check the time bound before beginning another; never interrupt
receipt/ACK/reclamation midway. A single I/O call can exceed the scheduling
budget, so this is not a hard latency guarantee. Do not coalesce, alter or skip
original chunks, weaken binding/recovery checks or use a historical watermark
as receipt authority. The existing one-second observer cadence stays unchanged.

Only `runtime/terminal_archive.py`, its owning existing tests and the assigned
evidence file belong to this lane. Test many small chunks advancing beyond four,
time/byte/count bounds with deterministic time, and safe receipt-based continuation
after a partial batch. Reuse existing interrupted-transfer tests rather than
duplicate them. Do not change recorder framing, local/host schema, native capture,
shutdown lifecycle or other worker/observer threads. If the measured bottleneck
is elsewhere or these limits cannot meet the checks, return facts to Manager.

## Integration

Three Astra MAX workers execute these Manager-authored scopes independently;
self-review is disabled. Manager directly reviews/tests completed changes, merges
them and performs one focused combined real-core/UI check. Existing immutable
connected fixtures are evidence, never mutable test installations. Component
checks do not prove the combined burst/backlog behavior. Retain all unresolved
cold-start, echo-association, final-drain, browser/VM and full-system limitations.
