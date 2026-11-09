import io
import json
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from qorqut.bootstrap import mcp_reconnect as bridge_module


def call(i, name='read'):
    return {'jsonrpc': '2.0', 'id': i, 'method': 'tools/call', 'params': {'name': name}}


class BridgeConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(bridge_module, 'load_registration', return_value={'service_url': 'http://test'}),
                        patch.object(bridge_module, 'auth_headers', return_value={}),
                        patch('qorqut.bootstrap.bridge_binding.BridgeBinding')]
        mocks = [p.start() for p in self.patches]
        mocks[-1].return_value.headers.return_value = {}
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def test_overlap_bound_order_and_eof(self):
        gate = threading.Event()
        four = threading.Event()
        fast = threading.Event()
        active = 0
        maximum = 0
        seen = []
        guard = threading.Lock()
        def handler(request):
            nonlocal active, maximum
            m = json.loads(request.content)
            method = m['method']
            headers = {}
            if method == 'initialize':
                result = {'protocolVersion': '1', 'capabilities': {}}
                headers = {'mcp-session-id': 's'}
            elif method == 'tools/list':
                result = {'tools': [{'name': 'read', 'annotations': {'readOnlyHint': True}}]}
            elif method == 'tools/call':
                name = m['params']['name']
                with guard:
                    seen.append(m['id'])
                    if name == 'read':
                        active += 1
                        maximum = max(maximum, active)
                        if active == 4: four.set()
                if name == 'read' and m['id'] != 2:
                    gate.wait(5)
                if m['id'] == 2: fast.set()
                with guard:
                    if name == 'read': active -= 1
                result = {}
            else:
                return httpx.Response(202)
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': m.get('id'), 'result': result}, headers=headers)
        messages = [{'jsonrpc':'2.0','id':0,'method':'initialize'},
                    {'jsonrpc':'2.0','method':'notifications/initialized'},
                    {'jsonrpc':'2.0','id':1,'method':'tools/list'}]
        messages += [call(i) for i in range(2, 8)] + [call(8, 'write'), call(9)]
        output = io.StringIO()
        client = httpx.Client(transport=httpx.MockTransport(handler))
        thread = threading.Thread(target=bridge_module.run, args=('unused',), kwargs={
            'stream': io.BytesIO(b''.join(json.dumps(m).encode()+b'\n' for m in messages)),
            'output': output, 'client': client})
        thread.start()
        self.assertTrue(fast.wait(2))
        self.assertTrue(four.wait(2))
        self.assertIn('"id":2', output.getvalue())
        self.assertNotIn(8, seen)
        self.assertTrue(thread.is_alive())
        gate.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(maximum, 4)
        self.assertLess(seen.index(8), seen.index(9))
        self.assertEqual(set(seen), set(range(2, 10)))
        self.assertTrue(client.is_closed)

    def test_expiry_single_repair_and_reclassification(self):
        both = threading.Barrier(2)
        requests = []
        lock = threading.Lock()
        def handler(request):
            m = json.loads(request.content)
            method = m['method']
            session = request.headers.get('mcp-session-id')
            with lock: requests.append((method, session))
            if method == 'tools/call' and session == 'old':
                both.wait(2)
                return httpx.Response(404)
            if method == 'initialize':
                return httpx.Response(200, headers={'mcp-session-id':'new'}, json={
                    'jsonrpc':'2.0','id':m['id'],'result':{'protocolVersion':'1'}})
            if method == 'notifications/initialized': return httpx.Response(202)
            if method == 'tools/list':
                return httpx.Response(200, json={'jsonrpc':'2.0','id':m['id'],'result':{'tools':[]}})
            self.fail('reclassified read was dispatched')
        client = httpx.Client(transport=httpx.MockTransport(handler))
        b = bridge_module.ReconnectBridge('unused', client=client)
        b.session, b.protocol, b.discovered, b.read_tools = 'old', '1', True, {'read'}
        b.initialize = {'jsonrpc':'2.0','id':0,'method':'initialize'}
        responses = []
        threads = [threading.Thread(target=lambda i=i: responses.append(b.handle_read(call(i), time.monotonic()+5))) for i in (1,2)]
        for t in threads: t.start()
        for t in threads: t.join(5)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(sum(m == 'initialize' for m, _ in requests), 1)
        self.assertEqual(b.session, 'new')
        self.assertTrue(all(r['result']['isError'] for r in responses))
        b.close()

    def test_worker_and_output_errors_propagate_and_close(self):
        prefix = [{'jsonrpc': '2.0', 'id': 0, 'method': 'initialize'},
                  {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                  {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}]
        for failure, count in [('worker', 1), ('output', 5)]:
            with self.subTest(failure=failure):
                def handler(request):
                    m = json.loads(request.content)
                    method = m['method']
                    if method == 'notifications/initialized': return httpx.Response(202)
                    if method == 'tools/call' and failure == 'worker':
                        raise RuntimeError('worker failed')
                    if method == 'initialize':
                        result = {'protocolVersion': '1'}
                    elif method == 'tools/list':
                        result = {'tools': [{'name': 'read', 'annotations': {'readOnlyHint': True}}]}
                    else:
                        result = {}
                    return httpx.Response(200, headers={'mcp-session-id': 's'},
                                          json={'jsonrpc': '2.0', 'id': m['id'], 'result': result})
                class Output(io.StringIO):
                    def write(self, text):
                        if failure == 'output' and '"id":2' in text:
                            raise RuntimeError('output failed')
                        return super().write(text)
                client = httpx.Client(transport=httpx.MockTransport(handler))
                messages = prefix + [call(i) for i in range(2, 2 + count)]
                stream = io.BytesIO(b''.join(json.dumps(m).encode() + b'\n' for m in messages))
                with self.assertRaisesRegex(RuntimeError, failure + ' failed'):
                    bridge_module.run('unused', stream=stream, output=Output(), client=client)
                self.assertTrue(client.is_closed)
                self.assertFalse(any(t.name.startswith('qorqut-mcp-read') for t in threading.enumerate()))

    def test_late_success_does_not_replace_repaired_session(self):
        started, release = threading.Event(), threading.Event()
        def handler(request):
            m = json.loads(request.content)
            method = m['method']
            session = request.headers.get('mcp-session-id')
            if method == 'tools/call' and session == 'old':
                if m['id'] == 1:
                    started.set()
                    self.assertTrue(release.wait(3))
                    return httpx.Response(200, headers={'mcp-session-id': 'old'}, json={
                        'jsonrpc': '2.0', 'id': 1, 'result': {'value': 'old response'}})
                return httpx.Response(404)
            if method == 'initialize':
                return httpx.Response(200, headers={'mcp-session-id': 'new'}, json={
                    'jsonrpc': '2.0', 'id': m['id'], 'result': {'protocolVersion': 'new-version'}})
            if method == 'notifications/initialized': return httpx.Response(202)
            if method == 'tools/list':
                result = {'tools': [{'name': 'read', 'annotations': {'readOnlyHint': True}}]}
            else:
                result = {'value': 'replacement response'}
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': m['id'], 'result': result})
        b = bridge_module.ReconnectBridge('unused', client=httpx.Client(transport=httpx.MockTransport(handler)))
        b.session, b.protocol, b.discovered, b.read_tools = 'old', 'old-version', True, {'read'}
        b.initialize = {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize'}
        responses = []
        t = threading.Thread(target=lambda: responses.append(b.handle_read(call(1), time.monotonic()+5)))
        t.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertEqual(b.handle_read(call(2), time.monotonic()+5)['result']['value'], 'replacement response')
            self.assertEqual((b.session, b.protocol), ('new', 'new-version'))
        finally:
            release.set()
            t.join(5)
            b.close()
        self.assertFalse(t.is_alive())
        self.assertEqual(responses[0]['result']['value'], 'old response')
        self.assertEqual((b.session, b.protocol), ('new', 'new-version'))

    def test_deadline_and_uncertain_write(self):
        requests = []
        def handler(request):
            m = json.loads(request.content)
            requests.append(m['method'])
            if m['method'] == 'tools/call':
                raise httpx.ReadError('lost reply')
            if m['method'] == 'initialize':
                return httpx.Response(200, headers={'mcp-session-id': 'replacement'}, json={
                    'jsonrpc': '2.0', 'id': m['id'], 'result': {'protocolVersion': '1'}})
            if m['method'] == 'notifications/initialized': return httpx.Response(202)
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': m['id'], 'result': {'tools': []}})
        b = bridge_module.ReconnectBridge('unused', client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)
        b.session, b.discovered, b.read_tools = 'old', True, {'read'}
        b.initialize = {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize'}
        result = b.handle(call(1, 'write'), time.monotonic()+2)
        self.assertTrue(result['result']['isError'])
        self.assertEqual(requests.count('tools/call'), 1)
        before = len(requests)
        self.assertTrue(b.handle_read(call(2), time.monotonic()-1)['result']['isError'])
        self.assertEqual(len(requests), before)
        b.close()

    def test_auth_denial_does_not_retry(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(403)
        b = bridge_module.ReconnectBridge('unused', client=httpx.Client(transport=httpx.MockTransport(handler)))
        b.session, b.discovered, b.read_tools = 's', True, {'read'}
        self.assertTrue(b.handle_read(call(1), time.monotonic()+2)['result']['isError'])
        self.assertEqual(len(seen), 1)
        b.close()

if __name__ == '__main__': unittest.main()
