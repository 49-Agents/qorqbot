import pytest
from starlette.testclient import TestClient
from bazarlyk.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id
from test_agent_inbox import inbox


def test_all_missing_fields_are_reported_together():
    op = OperationDescriptor('example', {'type':'object','properties':{'alpha':{'type':'string'},'beta':{'type':'integer'}},
        'required':['alpha','beta'],'additionalProperties':False}, effect_kind='read', supports_signoff=False, read=lambda *args: {})
    with pytest.raises(DomainError) as caught:
        op.validate({'unexpected': 1})
    fields = str(caught.value.field_errors)
    assert all(word in fields for word in ('alpha','beta','unexpected'))


def test_diagnostics_work_during_mandatory_context_failure_but_business_reads_do_not(inbox, monkeypatch):
    s = inbox
    with TestClient(s.web) as client:
        headers = s.initialize(client, s.agent)
        def unavailable(*args, **kwargs):
            raise DomainError('context_unavailable', 'Mandatory source is applying')
        monkeypatch.setattr(s.runtime.resolver, 'resolve_agent_config', unavailable)
        discovery = client.post('/mcp', headers=headers, json={'jsonrpc':'2.0','id':new_id(),'method':'tools/list'})
        names = {item['name'] for item in discovery.json()['result']['tools']}
        assert names == {'agent_runtime_get', 'operations_describe', 'dev_tickets_create',
            'dev_tickets_agent_list', 'dev_tickets_get', 'dev_tickets_follow_up'}
        health = s.tool(client, headers, 'agent_runtime_get', {})['decoded']
        assert health['status'] == 'ok', health
        assert health['data']['preparation']['error']['code'] == 'context_unavailable'
        assert s.tool(client, headers, 'tasks_list', {})['decoded']['error']['code'] == 'context_unavailable'
        probe = s.tool(client, headers, 'operations_describe', {'names':['agent_work_update','signoffs_review']})['decoded']
        assert probe['status'] == 'ok', probe
        tools = probe['data']['tools']
        # Multi-name probes answer availability/permission; the exact schema
        # still comes from a single-name call during mandatory-context failure.
        assert 'input_schema' not in tools[0] and tools[0]['available'] is True
        assert tools[1]['available'] is False
        schema = s.tool(client, headers, 'operations_describe', {'names':['agent_work_update']})['decoded']
        assert schema['status'] == 'ok', schema
        assert 'idempotency_key' in schema['data']['tools'][0]['input_schema']['required']
        assert 'expected_work_revision' in schema['data']['tools'][0]['input_schema']['required']
        reported = s.tool(client, headers, 'dev_tickets_create', {'title':'Preparation unavailable','idempotency_key':new_id()})['decoded']
        assert reported['status'] == 'ok', reported


def test_configuration_and_polling_do_not_claim_hook_delivery(inbox):
    from dataclasses import replace
    from bazarlyk.agents.diagnostics import AgentDiagnostics
    from qorqut_operations.contracts import utc_now
    s=inbox
    registration=s.registrations[s.agent.id]
    ctx=replace(s.ctx(),session_registration_id=registration)
    diagnostics=AgentDiagnostics(s.runtime)
    def read():
        with s.fixture.db.read() as uow:return diagnostics.runtime_get(ctx,{},None,uow)
    initial=read()
    assert initial['preparation']['mandatory_context_available']
    assert initial['connection']['available']
    assert initial['context_delivery']['state']=='not_observed'
    response=s.runtime.resolver.resolve_agent_config(s.agent).to_dict()
    s.runtime.resolver.record_delivery(s.agent,registration_id=registration,session_id=s.consumer,
        effective_config_id=response['effective_config_id'],bundle_hash=response['mandatory_context']['bundle_hash'],
        event='prompt',emitted_at=utc_now())
    delivered=read()['context_delivery']
    assert delivered['state']=='emitted'
    assert delivered['receipt']['observation']=='emitted_by_hook_not_model_compliance'
