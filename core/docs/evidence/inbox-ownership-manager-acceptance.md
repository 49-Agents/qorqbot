# Local inbox ownership: Manager acceptance

Accepted on 2026-10-08: `1af5f852ccbc83cc898df11099beb4031ca0b2ca`, based on
`f4506c705f77f9c1b6b1575f1905b95c77b7c27f`, under D36/D37. This accepts the
local onbash ownership boundary. The integrated production and test bytes remain
identical to the reviewed candidate; newer Manager planning documents are retained.

The implementation persists one logical owner, distinguishes same-process
reconnect from another live process, retains unknown owners, releases verified
exits, and fences stale observations and release calls. Recovery retires prior
ownership with attributed events. Schema 6 preserves migrations 1–5;
`runtime_get` is the fifteenth public operation, with the same five job handlers.
Its connected observation is distinct from input readiness, which stays false.

Fresh independent Astra MAX review 168 found no in-scope defects. It checked all
20 changed paths, ran twelve runtime tests and three changed inventory/schema
checks, then built and installed a separate wheel and passed a real-child
race/reopen/exit flow. It verified all 179 tracked candidate files and all 61
package files against Git. The physical schema-5 upgrade check preserved message,
file and operation receipts, events and migration checksums; it used preserved
migrations and passive assembly, not a running historical server binary.

Manager inspected the production, migration, recovery/lifespan and owning-test
changes independently. Builder and reviewer worktrees were clean at the exact
candidate; the pushed builder branch matched. All 61 installed and wheel package
files matched Git bytes. Four focused checks passed from that verified installed
package with a tests-only import path:

- Actual owned children racing to connect, reconnect, reopen, verified exit and
  replacement, including two processes with the same supplied native UUID.
- Unknown observation retention, five-second read freshness and local admission.
- Delayed stale release/reconciliation across replacement and recovery.
- Fault rollback of claims/replacement and recovery together with their events.

The independent wheel SHA-256 is
`f88970fa432cc154ed345cf8e2c52afac995fd0500197c8c5d36671a208a8261`.
Private Manager commands, logs, import origins and byte manifests are retained in
`manager-inbox168/` within the recorded build run directory. Independent evidence
is retained at `/tmp/qorqut-inbox-review-r1-z_bqw5lg`. Reviewer and builder were
approved separately through ACLA; approval is not deployment.

The old shared agent-flow test still assumes onbash remains unprovisioned after
the server starts. Review reproduced that same failure at the exact base; only
its operation count changed here. The separate manual-creation correction owns
that test. This record does not claim the entire repository suite passed.

Native metadata is supplied fixture input; real children are disposable Python
processes. Startup-hook/process association, inbox consumption, Chat submission,
assistant capture, recorded-shell composition, Terminal UI and real VM/browser
integration remain pending. No native/provider turns, owner data or live Qorqut
sessions were used. No service was deployed and no full G0–G6 gate advanced.
