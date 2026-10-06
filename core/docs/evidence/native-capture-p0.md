# Native file capture P0: accepted-source evidence

Exploration only, 2026-10-07 America/New_York. Clean starting branch
`codex/native-capture-p0`, base `e9d87155588ccce89243eae6d83a8fb28aeed95e`.
All source line references below are at that base; paths without `docs/` or
`tests/` are relative to `src/qorqut_core/`. This report supplies facts and
unanswered questions, not a native-reader design or acceptance decision.

Read applicable ancestor/worktree AGENTS.md (no applicable CLAUDE.md found),
IMPLEMENTATION-PLAN, CONTRACTS, BUILD-EXECUTION, ARCHITECTURE, M04/M07,
D07/D16/D27/D30/D31, PORTING and the accepted adapters, recording,
runtime-composition and recorder-probe evidence. HOST-ARCHIVE.md:1–50 is a
Manager design, not implemented archive evidence. At this base the host has
migrations 1–3 (`storage/migrations.py:20,58,90`), and no archive assembly
(`app.py:126–145`). No other lane/worktree or its private validation material
was inspected.

Observed inspection environment: `/usr/bin/python3.12` 3.12.3, stdlib SQLite
3.45.1, Linux 6.8.0-138-generic x86_64. Product code was read/AST-parsed, never
imported or executed. The unchanged lock specifies Pydantic 2.12.3 and
pydantic_core 2.41.4; these are pins, not a newly verified installed environment.
Native versions/help and historical test results below are attributed to the
accepted reports; no provider probes, tests, builds or synthetic reproduction
were run here.

**1. Adapter records supply observations, not capture coordinates.**

The actual input/output types are:

```python
# adapters/types.py:12–30,129–144,163–201
Provider = Literal["claude", "codex"]
RunBinding:
    agent_id: UUID
    conversation_id: UUID
    recovery_epoch: UUID
    environment_generation: int  # Field(ge=1, strict=True)
    boot_id: UUID
    run_id: UUID
    run_generation: int          # Field(ge=1, strict=True)
NativeBinding:
    run: RunBinding
    native_session_id: UUID
    resumed_from_run_id: UUID | None = None
SourceEvidence:
    kind: str
    offset: int
    length: int
    sha256: str
NativeSource:
    provider: Provider
    binding: NativeBinding
    path: Path
    relative_path: str
    device: int
    inode: int
    observed_size: int
    ownership: tuple[SourceEvidence, ...]
    format_hint: str
    archived: Literal[False] = False
AttachmentReference:
    source_path: Path
    record: SourceEvidence
    kind: Literal["embedded", "external"]
    location: str | None = None
    captured: Literal[False] = False
DiscoveryGap:
    code: str
    relative_path: str | None = None
DiscoveryResult:
    sources: tuple[NativeSource, ...] = ()
    attachments: tuple[AttachmentReference, ...] = ()
    gaps: tuple[DiscoveryGap, ...] = ()

# transcripts/spool.py:68–119, frozen dataclasses
RunBinding:
    installation_id: str
    agent_id: str
    recovery_epoch: str
    environment_generation: int
    boot_id: str
    run_id: str
    run_generation: int
Source:
    source_id: str
    installation_id: str
    agent_id: str
    source_type: str
    native_session_id: str | None = None
    parent_source_id: str | None = None
Chunk:
    source_id: str
    source_epoch: str
    offset: int
    length: int
    sha256: str
    payload: bytes
PendingPage:
    chunks: tuple[Chunk, ...]
    next_offset: int
```

Adapter records are frozen Pydantic models with extra fields forbidden, but
not globally strict (`adapters/types.py:19–20`). UUID fields are parsed UUID
values; generations are strict positive integers with no declared upper bound.
SourceEvidence and NativeSource numeric/hash fields have no explicit
nonnegative, length or digest-format validators. `NativeBinding` rejects only
a prior run equal to the current run; it does not look up that prior run or
attest a process. Its docstring explicitly assigns native-process verification
to the caller (`:129–144`).

Capture UUIDs instead require **canonical strings** (`str(UUID(value)) == value`);
UUID objects and noncanonical spellings fail. Generations are strict Python
`int` values in `1..2**63-1`; offsets are `0..2**63-1`, rejecting booleans.
Source type is exactly `terminal`, `native` or `attachment`. Native session text
is optional, 1–512 characters when present, with no NUL; it need not be a UUID.
Terminal sources forbid it. Parent IDs are canonical UUID strings
(`transcripts/spool.py:20–21,48–103`). Chunk/PendingPage dataclasses themselves
have no post-init validation; the spool's producing/accepting methods enforce
their own constraints.

| Existing value | Capture seam and observed limitation |
| --- | --- |
| Agent/recovery/boot/run UUIDs and the two generations | Corresponding fields exist in capture RunBinding, with the representation/bounds above. There is no adapter-to-capture conversion callable. The existing cross-module fixture converts **runner** UUIDs to strings and adds a synthetic installation ID (`tests/integration/test_shared_local_db.py:29–38`); it does not ingest an adapter source. |
| Conversation UUID; provider; exact adapter version | Conversation/provider/version have no dedicated capture RunBinding/Source/epoch columns. Version is an input to discovery, not a NativeSource field. |
| Native session UUID and prior-run UUID | Source can hold native session text; the explicit `link_run` API takes an already allocated source/epoch/current-run tuple. No code resolves `resumed_from_run_id` to that tuple or checks the prior run's native association. |
| Path, relative path, observed `(device,inode)`, format hint, ownership proofs and gaps | Epoch metadata accepts optional `locator`/`file_identity` and required `capability_evidence` **strings**, not these structured values. No existing serializer or structured proof/gap owner writes them into capture. No native file is opened by the spool. |
| Observed size and proof offsets | Neither is a captured cursor or payload. New epochs start at byte zero; only committed `append` advances capture. Discovery supplies no bytes to `append`. |
| Installation/source/epoch IDs, parent source, native creation time | Not produced by discovery. Caller supplies them to capture. Discovery also returns no timestamp, retained fd, mtime/ctime, checkpoint/prefix hash or reader position. |

Metadata validation is `_metadata(locator, file_identity, capability_evidence,
native_created_at) -> dict` (`transcripts/spool.py:318–329`): text is nonempty,
at most 4096 characters, no NUL, without trimming or path/hash normalization;
whitespace-only text is not separately rejected. Native time is at most 64
characters, parsed using `datetime.fromisoformat(value.replace("Z", "+00:00"))`,
and must have zero UTC offset. It is saved unchanged; the implementation does
not enforce one canonical RFC 3339 spelling. These strings and all capture
bindings are supplied evidence, not filesystem/provider attestations. The
planned domain owner is M07 (`docs/CONTRACTS.md:298`); the missing parts here
are implemented producers/converters/consumers, not an invitation to invent
fields or ID-allocation policy.

Primary and resumed sessions: `discover(provider: Provider, version: str,
roots: ManagedRoots, binding: NativeBinding) -> DiscoveryResult`
(`adapters/discovery.py:129–224`) requires the exact Claude `2.1.287` or Codex
`0.160.0` profile (`adapters/capabilities.py:14–40`). It scans only
`HOME/.claude/projects` or `HOME/.codex/{sessions,archived_sessions}`. Claude
requires the canonical UUID filename and a non-child record's exact `sessionId`
string. Codex accepts `rollout-YYYY-MM-DDTHH-MM-SS-UUID[_UUID].jsonl`, parses the
first filename UUID, and requires exact `session_meta.payload.id` string
equality. The optional suffix does not become another binding. Record IDs are
compared to the canonical string, not independently normalized. Cwd and newest
mtime do not select ownership. Multiple paths for one session remain multiple
NativeSource results; no path/inode/session deduplication or durable source ID
allocation happens. Returned format hints are `claude-jsonl-p06-p11` and
`codex-jsonl-p06-p11` (`:23–25,131–156,180–195,215–219`).

Resume simply carries the supplied NativeBinding into each result. Claude
launch recipes require a reserved UUID for fresh launch; fresh Codex returns no
UUID and rejects a supplied non-resume identity. Both can build exact resume
argv with a supplied prior-run ID; neither executes/attests a resume
(`adapters/clients.py:13–52`; `tests/adapters/test_adapters.py:298–331,583–594`).
No accepted callable establishes fresh Codex's process-to-session binding.

Child-looking sources are separate: any `subagents` path component or basename
starting `agent-` is skipped with `unsupported_child_association`. In Claude,
`isSidechain is True` or a truthy `agentId` makes a record ineligible for primary
proof **and** attachment extraction, but does not discard an independently
owned primary file. A child-only file has unverified primary identity; a
conflicting non-child primary record still rejects it. Codex `session_meta`
with truthy `forked_from_id`, `parent_thread_id`, `parent_session_id`, or any
dictionary `source` stops the candidate with unsupported-child and identity
conflict gaps (`adapters/discovery.py:143–147,173–195,212–214`). Spool parent
links only check an existing parent's installation/agent; the synthetic child
registration is not provider association evidence (`transcripts/spool.py:342–359`).

Attachments: only Codex `event_msg / item_completed / UserMessage.content[]`
image blocks yield `image_url` references. Nonempty strings beginning `data:`
produce embedded references with `location=None`; others retain the exact
external location and `external_attachment_not_captured`. Every reference hashes
its entire JSONL record, not decoded image bytes. Missing/invalid URLs yield
`unsupported_attachment_reference`. Claude `attachment / queued_command`
remains inline with no separate reference; other non-child attachment layouts
yield that gap. References are returned only with an accepted primary source.
Both providers always report `child_association_contract_unverified` and
`attachment_coverage_partial` (`adapters/discovery.py:104–134,196–224`). There is
no extraction/download, attachment Source creation or missing-reference ledger.
`translate_hook(...)` checks the already supplied source/binding/session/path
and returns an unpersisted observation; it neither revalidates the file nor
creates capture sources (`adapters/hooks.py:15–42`).

**2. Discovery validates selected spans during one observation window.**

The scan retains **only the first matching primary record** as
`SourceEvidence(kind="native_session_id", offset, length, sha256)` and one
`attachment_reference` proof per emitted reference. Later primary records can
reject a candidate during the scan, but are not added to its ownership tuple.
Offsets and lengths count exact raw bytes including newline. No record bytes
are retained in the result (`adapters/discovery.py:54–55,190–204`).

`_records(stream)` reads at most `1 MiB + 1` per `readline`. An oversized record
is drained in bounded pieces and reported as `metadata_record_too_large`; a
short final line without newline is `incomplete_metadata_record`. Neither
becomes a parsed record/proof. Invalid JSON/Unicode/depth is an
`invalid_metadata_record` gap; non-dictionaries are ignored. Unknown record
types need not invalidate an otherwise owned file, but unknown layouts with
no usable primary proof cannot establish ownership. There is no 8 MiB file
ceiling; this is not a total scan-time/result-count bound. Gaps carry code/path,
not the skipped raw span's bytes or full coordinates
(`adapters/discovery.py:28–51,161–179,212–224`). A giant/partial sole identity
record therefore cannot prove ownership; a giant/partial later record leaves
an independently proven source eligible.

`_validate_source(fd, root, relative, initial, evidence, refs)` performs one
attempt, before closing the originally scanned fd (`:77–101,159–218`):

- It observes `(dev, ino, size, mtime_ns, ctime_ns)` with fstat; atime is excluded.
  Shrink from the pre-scan size, or equal size with any changed tuple element,
  fails. **Growth does not require unchanged pre-scan timestamps.**
- `_span_matches(fd, proof)` rereads every retained ownership/reference span
  through `os.pread`, at most 64 KiB per read. A short read or hash mismatch
  fails. It does not hash the other bytes or rescan newly appended records.
- A fresh `ManagedTree(root)` traverses the absolute root again without
  symlinks, opens the exact relative regular file, and compares its dev/inode
  to the original fd. That second descriptor supplies no proof bytes.
- A final fstat on the original fd must equal the tuple immediately before
  these proof/path checks. Any mismatch or caught OSError/AdapterError yields
  `source_changed_during_discovery`, withholding source and references. It does
  not retry or preserve the discarded bytes.

| Mutation / observation | Established boundary and limit |
| --- | --- |
| Same-inode rewrite | Changed retained proof bytes fail even if the file grows. Shrink and same-size timestamp changes fail separately. A growing rewrite confined to **unretained** spans can leave all checks in this function satisfied; size growth alone is not append-only evidence. |
| Append completed after scan, before final checks | Can pass when retained spans/path still match and final stat observations are stable. `observed_size` includes the growth, although appended records may never have been scanned. Appended ownership conflicts/references cannot be inferred absent from those bytes. |
| Append/rewrite/metadata change during final checks | Detected when proof reads or the bracketing stat tuple differ. This is a finite observation, not a filesystem snapshot or writer lock; timestamp/stat observations do not prove every intermediate byte version. |
| Rename/unlink/path or root substitution | Original fd remains the proof reader. A missing current path, symlink or different final inode fails. Root replacement tests install a new file inode. Code compares the final **file** identity, not old/new root-directory identity; reaching the same inode through a changed directory chain is not independently rejected solely for root identity. |
| Mutation after final observation or after return | No protection. Both file descriptors close before return. Later matching dev/inode is not a historical-content proof (and after descriptor closure does not rule out inode reuse). Old observed size/hash cannot certify a newly opened file's complete prefix or later records. |

Only OSError/AdapterError raised inside the per-base protected block, including
enumeration and the initial **source-file** open, are caught as
`managed_source_unavailable:<exception class>` (`adapters/discovery.py:138–163,220–221`).
The initial `ManagedTree(roots.home)` construction is outside that handler
(`:136–138`); its component-open failures propagate after descriptor cleanup
(`adapters/managed_io.py:28–37`). `ManagedRoots` supplies lexical validation
only (`adapters/types.py:33–50`), so a missing/replaced/symlink/non-directory
managed HOME at initial construction may raise without returning any
DiscoveryResult or gap. This differs from final revalidation's caught failures
inside `_validate_source` (`adapters/discovery.py:77–101`), which withhold the
source and become `source_changed_during_discovery` (`:207–211`). If execution
reaches the normal return with no accepted sources, it adds
`bound_native_source_not_found` (`:222–224`).

Existing filesystem primitives have different boundaries; none is a native
reader/checkpoint utility:

| Primitive | Held descriptor versus current path |
| --- | --- |
| `ManagedTree(root: Path)`, `.identity -> tuple[int,int]`, `.parent(relative: str, *, create=False)`, `.open_read(relative: str)` (`adapters/managed_io.py:23–83`) | Root is opened componentwise from `/` with directory/no-follow flags. Parent traversal starts from a duplicate of the **held root fd**; open_read opens a no-follow/nonblocking regular file and yields an `rb` stream until context exit. It can operate in a renamed/detached tree and does not by itself reconfirm the original absolute root path. A fresh ManagedTree, as discovery actually uses, re-traverses that path. |
| `.files(relative: str)` and `.read(relative: str, limit=GENERATED_FILE_LIMIT) -> bytes \| None` (`:85–93,126–151`) | Enumeration yields names, not held file handles; missing base returns, symlinks raise. `read` closes its fd and rejects more than its default 1 MiB, a generated-config convenience rather than an unlimited capture API. |
| `directory(path: Path)` (`storage/files.py:27–44`) | Opens/holds an absolute no-follow directory chain and asserts external I/O is outside host SQL. Existing fd continues to name that directory after rename; the helper alone does not recheck the absolute path later. |
| `file_hash(fd: int, name: str, *, sync=False) -> tuple[str,int] \| None` (`storage/files.py:47–63`) | `fd` is a **directory** fd; it opens `name` anew, hashes the whole regular file, compares size/mtime/ctime around the read, optionally fsyncs, then closes. It is not a range hash on an already held source fd or a comparison with a previously discovered inode. |
| `child_directory(fd, name, *, create=False)`, `same_child(fd, name, child)`, `read_regular(fd, name, limit)` (`agents/workspace.py:70–104`) | Workspace-owned name primitives: hold/open a child directory, compare that named directory with its held fd, or reopen/read a bounded regular file with stat checks. They do not validate an arbitrary native relative path or maintain reader state. Workspace publication separately reopens/compares the absolute root (`:359–364`). |

`ManagedRoots` rejects `/`, relative or `..` roots and identical lexical
home/workspace paths (`adapters/types.py:33–50`); these are caller-supplied
managed roots, not authenticated agent ownership. Dirfd/no-follow traversal
does not freeze file contents or prohibit another hard link to an inode.

**3. Existing spool behavior a restarting reader would encounter.**

The exact bound-method signatures are below. All source/epoch/run ID arguments
are `str`; the `RunBinding` and `Source` types are the capture dataclasses above.

```python
CaptureSpool(path: str | Path, *, busy_timeout_ms: int = 2000)
register_run(binding: RunBinding) -> None
register_source(source: Source, source_epoch: str, run_id: str, *,
    capability_evidence: str, locator: str | None = None,
    file_identity: str | None = None, native_created_at: str | None = None) -> None
link_run(source_id: str, source_epoch: str, run_id: str) -> None
begin_epoch(source_id: str, source_epoch: str, *, previous_epoch: str,
    reason: str, run_id: str, capability_evidence: str,
    locator: str | None = None, file_identity: str | None = None,
    native_created_at: str | None = None) -> None
state(source_id: str, source_epoch: str) -> dict
recording_binding(source_id: str, source_epoch: str, run_id: str) -> RunBinding
append(source_id: str, source_epoch: str, offset: int, payload: bytes) -> int
pending(source_id: str, source_epoch: str, *,
    after: int | None = None, limit: int = 4) -> PendingPage
acknowledge(source_id: str, source_epoch: str, offset: int) -> None
reclaim(source_id: str, source_epoch: str) -> int
block(source_id: str, source_epoch: str, reason: str) -> None
require_healthy(source_id: str, source_epoch: str) -> None
claim_recording(source_id: str, source_epoch: str, run_id: str) -> None
activate_recording(source_id: str, source_epoch: str) -> None
finish_recording(source_id: str, source_epoch: str, exit_code: int) -> None
```

`ensure_schema(connection: sqlite3.Connection, *, busy_timeout_ms: int = 2000)
-> None` requires no active transaction, sets WAL/FULL/foreign keys and
`busy_timeout` (strict `0..30000` ms), and creates six `capture_*` tables without
touching global user_version. Writes use BEGIN IMMEDIATE/COMMIT/rollback;
pending reads use BEGIN with a consistent WAL snapshot. No native-file read is
inside these transactions (`transcripts/spool.py:122–242`).

| Persisted table | Actual evidence and writer |
| --- | --- |
| `capture_runs` (`:123–129`) | Supplied full capture RunBinding plus local `observed_at`; `register_run` compares every binding field for exact retry (`:243–259`). No native process/session or conversation lookup. |
| `capture_sources` (`:130–136`) | Source fields, `current_epoch`, `observed_at`; initial `register_source` checks parent's installation/agent, creates source+epoch+run link atomically. Existing source/epoch metadata must match exactly (`:331–370`). No unique locator/native-session/inode constraint. |
| `capture_epochs` (`:137–153`) | Predecessor/reason, four metadata strings, observation time; local/host offsets; health/error; recorder run/start/finish, child exit code, finalized offset and drain flag. No dedicated reader checkpoint, prefix/tail hash, stat tuple or incomplete-record/gap ledger. `file_identity` is optional opaque caller text, not an observed/persistently maintained descriptor identity. |
| `capture_run_sources` (`:154–159`) | `(run_id, source_id, source_epoch)` and first `linked_at`; INSERT OR IGNORE checks only source/run installation and agent equality, not current recovery/boot/generations (`:300–315`). Retired lineage can link; there is no authentication in this local API. |
| `capture_chunks` (`:160–169`) | Primary key `(source_id, source_epoch, offset)`, length (1..256 KiB), SHA-256, nullable payload, captured time. `append` inserts bytes and advances local cursor in the same transaction (`:417–462`). Reclaim only NULLs payload (`:531–544`). |
| `capture_conflicts` (`:170–176`) | Separate UUID, coordinates/length/hash, non-null conflicting payload and observed time; unique source/epoch/offset/length/hash. Written after rollback of a conflicting append (`:463–475`). No callable enumerates conflicts or all retained chunk receipts. |

Registration freezes supplied source fields and epoch metadata; it cannot add
a new epoch to an existing source by simply repeating register_source. A new
`begin_epoch` requires an existing previous epoch which is still current,
distinct new UUID and nonempty reason; it creates zero offsets/starting health
and moves only `current_epoch`. An identical already existing epoch retry checks
metadata/predecessor/reason and adds the run link without rewinding a later
current epoch. The old epoch's bytes, health and metadata are unchanged. Late
old-epoch appends remain possible when that epoch otherwise admits bytes;
`current_epoch` is not append fencing (`:331–399,432–451`). No filesystem
divergence detection invokes this API automatically.

Append takes a materialized immutable `bytes` payload, not a stream iterator.
It splits from the supplied start into at most 256 KiB pieces, allowing a larger
call in one transaction; there is no whole-transcript ceiling apart from the
signed coordinate bound. Start beyond the cursor raises CoordinateConflict.
For each start below the cursor, **exact stored chunk start, length and hash**
must match. Same byte stream regrouped at different boundaries can conflict
(e.g. the separately stored `b"first"`, `b"second"` chunks in
`tests/transcripts/test_spool.py:140–159` are not a single 11-byte receipt).
Retry returns the existing final cursor, even when it replayed only an earlier
prefix. Empty payload makes no chunk or cursor/health advance (`:417–462`).

A mismatch rolls back the entire append and separately retains the **first
conflicting piece**, at most 256 KiB, not all input bytes. A forward gap does
not store conflict payload because no accepted-piece mismatch occurred.
Accepted receipts are never replaced. Successful conflict retention raises
CoordinateConflict without marking epoch health blocked; inability to persist
it, or an append SQLite failure, latches/tries to persist blocked health
(`:463–478`). Thus local conflict retention is not D31's planned host
conflict/ACK-admission policy.

`pending` bounds limit to 1..16 chunks (default 4), validates optional `after`
within the local cursor, and selects chunks whose end exceeds
`max(host_archived_offset, after or 0)`. It returns **whole chunks**, including
an overlapping partially ACKed chunk; `next_offset` is their final end (or the
selection cursor if empty). Payload presence/SHA are verified. Page reads do
not consume or ACK. Already wholly ACKed bytes cannot be requested through
pending, even before reclaim. Missing/corrupt returned payload raises
CaptureBlocked but that read does not call `block` (`:480–502`).

ACK commits a monotonic offset within locally captured bytes and checks receipt
coverage from the old watermark through the new one; unknown source/epoch,
regression, excessive end or missing range raises InvalidAcknowledgment.
An ACK can end inside a chunk. It checks coordinates, not payload hashes,
filesystem durability, source health or capture_conflicts. ACK is the caller's
assertion of host durability. Separate reclaim NULLs only payloads whose entire
chunk end is ACKed and returns the reclaimed byte count; receipt hashes,
coordinates, lineage and conflicts remain (`:1–6,504–544`).

Health is historical: only `starting`, `healthy`, `blocked`, `incomplete` exist,
not M07's full planned states. `state` returns an epoch row; there is no
`health()` callable. `block` first sets this connection object's `last_error`
(reason truncated to 512 characters), then attempts a durable update and
swallows SQLite failure. Reopen loses that **in-memory** latch, not a successfully
persisted block. `require_healthy` checks the latch and stored healthy value.
New appends reject blocked/incomplete/finished epochs; identical receipt
retries may still pass on a fresh non-latched object. Any newly admitted bytes
set health healthy (`:223–227,281–283,401–460`). None of this attests a live
native reader or prior-prefix stability.

The only finalization writer is `finish_recording`: it overwrites
`recorder_finished_at`, `child_exit_code`, `finalized_offset=local_captured_offset`,
`drain_complete=1`, and healthy/no error for exit 0 or incomplete/error otherwise.
It does not inspect a file, validate a prefix/partial record, or itself check
the source type/run/drain evidence (`:572–580`). Its production caller is the
terminal Recorder after child exit and PTY EOF/display drain
(`transcripts/recorder.py:367–453`). There is no native-file finalization
callable or supported clearing/reopening of those finish fields for a resumed
checkpoint; link_run does not clear them. `claim_recording` is terminal-only,
requires unused/zero/starting state and durably reserves before fork; a new
Recorder object cannot resume a used terminal epoch (`transcripts/spool.py:546–570`).
`recording_binding` is a read-only join requiring the reserved recorder run,
not a generic native-reader registration (`:285–298`). The D30 probe is a fresh
terminal-loop/child observation and supplies no native-file checkpoint
(`recorder.py:100–125`; `probe.py:179–246`).

Consequently M07's native identity/checkpoint prefix/tail tracking, full-prefix
finalization verification, discontinuity detection, held-descriptor drain,
resumable native-reader state, native checkpoint finalization, durable partial/gap
outcomes and child/attachment ingestion have no implemented native path here
(`docs/modules/07-transcripts.md:51–61,80–97`). Spool primitives cover supplied
byte transactions and lineage, not those missing observations. Host transfer,
native parsing/search and pressure-driven producer pause also remain outside
this accepted implementation; HOST-ARCHIVE's separate assignment is not credited.

**4. Surviving bytes and irreversible observation limits.**

| Situation | Recoverable evidence and unavailable data |
| --- | --- |
| Discovery succeeds, before ingestion | Only descriptor metadata/proof hashes/reference locations exist in the result. No raw bytes were copied or committed. A later read must obtain bytes again; a hash cannot reconstruct them. A partial/giant record gap does not preserve its content. |
| Provider rewrites/truncates after capture | Previously committed non-reclaimed chunk BLOBs retain exact original bytes independently of the provider. New candidate bytes at an existing chunk coordinate can be compared by append's retry rule and a mismatch separately retained. Overwritten bytes never observed/committed are unavailable unless another independent copy exists. No code checks divergence at the new append cursor, so blindly supplying a replacement suffix can append under old lineage without validating its earlier prefix. |
| Rename/unlink with an independently held file fd | Linux can still read that old inode while held; opening the managed path may instead find a replacement. Same-inode rewrite changes the held file too: holding a descriptor is not holding historical bytes. Discovery closes its fd, and no accepted native capture loop retains/drains one. After the last fd/path is lost, unarchived old-inode data is not recoverable from its inode number/hash. |
| Reader/caller restarts | Committed chunks, exact receipt boundaries, lineage, epochs and offsets survive SQLite reopen; uncommitted append transactions roll back. An uncertain committed append can match retained receipts if resubmitted on the original boundaries. No native seek/partial-buffer/prefix-hash/descriptor checkpoint survives because none is written. A cursor alone cannot prove that current file bytes preceding it are unchanged. |
| ACK committed; payload not yet reclaimed | BLOBs still exist, including partial final-record bytes; pending hides wholly ACKed chunks. This is not a public historical raw-read API. Tests inspect BLOBs through owning SQL; their `raw` helper skips NULLs and is not an archive reconstruction service (`tests/transcripts/support.py:32–37`). |
| Payload reclaimed | Accepted offset/length/SHA/captured time and conflict BLOBs remain, accepted payload is NULL. Replaying exact candidate bytes can still verify receipt hashes through append; it does **not** restore the NULL payload. Receipts cannot reproduce raw content. A true host copy could supply it, but ACK here is only caller assertion, with no host retrieval/inventory callable at this base. If provider and all independent copies are gone, those reclaimed original bytes are unavailable through accepted product storage. |

No public spool method verifies a whole previously captured file prefix or
enumerates all historical receipt boundaries. Pending verifies the chunks it
returns; append verifies caller-supplied candidates at exact original boundaries,
and may write conflict evidence, so it is not a read-only file verification
operation. Per-chunk hashes in SQL remain comparison evidence after reclaim;
they are not a saved cumulative prefix/tail hash, and do not prove anything about
unobserved versions of a mutating file (`transcripts/spool.py:160–176,417–544`).

Arbitrary bytes, including a partial UTF-8/JSON suffix, are preserved **if passed
to append**; no newline is needed. The PTY fixture proves a clean terminal exit
can retain such a suffix and have healthy/drain-complete state; those fields do
not imply syntactically complete native records
(`tests/transcripts/test_recorder.py:91–122`). Discovery's partial-line treatment
is metadata skipping, not capture. Child bytes embedded in a returned primary
file would travel with any eventual full raw ingestion, but currently discovery
only leaves that file eligible. Separate child/attachment files are not copied.
This follows D07/M07's explicit limit: no preservation claim for provider-internal,
unflushed or previously unobserved overwritten bytes
(`docs/DECISIONS.md:250–256`; `docs/modules/07-transcripts.md:16–27,84–97`).

**5. Small existing fixtures and owning evidence.**

The smallest checked-in native-format fixtures are synthetic JSONL under
`tests/adapters/fixtures/`: `claude-owned.jsonl` (1028 bytes/6 records),
`codex-owned.jsonl` (1007 bytes/7 records), `claude-child-unsupported.jsonl`
(197 bytes/1 record) and `codex-child-unsupported.jsonl` (179 bytes/1 record).
They include supplied exact UUIDs, changed cwd, repeated equal text, unknown
types, inline queued command, embedded/external image URLs and unsupported
child forms. No referenced external file is opened. `AdapterCase` constructs
private roots, synthetic run/boot IDs and headers (`tests/adapters/test_adapters.py:24–57`).

| Existing owning checks | Factual support and limit |
| --- | --- |
| `DiscoveryTests` — 12 tests (`tests/adapters/test_adapters.py:334–646`) | Five final-validation tests cover growing/shrinking/same-size proof rewrites, completed append, mutation during pread, file/root/symlink substitution, and each Codex reference changed while primary proof remains intact (`:356–516`). Pread assertions prove original-fd use and 64 KiB blocks. The ctime-only case supplies a deterministic changed fstat ctime after actual chmod (`:450–464`); it is not a filesystem-clock guarantee. |
| Primary/child/attachment cases (`:518–637`) | Twelve mixed Claude subcases preserve file eligibility/bytes and correct first-primary proof for both child markers, same/different session and before/between/after placement. Child-only/separate child paths are rejected; conflicting primaries remain rejected. Codex UUID suffix/multiple-path/resume bindings remain distinct observations. Attachment hashes/locations and captured=false are asserted, not attachment ingestion. |
| Large discovery record (`:639–646`) | Adds one **9 MiB** payload record plus `{"partial":` to an already owned Codex source. Asserts full observed size and large/partial gaps; it does not spool those bytes or establish an append-only provider contract. |
| `SpoolTests` — 9 tests (`tests/transcripts/test_spool.py:18–273`) | Minimal `registered` fixture supplies IDs, optional native text and capability text (`tests/transcripts/support.py:17–29`). Spool tests stream a **10 MiB** payload record plus incomplete UTF-8/JSON, compare exact bytes/pages/hashes and reopen/retry (`test_spool.py:51–77`); exercise synthetic resume/parent/epoch/old-lineage bytes (`:79–119`), conflicts before/after reclaim (`:121–138`), tiny `first`/`second` partial ACKs/gap rejection (`:140–163`), abort/FULL/latching and consistent pending snapshots (`:165–208`). No native source fd participates. |
| Local crash boundaries (`tests/transcripts/test_spool.py:210–273`) | Ten owned abrupt-exit cases surround insert/cursor/append COMMIT, ACK update/COMMIT and reclaim update/COMMIT. Reopen checks bytes/offsets/ACKs/retry and integrity. These are SQLite/process cases, not power-loss certification, native reader recovery or host fsync/ACK tests. |
| `RecorderTests` — 13 tests; `NestedTerminal` (`tests/transcripts/test_recorder.py:23–122,188–293`) | Real nested PTYs with synthetic Python child: **9 MiB** giant output, all byte values/Unicode, partial suffix, durable-before-display, 17-byte partial display writes, exit drain/failure, and killed recorder refusal to relaunch. D27 session-cleanup fixtures remain terminal evidence; none tails a provider file. |
| Probe and shared-local-DB owners | `tests/transcripts/test_probe.py:125–167` distinguishes stopped/exited loop from saved healthy state and preserves DB snapshot during queries; it covers terminal liveness only. `tests/integration/test_shared_local_db.py:24–136` checks both runner/capture initialization orders, reopen, exact receipts and ACK/reclaim with tiny supplied chunks. Its readiness and host ACK are explicitly simulated (`:1–4,54–57,72–74`), with no recorder/adapters joining. |

Static coverage limits: no discovery-to-spool reader, post-discovery mutation
ingestion, native restart/reopen/held-old-inode drain, full-prefix verification
after reclaim, or native finalization/resume test exists. Discovery's mutation
checks operate before its final return; they do not test a growing rewrite of
unretained nonproof records as a full-prefix guarantee. Its large-record case
is Codex with an already valid small identity record, not a giant/partial sole
ownership record or all provider formats. File/root replacement is tested with
a different inode, not bare-unlink drainage or every ancestor/hard-link case.
Successful child association, actual attachment capture and provider
append/compaction/finalization behavior have no verified native evidence.
These are coverage observations, not a proposed test matrix or new acceptance.

Source versus installed origin: transcript subprocesses use `child_env()`'s
`PYTHONPATH = Path(qorqut_core.__file__).resolve().parent.parent`, deriving from
the **loaded parent product**, not a hard-coded checkout. Adapter alias-test
children do the same (`tests/transcripts/support.py:40–53`;
`tests/adapters/test_adapters.py:122–153`). The probe child reports its package
file, checked equal to the parent's (`tests/transcripts/test_probe.py:45–52,75–95`).
These expressions follow the parent's origin; they do not independently prove
that a mistakenly source-loaded parent was installed.

Historical integration verification supplied that missing outer boundary:
source runs used `-P` with explicit checkout PYTHONPATH; installed runs used
`-I`, no PYTHONPATH, and a byte-identical **tests-only** staging directory.
Its inventory checks compared every package file/import path and 31 lock pins,
including parent/child roots. It records 28 adapter and 22 transcript tests
passing from both source and wheel
(`docs/evidence/core-integration-r1.md:37–43,100–157`). The newer accepted probe
report records 33 source transcript tests before its last correction, a focused
unsigned-ucred failure/correction, then 11 final installed probe passes and
repeated hash/origin/pin diagnostics (`docs/evidence/recorder-probe-r1.md:71–105,142–169`).
BUILD-EXECUTION.md:31 additionally records the independent review's 33
final-source tests. These are historical results, not reruns here. The original
adapter-only environment used Pydantic 2.13.5; combined verification deliberately
used the unchanged product pin 2.12.3 (`adapters-r1.md:61–64`;
`core-integration-r1.md:153–154`, both under `docs/evidence/`). Native version/help
observations do not prove native capture/resume/children/attachments
(`adapters/capabilities.py:17–35`; `docs/evidence/adapters-r1.md:12–27`).

**Checks, commands, provenance and limits.**

Initial `git status --short --branch` showed only `## codex/native-capture-p0`.
`git rev-parse HEAD`, `git branch --show-current`, `git log -1 --format='%H %s'`
and `git diff --name-status e9d87155588ccce89243eae6d83a8fb28aeed95e` confirmed the
base and empty diff. Read-only navigation used `rg --files`, targeted `rg -n`
for callsites/schema/checkpoints/tests, `cat`, and `nl -ba` with `sed -n` ranges
on the exact files cited above. Initial combined document output was truncated
by the display; focused numbered reads recovered the needed text. All commands
exited 0; this was output truncation, not a product/test failure.

The exact cross-package/callsite searches used to bound the absence findings
included:

```sh
rg --files src tests docs | sort
rg -n 'def |class ' src/qorqut_core/adapters/discovery.py src/qorqut_core/transcripts/spool.py src/qorqut_core/storage/files.py
rg -n 'CaptureSpool|discover\(|register_source\(|begin_epoch\(|link_run\(|finish_recording\(|file_identity|prefix_hash|tail_hash|checkpoint' src tests/adapters tests/transcripts tests/integration
rg -n 'Migration\(|registry|Operation\(|Files\(|Workspace|archive|capture' src/qorqut_core/storage/migrations.py src/qorqut_core/app.py
```

The following exact comparisons each exited 0 with empty diffs. They tie the
inspected base to accepted component source/tests/evidence, including the newer
probe changes; no unfinished branch was read:

```sh
git diff --exit-code 2db2be6d051e83b383e240aed41424a212153107 e9d87155588ccce89243eae6d83a8fb28aeed95e -- src/qorqut_core/adapters docs/evidence/adapters-r1.md
git diff --exit-code 9701c661104f7042d0c001796d517957697b5599 e9d87155588ccce89243eae6d83a8fb28aeed95e -- tests/adapters tests/transcripts/test_spool.py tests/integration/test_shared_local_db.py docs/evidence/core-integration-r1.md
git diff --exit-code 7a678ec62d8df0e062f8858ba480c762892cca72 e9d87155588ccce89243eae6d83a8fb28aeed95e -- src/qorqut_core/transcripts tests/transcripts docs/evidence/recorder-probe-r1.md docs/evidence/recording-r1.md
git diff --exit-code 1c983de4c84c9cabded4a870c0d3422c11f640dc e9d87155588ccce89243eae6d83a8fb28aeed95e -- src/qorqut_core/agents/workspace.py src/qorqut_core/storage src/qorqut_core/app.py pyproject.toml requirements.lock
```

This exact stdlib-only command exited 0, AST-parsed **40** package modules,
printed the types/method signatures above and counted 28 adapter tests (12
discovery), 9 spool, 13 recorder and 11 probe tests. It ran **zero tests**:

```sh
/usr/bin/python3.12 - <<'PY'
import ast, hashlib, platform, sqlite3, sys
from pathlib import Path
print('Environment:', sys.version.split()[0], 'SQLite', sqlite3.sqlite_version, platform.system(), platform.release(), platform.machine())
paths = sorted(Path('src/qorqut_core').rglob('*.py'))
trees = {p: ast.parse(p.read_bytes(), filename=str(p)) for p in paths}
print('AST parsed package modules:', len(trees))
selected = {'RunBinding', 'NativeBinding', 'NativeSource', 'SourceEvidence', 'AttachmentReference', 'Source', 'Chunk', 'PendingPage', 'CaptureSpool', 'ManagedTree'}
for p, tree in trees.items():
    if p.parts[2] not in {'adapters', 'transcripts'}:
        continue
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in selected:
            fields = [ast.unparse(n) for n in node.body if isinstance(n, ast.AnnAssign)]
            methods = [f'{n.lineno}:{n.name}({ast.unparse(n.args)})' + (' -> ' + ast.unparse(n.returns) if n.returns else '') for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            print(str(p), node.lineno, node.name, fields, methods)
for p in [Path('tests/adapters/test_adapters.py'), *sorted(Path('tests/transcripts').glob('test_*.py'))]:
    t = ast.parse(p.read_bytes(), filename=str(p))
    counts = {n.name: len([m for m in n.body if isinstance(m, ast.FunctionDef) and m.name.startswith('test_')]) for n in t.body if isinstance(n, ast.ClassDef)}
    print('Owning test inventory:', str(p), counts)
material = ['adapters/types.py','adapters/clients.py','adapters/capabilities.py','adapters/hooks.py','adapters/discovery.py','adapters/managed_io.py','transcripts/spool.py','transcripts/recorder.py','transcripts/probe.py','storage/files.py','storage/migrations.py','agents/workspace.py','app.py']
for relative in material:
    p = Path('src/qorqut_core') / relative
    print('SHA256', hashlib.sha256(p.read_bytes()).hexdigest(), p)
for p in [Path('pyproject.toml'), Path('requirements.lock'), Path('src/qorqut_core/storage/NOTICE'), *sorted(Path('tests/adapters/fixtures').glob('*.jsonl'))]:
    content = p.read_bytes()
    print('SHA256', hashlib.sha256(content).hexdigest(), p, 'bytes', len(content))
PY
```

SHA-256 fingerprints of materially inspected modules and unchanged metadata:

| Path | SHA-256 |
| --- | --- |
| `adapters/types.py` | `db7d0256a4165b5c4f2abfce30cf19abf10202a5d5dd52dc133d45bfed2f0ed6` |
| `adapters/clients.py` | `4dba9e9abbcb6e4d798f29248b6a82eb16f650c94e842c056386f3b3a48bf440` |
| `adapters/capabilities.py` | `2a21b636132772881c11bd4dc2986b6f77444e73f9309469f45801e15cee014d` |
| `adapters/hooks.py` | `de1a3d32ddf23a6bac738000b64200ec2390c9be45e7684a3a2e6a435da7caae` |
| `adapters/discovery.py` | `49a48b5a0ebb073ed7aa1113612efe71d6fa1f2da96aeea27482aaf10afb990a` |
| `adapters/managed_io.py` | `f8ff16ae332519cc94c6ebe418cc518a2f593462f630a2777536a2967d791e6a` |
| `transcripts/spool.py` | `caddd9c6a0c0d4e454efaa24eebdd1397bd68944c73a8e3f73ec40202f273919` |
| `transcripts/recorder.py` | `8e66649a15769a60d9a29ea43b24d0c9a0625b9c7cb545571417ea9345ed9d37` |
| `transcripts/probe.py` | `cb625ac751a305262e990c954e609ff3b60f0fcee04e1031ae145924405d4683` |
| `storage/files.py` | `411e5f8a724ee4bad21ae51774e63d56dd0fdaa521757636c0651b18f91059a2` |
| `storage/migrations.py` | `ec5ae65a56c52172b2e2f62f4441011f4908cb6ae4c71b75bffccaac8ec3843a` |
| `agents/workspace.py` | `e2f4d1fa9a458fc2be0b5760cfddde88f4620671adf357e8591f639e20249db1` |
| `app.py` | `43f45d40c0ffa08d3689cb4bee3544d39037e75b59874d4ee89b288e7233797e` |
| repository `pyproject.toml` | `753d092b92176a6c855b1f03b3b82ca30f7d8915bb6c77e2450624343fa44995` |
| repository `requirements.lock` | `f84936aefd43ea97e2c74caac5e51bf1a9176ddb4214e827414ec6223e31b639` |
| `storage/NOTICE` | `c9a80f7b863ca7ca6a1d640a5ad0cb72ca991eec41d862741038ef40a859ef0d` |

Fixture SHA-256, in `tests/adapters/fixtures/`:

| File | SHA-256 |
| --- | --- |
| `claude-owned.jsonl` | `fd6a98cfb0981143f611e0e6e5b84e5261f064bd8caf197936ad1a62900d2b3e` |
| `codex-owned.jsonl` | `9669819c03f0ff9560d70777f616dd4f9f57db113aa92d37a2eef02bf1d15f7d` |
| `claude-child-unsupported.jsonl` | `a1bfb62719ac273666b696f6d6040d1390c5bffea901d2869e3be9c415435ac5` |
| `codex-child-unsupported.jsonl` | `08c0a16a7db37a54152bf669c9a9f7f9e01851bc1d12dfb912c7c82c48c27580` |

No upstream source copy was needed. P06/P11 format provenance remains the
accepted adapter evidence's pinned Qorqut revision
`e71fd1e527368bfd41b9815126f4bf33452aeebb`
(`docs/PORTING.md:7–17,29–34`; `docs/evidence/adapters-r1.md:101–129`). No live
installation, native session/store/authentication, owner transcript or browser
profile was read. No new fixture processes, databases, endpoints or runtime
artifacts were created, so there are none to reap/remove. No dependency,
configuration, service/cgroup/VM/network or UI/browser changes occurred.

Questions reserved for Manager, without selecting answers:

- What supplied process/session evidence and durable source/epoch/run mapping
  connect discovery's observations, multiple paths and resumed sessions to the
  existing capture coordinates? No implemented mapping resolves these inputs.
- What native observation/checkpoint/finalization and gap contract applies across
  reopen, growth with rewrite, reclaimed prefixes and resumed checkpoints,
  given the exact retained evidence and missing callables above?
- What bounded scope and provider evidence cover child/attachment sources and
  native append/finalization behavior? Unsupported association/capability flags
  are evidence gaps, not proof that a provider never emits such data.

Static evidence sufficed; no Manager decision or reproduction was needed to
complete this exploration. No native capture, archive readiness or G0–G6 gate
is claimed. Only this report is owned/changed. `git diff --cached --check` and
the following scope checks passed (the same checks apply after this final
evidence edit):

```sh
git diff --cached --stat
git diff --exit-code e9d87155588ccce89243eae6d83a8fb28aeed95e -- . ':(exclude)docs/evidence/native-capture-p0.md'
git diff --cached --name-only
git status --short --branch
```

They showed one added report and an otherwise byte-identical base. Commit SHA,
post-commit clean state and ordinary dedicated-branch push are reported through
ACLA; no main merge, PR/release, force push, rebase or deployment.

Review correction, from report commit `580218bec5d53ee161997ec09516175e4824d96b`:
Manager confirmed that the original exception-boundary paragraph was too broad.
The corrected paragraph distinguishes initial managed-HOME construction,
per-base source-file operations and final revalidation. Static numbered reads
and AST parent inspection confirm those handler boundaries; no product code,
tests or reproduction were run or changed. The report-only diff, unchanged
source/tests/metadata and whitespace were checked against both that report
commit and the original source base. The correction's exact commit, push and
check results are reported through ACLA.
