"""Cross-agent coordination exposes job status without private provider payloads."""

from dataclasses import replace
from qorqut_operations.contracts import new_id
from test_service_transports import ServiceFixture


def test_jobs_keep_private_arguments_and_results_with_the_initiator_and_owner(tmp_path):
    service = ServiceFixture(tmp_path, full=True)
    runtime = service.runtime
    agent = runtime.resolver.invocation_context(service.agent)
    with runtime.database.read() as uow:
        manager = runtime.policy.context_for(service.manager.id, business_id=agent.business_id,
            review_business_id=agent.business_id, uow=uow)
    with runtime.database.transaction() as uow:
        values = runtime.background.programs.job_values(agent, 'platform.dispatch',
            {'signoff_id': new_id(), 'submission': {'payload': {'body': 'private-arguments'}}}, uow)
        values.update(result_json={'data': {'private_read': 'private-result'}},
            error_json={'code': 'provider_unavailable', 'retryable': True, 'message': 'private-message',
                'details': {'payload': 'private-error'}})
        uow.repository('jobs', business_id=agent.business_id).insert(values)
    args = {'job_id': values['id']}
    own = runtime.application.invoke(agent, 'jobs_get', args)
    assert own.status == 'ok' and own.data['details_available'] is True
    assert own.data['job']['result_json']['data']['private_read'] == 'private-result'
    shared = runtime.application.invoke(manager, 'jobs_get', args)
    assert shared.status == 'ok', shared.to_dict()
    assert shared.data['details_available'] is False
    assert shared.data['job']['state'] == 'queued'
    assert shared.data['job']['error_json'] == {'code': 'provider_unavailable', 'retryable': True}
    assert 'private-' not in str(shared.to_dict())
    owner = runtime.application.invoke(service.fixture.ctx, 'jobs_get', args)
    assert owner.status == 'ok' and owner.data['details_available'] is True
    foreign = runtime.application.invoke(replace(manager, business_id=new_id()), 'jobs_get', args)
    assert foreign.status == 'error'
