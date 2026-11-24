# Queue acceptance and dispatch

Integration POST and receipt GET use a short intent lock, independent of the
provider dispatch lock. Outlook browser latency must not make acceptance wait
for the entire send batch. Intent insertion, duplicate checks, and cancellation
tombstones remain serialized; receipt reads cannot observe the gap between the
intent and send-ledger commits.

Cancellation acquires the dispatch lock before the intent lock. No code may
acquire these in reverse order. Provider work never holds the intent lock. The
worker releases the dispatch lock between messages and reloads each ledger row
before acting, preserving cancellation and duplicate-send boundaries.

Cancellation can still wait for an in-progress provider operation. The intent lock
does not shorten provider timeouts, retry uncertain transmissions, or change
approvals, schedules, quotas, or reply-stop rules.

Regression checks: `tests/test_queue_concurrency.py` uses real temporary ledgers
and blocked fake provider work to verify responsive acceptance/receipts, duplicate
protection, cancellation races, and refreshed batch snapshots.
