# Transfer contiguous Terminal chunks together

Implementation has [Manager component acceptance](evidence/terminal-transfer-ranges-manager-acceptance.md).
Its isolated installed measurement passed. The full original two-tab workload
with the changed transfer framing is the next integration check.

Manager decision D50, 2026-10-09. D49's combined run kept its Terminal connected
and passed all 24 bounded burst reads, but failed the original 30-second archive
catch-up gate: 6,460 of 29,014 captured bytes were on the host. Its recorder made
420 small chunks. A separate Manager profile measured 289 ms for one 72-byte
transfer, including nine fsync calls and repeated durable SQLite transactions.
This profile is a diagnostic, not a latency guarantee or a replacement for the
failed integrated check. Raising the loop count did not remove per-chunk costs.

Keep capture framing unchanged. Combine already-durable, contiguous original
Terminal chunks into a bounded host transfer range. This uses the existing
Archive.receive contract and immutable object format; native-source transfer,
host schema, public APIs, recorder behavior and observer cadence do not change.
This explicitly supersedes D49's requirement that every original Terminal chunk
have its own host object. Raw bytes, offsets and original local receipt records
are preserved. Host chunk boundaries describe transfer ranges; they need not
equal recorder read boundaries.

## Range construction and limits

Within runtime/terminal_archive.py, read pending chunks through the existing
CaptureSpool API. Its pages contain at most 16 chunks. Follow next_offset with
bounded pages, validating source/epoch, hashes, exact contiguous offsets and the
starting local ACK. Never split an original chunk. A range contains at most 128
original chunks and 256 KiB, the existing maximum Archive chunk payload. Stop
before the next whole chunk would exceed either bound. Do not skip that chunk.
No waiting to fill a range: transfer whatever is already available.

Compute SHA-256 over the exact concatenated payload and construct one ordinary
Chunk at the first original offset with the summed length. Save a versioned
manifest of the original offset/length/hash triples in the transfer intent.
CaptureSpool retains those original triples after reclaim; do not replace them
with the aggregate hash or change recorder/capture tables.

Retain D49's per-observation limits of 128 original chunks, 1 MiB total and a
200 ms monotonic scheduling budget. Count manifest constituents, not host calls.
Allow the first complete pending range and finish its receipt/ACK/cleanup, even
if one I/O takes longer. Then check limits before beginning another range. Pages
and assembly are bounded; never load an entire recording. The existing pending
page may hold up to 16 maximum-size chunks, so do not claim a 256 KiB peak-memory
bound. No new observer thread, tight catch-up loop or scheduler is needed.

## Durable intent and compatibility

Extend the existing local terminal_archive_transfers table with nullable
chunks_json TEXT. Check table_info and add the column in a short transaction
when opening an older store; new tables include it. Use explicit INSERT column
names. No host migration or CaptureSpool schema change is needed. NULL means
the legacy exactly-one-chunk intent and must retain that validation/replay path.
New values use an explicit version and a nonempty bounded list of original
coordinates/hashes. Reject malformed, unknown-version, gapped, reordered,
oversized or mismatched manifests; never reinterpret one as legacy.

On every attempt, keep the existing exact producer, workspace, recovery,
conflict, invocation and lifetime/effects checks. Validate all manifest entries
against the original CaptureSpool receipt ledger, including after ACK/reclaim,
using bounded receipt pages. Their contiguous range must equal the intent's
offset and length. The local ACK must be the range start or exact range end.
Older retained bytes without receipt authority still block cleanup.

The sequence remains:

1. Persist the aggregate intent and full manifest before host I/O.
2. When no saved receipt exists, reassemble that exact saved range from retained
   payloads, validate each original hash and the aggregate hash, then call the
   unchanged Archive.receive. New recording output cannot enlarge an old intent.
3. Validate the returned binding/source/epoch/recovery and exact end offset;
   persist the actual receipt before ACK. A historical host watermark never
   authorizes ACK or local cleanup.
4. ACK the whole range, reclaim only its wholly acknowledged payloads, then
   retire the intent. Retry saved receipts after interruption without requiring
   reclaimed payloads. Recheck the original ledger and all existing holds.

A crash before saving a returned receipt repeats the exact same host range,
not freshly selected boundaries. A legacy pending intent is finished unchanged
before constructing new ranges. Existing host objects remain unchanged and new
ranges append at their end. Missing/corrupt bytes, receipt mismatch, storage
errors or conflicting host history retain unconfirmed local payloads.

## Implementation and verification

Own only runtime/terminal_archive.py, its existing owning test module, and one
short evidence report. Keep Archive, CaptureSpool, recorder and native collector
unchanged. Adapt the owning tests' host-boundary expectations to this decision,
preserving byte identity, original local ledgers and prior failure assertions.
Group coverage for binary and many-small-chunk ranges, byte/count/time bounds,
exact replay through receive/receipt/ACK/reclaim interruption, altered manifest
or payload rejection, and genuine legacy intents before and after saved receipt.
Reuse the existing receipt/conflict/recovery tests; do not duplicate archive tests.

After focused tests, measure one disposable installed-core recorded-Bash run
with 400 distinct short writes about 10 ms apart and the normal observer. Record
source chunk count, host range count, original ledger and reconstructed host
bytes, and automatic catch-up within the unchanged 30-second target. No native
CLI, provider, live owner data, manual observer pumping or product patching in
the fixture. Preserve a failed measurement and ask Manager rather than changing
limits silently. Clean only owned fixture processes.

Manager reviews the completed result directly. The original D49 failure remains
part of its own report; it is not overwritten or relabelled as a passing run.
The separate native cold-start facts do not yet authorize a new input or owner
admission rule. Initial Chat, final drain, browser/VM and full-system gates remain
open.
