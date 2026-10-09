# Bounded request admission: Manager acceptance

2026-10-09. Accepted `9279768622e5b6970b3fb566850d116efb9d83a3` after
complete source/test diff review and exact clean/pushed three-file verification.
Application and native-hook calls share four executing slots and twelve timed
async waiters. No await separates the capacity check from executor submission.
Overflow/timeout/closing refuse before execution; application refusal is retryable.
After submission, cancellation retains the slot until the original future ends.
Shutdown wakes waiters and drains executing calls without replay.

Manager independently ran four owning cases: mixed burst, waiting
overflow/timeout/cancellation, running cancellation/shutdown, and possibly-executed
error/lifespan handling. All passed in 1.981 seconds; the private log is
`manager-review326-focused.log`. Existing slow-task diagnostics are retained.
The worker's five focused checks also passed after its documented fixture repair.

This accepts the Dispatcher component. The real installed core/UI burst and
archive-catch-up check remains pending. It establishes no workload capacity beyond
these bounds, complete capture, native startup behavior or deployment.
