# D60 — Terminal capture storage and stopped-shell recovery

Manager decision, 2026-10-10. The owner cannot open onbash Terminal. Its recorder
exited with code 74 and CaptureBlocked; Bash and Codex are gone. The retained
spool contains 1,173,847 original chunks / 40,945,718 captured bytes. Disk space
is available. The precise underlying historical SQLite exception was overwritten
by the recorder's generic CaptureBlocked diagnostic, so it is not established.
Read-only timings show ACK range, retained-prefix and reclaim queries taking
about 0.53 / 0.39 / 0.47 seconds each, scanning original history. Reclaim runs
inside a write transaction and performs both a SELECT and UPDATE. Correct this
established scaling defect without altering capture bytes or receipt semantics.

## Implementation

1. Make CaptureSpool.pending and acknowledge seek from the relevant coordinate
   using the existing primary index. Preserve existing interior-byte cursors and
   partial ACK behavior: seek the predecessor which can overlap the starting
   cursor, then read the forward bounded interval. Do not change a partial cursor
   into permission to skip, reclaim or acknowledge an incomplete range. Keep
   missing-range/conflict checks and exact replay behavior.
2. Add one compatible partial index on capture_chunks(source_id, source_epoch,
   offset) WHERE payload IS NOT NULL. Use it for reclaim and terminal archival's
   older-retained-payload check. Reclaim scans retained rows only and releases
   only wholly acknowledged payloads; retain every historical receipt row. Include
   an indexed offset bound in addition to the end-coordinate check. New and old
   spools acquire the index through existing local schema initialization, with no
   host migration or table/format change. Plan deployment while the failed recorder
   is already stopped; do not run an index migration on the owner's live store
   from a worker.
3. Preserve a bounded diagnostic category through capture failure wrapping: when
   an underlying SQLite error exists, retain its sqlite_errorname or numeric code
   beside the outer error class. Do not include SQL, payloads, paths or secrets.
   Do not change fail-closed recording behavior, retry policy or process cleanup.
4. Keep transfer byte/count/time budgets, recorder framing, receipt authorization,
   native capture semantics, public operations, UI and host schema unchanged.

## Focused verification

Use compact existing spool/terminal archive tests for partial cursor/ACK,
reclaim, exact replay and failures. Add one owning synthetic large-ledger check
(roughly one million short original records, most payloads already reclaimed)
showing near-tail reads/ACK/reclaim avoid work proportional to retained history;
record query plans/SQLite progress counts and timing without a fragile wall-clock
pass threshold. Include an installed-package recorded-shell/archival check and
confirm input/output capture and final drain, using only a disposable fixture.
No inference, browser testing, live owner DB mutation or copied transcripts.
Build wheel, commit, push, and report exact artifact/hash and limitations.

## Recovery scope

Manager will preserve the entire failed terminal store and runtime metadata,
finish archiving all committed bytes, and corroborate that the old recorder,
Bash, Codex and inbox owner are gone before establishing a fresh shell. Preserve
agent identity, workspace, native HOME, conversation stores and browser. Never
clear failed capture health, reuse the failed source epoch, replay old messages,
or auto-start/resume a model. A separate read-only exploration lane traces exact
current path and receipt dependencies to identify blockers to this operator
recovery; it makes no design decisions or changes. No generic restart UI/service
or unrelated lifecycle redesign is part of this repair. Further recovery decisions
belong to Manager after the facts arrive.

Owner instructions: Astra MAX only, no review rounds, no mobile/auth work; merge
and deployment authorized. This repair is not full-system acceptance.
