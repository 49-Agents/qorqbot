from dataclasses import replace

import pytest
from qorqut_operations.contracts import DomainError, new_id

from bazarlyk.overseer.authority import current_overseer
from test_agentic_access import host, context  # noqa: F401
from host_foundation_support import grant_capabilities, revoke_capabilities


def test_only_current_overseer_with_native_registration_is_admitted(host):
    runtime = host.instance.runtime
    with runtime.database.read() as uow:
        ctx, profile = current_overseer(runtime, context(host, host.applier), uow)
        assert ctx.principal_id == profile.agent_id == host.applier
    for denied in (context(host, host.worker), context(host, host.manager),
                   replace(context(host, host.applier), session_registration_id=None),
                   replace(context(host, host.applier), session_registration_id=new_id()),
                   replace(context(host, host.applier), source='worker'),
                   replace(context(host, host.applier), delegation_binding_id=new_id(),
                           delegation_assignment_id=new_id(), delegation_generation=1)):
        with runtime.database.read() as uow, pytest.raises(DomainError):
            current_overseer(runtime, denied, uow)
    with runtime.database.transaction() as uow:
        uow.connection.execute("UPDATE credentials SET revoked_at='2000-01-01T00:00:00Z' WHERE id="
                               '(SELECT credential_id FROM machine_registrations WHERE id=?)',
                               (host.registrations[host.applier],))
    with runtime.database.read() as uow, pytest.raises(DomainError):
        current_overseer(runtime, context(host, host.applier), uow)


def test_experience_capability_must_be_explicit_and_current(host):
    runtime = host.instance.runtime
    with runtime.database.read() as uow, pytest.raises(DomainError):
        current_overseer(runtime, context(host, host.applier), uow, capability='read_agent_experience')
    grant_capabilities(host.instance, host.applier, 'read_agent_experience')
    with runtime.database.read() as uow:
        current_overseer(runtime, context(host, host.applier), uow, capability='read_agent_experience')
    revoke_capabilities(host.instance, host.applier, 'read_agent_experience')
    with runtime.database.read() as uow, pytest.raises(DomainError):
        current_overseer(runtime, context(host, host.applier), uow, capability='read_agent_experience')


def test_experience_selection_and_task_locator_require_current_access(host):
    from bazarlyk.overseer.access import EvidenceAccess
    from host_foundation_support import owner_global_context
    runtime = host.instance.runtime
    grant_capabilities(host.instance, host.applier, 'read_agent_experience', 'manage_agentic_layer')
    # The published role itself must admit its evidence dependencies.
    ctx = context(host, host.applier)
    created = host.instance.invoke(host.instance.human(host.instance.business), 'tasks_create', {'title': 'Synthetic task'})
    assert created.status == 'ok', created.error
    identity = created.to_dict()['data']['task']['id']
    scope = {'kind': 'business', 'business_id': host.instance.business}
    gate = EvidenceAccess(runtime, ctx)
    selected = gate.resolve_selection({'mode': 'explicit', 'namespaces': [scope], 'agent_ids': [], 'task_ids': [identity]})
    assert selected['namespaces'] == [scope]
    ref = {'provider': 'operations', 'kind': 'task', 'record_id': identity, 'namespace': scope, 'locator': {'task_id': identity}}
    gate.authorize_source(ref)
    from bazarlyk.overseer.coverage import AnalysisCoverage
    from bazarlyk.overseer.evidence import EvidenceProvider
    archive = runtime.paths.overseer_archive
    archive.mkdir(mode=0o700, parents=True)
    provider = EvidenceProvider(runtime, ctx, archive, gate, overseer_ids={host.applier}, coverage=AnalysisCoverage(runtime.agentic_layer))
    page = provider.list_changes({'mode': 'explicit', 'namespaces': [scope], 'agent_ids': [], 'task_ids': [identity]})
    source = next(item for item in page['items'] if item['kind'] == 'task' and item['record_id'] == identity)
    assert provider.get_source(source)['content']['title'] == 'Synthetic task'
    import json
    from pathlib import Path
    from uuid import uuid4
    fixture = json.loads(Path('/home/alp/qorqbot-v1.3/execution/overseer-v1/fixtures/contracts-v1.json').read_text())
    proposal = {**fixture['proposals'][0], 'id': str(uuid4()), 'sources': [{'ref': source, 'role': 'supporting'}]}
    events = [source['origin_event_id']] if source['origin_event_id'] else []
    episodes = [source['episode_id']] if source['episode_id'] else []
    delta = {**fixture['delta_revision'], 'delta_id': str(uuid4()), 'proposal_ids': [proposal['id']],
             'support_summary': {'proposal_count': 1, 'event_count': len(events), 'episode_count': len(episodes),
                                 'event_refs': events, 'episode_refs': episodes}}
    runtime.agentic_layer.overseer.proposals.record(proposal, 'synthetic-reference-proposal')
    runtime.agentic_layer.overseer.deltas.create(delta, 'synthetic-reference-delta')
    reference = {'type': 'delta', 'id': delta['delta_id'], 'business_id': None}
    with runtime.database.read() as uow:
        with pytest.raises(DomainError):
            runtime.user_input.caller(ctx, uow)  # no saved native session exists in this fixture
        assert runtime.user_input.resolve(ctx, reference, uow)['label'] == delta['summary']
        owner = runtime.user_input.owner_references(host.instance.owner, [('delta', delta['delta_id'], None)], uow)
        assert owner[('delta', delta['delta_id'], None)]['label'] == delta['summary']
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from bazarlyk.transport.auth import Authentication, OriginPolicy
    from bazarlyk.transport.http import HTTPAPI
    authentication = Authentication(runtime.database, host.instance.paths, signing_key=b'synthetic-owner-dispatch-key-000000')
    api = HTTPAPI(runtime, authentication, OriginPolicy(['http://testserver']))
    async def dispatch(request):
        return await api(request)
    client = TestClient(Starlette(routes=[Route('/api/{path:path}', dispatch, methods=['GET', 'POST'])]))
    detail = client.get('/api/overseer/deltas/' + delta['delta_id'])
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body['status'] == 'ok' and body['data']['revision'] == 1 and body['data']['decision'] == 'undecided'
    assert body['data']['evidence'][0]['availability'] == 'available'
    assert client.get('/api/overseer/deltas/' + delta['delta_id'] + '?revision=99').status_code == 404
    assert client.post('/api/overseer/deltas/' + delta['delta_id'] + '/accept').status_code >= 400
    assert client.get('/api/overseer/deltas/' + delta['delta_id'], headers={'authorization': 'Bearer synthetic'}).status_code >= 400
    with pytest.raises(DomainError):
        gate.authorize_source({**ref, 'record_id': new_id()})
    with pytest.raises(DomainError):
        gate.authorize_source({**ref, 'namespace': {'kind': 'business', 'business_id': new_id()}})
    revoke_capabilities(host.instance, host.applier, 'read_agent_experience')
    with pytest.raises(DomainError):
        gate.authorize_source(ref)
    with pytest.raises(DomainError):
        provider.get_source(source)
    with runtime.database.read() as uow, pytest.raises(DomainError):
        runtime.user_input.resolve(context(host, host.applier), reference, uow)
