"""Regressions from the actual Ordubashy provider/browser trial."""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from bazarlyk.agents.operational_delegation import OperationalDelegationOperations
from bazarlyk.application import Application
from bazarlyk.application.catalog import builtin_registry
from bazarlyk.bootstrap.delegated_runtime import startup_diagnostic
from bazarlyk.browser.access import BusinessBrowserAccess
from bazarlyk.browser.turns import BrowserTurnQueue
from qorqut_operations.contracts import ArtifactRef, DomainError, InvocationContext, PolicyDecision, Principal, new_id
from bazarlyk.transport.auth import Authentication, AuthenticatedPrincipal, OriginPolicy
from bazarlyk.transport.mcp import MCPTransport
from test_operational_delegation_store import setup_store


def test_real_delegated_binding_initializes_lists_and_checkpoints_over_http(tmp_path):
    db, store, parent_id, business_id, _ = setup_store(tmp_path)
    store.clock = lambda: datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    registry = builtin_registry()
    policy = SimpleNamespace(resolve=lambda *a: PolicyDecision('allow', 'isolated regression'))
    app = Application(db, registry, policy=policy)
    operations = OperationalDelegationOperations(store).register(registry, app)
    app.operational_delegation = store
    parent = InvocationContext(Principal(parent_id, 'agent', 'Astra'), source='mcp',
        allowed_business_ids=frozenset({business_id}), profile_revision=1,
        global_capabilities=frozenset({'manage_delegated_assignments'}),
        role_artifact_ref=ArtifactRef('ordubashy', '1.36.1', 'sha256:' + 'a'*64))
    # Exercise the public create operation with omitted optional business_id and no domain grants.
    result = app.invoke(parent, 'delegated_assignments_create', {
        'scope': {'kind': 'global', 'allowed_business_ids': [], 'record_ids': {}, 'source_ids': {}},
        'objective': 'Return a marker', 'plan': {'steps': ['save marker']}, 'allowed_operations': []},
        {'idempotency_key': new_id()})
    assert result.status == 'ok', result.to_dict()
    assignment = result.data['assignment'] if 'assignment' in result.data else result.data
    with db.transaction() as uow:
        registration_id = new_id()
        uow.repository('machine_registrations').insert({'id': registration_id,
            'principal_id': parent_id, 'workspace': str(tmp_path/'parent'),
            'service_url': 'http://testserver', 'profile_revision': 1,
            'created_at': store.clock(), 'updated_at': store.clock()})
        running = store.claim(uow, assignment['id'], actor_principal_id=parent_id)
        issued = store.issue_worker_binding(uow, assignment['id'], parent_principal_id=parent_id,
            generation=running['execution_generation'], lease_id=running['lease_id'],
            expected_revision=running['revision'], workspace=str(tmp_path/'worker'),
            service_url='http://testserver', expires_at=running['lease_until'])
    runtime = SimpleNamespace(database=db, registry=registry, application=app,
        require_authority=lambda: None,
        agent_context=lambda principal, **kw: replace(parent, principal=principal,
            session_registration_id=kw.get('session_registration_id')),
        resolver=SimpleNamespace(resolve_agent_config=lambda principal: SimpleNamespace(data={'tools': [
            {'canonical_operation': name, 'exposed_aliases': [], 'available': True}
            for name in ['delegated_assignment_get', 'delegated_assignment_checkpoint']]})))
    from bazarlyk.browser.operations import register_browser_operation
    register_browser_operation(runtime)
    auth = Authentication(db, tmp_path, signing_key=b'k'*48)
    actor = auth.agent('Bearer ' + issued['worker_token'])
    assert actor.credential_id is None and actor.delegation_binding_id
    transport = MCPTransport(runtime, auth, OriginPolicy(['http://testserver']))
    @asynccontextmanager
    async def lifespan(_):
        async with transport.manager.run():
            yield
    web = Starlette(routes=[Mount('/mcp', app=transport)], lifespan=lifespan)
    with TestClient(web) as client:
        headers = {'Authorization': 'Bearer '+issued['worker_token'],
            'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        init = client.post('/mcp/', headers=headers, json={'jsonrpc':'2.0','id':1,'method':'initialize',
            'params': {'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'trial','version':'1'}}})
        assert init.status_code == 200, init.text
        headers['Mcp-Session-Id'] = init.headers['mcp-session-id']
        headers['MCP-Protocol-Version'] = init.json()['result']['protocolVersion']
        assert client.post('/mcp/', headers=headers, json={'jsonrpc':'2.0','method':'notifications/initialized'}).status_code == 202
        listed = client.post('/mcp/', headers=headers, json={'jsonrpc':'2.0','id':2,'method':'tools/list'})
        assert 'delegated_assignment_get' in [t['name'] for t in listed.json()['result']['tools']]
        def call(name, arguments):
            response = client.post('/mcp/', headers=headers, json={'jsonrpc':'2.0','id':new_id(),
                'method':'tools/call','params':{'name':name,'arguments':arguments}})
            assert response.status_code == 200, response.text
            return json.loads(response.json()['result']['content'][0]['text'])
        current = call('delegated_assignment_get', {'assignment_id':assignment['id']})
        assert current['status'] == 'ok', current
        current = current['data']
        if 'assignment' in current:
            current = current['assignment']
        saved = call('delegated_assignment_checkpoint', {'state':'completed',
            'checkpoint':{'marker':'real-http'}, 'result':{'summary':'Saved real HTTP marker', 'marker':'real-http'},
            'expected_revision':current['revision'], 'idempotency_key':new_id()})
        assert saved['status'] == 'ok', saved
        # Every HTTP request authenticates anew; completion invalidates the binding.
        assert client.post('/mcp/', headers=headers, json={'jsonrpc':'2.0','id':4,'method':'tools/list'}).status_code != 200
    db.close()


def test_native_mcp_browser_is_lease_free_and_limited_to_own_target(tmp_path, monkeypatch):
    db, store, parent_id, business_id, _ = setup_store(tmp_path)
    runtime = SimpleNamespace(root=tmp_path, database=db, browser_turns=BrowserTurnQueue(clock=store.clock))
    access = BusinessBrowserAccess(runtime)
    monkeypatch.setattr(access, '_worker_target', lambda *a: {'cdp_endpoint':'http://127.0.0.1:19334',
        'runtime_dir':str(tmp_path), 'instance':'trial'})
    from bazarlyk.bootstrap import browser
    monkeypatch.setattr(browser, 'runtime_executable', lambda p:'agent-browser')
    monkeypatch.setattr(browser, 'run', lambda *a, **kw: (0, 'Saved: native', ''))
    actor = AuthenticatedPrincipal(Principal(parent_id,'agent','Astra'), credential_id=new_id(), registration_id=new_id())
    consumer1, consumer2 = new_id(), new_id()
    with pytest.raises(DomainError, match='must omit business_id') as error:
        access.worker(actor, 'acquire', {'consumer_id': consumer1, 'business_id': business_id})
    assert error.value.code == 'validation_error'
    with pytest.raises(DomainError) as error:
        access.worker(replace(actor, registration_id=None), 'acquire', {'consumer_id': consumer1})
    assert error.value.code == 'forbidden'
    first = access.worker(actor, 'acquire', {'consumer_id':consumer1})
    second = access.worker(actor, 'acquire', {'consumer_id':consumer2})
    assert first['status'] == second['status'] == 'owning' and first['handle'] != second['handle']
    for consumer in (consumer1, consumer2, consumer1):
        result = access.worker(actor,'command',{'consumer_id':consumer,'action_id':new_id(),'command':['observe','overview']})
        assert result['exit_code']==0 and result['action']['state']=='succeeded'
    assert access.worker(actor,'release',{'consumer_id':consumer1})['status'] == 'released'
    assert access.worker(actor,'command',{'consumer_id':consumer1,'command':['observe','overview']})['exit_code'] == 0
    with pytest.raises(DomainError):
        access.worker(actor,'acquire',{'consumer_id':consumer1,'browser_key':'configured:'+new_id()})
    with db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM browser_turns').fetchone()[0] == 0
        assert uow.connection.execute('SELECT count(*) FROM browser_turn_actions').fetchone()[0] == 0
    db.close()


def test_startup_diagnostic_retains_correlation_without_credentials():
    correlation = new_id()
    result = startup_diagnostic('MCP failed HTTP 401 Bearer sk-secret credential_file=/secret '+correlation)
    assert correlation in result and 'MCP startup' in result
    assert 'sk-secret' not in result and '/secret' not in result


@pytest.mark.parametrize('reconcile', [False, True])
def test_rejected_native_command_cannot_poison_turn(tmp_path, monkeypatch, reconcile):
    from bazarlyk.bootstrap import browser
    db, store, parent_id, _, _ = setup_store(tmp_path)
    queue = BrowserTurnQueue(clock=store.clock)
    access = BusinessBrowserAccess(SimpleNamespace(root=tmp_path, database=db, browser_turns=queue))
    monkeypatch.setattr(access, '_worker_target', lambda *a: {})
    actor = AuthenticatedPrincipal(Principal(parent_id, 'agent'), credential_id=new_id(), registration_id=new_id())
    consumer = new_id()
    with pytest.raises(DomainError) as exc:
        access.worker(actor, 'command', {'consumer_id': consumer, 'command': ['tabs'], 'idempotency_key': 'bad-tabs'})
    assert exc.value.code == 'validation_error'
    with db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM browser_turn_actions').fetchone()[0] == 0
    # Reproduce a persisted turn from the leased model without executing a browser command.
    owner = dict(parent_principal_id=parent_id, requester_id='native:'+parent_id+':'+consumer, generation=None)
    with db.transaction() as u:
        turn = queue.acquire(u, browser_key='configured:'+parent_id, business_id=None, requester_kind='agent', **owner)
    owner['lease_id'] = turn['lease_id']
    with pytest.raises(browser.BrowserError) as rejected:
        browser.validate_arguments(['tabs'])
    with db.transaction() as u:
        queue.begin_action(u, turn['id'], **owner, action_id='bad-tabs', command=['tabs'])
        queue.settle_action(u, turn['id'], **owner, action_id='bad-tabs', state='uncertain',
                            outcome={'error': str(rejected.value)}, error=str(rejected.value))
    with db.transaction() as u:
        with pytest.raises(DomainError):
            queue.recover_rejected_action(u, turn['id'], **{**owner, 'lease_id':new_id()}, action_id='bad-tabs')
    queue.clock = lambda: '2099-01-01T00:00:00Z'
    with db.transaction() as u:
        if reconcile:
            recovered = queue.reconcile_action(u, turn['id'], **owner, action_id='bad-tabs', outcome={})
            assert recovered['state'] == 'failed' and json.loads(recovered['outcome_json'])['executed'] is False
        assert queue.release(u, turn['id'], **owner)['state'] == 'released'
    assert access.worker(actor, 'acquire', {'consumer_id':new_id()})['status'] == 'owning'
    db.close()


def test_native_uncertain_executed_command_remains_fenced(tmp_path):
    db, store, parent_id, _, _ = setup_store(tmp_path)
    queue = BrowserTurnQueue(clock=store.clock)
    owner = dict(parent_principal_id=parent_id, requester_id='native:'+parent_id+':'+new_id(), generation=None)
    with db.transaction() as u:
        turn = queue.acquire(u, browser_key='configured:'+parent_id, business_id=None, requester_kind='agent', **owner)
        owner['lease_id'] = turn['lease_id']
        queue.begin_action(u, turn['id'], **owner, action_id='maybe-click', command=['click','e1'])
        queue.settle_action(u, turn['id'], **owner, action_id='maybe-click', state='uncertain', outcome={}, error='timeout')
    with db.transaction() as u:
        with pytest.raises(DomainError) as exc:
            queue.release(u, turn['id'], **owner)
        assert exc.value.code == 'browser_busy'
        with pytest.raises(DomainError):
            queue.reconcile_action(u, turn['id'], **owner, action_id='maybe-click', outcome={'executed':False})
    db.close()


def test_launcher_drains_large_stderr_and_preserves_provider_home(tmp_path, monkeypatch):
    import os
    import sys
    import time
    from bazarlyk.bootstrap.delegated_runtime import DelegatedRuntime
    monkeypatch.delenv('CODEX_HOME', raising=False)
    correlation = new_id()
    script = tmp_path/'failed.py'
    script.write_text("import os,sys\nassert os.environ['CODEX_HOME'].endswith('/.codex')\nsys.stderr.write('x'*200000+' MCP HTTP 401 sk-secret " + correlation + "')\nsys.exit(1)\n")
    driver = DelegatedRuntime({}, new_id(), alive=lambda:True, client=object())
    with pytest.raises(DomainError) as error:
        driver._run_turn([sys.executable,str(script)], '', {'id':new_id()}, 1,
            time.monotonic()+10, str(tmp_path), None, 1)
    assert correlation in error.value.message
    assert 'sk-secret' not in error.value.message
    assert not driver.processes


def test_unattended_worker_approves_only_bound_mcp_tools(tmp_path):
    import tomllib
    from bazarlyk.bootstrap.delegated_runtime import worker_command
    from bazarlyk.agents.operational_delegation import WORKER_OPERATIONS
    assignment = {'selected_model':'gpt-6-luna','reasoning_effort':'low',
        'allowed_operations_json':['tasks_get','tasks_update']}
    registration = {'workspace':str(tmp_path),'service_url':'http://testserver'}
    for thread in (None, new_id()):
        command = worker_command(assignment, registration, tmp_path/'registration.json', thread_id=thread)
        server = next(tomllib.loads(v)['mcp_servers']['bazarlyk'] for v in command if v.startswith('mcp_servers.'))
        assert server['default_tools_approval_mode']=='approve'
        assert set(server['enabled_tools']) == set(WORKER_OPERATIONS)|{'tasks_get','tasks_update'}
        assert 'agents_create' not in server['enabled_tools']
        assert '--ignore-user-config' in command
        assert not any('danger-full-access' in v for v in command)


def test_heartbeat_preserves_checkpoint_revision_but_plan_changes_remain_fenced(tmp_path):
    from datetime import timedelta
    db, store, parent_id, business_id, now = setup_store(tmp_path)
    from test_operational_delegation_store import create_assignment
    assignment = create_assignment(db, store, parent_id, business_id)
    with db.transaction() as uow:
        running = store.claim(uow, assignment['id'], actor_principal_id=parent_id)
        revision = running['revision']
        for seconds in (30,60,90):
            stamp=(now+timedelta(seconds=seconds)).isoformat().replace('+00:00','Z')
            renewed=store.renew(uow,assignment['id'],actor_principal_id=parent_id,
                generation=running['execution_generation'],lease_id=running['lease_id'],
                lease_seconds=300,expected_revision=revision,now=stamp)
            assert renewed['revision']==revision
        saved=store.checkpoint(uow,assignment['id'],actor_principal_id=parent_id,
            generation=running['execution_generation'],lease_id=running['lease_id'],
            expected_revision=revision,checkpoint={'done':True},state='completed',
            result={'summary':'slow model completed after three heartbeats'},now=stamp)
        assert saved['revision']>revision and saved['state']=='completed'
        with pytest.raises(DomainError):
            store.renew(uow,assignment['id'],actor_principal_id=parent_id,
                generation=running['execution_generation'],lease_id=running['lease_id'],
                expected_revision=revision,now=stamp)
    db.close()
