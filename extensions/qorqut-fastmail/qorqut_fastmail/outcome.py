"""Local, bounded send status for Fastmail Signoffs. Reading a card never calls a provider."""


def email_outcome(row, uow, *, detail=False):
    if row['operation_name'] not in {'fastmail_send', 'fastmail_reply', 'fastmail_forward'}:
        return None
    state = row['state']
    stage = {'pending': 'awaiting_approval', 'approved': 'approved', 'applied': 'approved',
             'rejected': 'cancelled', 'conflicted': 'failed', 'failed': 'failed'}.get(state, 'failed')
    result = {'kind': 'account_email', 'stage': stage, 'provider_submission_accepted': False}
    job_id = ((row.get('apply_result_json') or {}).get('data') or {}).get('job_id')
    job = uow.repository('jobs', business_id=None).get(job_id) if job_id else None
    if not job or job['kind'] != 'fastmail.account_mutation':
        return result
    data = job.get('result_json') or {}
    error = job.get('error_json') or {}
    receipt = (data.get('data') or {}).get('receipt') or {}
    if job['state'] == 'succeeded' and receipt.get('state') == 'completed' and receipt.get('request_id') == job_id:
        stage = 'submitted'
    elif job['state'] == 'failed':
        stage = 'unconfirmed' if (error.get('code') == 'external_unconfirmed' or
            (data.get('dispatch_intent') and (data.get('external_receipt') or {}).get('state') != 'failed')) else 'failed'
    elif job['state'] in {'cancelled', 'cancel_requested'}:
        stage = 'unconfirmed' if data.get('dispatch_intent') else 'cancelled'
    elif data.get('dispatch_intent'):
        stage = 'reconciling'
    else:
        stage = 'queued' if job['state'] == 'queued' else 'running'
    result.update(stage=stage, provider_submission_accepted=stage == 'submitted')
    if error.get('code'):
        result['error'] = {'code': error['code'], 'retryable': bool(error.get('retryable'))}
    return result
