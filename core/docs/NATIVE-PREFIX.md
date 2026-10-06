# Native file prefix verification

Manager design, 2026-10-08. This is the first implementation following the
accepted [native exploration](evidence/native-capture-p0.md), D32. It provides
read-only evidence needed by the later native reader. It does not ingest files,
persist checkpoints, authenticate a native session or establish runtime health.

## Scope and ownership

Add receipt pagination to `transcripts/spool.py` and a narrow file verifier in
`transcripts/prefix.py`. Reuse the permanent `capture_chunks` receipt coordinates
and hashes, including receipts whose payload has been reclaimed. No schema,
database, dependency, thread, service, network operation or public tool is added.
Existing spool writes, health, ACK/reclaim, recorder, adapters, host archive and
application assembly keep their semantics. Neither new callable mutates them.

The subsequent native-reader assignment must still define source/run mapping,
safe managed-path opening and retained ownership proofs, durable checkpoints,
append/discontinuity handling, held-descriptor drainage and provider evidence.
This verifier must not fill those design gaps or wire itself into input readiness.

## Receipt view

Add immutable local dataclasses `ChunkReceipt(offset, length, sha256)` and
`ReceiptPage(source, source_epoch, through_offset, receipts, next_offset)`.
`source` is the existing capture `Source`; `receipts` is a tuple of
`ChunkReceipt`. No payload, ACK permission or mutable SQLite row escapes.

Freeze this internal method:

`CaptureSpool.receipts(source_id, source_epoch, *, after=0, through=None, limit=16)`
returns `ReceiptPage`. `after` is an inclusive next receipt boundary, unlike
`pending`'s overlapping chunk behavior. `through=None` freezes the current
`local_captured_offset` in that call's short read transaction. An explicit
`through` may select an earlier complete receipt boundary. It cannot exceed
the current captured cursor. `limit` is a strict integer in 1..16; coordinates
use the existing canonical IDs and strict signed-range integer validation.

Use one short consistent SQL snapshot for source, epoch and each bounded page;
close it before returning. Return receipts regardless of ACK, payload presence,
current source epoch or historical health. Do not use `pending` or `append` as
a read API. Pagination neither reconstructs bytes nor checks a file.

Require `0 <= after <= through <= captured_cursor`. Zero is an empty valid
prefix; other endpoints must be actual receipt boundaries. Unknown lineage
raises existing `BindingConflict`; malformed arguments raise `ValueError`;
caller-selected non-boundaries raise `CoordinateConflict`. Invalid stored
receipt lengths/hashes, gaps in the traversed range or a saved captured cursor
without its ending receipt raise `CaptureBlocked`, without latching/writing
health. For each page check contiguous offsets from `after`, valid 1..256 KiB
lengths, lowercase SHA-256 and ends at or below `through`. An empty page is
successful only at `after == through`. Do not silently skip missing receipts.
Bounds validation must not scan/materialize all history to serve one page.

`next_offset` is the last returned end, or `after` for an empty completed page.
Every page preserves its selected `through_offset`. A caller passes the first
page's target explicitly on subsequent pages, so concurrent appends cannot
extend its work indefinitely. Existing append/reclaim APIs preserve receipt
coordinates/hashes; separate pages need no long-lived SQL transaction.

## Borrowed descriptor verification

Freeze the internal function in `prefix.py`:

`verify_file_prefix(spool, source_id, source_epoch, fd, *, expected_device,
expected_inode, through=None) -> FilePrefixProof`

The caller supplies an already open regular-file descriptor and its expected
device/inode. The descriptor and both identity integers must be strict and nonnegative; do not
impose SQLite's signed coordinate limit on operating-system identity values.
Require a declared native source. Reject other source types with
`BindingConflict`; this is not an adapter/source ownership check. The verifier
never opens a pathname, adopts a replacement, changes the descriptor position,
closes the borrowed descriptor or retries discovery. Path/root/session/run
attestation belongs to the later caller.

Obtain the first receipt page and its fixed target. `fstat` the descriptor,
require regular type, expected device/inode and size at least the target.
Stream every original receipt range through `os.pread`, at most 64 KiB per read,
and compare each complete range's SHA-256 with its permanent receipt. Handle
positive short reads without skipping bytes; zero before a required end is
an incomplete prefix. No JSONL parsing or whole-file materialization. Compute
SHA-256 of the whole verified prefix and its final at-most-64-KiB tail, keeping
bounded page/read/tail memory. No file I/O occurs inside a spool transaction.

After all pages, repeat `fstat`. Device/inode, size, mtime_ns and ctime_ns must
equal the initial observation; reject growth as well as other changes during
verification. A file already longer than the selected prefix is allowed:
suffix bytes are neither read nor certified. Unlink/rename before verification
does not invalidate an independently held regular descriptor. Changes during
the observation are handled by the same finite stat comparison.

Return an immutable `FilePrefixProof` with `source_id`, `source_epoch`,
`through_offset`, `device`, `inode`, `file_size`, `mtime_ns`, `ctime_ns`,
`prefix_sha256`, `tail_offset`, `tail_length` and `tail_sha256`. Tail offset is
`max(0, through_offset - 65536)`; empty prefix/tail use SHA-256 of empty bytes.
The proof describes that selected prefix only, even if the spool grew while
it was inspected. It is not a claim about the latest cursor, future file bytes,
all intermediate file versions, append-only provider behavior, liveness or ACKs.

Use one `FilePrefixChanged(CaptureError)` exception with a fixed `reason` from
`identity_changed`, `prefix_shorter`, `prefix_mismatch`, `changed_during_read`.
Nonregular or no-longer-open descriptors and failed file I/O raise `CaptureBlocked` with
a bounded fixed diagnostic; ordinary malformed function arguments raise
`ValueError`. Propagate receipt-view failures. Never include source bytes or
OS exception text/path data in a diagnostic. No silent retry, partial-success
proof, health change, conflict insertion or epoch allocation on failure.

Hashes allow comparison with supplied current bytes after reclamation; they
cannot recover overwritten originals. Stat/proof checks are finite observations,
not a filesystem snapshot or protection against a hostile writer. A future
reader must decide how to persist/revalidate this evidence and handle divergence.

## Focused acceptance

Own one compact `tests/transcripts/test_prefix.py`; preserve existing tests and
reuse the small synthetic spool fixtures. Cover these boundaries without a
duplicate matrix or native/provider turns:

- Page boundaries, explicit frozen targets, gaps and unchanged SQL/health/ACK
  state, including ACKed/reclaimed payloads and a retired source epoch.
- One exact stream over 8 MiB with binary bytes, a giant record and partial
  UTF-8/JSON suffix. After real ACK/reclaim/reopen, verify all original receipt
  boundaries plus prefix/tail hashes through a held descriptor in bounded reads.
- Earlier-byte rewrite with an unchanged tail, truncation, different expected
  inode, invalid/nonregular descriptor, I/O failure and a mutation during reads
  fail without mutation. Preserve descriptor position/ownership on every path.
- A renamed/unlinked held file remains verifiable; a replacement cannot bless
  its identity. Empty prefix, short reads and a preexisting suffix are honest.
- A concurrent spool append cannot expand the selected target. Observe actual
  SQLite transaction state during file reads; do not simulate success using
  a mocked spool or bypass receipt validation.

Run the new suite and the nine existing spool tests once on final source. Build
and install the unchanged lock in a private environment; run the new suite from
byte-identical tests-only staging, asserting installed product origins and
contents. There are no new child processes to justify duplicating the recorder
or provider suites. Any additional run needs a failure/change-specific reason.
Record real commands, failures/corrections, provenance and cleanup in
`docs/evidence/native-prefix-r1.md`. Commit/push the owned worktree, stop edits
and report once for fresh independent review. No deployment or G0–G6 acceptance.
