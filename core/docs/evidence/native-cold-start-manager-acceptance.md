# Native cold-start facts: Manager acceptance

2026-10-09. Accepted the factual report at
`917974007f4b66aba7a7f63eeaf23431a2228ff8`, whose only tracked change is
[native-cold-start-facts-r1.md](native-cold-start-facts-r1.md). Its parent,
clean worktree and pushed remote SHA were verified. This accepts the exploration,
not first-Chat delivery or a new admission design.

Manager inspected the report, startup/input source and original evidence, then
independently checked both startup/30-second snapshot pairs: exact native
fingerprints and recorded-shell ancestry; no messages, attempts or native source;
Codex's held writer-lock UUID and matching log with no hook/owner; Claude's
SessionStart, owner and independently saved native session metadata; absent
primary transcripts; no inference or native tool calls; and recorded cleanup of
owned processes, ports and namespaces. The original pre-seed evidence hashes
still match. Private checks and results are manager-review330.py and
manager-review330-results.json under the existing qorqut-builds run directory.

Both CLIs select a native session before the first prompt. Claude's startup hook
precedes transcript materialization; Codex exposes a held writer lock without
the hook or owner. Neither is healthy primary capture under the existing
contract. Executed older core pins are distinguished from the report's source
pin, and preparation failures/observation limits remain in the report. No
provider refusal was bypassed. First-Chat behavior and broader session coverage
still require a separate Manager design and implementation.
