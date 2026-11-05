"""Release recovery uses disposable HTTP peers and fake time, never live agents."""
import io
import json
from pathlib import Path

import httpx
import pytest

from bazarlyk.bootstrap.mcp_reconnect import RETRY_WINDOW, ReconnectBridge, run


@pytest.fixture
def registration(tmp_path):
    folder = tmp_path / '.bazarlyk'
    folder.mkdir()
    credential = folder / 'credential'
    credential.write_text('x' * 40)
    credential.chmod(0o600)
    path = folder / 'registration.json'
    path.write_text(json.dumps({'schema_version': 1, 'registration_id': 'registration', 'agent_id': 'agent',
        'workspace': str(tmp_path), 'service_url': 'http://qorqut.test:1071', 'credential_file': '.bazarlyk/credential'}))
    return path


class Peer:
    def __init__(self):
        self.generation = 1
        self.requests = []
        self.calls = []
        self.offline = False
        self.refused = False
        self.lost = None

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((body, request.headers.get('mcp-session-id')))
        if self.refused:
            return httpx.Response(403)
        if self.offline:
            raise httpx.ConnectError('SECRET transport detail', request=request)
        method = body['method']
        if method == 'initialize':
            return httpx.Response(200, headers={'mcp-session-id': str(self.generation)}, json={
                'jsonrpc': '2.0', 'id': body['id'], 'result': {'protocolVersion': '2025-03-26',
                'capabilities': {'tools': {'listChanged': True}}, 'serverInfo': {'name': 'fake', 'version': '1'}}})
        if request.headers.get('mcp-session-id') != str(self.generation):
            return httpx.Response(404)
        if method.startswith('notifications/'):
            return httpx.Response(202)
        result = {}
        if method == 'tools/list':
            result = {'tools': [{'name': name, 'inputSchema': {'type': 'object'},
                'annotations': {'readOnlyHint': read}} for name, read in [('read', True), ('write', False)]]}
        if method == 'tools/call':
            name = body['params']['name']
            self.calls.append(name)
            if self.lost == name:
                self.lost = None
                self.generation += 1
                raise httpx.ReadTimeout('SECRET lost response', request=request)
            result = {'content': [{'type': 'text', 'text': 'ok'}]}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})


def make_bridge(registration):
    peer, now, sleeps, notifications = Peer(), [0.0], [], []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    bridge = ReconnectBridge(registration, client=httpx.Client(transport=httpx.MockTransport(peer)),
        clock=lambda: now[0], sleep=sleep, emit=notifications.append)
    init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26',
        'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}}
    assert 'result' in bridge.handle(init)
    bridge.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    bridge.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    return bridge, peer, now, sleeps, notifications


def call(name='write', identity=3):
    return {'jsonrpc': '2.0', 'id': identity, 'method': 'tools/call',
            'params': {'name': name, 'arguments': {'idempotency_key': 'original'}}}


def test_expired_session_recovers_before_call_and_refreshes_tools(registration):
    bridge, peer, now, sleeps, notifications = make_bridge(registration)
    peer.generation += 1
    result = bridge.handle(call())
    assert result['id'] == 3 and not result['result'].get('isError')
    assert peer.calls == ['write']
    assert [r[0]['method'] for r in peer.requests[-4:]] == [
        'initialize', 'notifications/initialized', 'tools/list', 'tools/call']
    assert bridge.session == '2' and not sleeps
    assert notifications == [{'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'}]


def test_restart_downtime_uses_fast_bounded_backoff(registration):
    bridge, peer, now, sleeps, _ = make_bridge(registration)
    peer.offline = True
    original_sleep = bridge.sleep
    def sleep(seconds):
        original_sleep(seconds)
        if now[0] >= .75:
            peer.offline = False
            peer.generation += 1
    bridge.sleep = sleep
    result = bridge.handle(call())
    assert not result['result'].get('isError')
    assert sleeps == [.25, .5] and peer.calls == ['write']
    assert peer.requests[-1][0]['params']['arguments']['idempotency_key'] == 'original'


def test_budgeted_failure_keeps_bridge_usable_for_later_recovery(registration):
    bridge, peer, now, sleeps, _ = make_bridge(registration)
    peer.offline = True
    result = bridge.handle(call())
    assert result['result']['isError'] and now[0] == RETRY_WINDOW
    assert sum(sleeps) == RETRY_WINDOW and max(sleeps) <= 5 and not peer.calls
    peer.offline = False
    peer.generation += 1
    assert not bridge.handle(call(identity=4))['result'].get('isError')
    assert peer.calls == ['write']


@pytest.mark.parametrize('name', ['read', 'write'])
def test_original_request_survives_release_length_outage(registration, name):
    bridge, peer, now, sleeps, notifications = make_bridge(registration)
    peer.offline = True
    original_sleep = bridge.sleep
    def sleep(seconds):
        original_sleep(seconds)
        if now[0] >= 17:
            peer.offline = False
            peer.generation += 1
    bridge.sleep = sleep
    result = bridge.handle(call(name, 'pending-through-release'))
    assert 17 <= now[0] < RETRY_WINDOW
    assert result['id'] == 'pending-through-release' and not result['result'].get('isError')
    assert peer.calls == [name]
    assert peer.requests[-1][0] == call(name, 'pending-through-release')
    assert len(notifications) == 1


def test_read_after_connection_reset_is_classified_from_refreshed_discovery(registration):
    bridge, peer, _, _, _ = make_bridge(registration)
    bridge.reset_connection()
    peer.lost = 'read'
    result = bridge.handle(call('read'))
    assert not result['result'].get('isError')
    assert peer.calls == ['read', 'read']


def test_repeated_lost_reads_cannot_extend_request_deadline(registration):
    bridge, peer, now, _, _ = make_bridge(registration)
    def repeated_timeout(request):
        body = json.loads(request.content)
        if body['method'] == 'tools/call':
            now[0] += min(10, request.extensions['timeout']['read'])
            peer.lost = 'read'
        return peer(request)
    bridge.client.close()
    bridge.client = httpx.Client(transport=httpx.MockTransport(repeated_timeout))
    result = bridge.handle(call('read'))
    assert result['result']['isError']
    assert now[0] == RETRY_WINDOW
    assert peer.calls == ['read'] * 3


@pytest.mark.parametrize('name,replayed', [('write', False), ('read', True)])
def test_lost_response_replays_only_a_known_read(registration, name, replayed):
    bridge, peer, _, _, _ = make_bridge(registration)
    peer.lost = name
    result = bridge.handle(call(name))
    assert peer.calls == [name] * (2 if replayed else 1)
    assert bool(result['result'].get('isError')) is not replayed
    if not replayed:
        assert 'outcome is unknown' in result['result']['content'][0]['text']
    assert 'SECRET' not in json.dumps(result)
    assert bridge.session == '2'


def test_auth_failure_does_not_retry_or_replace_identity(registration):
    bridge, peer, _, sleeps, _ = make_bridge(registration)
    peer.refused = True
    assert bridge.handle(call())['result']['isError']
    assert not sleeps and not peer.calls


@pytest.mark.parametrize('name', ['read', 'write'])
def test_server_error_after_dispatch_only_replays_reads(registration, name):
    bridge, peer, _, _, _ = make_bridge(registration)
    failed = [False]
    def server_error(request):
        response = peer(request)
        if json.loads(request.content)['method'] == 'tools/call' and not failed[0]:
            failed[0] = True
            return httpx.Response(503)
        return response
    bridge.client.close()
    bridge.client = httpx.Client(transport=httpx.MockTransport(server_error))
    result = bridge.handle(call(name))
    assert peer.calls == [name] * (2 if name == 'read' else 1)
    assert bool(result['result'].get('isError')) == (name == 'write')
    if name == 'write':
        assert 'outcome is unknown' in result['result']['content'][0]['text']


def test_stdio_preserves_rpc_ids_and_never_prints_credentials(registration):
    peer = Peer()
    source = io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
                       b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                       b'{"jsonrpc":"2.0","id":"tools","method":"tools/list"}\n')
    output = io.StringIO()
    assert run(registration, stream=source, output=output, client=httpx.Client(transport=httpx.MockTransport(peer))) == 0
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [message['id'] for message in messages] == [1, 'tools']
    assert 'x' * 40 not in output.getvalue()


def test_real_stdio_client_survives_remote_session_replacement(registration):
    """One real pipe completes an inbox read across a 17s service interruption."""
    import asyncio
    import os
    import sys
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    peer = Peer()
    offline_until = [0.0]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            if time.monotonic() < offline_until[0]:
                self.send_response(503)
                self.end_headers()
                return
            response = peer(httpx.Request('POST', 'http://localhost/mcp', headers=dict(self.headers), content=raw))
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.content)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    data = json.loads(registration.read_text())
    data['service_url'] = f'http://127.0.0.1:{server.server_port}'
    registration.write_text(json.dumps(data))

    async def exercise():
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'bazarlyk.bootstrap.mcp_reconnect', '--registration', str(registration)],
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert {tool.name for tool in (await session.list_tools()).tools} == {'read', 'write'}
                assert not (await session.call_tool('read', {})).isError
                peer.generation += 1  # same origin, replacement service has lost its old MCP sessions
                offline_until[0] = time.monotonic() + 17
                began = time.monotonic()
                assert not (await session.call_tool('read', {})).isError
                assert 17 <= time.monotonic() - began < RETRY_WINDOW
                assert not (await session.call_tool('write', {'idempotency_key': 'one-send'})).isError
                assert peer.calls == ['read', 'read', 'write']
                assert len([body for body, _ in peer.requests if body['method'] == 'initialize']) == 2
    try:
        asyncio.run(asyncio.wait_for(exercise(), timeout=40))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
