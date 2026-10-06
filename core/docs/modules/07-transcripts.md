# M07 — Durable transcript capture, archive and searchable history

Status: Planned. Owns R10 and history portions of R11/R13/R14/R15. Depends on M00/M01/M03/M04; supplies readiness/recovery watermarks and integrates M05/M06/M09 records into the conversation read projection.

Repository: `qorqut-core`. Recorder, archive and conversation projection remain backend responsibilities and continue operating with no UI connected or installed.

The bounded local recorder probe is accepted as recorded in
[BUILD-EXECUTION.md](../BUILD-EXECUTION.md). Its design is
[RECORDER-PROBE.md](../RECORDER-PROBE.md): a fresh capture-loop observation,
separate from native ingestion and complete runtime readiness. The next host
storage increment follows [HOST-ARCHIVE.md](../HOST-ARCHIVE.md). The accepted
[native exploration](../evidence/native-capture-p0.md) supplies source and spool
facts; native file capture still requires its own concrete Manager design.

## 1. Guarantee and fault model

This is a first-class subsystem, not a Stop hook that copies a small file. Preserve exact emitted native streams and terminal output independently of the currently selected CLI/UI session, with explicit capture health.

Four separate guarantees must be observable:

1. **Local capture:** bytes committed to the recorder's persistent spool survive recorder/runner/guest restart until the host has acknowledged them.
2. **Host archive:** an acknowledgment means exact bytes and their inventory are durable on the host; a retry cannot overwrite an accepted range.
3. **Conversation/search:** rebuildable projections of archived records, with explicit unsupported/invalid/unindexed outcomes.
4. **Restoration:** an exact bounded context packet was supplied to a new runtime; this is M08, not proof that the archive fits in a model context window.

Cover crashes, ordinary network outages, process restarts and failure at every commit/rename/ACK boundary on storage honoring fsync. Independent backups address loss of the underlying storage. No collector can preserve provider-internal data never emitted, a killed producer's unflushed buffer or previously unobserved bytes overwritten by the producer. Supported native adapters need a verified append/finalization contract; violations produce an explicit incomplete/unknown condition.

The platform records managed cooperative sessions. An unrestricted actor can deliberately bypass a launcher or destroy its own files; this is not tamper-proof recording against a hostile root process.

## 2. Recorder and all-source registry

Run a small recorder inside the managed tmux session. It owns the native CLI's child PTY, commits output to the local spool before forwarding it to tmux, and preserves terminal resize/signals/interactive behavior. It remains alive when the runner service restarts; the runner serves its persistent spool to the host collector independently. The P0 nested-PTY rehearsal must prove this works before the architecture is considered settled.

The initial local recorder's failure cleanup is bounded independently of the
later cgroup guarantee. Preserve the unreaped direct-child identity while
observing exit, terminate live members of the verified original PTY session as
well as the direct child, and do not infer session completion from leader exit.
The child's PID/start and matching SID anchor ownership after terminal hangup;
failure cleanup includes background groups in that SID and revalidates members
through pidfds. Normal interactive forwarding retains foreground semantics. Use
finite cleanup deadlines; unresolved termination remains failed/incomplete with
admission closed. Restore parent terminal, descriptor flags and signal state on
every returning path. Reaping the direct child is not reaping an arbitrary group.
Signals arriving before child PTY/session creation must survive startup handler
reset and reach the owned child; block them across fork/setup and restore masks
in parent and child as specified by [D27](../DECISIONS.md). No shared/inherited
parent session or process group is a cleanup target; detached new sessions still
require the later cgroup supervision boundary.

`tmux pipe-pane` can provide diagnostic fallback but is not the primary durability boundary: it is another output consumer rather than the path before displayed output. Do not record every raw manual keystroke as a semantic user transcript. Native emitted user messages and durable submitted message bodies carry that meaning; terminal output remains a separately labeled recording.

Register a run before launch. Register each exact native source durably when hooks or adapter-owned discovery identify it. The registry includes parent/child sessions, native session ID, source type, inode/file identity where available, run links, native and observation times, source epoch and capability evidence. A source remains tracked after another run becomes current. Native resume may link an existing source to another run.

Discovery is bounded to this agent's dedicated native HOME and recorded run roots. Do not scan unrelated personal stores, select “newest transcript,” or infer ownership from cwd. Working-directory changes cannot invalidate the source. Known attachment references are captured through the owning adapter; missing dependencies remain explicit gaps.

## 3. Local transactional spool

Use one small local SQLite database shared by recorder/runner for source state, pending chunk BLOBs, event sequences, acknowledgment watermarks and local command receipts. The host remains the authority for agent operations; this database is recoverable local execution evidence.

For each source, commit chunk bytes/hash/offset plus advanced capture cursor in one `synchronous=FULL` transaction. A crash must not persist a cursor ahead of its bytes. Read bounded chunks, initially 256 KiB–1 MiB; chunk boundaries do not need to match JSONL lines. Benchmarks choose a small commit batching interval, but visible output cannot be forwarded ahead of its required durable boundary.

Backpressure has reserved headroom for receipts and graceful pausing. Stop admitting new input/launches before exhaustion, and pause the managed producer subtree when capture cannot proceed, keeping the recorder and runner runnable for draining/recovery. Never silently overwrite old chunks. Runner/process failures make capture unhealthy and block autonomous input until reconciled.

## 4. Host transfer and acknowledgment

Use ordered transfer per source/epoch. No general out-of-order stream assembly service is needed.

1. Host pulls bounded pending chunks from the runner with exact `(source_id, source_epoch, offset, length, sha256)` and bytes. A GET/page cursor is not acknowledgment.
2. Host validates bound agent/source lineage, expected contiguous coordinate and content hash.
3. Write a temporary object in the final archive filesystem, flush/fsync, install immutable final object and fsync its directory.
4. Commit the chunk receipt, source manifest update and contiguous host watermark in SQLite.
5. Host POSTs the durable watermark ACK to the runner only after both filesystem and DB steps succeed.
6. Runner commits the ACK locally before reclaiming acknowledged spool payloads.

Identical fetched chunks reuse the existing receipt. Conflicting bytes for an accepted coordinate never replace history; retain conflict evidence separately and expose a typed error. A host file installed before the DB commit is an orphan to verify/reconcile, not evidence that the data was lost. DB commit followed by lost ACK is safely retried. Reconnect resumes from committed per-source host watermarks, never a fetched-but-uncommitted page cursor. ACKs are monotonic and must name a valid source/epoch within locally captured ranges.

Accept late bytes from an authenticated retired generation into its original lineage. Current-generation fencing must not discard old history. Snapshot rollback or native-source divergence starts a distinct epoch/incarnation and cannot reuse accepted coordinates with different bytes.

The acknowledgment promises one durable host copy. Backup coverage/replication is a separate reported watermark. Optional longer local retention can be added without changing that meaning.

## 5. Source changes and finalization

| Source behavior | Required treatment |
| --- | --- |
| Partial JSONL line | Archive immediately; parse only after completion; retain incomplete suffix on exit |
| Transcript beyond 8 MiB | Stream indefinitely; no whole-file rejection |
| Single giant tool record | Preserve all bytes; bounded indexing/rendering can lag or reference raw ranges |
| Rename/unlink/replacement | Drain held original descriptor where possible; bind replacement as a new epoch/source |
| Truncation/rewrite | Preserve prior bytes; record discontinuity/new epoch; never reset cursor silently |
| Cwd change | Continue capture using registered run/source identity |
| Native resume | Link new run to source; finalization means complete through a checkpoint offset, not forever closed |
| Child/subagent session | Preserve and link its own sources under the owning logical agent |
| Unknown native format | Archive unchanged; report parser/capability gap |
| Missing attachment | Retain reference and explicit missing evidence; no self-contained export claim |

Track file identity and checkpoint prefix/tail hashes during capture. Verify the full captured prefix at finalization for supported file sources. This detects rewrites but cannot recover overwritten unseen bytes; report that limit honestly. Prefer native append-preserving event streams if a client provides a reliable one without changing the execution model.

Finalization records source watermark, descriptor/drain evidence and any incomplete records or gaps. A forced stop can finalize as interrupted/incomplete. It must not falsely become a clean complete transcript.

## 6. Parsing, normalization and search

Preserve raw bytes exactly. A parser emits versioned records with raw source/range references and normalized events for user/assistant text, tool calls/results, emitted reasoning, system/context material, compaction, usage, attachments, child links and unknown records. Hidden provider reasoning is not invented.

Native IDs can reconcile known echo formats only when the adapter proves their relationship. Equal text, nearby timestamps and adjacency are not deduplication keys. Terminal streams remain separate from native semantic messages; do not guess a merged conversation by matching text.

Index bounded meaningful text fragments in FTS5. Large/binary material remains accessible through raw/artifact references. Parser failures and unindexed records are counted and queryable. A default conversation view may hide technical records, but that is a display filter over retained history.

A parser upgrade rebuilds its projection from immutable sources. Save parser/version/checkpoint and switch the active projection only after a verified rebuild. Ordinary reads cannot mutate capture state. Search/list APIs have stable cursors and expose archive/index lag.

Expose a parsing checkpoint barrier to M08: every raw range through a selected finalized watermark must have a complete parse outcome or explicit gap. Context restoration waits for that barrier by default; it does not mistake successful raw drain for an up-to-date conversation projection. FTS indexing can still lag independently.

### Conversation presentation

`conversation_list` combines normalized native entries with owner messages, browser requests and file references, without taking ownership of their business rules. Keep a durable entry ledger of stable IDs/source references/insertion cursors; platform operations append in their own transaction, native normalization appends once per verified source record. Retain ledger coordinates when rebuilding derived content; exports/imports preserve original provenance and remap destination cursors. Late capture appends with original occurrence time rather than invalidating issued cursors.

Browser/file cards render from domain IDs. A native transcript tool record can link to a known operation but never create/re-execute a request or reference. Exact native metadata may link owner-message echoes; equal text cannot deduplicate them. Default chat omits technical raw records by display policy while the archive retains them. UI descriptions, iframe URLs and live VNC bytes do not belong in native transcript normalization. See [UI-PLAN.md](../UI-PLAN.md).

## 7. Implementation sequence

1. **M07.01 P0 recorder:** verify nested PTY behavior, process lifetime and durable-before-display ordering under deterministic producers/native fixtures.
2. **M07.02 Source registry/spool:** implement source/run lineage, native discovery hooks, atomic local cursor+bytes and capture health.
3. **M07.03 Archive protocol:** implement ordered transfer, immutable file receipts, fsync-before-ACK, duplicate/conflict handling and orphan reconciliation.
4. **M07.04 Source edge cases:** add partial/giant records, inode changes, truncation/rewrite, cwd changes, native resume, children and attachments.
5. **M07.05 Projections:** adapt Claude/Codex format knowledge, normalized record schema, stable provenance and FTS reindexing.
6. **M07.06 Control integration:** expose readiness/backlog/blocked states; connect storage pressure to M03 input and process pause; provide M08 drain checkpoints and M11 backup inventories.
7. **M07.07 Fault campaign:** execute every storage/ACK boundary and real VM network/reboot scenario before G3 approval.
8. **M07.08 Conversation projection:** integrate platform cards/owner messages for Phase 4, retain stable insertion coordinates and prove reconnect/late parsing/provider switches do not duplicate cards or effects. G3 archive completion does not wait on the chat UI.

## 8. Source decision and done gate

P11 supplies native parsing knowledge and relevant fixtures. Rewrite Qorqut's snapshot/current-marker reader and storage path. Do not retain its 8 MiB cap, original-cwd restriction, omitted children, or visible-text-only archive semantics.

Required acceptance includes all scenarios in the archive matrix in [ACCEPTANCE.md](../ACCEPTANCE.md): commit-boundary crashes, network partition, disk full, >8 MiB streams, giant/partial records, source replacement, native children, unknown schemas, attachment gaps, VM rollback and parser rebuild.

**Done:** G3 provides evidence that no acknowledged byte disappears under the fault model, every source has honest completeness/health, and archived history remains accessible after any current runtime/UI marker changes.
