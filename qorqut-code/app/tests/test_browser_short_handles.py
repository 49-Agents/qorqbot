"""Handle-only browser calls: persisted binding, fences, queueing and recovery."""
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import anyio
import pytest

from bazarlyk.application.catalog import builtin_registry
from bazarlyk.browser.access import BusinessBrowserAccess
from bazarlyk.browser.operations import register_browser_operation
from bazarlyk.bootstrap import browser
from qorqut_operations.contracts import DomainError, InvocationContext, Principal, new_id
from bazarlyk.storage import Database
from bazarlyk.transport.auth import AuthenticatedPrincipal
from bazarlyk.transport.mcp import MCPTransport
from test_operational_delegation_store import setup_store
from test_browser_worker_runtime_path import _configured_runtime, _configured_running, _running


@pytest.fixture
def env(tmp_path, monkeypatch):
    database, store, parent, business, now = setup_store(tmp_path)
    database.delegation = store
    runtime = _configured_runtime(tmp_path, database, store, parent)
    access = BusinessBrowserAccess(runtime)
    actor = AuthenticatedPrincipal(Principal(parent, "agent"), registration_id=new_id(), credential_id=new_id())
    calls = []
    def run(workspace, command, **kwargs):
        calls.append((command, kwargs))
        return 0, '{"ok":true}', ''
    monkeypatch.setattr(browser, 'run', run)
    return SimpleNamespace(database=database, store=store, parent=parent, business=business,
        now=now, runtime=runtime, access=access, actor=actor, calls=calls)


def acquire(e, actor=None, **kwargs):
    actor = actor or e.actor
    if actor.delegation_assignment_id is None:
        kwargs.setdefault('consumer_id', new_id())
    return e.access.tool(actor, 'acquire', {'action': 'acquire', 'protocol': 'handles', **kwargs})


def command(e, handle, actor=None, **kwargs):
    return e.access.tool(actor or e.actor, 'command', {'handle': handle,
        'command': ['observe', 'overview'], 'action_id': new_id(), **kwargs})


def worker(e, title='worker', business=False):
    assignment = (_running(e.database, e.store, e.parent, e.business, title) if business else
                  _configured_running(e.database, e.store, e.parent, title))
    return replace(e.actor, delegation_binding_id=new_id(), delegation_assignment_id=assignment['id'],
                   delegation_generation=assignment['execution_generation'])


def test_handle_only_lifecycle_survives_restart_and_deduplicates_commands(env):
    e = env
    actor = worker(e)
    receipt = acquire(e, actor)
    handle = receipt['handle']
    assert len(handle) == 13 and receipt['status'] == 'owning'
    assert not {'turn', 'target', 'turn_token', 'lease_id', 'turn_id'} & receipt.keys()
    e.runtime.database = Database(e.database.path).open_existing()  # no in-memory mapping
    e.access = BusinessBrowserAccess(e.runtime)
    action = new_id()
    first = command(e, handle, actor, action_id=action)
    replay = command(e, handle, actor, action_id=action)
    assert len(e.calls) == 1 and replay['replayed']
    assert first['action']['state'] == 'succeeded'
    assert first['stdout'] == '{"ok":true}'
    assert e.access.tool(actor, 'renew', {'handle': handle})['status'] == 'owning'
    assert e.access.tool(actor, 'checkpoint', {'handle': handle, 'checkpoint': {'tab': 't2'}})['status'] == 'owning'
    assert e.access.tool(actor, 'status', {'handle': handle})['checkpoint'] == {'tab': 't2'}
    assert e.access.tool(actor, 'status', {'handle': handle})['status'] == 'owning'
    assert e.access.tool(actor, 'release', {'handle': handle})['status'] == 'released'
    with pytest.raises(DomainError, match='expired or was released'):
        command(e, handle, actor)
    assert len(e.calls) == 1


def test_handles_reject_other_identity_selectors_and_raw_credentials(env):
    e = env
    owner = worker(e)
    handle = acquire(e, owner)['handle']
    for actor in (replace(owner, principal=Principal(new_id(), 'agent')),
                  replace(owner, credential_id=new_id()), replace(owner, registration_id=new_id())):
        with pytest.raises(DomainError, match='unavailable for this caller'):
            command(e, handle, actor=actor)
    for field in ('consumer_id', 'business_id', 'browser_key'):
        with pytest.raises(DomainError, match='selectors cannot be changed'):
            command(e, handle, owner, **{field: new_id()})
    with pytest.raises(DomainError, match='lease credentials are managed'):
        command(e, handle, owner, lease_id=new_id())
    with pytest.raises(DomainError, match='unavailable for this caller'):
        command(e, 'b000000000000', owner)
    assert not e.calls


def test_expired_replaced_and_superseded_handles_never_adopt_current_lease(env):
    e = env
    actor = worker(e)
    old = acquire(e, actor)['handle']
    current = acquire(e, actor)['handle']
    with pytest.raises(DomainError, match='expired or was released'):
        command(e, old, actor)
    with e.database.transaction() as uow:
        row = uow.connection.execute('SELECT turn_id FROM browser_handles WHERE id=?', (current,)).fetchone()
        uow.connection.execute('UPDATE browser_turns SET lease_id=? WHERE id=?', (new_id(), row[0]))
    with pytest.raises(DomainError, match='expired or was released'):
        command(e, current, actor)
    fresh = acquire(e, actor)['handle']
    e.access.turns.clock = lambda: (e.now + timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
    with pytest.raises(DomainError, match='expired or was released'):
        command(e, fresh, actor)
    assert not e.calls


def test_durable_wait_resume_and_cancel_use_handles_without_parent_polling(env):
    e = env
    first, second = worker(e, 'first'), worker(e, 'second')
    handle = acquire(e, first)['handle']
    waiting = acquire(e, second)
    assert waiting['status'] == 'waiting' and 'handle' not in waiting
    e.access.tool(first, 'release', {'handle': handle})
    with e.database.transaction() as uow:
        resumed = e.store.claim(uow, second.delegation_assignment_id, actor_principal_id=e.parent, dispatcher_ready=True)
    current = replace(second, delegation_generation=resumed['execution_generation'], credential_id=new_id())
    next_handle = acquire(e, current)['handle']
    with pytest.raises(DomainError):
        command(e, next_handle, second)
    assert command(e, next_handle, current)['action']['state'] == 'succeeded'
    with e.database.transaction() as uow:
        saved = uow.repository('operational_assignments').get(current.delegation_assignment_id, required=True)
        e.store.control(uow, saved['id'], parent_principal_id=e.parent, expected_revision=saved['revision'], action='cancel')
    with pytest.raises(DomainError):
        command(e, next_handle, current)
    assert len(e.calls) == 1


def test_business_gateway_token_remains_private_and_artifacts_still_work(env, monkeypatch):
    e = env
    actor = worker(e, business=True)
    def target(key, token, parent):
        return {'cdp_endpoint': 'http://gateway/' + token, 'instance': 'test', 'runtime_dir': str(e.runtime.root / 'browser-runtime')}
    monkeypatch.setattr(e.access, '_worker_target', target)
    handle = acquire(e, actor)['handle']
    with e.database.read() as uow:
        saved = json.loads(uow.connection.execute('SELECT arguments_json FROM browser_handles WHERE id=?', (handle,)).fetchone()[0])
    token = saved['turn_token']
    assert len(token) == 64
    def run(workspace, command, **kwargs):
        assert kwargs['target'][0] == 'http://gateway/' + token
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / 'screen.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        return 0, 'http://gateway/' + token, ''
    monkeypatch.setattr(browser, 'run', run)
    result = command(e, handle, actor, command=['screenshot', 'screen.png'])
    assert token not in json.dumps(result)
    artifact = result['artifacts'][0]['artifact_handle']
    image = e.access.tool(actor, 'artifact', {'handle': handle, 'artifact_handle': artifact})
    assert image['_mcp_images'][0]['mime_type'] == 'image/png'
    e.access.tool(actor, 'release', {'handle': handle})
    assert json.loads((e.runtime.root / '.bazarlyk/browsers' / e.business / 'lease.json').read_text()) == {}


def test_mcp_schema_and_transport_accept_only_handle_for_followup(env):
    e = env
    class Caller:
        def exposes(self, descriptor): return True
        def check_operation(self, name): pass
        def validate_options(self, actor, name, options): pass
        def validate_arguments(self, actor, name, args): pass
        def invocation_context(self, actor, **kwargs): return InvocationContext(actor.principal, source='mcp')
    class Application:
        def namespace(self, *args): return 'global'
        def authorize(self, *args, **kwargs): pass
    runtime = SimpleNamespace(database=e.database, registry=builtin_registry(), application=Application(),
                              browser_access=e.access, require_authority=lambda: None)
    register_browser_operation(runtime)
    descriptor = runtime.registry.get('browser_turn')
    assert 'handle' in descriptor.input_schema['properties']
    transport = MCPTransport(runtime, authentication=None, origins=[], caller=Caller())
    acquired = anyio.run(transport._single_result, 'browser_turn', {'action': 'acquire', 'protocol': 'handles', 'consumer_id': new_id()}, e.actor)
    assert acquired['status'] == 'ok', acquired
    handle = acquired['data']['handle']
    result = anyio.run(transport._single_result, 'browser_turn',
        {'action': 'command', 'handle': handle, 'command': ['observe', 'overview'], 'idempotency_key': new_id()}, e.actor)
    assert result['status'] == 'ok', result
    assert len(e.calls) == 1


def test_browser_handle_migration_preserves_existing_database(tmp_path, monkeypatch):
    migrations = Database.migrations()
    with monkeypatch.context() as m:
        m.setattr(Database, 'migrations', staticmethod(lambda: migrations[:-1]))
        database = Database(tmp_path / 'upgrade.sqlite3').initialize()
    database.initialize()
    with database.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM browser_handles').fetchone()[0] == 0
        assert uow.connection.execute('PRAGMA user_version').fetchone()[0] == migrations[-1][0]


def test_resumed_worker_can_only_reconcile_old_handle_not_execute(env, monkeypatch):
    e = env
    actor = worker(e)
    handle = acquire(e, actor)['handle']
    action_id = new_id()
    def uncertain(*args, **kwargs):
        raise TimeoutError('Browser response was lost')
    monkeypatch.setattr(browser, 'run', uncertain)
    result = command(e, handle, actor, action_id=action_id)
    assert result['outcome'] == 'uncertain'
    # Simulate the persisted generation boundary after a dispatcher recovery.
    # Existing dispatcher tests cover the recovery transition itself.
    with e.database.transaction() as uow:
        uow.connection.execute('UPDATE operational_assignments SET execution_generation=execution_generation+1 WHERE id=?',
                               (actor.delegation_assignment_id,))
    current = replace(actor, delegation_generation=actor.delegation_generation + 1,
                      credential_id=new_id(), registration_id=new_id(), delegation_binding_id=new_id())
    for action in ('command', 'renew', 'release', 'checkpoint', 'artifact'):
        with pytest.raises(DomainError):
            e.access.tool(current, action, {'handle': handle, 'action_id': action_id, 'command': ['observe', 'overview']})
    other = worker(e, 'unrelated')
    with pytest.raises(DomainError):
        e.access.tool(other, 'reconcile', {'handle': handle, 'action_id': action_id})
    settled = e.access.tool(current, 'reconcile', {'handle': handle, 'action_id': action_id,
        'pending_outcome': {'result': 'Observed completion; do not repeat'}})
    assert settled['action']['state'] == 'reconciled'
    assert settled['action']['outcome']['result'] == 'Observed completion; do not repeat'
    with pytest.raises(DomainError):
        command(e, handle, current)
    fresh = acquire(e, current)
    assert fresh['handle'] != handle and fresh['status'] == 'owning'


def test_lease_replaced_after_handle_lookup_is_still_fenced(env, monkeypatch):
    e = env
    actor = worker(e)
    handle = acquire(e, actor)['handle']
    original = e.access.worker
    def replace_before_execute(actor, action, body):
        with e.database.transaction() as uow:
            uow.connection.execute('UPDATE browser_turns SET lease_id=? WHERE id=?', (new_id(), body['turn_id']))
        return original(actor, action, body)
    monkeypatch.setattr(e.access, 'worker', replace_before_execute)
    with pytest.raises(DomainError, match='lease is no longer current'):
        command(e, handle, actor)
    assert not e.calls


def test_routine_command_receipt_omits_browser_bookkeeping(env):
    e = env
    actor = worker(e)
    handle = acquire(e, actor)['handle']
    action_id = new_id()
    result = command(e, handle, actor, action_id=action_id)
    assert result == {'action': {'action_id': action_id, 'state': 'succeeded'},
                      'exit_code': 0, 'stdout': '{"ok":true}'}
    checkpoint = e.access.tool(actor, 'checkpoint', {'handle': handle, 'checkpoint': {'tab': 't3'}})
    assert checkpoint == {'status': 'owning'}
    assert e.access.tool(actor, 'status', {'handle': handle})['checkpoint'] == {'tab': 't3'}
    renewed = e.access.tool(actor, 'renew', {'handle': handle})
    assert set(renewed) == {'status', 'lease_until'}
    assert e.access.tool(actor, 'release', {'handle': handle}) == {'status': 'released'}


def test_unpublished_role_keeps_legacy_browser_flow_while_handles_opt_in(env):
    e = env
    actor = worker(e)
    legacy = e.access.tool(actor, 'acquire', {})
    assert legacy['status'] == 'owning' and 'turn_token' in legacy
    args = {'turn_id': legacy['turn']['id'],
            'lease_id': legacy['turn']['lease_id'], 'turn_token': legacy['turn_token']}
    result = e.access.tool(actor, 'command', {**args, 'command': ['observe', 'overview'], 'action_id': new_id()})
    assert result['exit_code'] == 0
    assert e.access.tool(actor, 'release', args)['turn']['state'] == 'released'
    compact = acquire(e, actor)
    assert 'handle' in compact and 'turn_token' not in compact and 'turn' not in compact
    handle = compact['handle']
    assert command(e, handle, actor)['action']['state'] == 'succeeded'
    e.access.tool(actor, 'release', {'handle': handle})
    with pytest.raises(DomainError):
        e.access.tool(actor, 'command', {'handle': handle, 'protocol': 'legacy',
            'command': ['observe', 'overview'], 'action_id': new_id()})
    assert len(e.calls) == 2
