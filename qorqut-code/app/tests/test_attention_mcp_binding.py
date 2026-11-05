"""Real authenticated MCP invocation over a synthetic assembled host."""
import json
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from mcp import types
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from starlette.requests import Request
from starlette.datastructures import Headers

from attention_runtime_test_support import host_overlay
host_overlay()

from bazarlyk.bootstrap.preparation import prepare_agent
from bazarlyk.transport.auth import Authentication
from bazarlyk.transport.mcp import MCPTransport, attention_binding
from host_foundation_support import (build_instance, create_registered_global, owner_global_context,
                                     register_sources, select_coordinator, set_agent_overrides)
from qorqut_operations.contracts import DomainError, InvocationOptions, Principal, new_id, plain


@pytest.mark.parametrize('generation', ['', '0', '-1', '01', '1e0', ' 1', '١', str(2**63)])
def test_native_binding_rejects_malformed_generations(generation):
    headers = Headers(raw=[(b'x-qorqut-native-session', new_id().encode()),
                           (b'x-qorqut-binding-generation', generation.encode())])
    with pytest.raises(DomainError):
        attention_binding(headers)


def test_native_binding_rejects_duplicate_headers():
    session = new_id().encode()
    headers = Headers(raw=[(b'x-qorqut-native-session', session), (b'x-qorqut-native-session', session),
                           (b'x-qorqut-binding-generation', b'1')])
    with pytest.raises(DomainError):
        attention_binding(headers)


def test_authenticated_attention_operations_use_exact_native_binding(tmp_path):
    host, _ = build_instance(tmp_path)
    try:
        register_sources(host, host.business, 'first')
        agent = select_coordinator(host, create_registered_global(host, 'ordubashy'))
        operations = ('attention_get', 'attention_add', 'attention_remove', 'attention_focus_set',
                      'attention_focus_clear', 'attention_workflow_update', 'tasks_get', 'inbox_list', 'operations_batch')
        set_agent_overrides(host, agent, {name: 'allow' for name in operations}, (host.business,))
        principal = Principal(agent, 'agent')
        prepared = prepare_agent(host.runtime.resolver, principal, endpoint='http://127.0.0.1:9988',
                                 versions={'codex': 'codex-cli 0.156.1', 'claude-code': '2.1.263 (Claude Code)'})
        token = (Path(prepared['workspace']) / '.bazarlyk/credential').read_text().strip()
        auth = Authentication(host.database, host.paths)
        actor = auth.agent('Bearer ' + token)
        session = new_id()
        binding = {'agent_id': agent, 'registration_id': prepared['registration_id'], 'workspace': prepared['workspace'],
                   'session_id': session, 'runtime': 'codex', 'pid': 123, 'started': '456',
                   'owner': '123:456', 'execution_target': 'local', 'event': 'startup'}
        ctx = host.runtime.resolver.invocation_context(principal)
        host.runtime.inbox.start_session(ctx, actor.registration_id, session, 'startup', binding=binding)
        owner = owner_global_context(host)
        made = host.runtime.application.invoke(owner, 'tasks_create', {'scope': 'global', 'title': 'Synthetic work'},
                                               InvocationOptions(idempotency_key=new_id()))
        assert made.status == 'ok', made.to_dict()
        task = plain(made.data)['task']
        ref = {'type': 'task', 'id': task['id'], 'business_id': None}
        transport = MCPTransport(host.runtime, auth, [])

        def call(name, arguments, *, headers=None, selected=actor, meta=None):
            entries = [(b'authorization', ('Bearer ' + token).encode())]
            for name_, value in (headers or {}).items():
                entries.append((name_.lower().encode(), value.encode()))
            request = Request({'type': 'http', 'method': 'POST', 'path': '/mcp', 'headers': entries,
                               'query_string': b'', 'scheme': 'http', 'server': ('test', 80),
                               'client': ('test', 1), 'root_path': '', 'app': None})
            request.scope['bazarlyk.auth'] = selected
            context = RequestContext('test', types.RequestParams.Meta(**meta) if meta else None, None, None, request=request)
            message = types.CallToolRequest(params=types.CallToolRequestParams(name=name, arguments=arguments))
            async def invoke():
                current = request_ctx.set(context)
                try:
                    return await transport.server.request_handlers[types.CallToolRequest](message)
                finally:
                    request_ctx.reset(current)
            result = anyio.run(invoke).root
            return json.loads(result.content[0].text)

        headers = {'X-Qorqut-Native-Session': session, 'X-Qorqut-Binding-Generation': '1'}
        assert call('attention_get', {'scope': 'global'})['status'] == 'error'
        for invalid in ({'X-Qorqut-Native-Session': session},
                        {**headers, 'X-Qorqut-Binding-Generation': 'bad'},
                        {**headers, 'X-Qorqut-Binding-Generation': '2'},
                        {**headers, 'X-Qorqut-Native-Session': new_id()}):
            assert call('attention_get', {'scope': 'global'}, headers=invalid)['status'] == 'error'
        assert call('attention_get', {'scope': 'global'}, headers=headers)['status'] == 'ok'
        shared = {'X-Qorqut-Binding-Reason': 'shared_app_server_without_thread_provenance'}
        meta = {'threadId': session, 'sessionId': new_id()}
        assert call('attention_get', {}, headers=shared, meta=meta)['status'] == 'ok'
        for invalid_meta in (None, {'threadId': new_id()}, {'threadId': 'invalid'}, {'threadId': 42}):
            assert call('attention_get', {}, headers=shared, meta=invalid_meta)['status'] == 'error'
        assert call('attention_get', {}, meta=meta)['status'] == 'error'
        assert call('attention_get', {}, headers={'X-Qorqut-Binding-Reason': 'native_process_binding_lost'}, meta=meta)['status'] == 'error'
        assert call('attention_get', {}, headers={**shared, 'X-Qorqut-Native-Session': session}, meta=meta)['status'] == 'error'
        batch = call('operations_batch', {'operations': [{'id': 'board', 'operation': 'attention_get', 'arguments': {}}]},
                     headers=shared, meta=meta)
        assert batch['status'] == 'ok', batch
        added = call('attention_add', {'scope': 'global', 'task': ref, 'idempotency_key': new_id()}, headers=shared, meta=meta)
        assert added['status'] == 'ok', added
        focused = call('attention_focus_set', {'scope': 'global', 'task': ref, 'focus': ref, 'intent': 'Find evidence',
            'expected_revision': added['data']['revision'], 'idempotency_key': new_id()}, headers=shared, meta=meta)
        assert focused['status'] == 'ok', focused
        wrong = replace(actor, registration_id=new_id())
        assert call('attention_get', {'scope': 'global'}, headers=headers, selected=wrong)['status'] == 'error'
        assert call('attention_get', {}, headers=shared, meta=meta, selected=wrong)['status'] == 'error'
        delegated = replace(actor, delegation_binding_id=new_id(), delegation_assignment_id=new_id(), delegation_generation=1)
        assert call('attention_get', {'scope': 'global'}, headers=headers, selected=delegated)['status'] == 'error'
        assert call('attention_get', {}, headers=shared, meta=meta, selected=delegated)['status'] == 'error'
        with host.database.transaction() as uow:
            uow.connection.execute('UPDATE native_session_owners SET desired_state=? WHERE agent_id=?', ('stopped', agent))
        assert call('attention_get', {}, headers=shared, meta=meta)['status'] == 'error'
    finally:
        host.runtime.release_instance()
        host.database.close()
