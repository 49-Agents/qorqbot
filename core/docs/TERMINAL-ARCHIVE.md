# Automatic host archival of the recorded Terminal

Current transfer framing follows [D50](TERMINAL-TRANSFER-RANGES.md): preserve
original local capture chunks, combine contiguous chunks into bounded host
ranges, and keep durable manifests with exact receipt-based ACK/replay.
That decision supersedes the single-chunk intent description below. D49 replaced
the original four-chunk scheduling limit; the remaining ownership/durability
rules still apply.

Manager decision D41, 2026-10-09. The accepted recorder saves Bash's PTY output
in `runtime/terminal.sqlite3`; its host ACK remains zero because no production
caller transfers it. Archive already accepts this terminal producer independently
of a native CLI. This increment connects those existing boundaries for onbash.

## Implementation

Add a small `runtime/terminal_archive.py` callable, invoked by `Terminals.observe`
after the existing owned-workspace and saved-shell checks. Keep it on the existing
terminal observation thread; add no service, worker thread, host job, dependency,
public operation or host migration. The normal server must perform the transfer
without a viewer, native owner, manual tick or export command. The existing native
collector and its durable transfer format are unchanged in this increment.

The callable takes the explicit existing terminal database, inspected ShellView,
host Archive and registered agent Invocation. Open and close its local SQLite/
CaptureSpool connections within the call. Do not retain a connection across
observation threads. Construction/import must not start execution. Read the exact
saved `runner_shell_terminals` record and capture source/epoch metadata; compare
terminal/spec/source IDs with the supplied shell and installation/agent lineage.
Use `CaptureSpool.recording_binding` to corroborate the recorder's original run.
Its run ID is the terminal ID, generation 1, and boot/recovery identities are the
persisted recording identities. Never substitute a native owner or current boot.
The source must actually be terminal, with no native-session ID. Preserve the
original capability evidence and metadata; do not manufacture registration facts.

Archive is about already recorded bytes. A stopped shell, temporarily unavailable
probe or closed native CLI does not invalidate its saved capture identity. Drain
that existing spool when the enclosing owned-workspace/provisioning/recovery
checks permit it, without declaring the process live or relaunching anything.
Do not scan arbitrary databases or bypass the existing recovery hold when the
installation/spec no longer matches.

Use `Archive.register`, `receive` and the existing spool APIs directly. Hold the
host lifetime/effects guard through registration, receive, local ACK and reclaim;
Terminals already holds these guards and the workspace guard. No host database
transaction may surround filesystem/runner I/O. Bound one observation to four
chunks (at most 1 MiB); later observations continue at the durable watermark.
Do not load the whole recording or run a catch-up loop without a bound.

Keep one durable transfer intent per terminal source/epoch in a new local table
in the same `terminal.sqlite3`, with original binding, offset, length, hash,
host recovery epoch and nullable exact ArchiveReceipt. Use WAL, FULL and short
transactions as the existing local storage does. The transfer sequence is:

1. Register/verify the exact producer. A host conflict or recovery hold stops
   transfer and local cleanup, including an intent already locally ACKed.
2. Persist the next exact chunk's intent before host receive. Retrying an intent
   must use its saved binding/coordinates/hash and the same host recovery epoch.
3. Receive and validate the complete returned receipt against that intent:
   binding, source, epoch, host recovery epoch and exact end coordinate. Persist
   that receipt before advancing the local ACK.
4. ACK, reclaim only the receipted payload, then retire that completed intent.
   A crash after ACK resumes from its saved matching receipt before reclaim.
   A crash before saving the receipt retries the same idempotent host receive.

A historical `Archive.state` watermark is never an ACK permission. Mismatch,
storage failure and missing receipt leave unconfirmed bytes/intents intact.
Do not clear a recorder health error or advance capture state to make archival
succeed. No local cleanup is authorized merely because the pending page is empty.

Catch transfer failures separately from terminal availability: a host storage
problem must not close the recorded shell or discard its local spool. Keep logs
to fixed diagnostic categories without raw content/paths. Existing recording
local/host counters remain truthful and show lag; no new public fields or false
complete-history status are added. Subsequent observations retry safe stored
work. Re-read the saved capture counters for the published view after transfer.
Local recording remains separate from native text/dated Chat publication.

## Focused acceptance

Use a few grouped tests at this boundary: binary/multiple-chunk transfer with
the original terminal binding and no native owner; interrupted receive/receipt/
ACK cleanup and identical retry; mismatched receipt, conflict and recovery hold
retaining payloads. Reuse existing Archive tests for its file-durability rules;
do not duplicate that suite or change accepted native transfer behavior.

Exercise one normal installed-server flow in a fresh private fixture: create
onbash and let the normal recorded shell/observer archive output, stop only core,
write another marker through the surviving owned shell, restart identical core,
and observe automatic host catch-up without input replay or native launch. Check
exact chunk coordinates/hashes/bytes, local ACKs, one registration and unchanged
terminal identity. Finish the owned shell normally and verify its remaining
captured bytes drain. Clean up only the fixture and record process/socket checks.
No paid model, live Qorqut/user state or VM installation is involved.

The parallel UI input-batching correction owns no core files. Concurrent native
workflow verifiers remain pinned to their existing core/UI versions; their
fixtures must not be inspected or repinned. Manager reviews this completed change
directly. Record the candidate, focused checks and installed-flow limits in
`docs/evidence/terminal-archive-r1.md`. No complete G3, backup or deployment claim.
