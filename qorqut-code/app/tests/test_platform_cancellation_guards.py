"""Cancellation authority and provider recovery use the current Signoff jobs."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from qorqut_operations.contracts import DomainError, new_id
from test_platform_signoff_submission import platform, row
from test_platform_execution import approved, cancel
from test_service_remote import fleet


def queued_cancel(platform):
    fixture = platform[0]
    action = approved(platform)
    assert fixture.platform_background.run_one(action['execution_job_id'])['state'] == 'succeeded'
    assert cancel(platform, row(platform, action['id'])).status == 'ok'
    current = row(platform, action['id'])
    return current, current['execution_json']['cancellation']['job_id']


@pytest.mark.parametrize('fault', ['unbound_job', 'wrong_reference', 'changed_intent'])
def test_cancellation_rejects_unbound_job_or_altered_intent_before_network(platform, fault):
    fixture = platform[0]
    current, identity = queued_cancel(platform)
    background = fixture.platform_background
    with fixture.db.transaction(business_id=fixture.business_id) as uow:
        job = uow.repository('jobs').get(identity)
        if fault == 'unbound_job':
            clone = background.programs.job_values(fixture.ctx, 'platform.cancel', job['arguments_json'], uow)
            clone['dedupe_key'] = new_id()
            uow.repository('jobs').insert(clone)
            identity = clone['id']
        elif fault == 'wrong_reference':
            with pytest.raises(DomainError, match='Immutable'):
                uow.repository('jobs').update(identity, {'arguments_json': {
                    'signoff_id': current['id'], 'job_ref': 'foreign-job'}}, expected_revision=job['revision'])
            assert uow.repository('jobs').get(identity) == job
            return
        else:
            uow.repository('jobs').update(identity, {'result_json': {'dispatch_intent': {
                'signoff_id': current['id'], 'job_ref': 'foreign-job'}}}, expected_revision=job['revision'])
    def unexpected(*args):
        raise AssertionError('Invalid cancellation reached the provider')
    fixture.platform_client.cancel_job = unexpected
    result = background.run_one(identity)
    assert result['error']['code'] == ('invalid_plan' if fault == 'changed_intent' else 'forbidden'), result
    assert row(platform, current['id']) == current


@pytest.mark.parametrize('reply,code', [
    ({'job_ref': 'foreign', 'outcome': 'cancelled'}, 'invalid_external_result'),
    ({'job_ref': 'external-123', 'outcome': 'stopped'}, 'invalid_external_result'),
    (DomainError('external_auth_failed', 'Fleet credential requires owner attention'), 'external_auth_failed'),
    (None, 'operation_unavailable'),
])
def test_cancel_errors_preserve_approval_and_dispatched_evidence(platform, reply, code):
    fixture = platform[0]
    current, identity = queued_cancel(platform)
    if reply is not None:
        def cancel_job(job_ref):
            if isinstance(reply, DomainError):
                raise reply
            return reply
        fixture.platform_client.cancel_job = cancel_job
    result = fixture.platform_background.run_one(identity)
    assert result['error']['code'] == code, result
    with fixture.db.read(business_id=fixture.business_id) as uow:
        job = uow.repository('jobs').get(identity)
        assert job['state'] == 'failed' and job['error_json']['code'] == code
        assert 'external_receipt' not in job['result_json']
    assert row(platform, current['id']) == current
    assert fixture.platform_client.submits == 1


@pytest.mark.parametrize('exhaust', [False, True])
def test_cancel_network_retry_preserves_exact_job_and_never_resends(platform, tmp_path, exhaust):
    fixture = platform[0]
    current, identity = queued_cancel(platform)
    background = fixture.platform_background
    now = [datetime(2026, 9, 8, 12, tzinfo=timezone.utc)]
    background.clock = lambda: now[0]
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if exhaust:
            return httpx.Response(503, json={'error': 'private-token'})
        if len(calls) == 1:
            raise httpx.ReadTimeout('private-token', request=request)
        return httpx.Response(200, json={'id': 'external-123'})
    fixture.platform_client.cancel_job = fleet(tmp_path, handler).cancel_job
    saved_intent = None
    attempts = 3 if exhaust else 2
    for attempt in range(1, attempts + 1):
        result = background.run_one(identity)
        with fixture.db.read(business_id=fixture.business_id) as uow:
            job = uow.repository('jobs').get(identity)
        assert job['attempts'] == attempt
        saved_intent = saved_intent or job['result_json']['dispatch_intent']
        assert job['result_json']['dispatch_intent'] == saved_intent
        if attempt < attempts:
            assert result.get('state') == 'queued', result
            assert 'external_receipt' not in job['result_json']
            now[0] += timedelta(seconds=30)
            assert job['next_retry_at'] == now[0].isoformat().replace('+00:00', 'Z')
        else:
            assert job['state'] == ('failed' if exhaust else 'succeeded'), result
            if exhaust:
                assert result['error']['code'] == 'external_unavailable', result
            else:
                assert result.get('state') == 'succeeded', result
    assert 'private-token' not in str(job['error_json'])
    assert calls == [('DELETE', '/jobs/external-123')] * attempts
    after = row(platform, current['id'])
    assert after['decision_json'] == current['decision_json']
    assert after['change_set'] == current['change_set']
    assert after['execution_json']['state'] == ('dispatched' if exhaust else 'canceled')
    assert fixture.platform_client.submits == 1
