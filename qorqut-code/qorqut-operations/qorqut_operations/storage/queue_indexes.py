"""Optional, explicitly migrated indexes for durable application queue reads."""
from .assembly import StorageContribution

OWNER = 'operations.queue-indexes'
SCHEMA = (
    """CREATE INDEX apply_receipts_recovery ON apply_receipts(state,signoff_id,created_at,id)
WHERE signoff_id IS NULL AND state IN ('prepared','applying','failed')""",
    "CREATE INDEX resource_claims_application ON resource_claims(apply_id)",
    """CREATE INDEX signoffs_approved_queue ON signoffs(updated_sequence,id)
WHERE state='approved'""",
)
OBJECTS = frozenset({('index', 'apply_receipts_recovery'), ('index', 'resource_claims_application'),
                     ('index', 'signoffs_approved_queue')})

# The explicit state bound and both equality columns let SQLite prefer this
# queue index over the unique signoff_id index, even without ANALYZE statistics
# (many settled receipts have NULL signoff_id). Sorting only pending candidates
# is cheaper than loading every historical receipt. The remaining predicate
# preserves retryability and resource-claim requirements exactly.
RECOVER_PENDING = """SELECT id FROM apply_receipts
WHERE signoff_id IS NULL AND state IN ('prepared','applying','failed')
AND (state IN ('prepared','applying') OR (state='failed'
     AND json_extract(error_json, '$.retryable')=1
     AND EXISTS (SELECT 1 FROM resource_claims WHERE apply_id=apply_receipts.id)))
ORDER BY created_at,id LIMIT ?"""


def storage_contribution():
    return StorageContribution(OWNER, {}, schema=SCHEMA,
        required_tables=frozenset({'apply_receipts', 'resource_claims', 'signoffs'}))
