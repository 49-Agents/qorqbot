# Batch queued human terminal input

Manager correction D41, 2026-10-09. Actual native-workflow testing exposed about
0.337 seconds between successive one-character frame sends. The client currently
waits for a receipt per character, so a long typed command can take minutes.
This measurement is end-to-end; individual backend costs are not attributed.

Change only how the existing `TerminalClient.flush` packages queued bytes. Send
the first available input immediately. While a frame awaits its receipt, admit
the same existing human input events into the bounded queue. On the next flush,
combine the consecutive input entries at the front of that queue into one frame,
stopping before a resize entry. Concatenate their already encoded Uint8Arrays
without decoding, normalizing, adding Enter or changing byte order. The existing
64-KiB queued-input bound also bounds the combined frame. Account for all removed
bytes exactly once. Allocate only for the batch being sent; do not repeatedly copy
the whole pending queue on every key.

Retain one in-flight frame, one assigned sequence and one exact byte-count receipt
per sent batch. Do not append to or retry an already sent frame. No debounce timer,
new acknowledgement protocol, pipelined writes, backend change or dependency is
needed. Preserve the current resize coalescing/order, all-or-nothing paste size
admission, UTF-8 and xterm framing. A control/connection/identity change clears
unsent batches exactly as it clears today's queue; stale receipts never revive
them. Keep the existing overflow behavior, output limits and human-input-only
sources. This does not grant automated Chat delivery or native input readiness.

Extend the owning Chrome test group rather than making a new suite. Hold one
receipt, type a substantial burst with actual Playwright keyboard events, and
verify that it stays unsent until that receipt then becomes one contiguous batch
with exact bytes and one matching receipt. Cover an intervening resize barrier,
Unicode/control/paste bytes and control-loss/no-replay using the existing checks.
Update the 64-KiB queue expectation from two 32-KiB input frames to one 64-KiB
batch while retaining overflow rejection and the final resize. Do not replace
real typing by insertText merely to make the batching check pass.

Run the locked build and the owning Terminal Chrome checks. Then use one fresh
private installed-core/compiled-UI Bash fixture to type a harmless approximately
500-byte command through the real textarea with keyboard.type(delay=2). Let its
exact input receipts settle, press Enter separately and verify the intended shell
output. Record total typing/drain time, frame counts, exact concatenated bytes,
matching receipts and absence of replay. The burst should drain within 15 seconds
after typing ends; report a failure or remaining backend delay rather than raising
the bound. That is a bounded regression target on this fixture, not a universal
latency guarantee. No native CLI, real provider call or broad UI QA rerun is needed.

Use core executable pin `a2a162ce67f0eccafee537bd75a97069c1280788`, which excludes
the concurrently built Terminal archive connection. Native workflow actors retain
their earlier UI pin and fixtures. Do not inspect or edit their unfinished work.
Reuse only the completed recorded-Terminal fixture's normal-server setup and
owned-process cleanup, copied into a new private directory. No mocked backend,
internal client method calls or direct WebSocket input substitution in this flow.

Record the candidate, focused checks, real-browser result and remaining single-
frame latency limits in `docs/evidence/terminal-input-batching-r1.md`. Manager
reviews the completed change directly. No deployment or native-workflow claim.
