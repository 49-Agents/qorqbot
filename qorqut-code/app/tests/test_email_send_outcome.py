"""Projection cannot turn an approval or ambiguous dispatch into a delivery claim."""
from types import SimpleNamespace
import pytest
from bazarlyk.signoffs.email_outcome import email_outcome

@pytest.mark.parametrize('state,data,error,stage', [
    ('queued', {}, {}, 'queued'),
    ('running', {}, {}, 'running'),
    ('queued', {'dispatch_intent': {}}, {}, 'queued'),
    ('queued', {'dispatch_intent': {'account_id': 'acct'}}, {}, 'reconciling'),
    ('failed', {}, {'code': 'forbidden'}, 'failed'),
    ('failed', {'dispatch_intent': {'account_id': 'acct'}}, {'code': 'forbidden'}, 'unconfirmed'),
    ('failed', {'dispatch_intent': {'account_id': 'acct'}, 'external_receipt': {'state': 'failed'}}, {'code': 'external_failed'}, 'failed'),
    ('succeeded', {'data': {'receipt': {'state': 'completed', 'request_id': 'job'}}}, {}, 'submitted'),
])
def test_job_projection(state, data, error, stage):
    job = dict(kind='fastmail.account_mutation', state=state, result_json=data, error_json=error)
    uow = SimpleNamespace(repository=lambda *a, **kw: SimpleNamespace(get=lambda key: job))
    row = dict(operation_name='fastmail_reply', state='applied', apply_result_json={'data': {'job_id': 'job'}})
    result = email_outcome(row, uow)
    assert result['stage'] == stage
    assert result['provider_submission_accepted'] == (stage == 'submitted')
    assert not result['recipient_delivery_confirmed']
