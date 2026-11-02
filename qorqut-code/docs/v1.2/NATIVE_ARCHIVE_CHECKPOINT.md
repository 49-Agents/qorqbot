# Native archive checkpoint: the lossless raw transcript library

This is a library checkpoint: `bazarlyk.native_archive` is importable and tested, and nothing calls it. No provider, native product agent, model, database, discovery, Chat projection, notification or lifecycle code runs or was changed, and nothing was deployed, merged or published.

## Scope

New files only: `app/bazarlyk/native_archive/` (six modules), `app/tests/test_native_archive*.py` (five files) and this document. No existing runtime, Chat, provider bootstrap/session adapter, schema, import, dependency manifest or package export changed; the package is stdlib-only, so the dependency list is untouched, and the wheel picks the package up through the existing `bazarlyk*` package discovery.

The library archives bytes. It never decides who wrote a record: no record text, role or equality is used to infer owner/system provenance, and `RawRecord.value` is only the parsed JSON object of a valid line. Deriving Chat identity, read metadata or input correlation from the archive is later work (see "Remaining P2 work").

## Donor reuse

Read from the v2.2 donor `qorqut-v2.2/qorqut-code` at `df7daa7fddf387a7e91212f5ed831b32c0735f96`, through Git objects only (`native/transcripts/_files.py` and `_providers.py`):

| Module | From the donor | Changed |
| --- | --- | --- |
| `_files.py` | descriptor-relative traversal from `/` with `O_NOFOLLOW`/`O_DIRECTORY`; regular-file checks; lstat → open → fstat source identity; process-safe `flock`; fsynced temporary + `os.link` immutable publish; atomic replace; directory fsync | creation limited to one directory level at a time (the archive root leaf is the only root component created); positional `pread`/`pwrite` helpers; no snapshot-copy helpers |
| `_providers.py` | Claude `sessionId`/`cwd`/sidechain evidence; Codex `session_meta` before records | run incrementally over a bounded stream with one sticky `bound` flag; JSONL framing with a 32 MiB record cap that reports oversized lines without buffering or parsing them; 32-byte index entry packing; no Chat-text projection and no input correlation |

The donor's whole-prefix snapshots are not used: the archive keeps one append-only payload per lineage and a fixed-size receipt per capture.

## Public API

`from bazarlyk.native_archive import CaptureBinding, CaptureError, CaptureReceipt, FinalCaptureEvidence, IndexEntry, MAX_READ_BYTES, RawRange, RawRecord, RecordIterator, TranscriptArchive`.

- `TranscriptArchive(archive_root, *, clock=None, lock_timeout=10.0)` validates an absolute path string and creates nothing. The caller supplies the root (an agent's transcripts directory from `InstancePaths`); the library never discovers agents, businesses, provider homes or processes. `capture` creates only the root's final directory (its parent must exist: `archive_root_missing`), so a mistaken root cannot fabricate a tree.
- `capture(binding, *, final_evidence=None) -> CaptureReceipt`: the only writer. Returns the existing latest receipt with no new storage when bytes, binding generation and completeness evidence are unchanged.
- `latest(binding)`, `load_receipt(receipt_id)`: committed receipts only. A receipt published ahead of the pointer is `receipt_uncommitted`.
- `read_raw(receipt, *, offset, limit)`: up to `limit` (1 … `MAX_READ_BYTES`, 16 MiB) exact bytes, never beyond `receipt.length`; `RawRange.next_offset` is `None` at the end.
- `iter_records(receipt, *, offset=0)`: a `RecordIterator` (context manager) of `RawRecord(offset, length, kind, value)`; kinds `json`, `blank`, `malformed`, `oversized`, and a final `partial` locator for an unterminated tail. `offset` must be a record boundary.
- `index_entries(receipt, *, start=0, limit=1024)`: verified `IndexEntry(offset, length, kind)` for malformed/oversized complete lines. This is a supplementary read helper beyond the assignment's minimum; it adds no authority.
- `CaptureBinding(agent_id, provider, conversation_id, generation, workspace, source_path)`: `provider` is `claude` or `codex`; `generation` is the process generation of the native session; `workspace` and `source_path` are absolute. `FinalCaptureEvidence(agent_id, provider, conversation_id, generation, process_token, exited_at)` carries exit evidence supplied by a future lifecycle owner; `exited_at` must be timezone-aware.
- `CaptureError(code, message, retryable, latest)`: `latest` is the newest trusted committed receipt of the same agent's lineage when one exists (it is never attached to an identity mismatch).

A lineage is one agent + provider + native conversation ID, independent of process generation. Resuming with a new generation reuses it.

## On-disk layout

```
<archive_root>/<provider>/<encoded conversation>/
    lineage.json          immutable header: agent, provider, original conversation ID
    payload.raw           the single append-only raw payload (exact provider bytes)
    index.bin             append-only 32-byte entries for complete malformed/oversized lines
    receipts/<12-digit sequence>.rcpt   immutable 4096-byte hash-chained receipt records
    latest.json           atomically replaced pointer (sequence + digest of the newest committed receipt)
    staging/              transient: the current new suffix, unpublished receipt images
    .lock                 capture lock (flock)
```

Directories are `0700`, files `0600`. The conversation ID is encoded into one path component (`[A-Za-z0-9_-]` kept, everything else `%XX`, at most 128 encoded characters); the original ID is kept whole in the header and every receipt. The receipt ID is `<provider>:<encoded conversation>:<sequence>`.

**Receipt record** (exactly 4096 bytes): one canonical JSON line (sorted keys, compact), a `sha256=<digest of that line>` line, space padding, a final newline. The record digest (`record_sha256`) covers all 4096 bytes. Fields: `format`, `archive_id`, `sequence`, `predecessor_sha256` (digest of the previous record, `null` for sequence 1), `agent_id`, `provider`, `conversation_id`, `generation`, `workspace`, `source_path`, `source_device`, `source_inode`, `observed_at`, `source_size` (at the start of the observation), `source_size_end`, `length` (committed payload length), `prefix_sha256` (SHA-256 of `payload[0:length]`), `appended`, `complete`, `incomplete_reasons`, `final` (`process_token`, `exited_at`, or `null`), `records` (complete JSON-object lines), `malformed` (complete lines that are not JSON objects, including oversized), `record_boundary` (offset just after the last complete line), `partial_tail_bytes` (`length − record_boundary`), `index_entries`, `index_sha256` (SHA-256 of the first `index_entries × 32` index bytes). A strict decoder rejects unknown fields, wrong types and any record whose place does not match its content.

**Pointer**: `{format, archive_id, sequence, receipt_sha256}`. **Header**: `{format, agent_id, provider, conversation_id}`, created once with a fsynced temporary and `os.link` (so a racing creator can only lose).

**Index entry**: `<QQII8x` — offset, length, kind (1 malformed, 2 oversized), 0. Valid JSON-object lines and blank lines need no entry.

## Completeness and final-tail semantics

- A receipt is `complete` when the observation was stable (source size and modification time identical at the start and end of the read) and no unterminated tail is left in an active session. Incomplete reasons are `partial_tail` (unterminated final line, no final evidence) and `source_changed_during_observation` (not stable). An incomplete receipt still archives exactly the bytes it read.
- Final evidence is accepted only when it names the binding's agent/provider/conversation/generation (`final_evidence_mismatch` otherwise) and only on a stable observation; on an unstable one it is not recorded and the receipt says why. With final evidence an unterminated tail is retained as bytes, reported as `partial_tail_bytes`, and no longer makes the receipt incomplete: the tail is final-tail evidence, not a record.
- A capture that adds no bytes still publishes a metadata-only receipt for genuinely new evidence: a new generation, final evidence not yet recorded, or a stable observation that upgrades an incomplete receipt. Different final evidence for an already finalized generation is `final_evidence_conflict`. There are no downgrade receipts: an unstable re-observation of unchanged bytes publishes nothing.
- Receipts never carry a verdict about authorship, delivery or session health.

## Capture write order

Every capture holds `.lock` (non-blocking `flock` polled until `lock_timeout`; `archive_busy`, retryable, on timeout). In order:

1. Validate the binding and that its worst-case receipt fits 4096 bytes (`receipt_too_large` before any write).
2. Open the lineage (new directories are created one level at a time with the parent directory fsynced), read the header and check identity before anything else: another agent is `agent_mismatch` (carrying no receipt), a header naming another provider or conversation is `archive_integrity`.
3. Load and validate state, recover (next section).
4. Open the source descriptor-relative with no symlink, require a regular file and the archived device/inode.
5. Verify the archived prefix against the exact source in one bounded pass; copy only the suffix beyond the payload to `staging/<random>.suffix`, hashing and framing it incrementally (identity evidence is checked per record; a contradicting valid record fails the capture as `session_mismatch`, a transcript with no matching evidence as `no_identity_evidence`).
6. Re-check that the source path still names the same file, the source did not shrink, and the staged suffix equals a second read of the source (`source_replaced`/`source_changed`, retryable).
7. Create the header if the lineage is new (before any payload byte exists).
8. Append the staged suffix to `payload.raw` at exactly the committed end with positional writes (crash point `payload_chunk` after every chunk). Then `fsync` the payload whenever this receipt commits any bytes beyond the previous one — **including bytes a crashed writer already left in place, where nothing is written now** (a matching source and digest prove content, not durability) — and only then reach `payload_appended`.
9. Append the new index entries (any already present uncommitted entries must equal them) and `fsync` the index whenever the receipt commits entries, again including entries that were already present (`index_appended`). Creating `payload.raw` or `index.bin` fsyncs the lineage directory; a capture that recovers uncommitted payload or index bytes also fsyncs the lineage directory, because the writer that created those files may have crashed before its directory sync.
10. Build the receipt (digest of the payload via the continued hash state; index digest). Write it to a fsynced temporary in `staging/` (`receipt_temp`), `os.link` it as `receipts/NNN.rcpt` — never replacing — and fsync `receipts/` (`receipt_published`).
11. Write the pointer to a fsynced temporary (`pointer_temp`), rename over `latest.json` and fsync the lineage directory (`pointer_published`).
12. Delete `staging/` files.

The pointer is the commit. Everything before step 11 is either disposable (staging) or recoverable evidence that the next capture re-verifies. No receipt is published before every payload byte, index entry and file name it cites has crossed its fsync barrier in the capture that publishes it.

**I/O failure.** An `OSError` anywhere in a capture (an injected or real `fsync` failure included) becomes the retryable `io_error`, carries the newest trusted receipt as `error.latest`, deletes the capture's staged copy and publishes nothing: the pointer, the committed read boundary and every existing receipt are unchanged, and bytes already written stay in the payload as an uncommitted tail. A retry takes the recovery path below — tail confirmed by the source, adopted, flushed, committed once. The library cannot make a kernel that has reported a lost write durable again; it re-verifies the content and repeats the barrier, and a persistent device error keeps failing the capture rather than publishing.

## Recovery algorithm

Recovery is not a separate mode: it is steps 3–5 of every capture, run under the lock before any new write, and it only ever converges to one of three outcomes — an adopted state, an explicit error that leaves every byte as found, or a normal capture.

1. **Header.** Present: its agent/provider/conversation must equal the binding (`agent_mismatch`, which carries no receipt; `archive_integrity` for a header naming another provider or conversation). Absent: a pointer, receipts directory, non-empty payload or non-empty index means corruption (`archive_integrity`); otherwise the lineage is new.
2. **Pointer and chain.** The pointer's receipt must exist, decode strictly and have `record_sha256` equal to the pointer digest. Receipts numbered after the pointer are "ahead". When any are ahead (a crash between receipt publish and pointer replace, or a lost pointer — then the pointer counts as sequence 0) receipts `1…N` must all exist and every link must pass: same agent/provider/conversation as the header; `appended == length − previous length`; `index_entries == malformed`; `predecessor_sha256` equals the previous record digest; length, records, malformed and record boundary never decrease; workspace, source path, device and inode unchanged. Without ahead receipts the pointer receipt and its predecessor link are checked. A failure is `archive_integrity` and nothing is repaired or deleted.
3. **Temporaries.** `.tmp-*` files in the lineage directory (unpublished pointer or header images) are removed. If the payload is exactly as long as the newest candidate receipt, every file in `staging/` is removed (abandoned suffix copies and receipt images). Otherwise `staging/` is kept untouched as evidence until a later successful publish.
4. **Binding and source.** The binding's workspace and source path must equal the newest candidate's (`workspace_mismatch`, `source_path_changed`). The source must open safely (`unsafe_path`, `source_unavailable`) and be the archived device/inode (`source_replaced`). The payload must be at least as long as the candidate (`archive_integrity`).
5. **One verification pass.** The payload `[0, payload_size)` is hashed in 1 MiB chunks and compared with the source in lockstep. The hash must equal `prefix_sha256` at the length of every receipt loaded in step 2 — the whole chain when receipts are ahead, otherwise the pointer's receipt (`archive_integrity`). If the source disagrees before the candidate's length the failure is `source_shrunk` (source shorter) or `source_prefix_changed`; the prior receipt stays trusted.
6. **Index.** `index.bin` must be at least `index_entries × 32` bytes, hash to each receipt's `index_sha256`, and may be longer than the candidate only while the payload is also longer (an index ahead of the payload is `archive_integrity`). Longer-than-committed entries are checked against the entries recomputed from the payload in step 8.
7. **Adopt ahead receipts.** Only now — chain valid, payload digests matching at every receipt length, current exact source still containing the committed prefix — the pointer is atomically replaced to the newest ahead receipt. A receipt that fails any check is never adopted and its file is left in place.
8. **Uncommitted payload tail** (`payload_size > candidate.length`): if the source contains exactly those bytes (the pass in step 5 agreed to `payload_size`), they are *adopted*: the record boundary and unterminated tail of the candidate are re-framed from the stored bytes (the boundary must follow a newline and the bytes after it must hold no complete line — `archive_integrity` otherwise), the adopted bytes are scanned for identity and index evidence, and the next receipt covers them as part of `appended`. Nothing is appended twice, and the adopted bytes (and any index entries already present for them) are flushed — payload, index, then the lineage directory — before that receipt is published, exactly as if they had been written by this capture. If the source cannot confirm them (it shrank, was rewritten or rotated) the capture fails `unresolved_tail`; the payload, the index and `staging/` are retained, `error.latest` is the newest adopted receipt, and the read boundary does not advance.
9. **Normal capture** resumes from `payload_size`: the rest of the source is staged and published as in the write order. A source that grew after the crash is therefore extended in the same capture.

### Crash gaps

| Last step reached (`_crash_point`) | Left on disk | Next capture |
| --- | --- | --- |
| `suffix_read`, `staged` | staging copy only | staging removed; ordinary capture |
| `payload_chunk` (k of n chunks; the last chunk leaves the whole suffix written but unsynced) | payload partly or fully extended, not necessarily flushed | tail confirmed by the source: adopted, completed, flushed before its receipt, no duplicate bytes; unconfirmed: `unresolved_tail` |
| `payload_appended` | payload fully extended and flushed, index old | tail adopted; index entries recomputed and appended |
| index `fsync` not reached (index entries written, unflushed) | payload flushed, index entries present | tail adopted; present entries verified equal, not duplicated, and flushed before the receipt |
| `index_appended` | payload and index ahead of the newest receipt, flushed | tail adopted; present entries verified equal, not duplicated |
| `receipt_temp` | unpublished receipt image in `staging/` | image deleted; receipt rebuilt identically from verified state |
| `receipt_published` | receipt N+1 published, pointer old | chain validated, receipt adopted, pointer replaced |
| `pointer_temp` | as above, plus a `.tmp-*` pointer image | image removed, receipt adopted |
| `pointer_published` | pointer new, staging not cleaned | leftovers removed; an unchanged poll returns the same receipt |

The crash seam is a test hook (`_crash_point`) that production never overrides. A lost `latest.json` with intact receipts converges the same way as `receipt_published`. `app/tests/test_native_archive_recovery.py` runs every row for a first capture and for a later capture, repeated crashes in a row, a source that grew after the crash, a replaced source, a corrupt payload, a broken chain, index extras that do not match, stale temporaries, and a rejected binding that must not disturb an unresolved lineage. Ordering regressions record the `fsync` targets and the stage names of the recovering capture (a synthetic `Workbench` and an `os.fsync` wrapper; no real power failure) and require the payload, index and lineage-directory flushes before the `receipt_temp` stage when the crash left the bytes in place and nothing new needs writing. Injected `EIO` from the payload or the index flush must leave the pointer, the receipts and the committed read boundary unchanged, report `io_error` with the prior receipt, and the retry must commit the bytes exactly once.

### Retained uncertainty

`unresolved_tail` is deliberately sticky: the library has no delete, truncate or quarantine operation, so bytes it cannot confirm are never removed or silently trusted, and every following capture reports the same error until the source again contains those bytes. Resolving it otherwise is a deliberate operator action outside this library (a later lifecycle owner decides it); the library only reports it. An inconsistent state (`archive_integrity`) is likewise reported, not repaired.

## Integrity and read cost

Capture, `read_raw`, `iter_records` and `index_entries` each stream-verify the whole committed prefix (payload against `prefix_sha256`, index against `index_sha256`) in 1 MiB chunks before returning a byte, with constant memory (the parse buffer is bounded by the 32 MiB record cap, beyond which lines are reported `oversized` without being parsed). An unchanged poll adds no storage but costs a pass over the payload and the source prefix; a read costs a pass over the payload prefix. There is no cache of trusted state across calls. If that cost becomes a problem for very large transcripts it is a decision for the integration owner (for example a per-process verified-length cache), not something this checkpoint assumed.

## Errors

`CaptureError.code`: `archive_busy` (retryable), `archive_root_missing`, `unsafe_path`, `io_error` (retryable), `agent_mismatch`, `workspace_mismatch`, `source_path_changed`, `session_mismatch`, `no_identity_evidence` (retryable), `source_unavailable` (retryable), `source_replaced`, `source_changed` (retryable during an observation), `source_shrunk`, `source_prefix_changed`, `final_evidence_mismatch`, `final_evidence_conflict`, `receipt_too_large`, `unresolved_tail`, `archive_integrity`, `not_found`, `receipt_uncommitted`, `receipt_mismatch`. Programmer errors (wrong types, bad offsets, malformed receipt IDs) raise `TypeError`/`ValueError`.

## Limits

| Item | Limit |
| --- | --- |
| Receipt record | 4096 bytes, fixed |
| Encoded conversation component | 128 characters |
| Workspace / source path | 1024 bytes each |
| Process token | 128 bytes |
| `read_raw` limit | 16 MiB |
| Record cap (framing/parsing) | 32 MiB, longer lines are `oversized` |
| Streaming chunk | 1 MiB |
| Capture lock wait | `lock_timeout`, default 10 s |

A binding whose identifiers and paths cannot make a worst-case receipt fit 4096 bytes is refused before any write.

## Storage growth

The required fixture (`test_native_archive_growth.py`) appends 64 KiB (64 one-KiB Claude records) to a synthetic source 256 times and captures after each append. Declared metadata allowance: 4096 bytes per capture (one receipt) + 32 bytes per index entry + an 8 KiB lineage allowance (header, pointer, lock, rounding). Measured, counting every file under the archive root (payload, 256 receipts, header, pointer, lock, staging and temporaries):

| | Bytes |
| --- | --- |
| Payload | 16,777,216 (exactly 16 MiB, equal to the source) |
| Metadata | 1,049,010 (256 × 4096 + 231 header + 203 pointer + 0 lock) |
| Total | 17,826,226 (allocated blocks 17,833,984) |
| Declared allowance | 1,056,768 |
| Sum of whole-prefix snapshots (the rejected design) | 2,155,872,256 |

The suite also checks that 25 unchanged polls leave the tree byte- and mtime-identical, that a half-length run stores exactly half the payload and metadata within one lineage allowance, that doubling bytes per capture doubles the payload and leaves the metadata unchanged, and that 120 captures each adding one malformed line grow `index.bin` by exactly 32 bytes per entry while receipts stay 4096 bytes.

## Checks

From `app/`, with a disposable venv that holds only pytest (the system interpreter has none); no full suite was run:

```
python -m pytest tests/test_native_archive.py tests/test_native_archive_recovery.py tests/test_native_archive_growth.py tests/test_native_archive_installed.py -q
```

Result at the first checkpoint commit: 66 passed (31 core, 29 recovery, 5 growth, 1 installed-wheel; the growth module takes about 45 s because the required fixture keeps real fsyncs). Mutation checks of the recovery and integrity paths (four during development, then removing the record-boundary/tail-cursor checks and the missing-header check) each made a test fail, and the code was restored.

Durability-ordering correction (review of `7af4c84`): the payload flush used to run only after bytes were written by the same capture, and a present index tail was never flushed, so a capture recovering a fully written but unflushed suffix published a receipt over bytes that had never crossed a file barrier. Five regressions were added to the recovery file (payload ordering, lineage-directory ordering, index ordering, and `EIO` from the payload and from the index flush with a safe retry); each failed on the uncorrected code or under a mutation that removed the corresponding barrier, and passes now. Directly affected runs: the core and recovery files, 65 passed (31 core, 34 recovery). The growth fixture and installed-wheel evidence were not rerun: the correction moves one `fsync` call and adds a directory `fsync` only to captures that recover uncommitted files, so the stored representation (and the figures below) is unchanged.

**Installed-wheel check.** `test_native_archive_installed.py` skips unless `NATIVE_ARCHIVE_INSTALLED_PYTHON` names an interpreter. Procedure used (all under `/tmp`, nothing global):

```
cp -r pyproject.toml bazarlyk /tmp/na-build/src
python -m pip wheel --no-deps -w /tmp/na-build/wheels /tmp/na-build/src      # bazarlyk_core-1.2.0.dev0-py3-none-any.whl
python -m venv /tmp/na-installed
/tmp/na-installed/bin/python -m pip install --no-deps /tmp/na-build/wheels/bazarlyk_core-*.whl
NATIVE_ARCHIVE_INSTALLED_PYTHON=/tmp/na-installed/bin/python python -m pytest tests/test_native_archive_installed.py
```

`--no-deps` is used because the distribution's other dependencies (one a Git URL) are irrelevant to this stdlib-only package. The child runs `python -I` from an unrelated directory with no `PYTHONPATH`, asserts the imported `bazarlyk.native_archive` lives in `site-packages` and not in the source tree, performs a synthetic Claude capture sequence (partial tail → completed record → final-tail evidence), reads the bytes and records back and walks the hash chain. The built wheel (sha256 `739f8a842d04560c34777b50f6029aac9d6109916ccacd6f0bdd762e7956e93a`) contained `bazarlyk/native_archive/{__init__,_archive,_files,_format,_providers,_types}.py`. The wheel's build backend was fetched from the package index by pip in the isolated build environment; nothing else left the machine.

## Decisions

- Creation is limited to the archive root's final directory, so a wrong root (or a deleted agent directory) fails with `archive_root_missing` instead of recreating a tree.
- A receipt records the exact `workspace` and `source_path`; a later capture must present the same strings (no normalization or aliasing). Moving a workspace or transcript requires a new lineage decision by the integration owner.
- No downgrade receipts and no deletion, truncation or quarantine operation.
- A first capture rejected before any evidence leaves an empty lineage directory containing `.lock` and an empty `staging/`, and no header, payload or receipt.
- `index_entries` is a read helper added beyond the minimum API.

## Remaining P2 work (not done here)

- Verified native-session integration: choosing the source path and generation for a started session, calling `capture` on a cadence, supplying `FinalCaptureEvidence` from the lifecycle owner, and deciding how `unresolved_tail`/`archive_integrity` are surfaced.
- Input adapters and correlation of submitted input to archived records.
- Derived Chat identity and read metadata (owner/system provenance, message ordering, read markers) computed from archive receipts and records.
- Notifications, backend inbox nudges and lifecycle activation/wiring; no startup path imports this package.
- Any per-process verified-length cache, retention/deletion policy or operator procedure for an unresolved tail.
