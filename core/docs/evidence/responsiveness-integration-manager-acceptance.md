# D49 combined evidence: Manager review

2026-10-09. Accepted the factual report at
`57e254745a6f8e287912265a496d880dcd05fca0`, its sole tracked change, after checking
its exact parent, clean worktree and pushed remote SHA. **The archive performance
gate failed and remains unaccepted.** [Original report](responsiveness-integration-r1.md).

At core `6c1ad0e11f8e29ba1e23d50a1314bc6fd2c06fdd` and UI
`6e3cb819764dd706accb12528d9f40fd4ff70da6`, all 24 bounded burst reads passed and
Terminal stayed connected. At 30 seconds, only 6,460 of 29,014 captured bytes were
archived. Later natural catch-up at about 149.85 seconds does not change this
failure. [D50](../TERMINAL-TRANSFER-RANGES.md) is the separate correction.

Manager independently reconstructed all 447 final immutable host objects,
compared their coordinates/hashes with the original recorder ledger and saved
local bytes, and found all 400 ordinary and 20 outage markers exactly once.
The independent outage/restart preserved Bash/recorder/tmux and terminal/source
identities, saved new local output while core was down, and caught up within
8.635 seconds after restart. Exact command frames and matching control/input
receipts show no replay and a new explicit takeover. The 148 saved receipt
samples matched their original intents; this is not a complete per-ACK trace.

Manager viewed the desktop Terminal and 390px inspector screenshots, checked
the normal Bash exit/final offsets and original owned-cleanup records. This
fixture finalized and archived 30,614 bytes, SHA-256
`a97d74bd3e1f2de38b5c7c84250320d6885e92f3d67181cced51a41a7675dd3e`.
Private checks/results are manager-review333.py and manager-review333-results.json
under the existing qorqut-builds run directory. The original failed request and
its separate helper timeout remain recorded. No product test was replayed to
erase a failure, and no live owner data or provider was used.

This accepts bounded request/connection, independent persistence and disposable
shell evidence. It does not accept archive throughput, general final drain,
initial native Chat, browser/VM integration or the complete system.
