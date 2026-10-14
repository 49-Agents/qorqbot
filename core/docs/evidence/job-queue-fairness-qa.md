# Direct QA: job retry starvation

2026-10-14. Manager tested the installed `6bf4ad1` release in a disposable
console instance with its real HTTP API, WebSocket terminals and 125 synthetic
history messages. No owner data, login, native session or browser profile was
used. Codex browser control was unavailable; these are integration checks,
not visual UI acceptance.

The run exposed a queue starvation defect. Old transcript publishers retried
by original creation order while waiting for newer `file_install` jobs. They
could repeatedly displace those dependencies and later agent provisioning.
The new agent remained uninitialized beyond the 60-second test bound, and
the oldest publishers had retried nearly 30 times without an installed file.

The fix orders eligible work by saved next-attempt time, or creation time for
new work. A focused test failed before the fix and passed afterward. It also
checks that an overdue retry precedes later arrivals, preserving fairness in
both directions. Eligibility, recovery holds, leases and delivery safeguards
are unchanged.

Validation:

- The initial installed-release selection passed 51 of 52 checks. The failure
  was an obsolete schema-six migration expectation missing the existing nullable
  `browser_request_id` column. That explicit expectation is corrected, retaining
  the assertions that original entries, operation receipts and file bytes survive.
- All 18 focused scheduler, lease, recovery and message-dispatch checks passed
  with the fix, including FIFO and uncertain-effect protection.
- All three migration/publication checks passed, including crash receipts and
  streamed large-day publication with coalesced snapshots.
- Restarting only the disposable core against its unchanged blocked queue let
  all 169 original publication jobs complete and the stuck agent become active.
  A real HTTP/WebSocket journey then passed ten checks: exact served assets,
  idempotent creation, private terminal paths, identity-preserving rename,
  offline storage, UTF-8 byte limits, peer inbox handling/replies, saved file
  references, dated publication and unavailable-browser behavior.

The peer exchange used QA API clients with fixture actor identities; it was
not a native model turn. The browser-service and native-record unit fixtures
are also synthetic. Full browser UX, authenticated provider replies and live
browser takeover are not established by these checks.

Backlog latency remains a limitation: recovery of the stuck creation took
about three minutes after restarting with the fix; publication of subsequent
messages exceeded the initial 45-second check while older work drained. Those
failed timing checks remain recorded. Eventual completion does not make those
latencies a passing responsiveness result.

Commands, receipts, before/after failures and timing evidence are retained in
`/home/alp/qorqbot-launch/qa/2026-10-14/direct-selftest/`. Deployment and browser
retesting are tracked separately.
