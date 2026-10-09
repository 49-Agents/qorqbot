"""Real installed/source core HTTP and WS against one disposable protocol peer."""

import json
import time
import unittest
from uuid import uuid4

import httpx
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from qorqut_core.app import Application
from qorqut_core.contracts import Invocation
from transports.support import Installation
from .peer import Peer


def wait_for(function):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(.04)
    raise AssertionError('Focused browser condition did not settle')


class Transport(unittest.TestCase):
    def test_handoff_attribution_exact_retry_ready_handshake_binary_relay_and_restart(self):
        f = Installation()
        self.addCleanup(f.close)
        app = Application.open(f.settings)
        try:
            host = app.invoke('agents_list', Invocation(actor_id=f.owner.actor_id)).data['items'][0]
            app.jobs.run_next(worker_id=uuid4())
        finally:
            app.close()
        peer = Peer(host['id'], f.initial['data']['recovery_epoch'])
        self.addCleanup(peer.close)
        f.start()
        self.assertTrue(f.post('browsers_attach', {'agent_id': host['id'], 'service_url': peer.url,
            'idempotency_key': 'attach'}).json()['ok'])
        # A genuine registered requesting agent, distinct from the target/owner.
        requester = f.post('agents_create', {'display_name': 'Requesting agent', 'idempotency_key': 'requester'}).json()['data']['agent']
        payload = {'protocol': 1, 'actor_id': requester['actor_id'], 'target_agent_id': host['id'],
            'prompt': 'Type in the ordinary local form', 'idempotency_key': 'handoff', 'observed_tab': None}
        with httpx.Client(trust_env=False, timeout=5) as http:
            response = http.post(f.url + '/api/v1/browser/handoff', json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            receipt = response.json()
            self.assertEqual(set(receipt), {'protocol', 'actor_id', 'target_agent_id', 'idempotency_key', 'request_id',
                'conversation_id', 'surface_id', 'state', 'browser_binding'})
            self.assertEqual(receipt['actor_id'], requester['actor_id'])
            self.assertEqual(http.post(f.url + '/api/v1/browser/handoff', json=payload).json(), receipt)
            conflict = http.post(f.url + '/api/v1/browser/handoff', json={**payload, 'prompt': 'different'})
            self.assertEqual(conflict.status_code, 409)
        def current():
            return f.post('input_requests_list', {'agent_id': host['id']}).json()['data']['items'][0]
        ready = wait_for(lambda: v if (v := current())['state'] == 'ready' else None)
        url = f.url.replace('http:', 'ws:') + '/api/v1/browser/' + receipt['request_id'] + '/stream'
        # Wrong ready binding never upgrades the UI connection or leaks RFB.
        peer.bad_ready = True
        with self.assertRaises(InvalidStatus):
            connect(url, origin=f.url, subprotocols=['binary'], proxy=None, open_timeout=3)
        peer.bad_ready = False
        wait_for(lambda: peer.viewers == 0)
        with connect(url, origin=f.url, subprotocols=['binary'], proxy=None, open_timeout=3) as ws:
            self.assertEqual(ws.recv(timeout=3), b'RFB 003.008\n')
            self.assertEqual(ws.subprotocol, 'binary')
            data = b'\x05\x01\x00\x08\x00\x0a'
            ws.send(data)
            self.assertEqual(ws.recv(timeout=3), data)
            self.assertEqual(peer.handshakes[-1], ({'protocol': 1, 'binding': ready['browser_binding']}, str(f.owner.actor_id)))
            with self.assertRaises(InvalidStatus):
                connect(url, origin=f.url, proxy=None, open_timeout=3)
        wait_for(lambda: peer.viewers == 0)
        self.assertEqual(current()['state'], 'ready')
        self.assertEqual(peer.control.mode, 'human')
        # Closing core's viewers never releases the human hold or kills peer.
        with connect(url, origin=f.url, proxy=None, open_timeout=3) as ws:
            self.assertEqual(ws.recv(timeout=3), b'RFB 003.008\n')
            f.stop()
            with self.assertRaises(ConnectionClosed):
                ws.recv(timeout=3)
        self.assertTrue(peer.thread.is_alive())
        self.assertEqual(peer.control.mode, 'human')
        f.start()
        self.assertEqual(current()['state'], 'ready')
        with connect(url, origin=f.url, proxy=None, open_timeout=3) as ws:
            self.assertEqual(ws.recv(timeout=3), b'RFB 003.008\n')
            done = f.post('input_requests_complete', {'request_id': receipt['request_id'], 'expected_revision': ready['revision'],
                'idempotency_key': 'done', 'note': 'Local form action finished'}).json()
            self.assertTrue(done['ok'], done)
            with self.assertRaises(ConnectionClosed):
                ws.recv(timeout=3)
        wait_for(lambda: not current()['control_pending'])
        self.assertEqual(peer.control.mode, 'automation')
        with self.assertRaises(InvalidStatus):
            connect(url, origin=f.url, proxy=None, open_timeout=3)
        messages = f.post('messages_list', {'agent_id': requester['id']}).json()['data']['items']
        self.assertEqual(len(messages), 1)
        self.assertIn('completed', messages[0]['message']['body'])
        self.assertEqual(messages[0]['message']['sender_actor_id'], str(f.owner.actor_id))
        card = f.post('conversation_list', {'agent_id': host['id']}).json()['data']['items'][0]
        self.assertEqual(card['browser_request']['state'], 'completed')
        self.assertEqual(card['source']['request_id'], receipt['request_id'])
