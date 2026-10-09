# D50 Terminal transfer ranges: Manager acceptance

2026-10-09. Accepted implementation
`f17ce7321ff749eec6c4f7efd761ff91167c8ef0` after inspecting its complete production
and test diff against [D50](../TERMINAL-TRANSFER-RANGES.md), confirming the exact
parent, three-file scope, clean worktree and pushed SHA. The changed production
file also matches the installed package used by the worker's measurement.

Manager independently ran three focused groups: 130 original chunks across the
128 boundary, interrupted transfer/receipt/ACK/reclaim/retirement with later
output, and genuine legacy table/intents at four interruption states. All passed
in 11.184 seconds. The worker's owning suite passed all 13 tests. Inspection
confirmed bounded pages and original-chunk counts, strict manifest/ledger checks,
actual receipt persistence before cleanup, legacy NULL semantics and unchanged
producer/recovery/conflict/lifetime guards. Archive and recorder code are unchanged.

The separate installed measurement reported automatic catch-up 0.487 seconds
after 400 short writes, with 397 original chunks in 11 host ranges. Manager
independently checked saved raw bytes against every original ledger hash and host
range hash, all 400 unique markers, all ten sampled saved receipts/manifests,
the input receipt, final offsets and recorded owned cleanup. These checks use
the preserved snapshot and reconstructed bytes; original object files were
removed with disposable fixture data after the worker's verification.
The 10,846-byte stream SHA-256 is
`2b5437d0740a1fc3b29da8f1152fdd14484424504e4e56adf460cf8e13bbadb2`.

Private commands/results: manager-review334-tests.log, manager-review334.py and
manager-review334-results.json under the existing qorqut-builds run directory.
[Worker evidence](terminal-transfer-ranges-r1.md) preserves its workload,
installation and measurement limits. This was a caught-up live recording, with
no final-drain claim. It was smaller in bytes than D49's original two-tab workload;
the full combined UI/recording check remains required at the new core pin.
The original D49 failure is unchanged. Initial native Chat and broader system
gates remain open; no deployment was performed.
