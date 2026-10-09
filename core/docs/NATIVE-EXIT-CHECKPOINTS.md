# Native exit checkpoints and a final collection pass

Manager decision D54, 2026-10-09. Implement this narrow correction on the existing
onbash collector; creation and manual CLI management are unchanged.

The inspected normal shutdown path closes capture without another tick. A
disposable diagnostic reached assertions proving that a fsynced suffix remained
only in its native file at close and was archived on reopen. The diagnostic then
failed an invalid zero-parser-gap assertion, so it is not a passing test. Source
inspection independently confirms there is no production checkpoint caller.

## Implementation

On an orderly SessionMonitor stop, after the in-progress iteration settles and
before services close, perform one final owner reconciliation and collector tick
on that same monitor thread. Skip native setup in this final pass. Do not launch,
signal, stop, or release a native process. Keep the existing finite source/byte/
transfer budgets; do not loop until an actively written file stops growing.
Always close services if the pass fails, using fixed diagnostics without content.
An unexpected monitor failure must remain unhealthy, not become successful
shutdown evidence. NativeCapture.close remains a resource-only operation.

During ordinary collection, use the existing NativeCapture.checkpoint after a
source's bounded poll/transfer work when its admitted owners are closed with
verified exited observations. Any active/unknown or recovery-retired owner for
the same agent/provider/native UUID prevents treating that source as exited,
including a newly resumed owner whose source link has not yet been collected.
Keep the existing directory, recovery, lineage and conflict guards. Check current
owner state around checkpoint work; the saved proof never grants input authority.

A checkpoint is finite byte evidence. An EOF observation requires
proof.through_offset == proof.file_size == local_captured_offset ==
host_archived_offset, with no capture/transfer/ownership error or gap. A shorter
proof is not a drained source; continue normal bounded work. Reuse an unchanged
successful checkpoint that covers these bytes and was recorded after the latest
relevant owner closure, rather than appending one every tick or restart. Preserve
older evidence and capture late appends normally. A resumed run may append to the
same source/epoch. Never seal it using the Terminal recorder's finish_recording.

If such an exited-source EOF checkpoint is host-archived and fully scanned but
parse through_offset is behind it, expose the existing gap/parser_gap observation
with the existing pending_parse_bytes count. This identifies an unfinished final
record; it does not fabricate an assistant message or move parser cursors. A later
append can complete the same record through the existing parser. Reevaluate this
derived condition after append/resume; no permanent invented parse-gap row.

There is no public model, operation, host/local schema, parser-version, target,
permission or UI change. Keep daily-publication jobs durable through shutdown;
their normal restart catch-up remains unchanged. The final pass is best effort
within existing bounds, not proof of future producer bytes or an empty backlog.
An exited process is not proof that all descendants or file writers are quiescent.
These checkpoints do not establish all-source completeness, success exit codes,
child/attachment coverage or a backup barrier.

## Ownership and checks

Own runtime/native_collector.py and runtime/monitor.py, small helpers only if
needed, focused owning tests and one concise evidence note. Keep the primitive
checkpoint/receipt implementation and projector cursor semantics unchanged.

Use two grouped checks with existing temporary fixtures: final monitor pass/order/
failure cleanup and native producer survival; exited-source checkpoint, backlog/
failure/recovery refusal, repeat/reopen, incomplete suffix, append and resume.
Use a correct native header for the declared parser pin. Do not repeat the entire
native CLI/provider/UI workflow or fault matrix. Manager reviews the completed
candidate and reruns focused changed-behavior checks.

In parallel, a separate factual Codex fixture may establish actual child/media
formats with a disposable native HOME and canned loopback responses. It owns no
production files, uses no real model/provider, and cannot grant child capture
acceptance or change this implementation contract.
