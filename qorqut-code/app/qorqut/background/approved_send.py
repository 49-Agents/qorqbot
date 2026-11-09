"""One proof that an outbound job contains the exact effect the installation owner approved."""
from qorqut_operations.contracts import ChangeSet, DomainError, canonical_hash, plain
from qorqut_operations.storage.repositories import decode_row


def approved_send_plan(job, args, operation, uow):
    denied = DomainError('forbidden', 'This send job is not backed by an exact owner-approved Signoff')
    rows = uow.connection.execute(
        "SELECT * FROM signoffs WHERE operation_name=? AND state='applied' "
        "AND json_extract(apply_result_json,'$.data.job_id')=?", (operation, job['id'])).fetchall()
    if len(rows) != 1:
        raise denied
    row = decode_row(rows[0])
    owner = (uow.repository('installation').get('service') or {}).get('owner_principal_id')
    decision = row['decision_json'] or {}
    if (row['category'] != 'internal_change' or row['review_scope_kind'] != 'global'
            or row['proposer_principal_id'] != job['initiated_by_principal_id'] or row['superseded_by_id']
            or decision.get('kind') != 'approve' or 'authority' in decision or not owner or decision.get('by') != owner):
        raise denied
    plan = ChangeSet.from_bytes(row['change_set'], expected_hash=row['payload_hash'])
    changes = [change for change in plan.changes if change.resource.resource_type == 'jobs'
               and change.resource.resource_id == job['id']]
    if (len(changes) != 1 or plan.operation_name != operation
            or canonical_hash(plain(changes[0].values.get('arguments_json'))) != canonical_hash(plain(args))
            or changes[0].values.get('kind') != job['kind']):
        raise denied
    return plan
