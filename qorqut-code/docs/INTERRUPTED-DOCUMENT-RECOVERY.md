# Interrupted document application recovery

## Cause and fix

A file application can replace its file successfully and then fail on a transient
storage error before its record transaction finishes. The receipt becomes failed,
while its resource claims intentionally remain: releasing them would expose file
bytes that disagree with the document record.

Startup recovery previously selected only prepared/applying receipts. It now also
selects failed, non-signoff receipts whose error is retryable and which still own
resource claims. Each attempt uses the existing exact application executor; it
revalidates current authority, targets and file hashes, finalizes records and
releases claims only on success. Terminal failures and released receipts are not
retried. Recovery remains bounded and does not create a background polling loop.

## Manual repair of one stuck receipt

Startup recovery does not replay a receipt whose bound profile or policy revision changed:
automatic replay must not override an authority change. For such a receipt, an operator can
repair it only after rechecking the receipt, claims, document record and file facts:

1. Back up the exact receipt, claims, document record, current file and both saved
   byte versions privately. Retain both versions even after recovery succeeds.
2. Use installation maintenance/writer admission and the receipt's application
   and file locks. Recheck the recorded revision, current after hash and absence
   of a newer working-memory import. Stop on any difference.
3. Restore the proven before bytes with an expected-current-hash check. Use the
   existing `release_unchanged` bookkeeping to release the old receipt's claims
   and restore document availability. Do not mark its old mutation applied, edit
   its authority binding or discard the receipt.
4. After leaving maintenance, submit the saved intended update afresh through the
   canonical application, using the original agent's current authority, current
   document revision and a new idempotency key. If current policy rejects it, leave
   the committed document readable and retain the intended update for owner review;
   never weaken policy to force it through.
5. Verify reads and working-memory recovery. Import through the normal working-memory
   operation only with current evidence/revision bindings. Do not wake the agent or
   alter paused tasks/inbox state as a side effect.
