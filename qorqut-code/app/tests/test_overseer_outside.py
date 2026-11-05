"""Outside adapter checks with a synthetic engine and captured source."""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from qorqut_agentic import AgenticLayer
from qorqut_operations.contracts import DomainError
from bazarlyk.overseer import outside, operations


class Database:
    @contextmanager
    def read(self):
        yield object()


def test_outside_adapter_attribution_authority_and_source(tmp_path, monkeypatch):
    layer = AgenticLayer.initialize(tmp_path / 'root', tmp_path / 'state',
        git_user={'name': 'Test', 'email': 'test@example.invalid'})
    ctx = SimpleNamespace(principal_id='current-agent')
    permitted = {'value': True}
    def current(runtime, context, uow, capability=None):
        if not permitted['value'] or context is not ctx or capability != 'manage_agentic_layer':
            raise DomainError('forbidden', 'denied')
        return ctx, None
    monkeypatch.setattr(outside, 'current_overseer', current)
    scope = {'description': 'fixture', 'project_ids': [], 'work_types': [], 'workflow_refs': [], 'conditions': [], 'exceptions': []}
    ref = {'provider': 'operations', 'kind': 'task', 'record_id': 'task', 'revision': '1', 'content_hash': 'hash',
           'origin_event_id': None, 'episode_id': None, 'actor_id': None, 'actor_kind': None, 'occurred_at': None,
           'locator': {}, 'scope': scope, 'provenance': {'producer_id': None, 'original_refs': [], 'overseer_origin': False},
           'availability': 'available', 'namespace': {'kind': 'global', 'business_id': None}}
    class Provider:
        def get_source(self, value):
            if value != ref: raise DomainError('evidence_unavailable', 'stale')
            return {'content': 'private source body'}
    runtime = SimpleNamespace(database=Database(), agentic_layer=layer,
                              overseer=SimpleNamespace(_provider=lambda *a: Provider()))
    args = {'id': str(uuid4()), 'summary': 'Observed', 'sources': [{'role': 'supporting', 'ref': ref}], 'operation_id': 'one'}
    saved = outside.record(runtime, ctx, args)
    assert saved['created_by'] == ctx.principal_id and 'private source body' not in str(saved)
    assert outside.record(runtime, ctx, args) == saved
    stale = {**args, 'id': str(uuid4()), 'operation_id': 'two', 'sources': [{'role': 'supporting', 'ref': {**ref, 'revision': '2'}}]}
    with pytest.raises(DomainError): outside.record(runtime, ctx, stale)
    permitted['value'] = False
    with pytest.raises(DomainError): outside.record(runtime, ctx, args)
    assert len(layer.overseer.outside.list()['items']) == 1
    assert layer.overseer.proposals.list()['items'] == []
    with layer._ctx.store.reader() as conn:
        assert conn.execute('SELECT count(*) FROM delta_applications').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM overseer_inputs').fetchone()[0] == 0
    assert 'overseer_outside_delta_record' in operations.WRITE
    assert 'overseer_outside_delta_get' in operations.READS
    assert 'overseer_outside_deltas_list' in operations.READS


def test_registered_mcp_outside_with_real_authority_and_evidence(host):
    import asyncio
    from copy import deepcopy
    from qorqut_operations.contracts import Principal
    from bazarlyk.transport.auth import AuthenticatedPrincipal
    from bazarlyk.transport.mcp import MCPTransport
    from bazarlyk.overseer.access import EvidenceAccess
    from bazarlyk.overseer.coverage import AnalysisCoverage
    from bazarlyk.overseer.evidence import EvidenceProvider
    from host_foundation_support import grant_capabilities, revoke_capabilities, owner_global_context
    from test_agentic_access import context

    runtime = host.instance.runtime
    grant_capabilities(host.instance, host.applier, 'manage_agentic_layer', 'read_agent_experience')
    ctx = context(host, host.applier)
    runtime.paths.overseer_archive.mkdir(mode=0o700, parents=True)
    result = host.instance.invoke(owner_global_context(host.instance), 'actions_record',
        {'text': 'Synthetic evidence', 'category': 'global-operational', 'real_time': '2026-10-20'})
    assert result.status == 'ok', result.error
    action = result.to_dict()['data']['action']
    provider = EvidenceProvider(runtime, ctx, runtime.paths.overseer_archive, EvidenceAccess(runtime, ctx),
                                overseer_ids={host.applier}, coverage=AnalysisCoverage(runtime.agentic_layer))
    page = provider.list_changes({'mode': 'explicit', 'namespaces': [{'kind': 'global', 'business_id': None}],
                                  'agent_ids': [], 'task_ids': []})
    source = next(ref for ref in page['items'] if ref['kind'] == 'action_revision' and ref['record_id'] == action['action_id'])
    transport = MCPTransport(runtime, None, [])
    def actor(identity):
        registration = host.registrations[identity]
        with runtime.database.read() as uow:
            credential = uow.connection.execute('SELECT credential_id FROM machine_registrations WHERE id=?', (registration,)).fetchone()[0]
        return AuthenticatedPrincipal(Principal(identity, 'agent'), credential, registration)
    def call(name, args, identity=None):
        return asyncio.run(transport._single_result(name, args, actor(identity or host.applier)))
    record = {'id': str(uuid4()), 'summary': 'Synthetic observation',
              'sources': [{'ref': source, 'role': 'supporting'}], 'operation_id': 'outside-real'}
    with runtime.database.read() as uow:
        before = {name: uow.connection.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in ('tasks', 'signoffs')}
    wire = call('overseer_outside_delta_record', record)
    assert wire['status'] == 'ok', wire
    assert wire['data']['created_by'] == host.applier
    assert call('overseer_outside_delta_record', record)['data'] == wire['data']
    assert call('overseer_outside_delta_get', {'id': record['id']})['data'] == wire['data']
    assert call('overseer_outside_deltas_list', {})['data']['items'] == [wire['data']]
    forged = {**record, 'id': str(uuid4()), 'operation_id': 'forged', 'created_by': 'someone-else'}
    assert call('overseer_outside_delta_record', forged)['status'] == 'error'
    stale = deepcopy(record); stale.update(id=str(uuid4()), operation_id='stale'); stale['sources'][0]['ref']['content_hash'] = 'altered'
    assert call('overseer_outside_delta_record', stale)['status'] == 'error'
    missing = deepcopy(record); missing.update(id=str(uuid4()), operation_id='missing'); missing['sources'][0]['ref']['record_id'] = 'missing'
    assert call('overseer_outside_delta_record', missing)['status'] == 'error'
    for name, args in [('overseer_outside_delta_record', record), ('overseer_outside_delta_get', {'id': record['id']}), ('overseer_outside_deltas_list', {})]:
        assert call(name, args, host.manager)['status'] == 'error'
    with runtime.agentic_layer._ctx.store.reader() as conn:
        for table in ('proposed_deltas', 'deltas', 'delta_applications', 'overseer_inputs', 'overseer_batches'):
            assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    with runtime.database.read() as uow:
        assert before == {name: uow.connection.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in before}
    revoke_capabilities(host.instance, host.applier, 'read_agent_experience')
    assert call('overseer_outside_delta_get', {'id': record['id']})['status'] == 'error'
    assert call('overseer_outside_deltas_list', {})['status'] == 'error'


from test_agentic_access import host  # noqa: F401,E402
