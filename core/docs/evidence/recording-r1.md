# M07 recording-r1 evidence

Date: 2026-10-07. Branch: `codex/recording-r1`. Initial status was clean at
`db25fc97a4127f8f46f3092ed96578bcdba685ce`. This evidence describes only the bounded
local spool/nested-PTY increment from BUILD-EXECUTION, not M07 completion or any
G0–G6 acceptance gate. No merge, deployment, release, or provider call occurred.

## Delivered boundary

- `src/qorqut_core/transcripts/spool.py`: explicit shared database path;
  `ensure_schema(connection)` creates only `capture_*` tables and leaves global
  `user_version` alone. Each owned connection uses WAL, FULL synchronization,
  foreign keys and a validated busy timeout (2 seconds by default).
- Durable installation/agent/recovery/environment/boot/run bindings, raw-source
  metadata and parent relationships, epoch divergence evidence, and run/source
  links. Native resume links the existing source without re-ingesting bytes.
  Metadata accepts explicit locator/file identity/capability/native-time evidence;
  this increment does not discover or tail native files.
- Exact byte chunks of at most 256 KiB, SHA-256/offset/length receipts and cursor
  advancement in the same transaction. Larger append calls split into chunks
  within one transaction. The streaming recorder reads bounded blocks. There is
  no line parser or arbitrary total-stream limit. Identical retry coordinates
  succeed even after payload reclamation; conflicting bounded bytes are retained
  separately and cannot replace accepted receipts. Retry chunk boundaries must
  match the original coordinates.
- Pending pages use a consistent read snapshot and never ACK. Explicit ACKs are
  monotonic, source/epoch bound, contiguous and within captured bytes. Reclamation
  is a separate transaction after the ACK commit; only wholly acknowledged
  payloads become NULL, while receipts and source lineage remain. Partial-chunk
  ACKs retain that entire local payload until its end is acknowledged.
- `python -m qorqut_core.transcripts.recorder` takes the configured spool, source,
  epoch and full run binding, followed by structured child argv after `--`.
  It reserves a terminal epoch durably before fork, creates a real controlling
  child PTY, commits output before display, handles partial writes, forwards
  resize/signals and raw input, and drains output through PTY EOF after child exit.
  Input is never inserted into a native semantic stream. Exit records retain the
  drained byte checkpoint; signal/nonzero exits are marked incomplete.
- A storage failure latches capture/input closed, stops forwarding, attempts to
  persist blocked health, and terminates/reaps its owned child process group.
  The in-process error/readiness remains observable if storage cannot write the
  health update. Accepted bytes are preserved. Used/reserved terminal epochs
  cannot relaunch implicitly, including after an abruptly killed recorder.

Concrete Python interfaces remain module-local for later Manager-owned
integration. This does not introduce a network contract, separate daemon, host
database migration, package manifest or root initializer.

## Passed checks

Task-local environment: `python3.12 -m venv .venv`; no third-party dependencies
were installed. Runtime code and checks use Python's standard library.

Observed versions: Python **3.12.3**, SQLite **3.45.1**, Linux kernel
**6.8.0-138-generic**, Git **2.43.0**. No tmux, provider CLI, browser or VM binary
is exercised by this increment.

`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/transcripts -v`
passed **17 tests** in **12.487 seconds** on the final code/test tree.

The one focused suite covers:

- A real outer PTY → recorder → child controlling PTY, exact Unicode and all
  byte values, a single record exceeding 9 MiB, an incomplete UTF-8/JSON suffix,
  raw multiline/bracketed-paste input, literal metacharacters in argv, and
  un-echoed input excluded from capture.
- Actual 24×80 → 41×132 resize, SIGINT/SIGTERM propagation, child SIGKILL and
  drained tails, child exit status, terminal settings restoration, recorder
  SIGKILL followed by spool reopen, and refusal to repeat an uncertain launch.
- An independent SQLite reader at every display-write seam verifies that all
  bytes about to be forwarded and their cursor are already committed. The seam
  deliberately accepts only 17 bytes per write to exercise partial forwarding.
- A real `SQLITE_FULL` error induced by the disposable database's
  `max_page_count`, bounded writer-lock contention, and an injected SQLite
  statement abort prove refusal does not advance the accepted byte cursor.
  Recorder failure injection proves uncommitted output is not forwarded and
  readiness closes while the committed prefix remains readable.
- Ten abrupt subprocess-exit cases: after chunk insert, after cursor update,
  before/after append COMMIT; after ACK update and before/after its COMMIT;
  after payload reclamation and before/after its COMMIT. Reopening checks exact
  bytes, cursors/ACKs, receipt-based retry, and `PRAGMA integrity_check`.
- A native raw stream over 10 MiB, idempotent retry, source/run relink, parent
  ownership, binding conflict, explicit new epoch and preserved old-epoch late
  bytes; conflicting coordinates before/after reclamation; wrong-source/epoch,
  excessive, regressing and gap-crossing ACKs; partial ACK retention; page cursors
  without ACK; and concurrent ACK/reclaim against a pending-read snapshot.
- Coexistence with a synthetic `runner_*` table and existing `user_version`,
  with required SQLite connection settings retained.

`git diff --cached --check` also passed before the commit. Tests use only disposable
SQLite files and deterministic Python children; no existing agent, native store,
tmux session, account, browser, network listener or live Qorqut data is a fixture.

## Source provenance

Read the pinned source and surrounding failure behavior before implementation:

| Revision | File | SHA-256 |
| --- | --- | --- |
| dashboard `fd9c1a924db70373eb8d06f3970a87c26bd84452` | `pty_broker.py` | `f7bc8b40524c4a76040735869dbcf4a1ca4f20e5c945fdc10e41427313d1b054` |
| core `e71fd1e527368bfd41b9815126f4bf33452aeebb` | `bazarlyk/bootstrap/transcript_snapshot.py` | `42fafd56d00d773fd11ba3de6793ae35e2c31b61c3e4ff1898a8810351cdf562` |
| core `e71fd1e527368bfd41b9815126f4bf33452aeebb` | `bazarlyk/global_chat/terminal.py` | `2ef2d6f1a9752b1756fa4cf08a3817c15dc42fd158c02841e1aea77388871237` |

The inspected snapshot caps/replaces one file and swallows capture failures;
the Chat reader selects visible records and skips giant non-display records;
the PTY broker is a lossy viewer ring. This increment is a fresh implementation
of the specified local spool and durable-before-display boundary. No source
text was copied or substantially adapted, no Qorqut runtime package is imported,
and no operational configuration/data was accessed. The referenced source files
contain no license header requiring propagation for this fresh implementation.

## Pending evidence and operational limits

- The ACK test caller only simulates durable host receipt. No host archive,
  fsync-before-ACK transfer, collector HTTP, immutable host objects or backup
  durability is implemented or proven. A caller must not ACK a mere page fetch.
- Native adapters, discovery/finalization of file sources, parsers, FTS/Chat,
  attachments, provider switching, runner/tmux integration, VM behavior, actual
  systemd restart survival and producer cgroup pause remain outside this scope.
- On capture/display failure this process closes admission and cleans up its
  direct owned PTY process group; it does not pause a cgroup while surviving for
  recovery, reserve disk headroom, or prove termination of detached descendants.
  Uncommitted/unflushed producer output is not claimed durable. OS disk
  exhaustion and physical fsync/storage failure were not rehearsed; SQLite FULL
  and transaction/process boundaries were.
- Durable health is a historical observation, not a live-process lease. A killed
  recorder cannot update SQLite. Runner integration must combine verified
  process identity/liveness with capture readiness; it must not admit input from
  a saved `healthy` row. A new Recorder object starts unready and refuses a used
  epoch. Blocked/incomplete epochs need explicit reconciliation by later
  integration; this increment does not automatically clear them or replay work.
- Runtime operation assumes storage honoring SQLite's fsync contract and a
  cooperative managed producer. An unrestricted actor can bypass the launcher
  or destroy data; no tamper-proof recording claim is made.

The owner-selected executor used its saved Codex/Astra identity. No internal
reviewers or delegation were launched; independent Manager review is pending.
