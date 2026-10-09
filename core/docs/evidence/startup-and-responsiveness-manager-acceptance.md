# Startup connection and combined Terminal acceptance

2026-10-09. Manager accepted startup candidate
`16cb4a024a31e14839ded086dfc622fdeada308d` and combined verification report
`c9bae71db75eaffc82b479746c284a635f9501d5`. Both clean branches matched their
remote SHAs. ACLA approvals 341/342 close those assignments.

The startup helper and setup integration were reviewed directly. Nine owning
startup/setup tests passed independently in 24.691 seconds. Original snapshots
before input and across core restart retain the same native PID/start time,
held UUID writer lock, owner/generation and single startup event. They contain
no native hook, primary source, message or input-readiness grant. Normal CLI
exit disconnects the owner. This accepts D51's pinned local Codex observation,
not first-Chat input or daemon/resume coverage.

The [combined report](responsiveness-integration-r2.md) executes core
`ef07b0b899f0b20e3058385921457aaa34d810cd` with UI
`6e3cb819764dd706accb12528d9f40fd4ff70da6`. Manager inspected the audit logic and
reran reconstruction from the preserved original immutable objects: 323
original chunks, 11 host ranges and 30,614 exact bytes, SHA-256
`e3b9add4ad036779ba3ca921d228f38cf58f0bac14d3344d46cef2a864360776`.
Ten saved receipts were checked; the first range's receipt was not sampled.

Independent checks confirmed all 24 burst reads succeeded, ordinary catch-up
at 2.895 seconds, 1,418 bytes recorded during the outage, automatic restart
catch-up at 1.934 seconds, unchanged terminal/process identities and eleven
submitted input receipts totaling 309 bytes. Desktop Terminal and 390px
inspector screenshots were inspected. Both fixtures' cleanup records show no
remaining owned processes/namespaces.

The original r1 timing failure remains recorded. One archive warning and one
redacted shutdown diagnostic in r2 remain unexplained; this is bounded workload
acceptance, not an error-free or complete-system claim. General native final
drain, child/attachment coverage, first Chat, browser and VM gates remain open.

Private Manager evidence is under the run state directory in
`manager-review339-tests.log`, `manager-review339-evidence.json` and
`manager-review340-evidence.json`; original worker bundles remain separate.
