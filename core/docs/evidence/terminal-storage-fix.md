# D60 terminal capture storage fix

Implemented the Manager's [D60 decision](../TERMINAL-STORAGE-RECOVERY.md) on
`codex/terminal-storage-fix`, based on
`3de9787a8ebbd7cabacc5626824026fda7b5412e`. This is implementation evidence;
Manager acceptance, integration and operator recovery remain separate.

`CaptureSpool.pending` and `acknowledge` now seek the original chunk at or before
the starting cursor through the primary coordinate index, then read forward
with an indexed offset bound. Interior cursors still return the whole overlapping
chunk. Partial ACKs retain its payload; missing ranges still reject ACKs.
Exact replay, the WAL read snapshot and every original receipt coordinate/hash
remain intact.

The compatible local `capture_chunks_retained` partial index contains only rows
whose payload is not NULL. Both reclaim statements explicitly use it, with an
offset bound plus the existing whole-chunk end check. The existing Terminal
older-retained-payload guard selects this index without a code change. Existing
stores acquire it through `ensure_schema`; no host migration, table-format
change, timeout increase, retry or lifecycle change was added.

Spool and recorder diagnostics preserve the outer exception class and a bounded
SQLite error name or numeric code from its exception chain. Tests cover an
actual SQLite trigger failure, actual full/locked storage, safe CLI wrapping,
numeric fallback and rejection of arbitrary diagnostic text. Recording remains
fail-closed and durable before display; cleanup behavior is unchanged.

## Focused checks

Private build/evidence directory:
`~/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/terminal-storage-fix-r1/`.
Python 3.12.3, SQLite 3.45.1, tmux 3.4 and Bash 5.2.21 were used. All 32
distributions matched the unchanged `requirements.lock`.

```text
PYTHONPATH=src:tests/transcripts <private>/venv/bin/python -m unittest test_spool test_recorder test_prefix -v
PYTHONPATH=src:tests <private>/venv/bin/python -m unittest runtime.test_terminal_archive -v
PYTHONPATH=src:tests <private>/venv/bin/python -m unittest runtime.test_terminal_archive.TerminalArchiveTests.test_million_receipts_near_tail_transfer_uses_bounded_index_work -v
git diff --check
```

The 34 spool/recorder/prefix tests passed in 43.580 seconds. All 13 existing
archival tests passed, including exact interrupted replay, mismatched receipts,
older unconfirmed payloads, conflict/recovery holds, transfer budgets and the
disposable shell observation test. The first large-ledger run passed its work
bound but failed a test assertion which recognized `USING INDEX` and omitted the
valid `USING COVERING INDEX` wording. Only that assertion changed. Its targeted
rerun passed in 14.565 seconds; the initial failed log is retained. There was no
broad-suite run or separate/self-review actor.

The one large fixture contains **1,000,000 original three-byte receipts**, with
999,984 payloads already reclaimed and 16 retained at the tail. The prefix has
real durable host archive objects. The normal transfer processes the final 48
bytes through its own intent, returned receipt, ACK and reclamation. All million
original coordinates/hashes remain, every payload is reclaimed only after its
ACK, and reconstructed host bytes equal all 3,000,000 synthetic bytes.

SQLite progress callbacks counted **2,477 instructions** on the spool connection
and **482** on the local archival connection for this transfer. Every recorded
chunk query uses a coordinate index search. The retained-prefix guard and both
reclaim statements use `capture_chunks_retained` with `offset<?`; pending and ACK
use primary-index predecessor/range seeks. The test enforces an instruction
bound of 50,000 per connection, not a wall-clock threshold. Measured transfer
time was **0.213239 seconds**, including real archive durability work; the first
complete range may exceed the unchanged 200 ms scheduling budget. These are
synthetic measurements, not a latency guarantee. Full query plans and counts are
in `large-ledger.log` and `large-ledger.json`.

## Installed package exercise

```text
<private>/venv/bin/python -m pip wheel --no-deps --no-build-isolation --wheel-dir <private>/dist .
<private>/venv/bin/python -m pip install --no-deps --force-reinstall <private>/dist/qorqut_core-0.1.0-py3-none-any.whl
env -u PYTHONPATH <private>/venv/bin/python <private>/installed-shell-check.py
```

Wheel: `dist/qorqut_core-0.1.0-py3-none-any.whl` in the private directory above.
SHA-256:
`01be25dfdee780a9a4c29fe1bc214664fe0976a633e18520efc51a59edf7f059`.
All 93 packaged files matched source, wheel and installation; all 89 imported
core modules in the exercise came from that installation. The normal generated
`qorqut-core serve` entrypoint ran with a fresh disposable data root, HOME,
private tmux socket and isolated loopback port, without a source import override.

Two confirmed WebSocket input frames submitted a marker command and a final
marker followed by `exit`. Shell-echoed input and both output markers were
captured. The normal observer archived all **164 bytes**, preserving five
original chunks in two host ranges. Each output marker appears once. Original
hashes match the reconstructed host stream, whose SHA-256 is
`0423deaca4059c2afce1bcf54c6c3045fca4b17bf2120a4ae5776a4c936dce0b`.
Bash exited 0; `drain_complete=1`, finalized/local/ACK offsets are all 164,
health remains healthy, and no payload or transfer intent remains. No native
session owner was created. Both saved synthetic SQLite snapshots pass integrity
checks. Cleanup confirmed the owned Bash/recorder/tmux fingerprints, listener
and socket were gone. Logs, provenance, snapshots and result JSON are retained
in the private directory; the disposable live fixture was removed.

## Limits and recovery handoff

The historical SQLite exception remains unproven. This repairs the measured
full-history query cost and preserves useful categories for future failures;
it does not establish the cause of the owner's code-74 exit. Busy timeouts and
failure behavior remain unchanged. First index creation necessarily reads the
existing ledger once; Manager must handle that while the failed recorder is
stopped. No worker opened or migrated the owner's store.

Existing archival can drain already committed bytes from the saved original
source/epoch, including a stopped or unhealthy recording, under its unchanged
binding, recovery, conflict and exact-receipt checks. A historical host watermark
alone still grants no ACK or reclaim authority; unconfirmed older payloads still
block cleanup. The repair cannot reconstruct uncommitted/lost bytes, clear failed
capture health, make a used source epoch launchable, or prove old processes dead.
Preserving the failed store/runtime metadata, settling ownership and establishing
a fresh shell remain Manager-owned recovery work. No native store, owner
session, service or configuration was modified. No recovery script, model
launch/resume, merge or live deployment was performed by this worker. This is
bounded storage/recorded-shell evidence, not full-system acceptance.
