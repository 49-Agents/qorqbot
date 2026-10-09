# Bounded Terminal transfer ranges r1

Implements [D50](../TERMINAL-TRANSFER-RANGES.md) on
`codex/fix-terminal-transfer-ranges-r1`, based on
`941123dccdd3ad589d524db24a6215435a031124`. Manager review is pending.
Only `runtime/terminal_archive.py`, its owning tests and this report changed.

New intents store a version-1 manifest in nullable `chunks_json`; older local
tables gain that column in a short transaction. NULL retains exact legacy
single-chunk replay. Pending and original-receipt pages remain bounded to 16.
Transfers concatenate at most 128 originals / 256 KiB without splitting chunks;
the original capture ledger survives reclamation. Each observation still allows
128 originals / 1 MiB / 200 ms, finishing the first range and every started
receipt/ACK/cleanup sequence. One blocking I/O can exceed that scheduling budget;
the pending page can exceed 256 KiB in memory.

Saved intent boundaries cannot grow on retry. Every attempt checks the manifest
against the original ledger, including after reclamation; unreceipted attempts
also reconstruct and hash the exact saved payload. Actual validated receipts
are saved before ACK. Producer/invocation, conflict/recovery, older retained
payload and lifetime/effects guards remain. Archive, CaptureSpool, recorder,
observer cadence, native collector, host schema and public operations are unchanged.

Focused check with Python 3.12.3 and all 32 unchanged locked distributions:

```text
PYTHONPATH=src:tests <private>/venv/bin/python -m unittest runtime.test_terminal_archive -v
git diff --check
```

All **13 tests passed in 46.216 seconds**, including the disposable recorded-Bash
observation/failure check. Coverage includes binary identity; 130 small originals
in ranges of 128 and 2; full/uneven byte boundaries; deterministic scheduling;
intent, receive, receipt, ACK, reclaim and retirement interruptions; exact replay
despite new output; malformed/changed manifests, ledgers and payloads; and the
existing receipt, recovery/conflict, identity, retention and lock assertions.
Genuine old tables reopen before receipt, after receipt, after ACK and after
reclamation; their old host objects stay unchanged before new ranges append.

The installed measurement used Git export tree
`c6bb76ee6c9403edfcaee2c67553bc286936d523`; its exact `src` subtree is
`c65d0936eaba06f876ad0a806af373ea513814e7`. The final commit adds this evidence to
that tested code/test snapshot. Wheel SHA-256:
`9bc6c5a416fa8345f1d6dd29e57797cc5ecb7e775eb8be7bc32e6bc621377d70`.
All 85 package files match source, wheel and installation. The normal installed
`qorqut-core serve` process loaded 81 core modules solely from that installation;
startup and shutdown completed. Schema 9, 17 operations and five handlers remain.

One fresh user/network namespace, private HOME/data root and owned loopback port
ran recorded onbash with the normal background observer. One confirmed control
action and one confirmed WebSocket input frame submitted the known producer;
the viewer then closed. Its 400 distinct 25-byte ASCII writes used 10 ms sleeps:
mean spacing **10.140 ms**, range **10.043–12.148 ms**, total **4.057 seconds**.
No workload retry, manual observer tick, SQL mutation, package patch, synthetic
receipt, native CLI/provider, external route or browser/VM installation was used.

The unchanged 30-second catch-up gate passed **0.487 seconds after producer
completion**, including a 200 ms stable observation. Initial recording was 18
bytes; the caught-up live recording was **10,846 bytes**, with **397 original
chunks and 11 host ranges**. Original counts per host range were
`1, 33, 6, 5, 118, 6, 4, 120, 6, 2, 96`. All original triples remain in the
local ledger and match slices of the reconstructed host bytes. Every expected
output line occurs once. Complete stream SHA-256:
`2b5437d0740a1fc3b29da8f1152fdd14484424504e4e56adf460cf8e13bbadb2`.

Read-only polling captured all ten workload manifests both before and after
receipt persistence; six saved receipts were observed before local ACK advanced.
Their bindings, range ends, constituent hashes and aggregate payloads match.
Final local ACK and host offset both equal 10,846; no intent or retained payload
remains. Both saved databases pass integrity checks. One original terminal
producer/source/epoch and three registration events remain; no native owner or
Chat entry was created.

Cleanup verified the four saved core/Bash/recorder/tmux fingerprints, normal
console shutdown, explicit owned-shell shutdown and descendant reaping. No known
process, loopback listener or matching tmux socket remained; disposable data was
removed after snapshots. Private commands, source export, wheel, logs, manifests,
receipts, raw bytes, database snapshots and cleanup/provenance checks remain under
`~/.local/state/qorqut-builds/01a116b0-8d6d-72f2-9da9-76bd2e898f04/fix-terminal-transfer-ranges-r1/`.
Only completed D41/D49 archive references were used.

This is one caught-up **live** Bash recording, not final-drain or combined UI
proof. The original D49 failed measurement remains separate. Initial/native
cold-start Chat, full native/child/attachment capture, final drain, browser/VM,
backup and full-system gates remain open. No PR, merge or deployment occurred;
Manager retains integration ownership.
