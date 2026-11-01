"""Agent-local stdio bridge for Qorqut's JSON Streamable HTTP MCP endpoint.

The CLI's pipe survives service releases. Only discovery/read requests, explicit
expired-session rejections, and requests that never connected may be replayed.
No credentials or provider exception strings are written to stdout/stderr.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import threading
import random

import httpx

from bazarlyk.contracts import DomainError
from .client import auth_headers, load_registration

# Keep the original request alive across an ordinary 15–17s app restart.
# This is a total deadline, including discovery, backoff and execution; failed
# reads must not repeatedly extend it. Healthy requests still return immediately.
RETRY_WINDOW = 30
HTTP_REQUEST_TIMEOUT = 15
MAX_MESSAGE = 2 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
SAFE_METHODS = frozenset({'ping', 'tools/list', 'resources/list', 'resources/templates/list',
                          'resources/read', 'prompts/list', 'prompts/get'})


class Unavailable(Exception):
    def __init__(self, *, undispatched=False):
        self.undispatched = undispatched


class ExpiredSession(Unavailable):
    pass


class ReconnectBridge:
    def __init__(self, registration_path, *, client=None, clock=time.monotonic, sleep=time.sleep, emit=None, background=False):
        self.registration_path = Path(registration_path)
        self.client = client or httpx.Client(follow_redirects=False, trust_env=False)
        self.clock, self.sleep = clock, sleep
        self.emit = emit or (lambda message: None)
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.background = background
        self.repair = None
        self.session = None
        self.protocol = None
        self.initialize = None
        self.read_tools = set()
        self.discovered = False
        self.initialized_notice = False
        self.listing_tools = set()
        from .bridge_binding import BridgeBinding
        self.binding = BridgeBinding(load_registration(self.registration_path))

    def post(self, message, deadline):
        registration = load_registration(self.registration_path)
        headers = {**auth_headers(registration), **self.binding.headers(), 'Accept': 'application/json, text/event-stream'}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        if self.protocol:
            headers['MCP-Protocol-Version'] = self.protocol
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise Unavailable(undispatched=True)
        read_timeout = min(HTTP_REQUEST_TIMEOUT, remaining)
        response_deadline = self.clock() + read_timeout
        timeout = httpx.Timeout(read_timeout, connect=min(HTTP_REQUEST_TIMEOUT, remaining),
                                pool=min(HTTP_REQUEST_TIMEOUT, remaining))
        try:
            with self.client.stream('POST', registration['service_url'] + '/mcp', json=message,
                                    headers=headers, timeout=timeout) as response:
                if response.status_code in (401, 403):
                    raise DomainError('forbidden', 'MCP access was refused; check agent preparation and grants')
                if response.status_code == 404 and self.session:
                    raise ExpiredSession(undispatched=True)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise Unavailable()
                if response.status_code >= 400:
                    raise DomainError('mcp_protocol_error', 'The MCP endpoint refused this request')
                if response.status_code in (202, 204):
                    return None
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_RESPONSE:
                        raise DomainError('mcp_protocol_error', 'MCP response exceeds its size limit')
                    if self.clock() >= response_deadline:
                        raise Unavailable()
                try:
                    value = json.loads(payload)
                except ValueError:
                    raise DomainError('mcp_protocol_error', 'MCP returned invalid JSON') from None
                if not isinstance(value, dict) or value.get('jsonrpc') != '2.0' or value.get('id') != message.get('id'):
                    raise DomainError('mcp_protocol_error', 'MCP returned an invalid response')
                if message.get('method') == 'initialize' and 'result' in value:
                    self.session = response.headers.get('mcp-session-id')
                    self.protocol = value['result'].get('protocolVersion')
                    value['result'].setdefault('capabilities', {}).setdefault('tools', {})['listChanged'] = True
                return value
        except httpx.ConnectError:
            raise Unavailable(undispatched=True) from None
        except httpx.ConnectTimeout:
            raise Unavailable(undispatched=True) from None
        except httpx.PoolTimeout:
            raise Unavailable(undispatched=True) from None
        except httpx.HTTPError:
            raise Unavailable() from None

    def reconnect(self, deadline):
        """Initialize, acknowledge, then refresh discovery before resuming requests."""
        if self.initialize is None:
            raise DomainError('mcp_not_initialized', 'Initialize MCP before calling tools')
        self.reset_connection()
        reads = set()
        try:
            response = self.post(self.initialize, deadline)
            if not response or 'result' not in response:
                raise DomainError('mcp_initialization_failed', 'MCP initialization was refused')
            self.post({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, deadline)
            cursor = None
            while True:
                listing = self.post({'jsonrpc': '2.0', 'id': 'qorqut-reconnect-tools', 'method': 'tools/list',
                                     'params': {'cursor': cursor} if cursor else {}}, deadline)
                if not listing or 'result' not in listing:
                    raise DomainError('mcp_discovery_failed', 'MCP tool discovery was refused')
                reads.update(self.read_names(listing))
                cursor = listing['result'].get('nextCursor')
                if not cursor:break
        except BaseException:
            # Tentative initialization/discovery is not a repaired connection.
            self.reset_connection()
            raise
        self.read_tools = reads
        self.initialized_notice = self.discovered = True
        self.emit({'jsonrpc': '2.0', 'method': 'notifications/tools/list_changed'})
        self.observe_migration()

    def observe_migration(self):
        # MCP activity does not prove which cached native configuration slot ran.
        return False

    def reset_connection(self):
        self.session = self.protocol = None
        self.read_tools.clear()
        self.listing_tools.clear()
        self.initialized_notice = self.discovered = False

    @staticmethod
    def read_names(listing):
        return {tool['name'] for tool in listing['result'].get('tools', [])
                if tool.get('annotations', {}).get('readOnlyHint') is True}

    def schedule_repair(self):
        if not self.background or self.initialize is None or self.stop.is_set(): return
        if self.repair is not None and self.repair.is_alive(): return
        def repair():
            failures = 0
            while not self.stop.is_set():
                delay = (.25,.5,1,2,5)[min(failures,4)] * random.uniform(.9,1.1)
                if self.stop.wait(delay): return
                try:
                    with self.lock:
                        if self.discovered: return
                        self.reconnect(self.clock()+HTTP_REQUEST_TIMEOUT)
                    return
                except Unavailable: failures += 1
                except (DomainError, OSError, ValueError): return  # Real denial is preserved.
        self.repair = threading.Thread(target=repair, name='qorqut-mcp-repair', daemon=True)
        self.repair.start()

    def close(self):
        self.stop.set()
        if self.repair is not None: self.repair.join(timeout=1)
        self.client.close()

    def handle(self, message):
        deadline = self.clock()+RETRY_WINDOW
        if not self.lock.acquire(timeout=RETRY_WINDOW):
            return self.failure(message, 'MCP recovery is in progress; this request was not dispatched')
        try:
            if self.clock() >= deadline:
                return self.failure(message, 'MCP recovery is in progress; this request was not dispatched')
            return self._handle(message, deadline)
        finally: self.lock.release()

    def _handle(self, message, deadline):
        method = message.get('method')
        if not method:  # No server-initiated requests are used by this endpoint.
            return None
        if not isinstance(method, str) or not isinstance(message.get('params', {}), dict):
            return self.failure(message, 'Invalid MCP request parameters')
        if method == 'initialize':
            self.initialize = message
            self.reset_connection()
        safe = method in SAFE_METHODS or (method == 'tools/call' and
                    message.get('params', {}).get('name') in self.read_tools)
        needs_reconnect = method != 'initialize' and self.session is None and self.initialize is not None
        immediate_reconnect = False
        uncertain = False
        attempt = 0
        while True:
            try:
                if needs_reconnect:
                    self.reconnect(deadline)
                    needs_reconnect = False
                    if uncertain:
                        return self.failure(message, 'MCP reconnected, but the previous tool outcome is unknown. '
                            'Do not send a new mutation. Reconcile its result or retry the exact original request '
                            'with its original idempotency key.')
                    # Discovery may have been empty when this request arrived.
                    # Reclassify before dispatch, never after an uncertain write.
                    safe = method in SAFE_METHODS or (method == 'tools/call' and
                        message.get('params', {}).get('name') in self.read_tools)
                response = self.post(message, deadline)
                if method == 'notifications/initialized':self.initialized_notice = True
                if method == 'tools/list' and response and 'result' in response:
                    if not message.get('params', {}).get('cursor'):self.listing_tools.clear()
                    self.listing_tools.update(self.read_names(response))
                    if not response['result'].get('nextCursor'):
                        self.read_tools = set(self.listing_tools)
                        self.discovered = self.initialized_notice
                return response
            except Unavailable as error:
                # A 404 is the MCP protocol's pre-dispatch expired-session rejection.
                # A lost reply to a write is never proof that it did not execute.
                if not needs_reconnect:
                    uncertain = not safe and not error.undispatched and method == 'tools/call'
                needs_reconnect = method != 'initialize'
                if isinstance(error, ExpiredSession) and not immediate_reconnect:
                    immediate_reconnect = True
                    continue  # A healthy replacement service need not wait 30s.
                attempt += 1
                self.sleep(min((.25,.5,1,2,5)[min(attempt-1,4)], max(0,deadline-self.clock())))
                if self.clock() >= deadline:
                    self.reset_connection()
                    self.schedule_repair()
                    return self.failure(message, 'MCP service temporarily unavailable within this request budget. The local connection '
                        'remains open; its helper reconnects in the background.' + (' The previous tool outcome is unknown; '
                        'reconcile it before retrying with the original idempotency key.' if uncertain else ''))
            except (DomainError, OSError, ValueError):
                return self.failure(message, 'MCP connection or preparation was refused. Check agent preparation '
                    'and access; no tool request was automatically replayed.' +
                    (' A write outcome may be unknown; reconcile it before retrying with the original idempotency key.'
                     if method == 'tools/call' and not safe else ''))

    @staticmethod
    def failure(message, text):
        if 'id' not in message:
            return None
        if message.get('method') == 'tools/call':
            return {'jsonrpc': '2.0', 'id': message['id'], 'result': {
                'isError': True, 'content': [{'type': 'text', 'text': text}]}}
        return {'jsonrpc': '2.0', 'id': message['id'], 'error': {'code': -32000, 'message': text}}


def run(registration_path, *, stream=sys.stdin.buffer, output=sys.stdout, client=None):
    output_lock = threading.Lock()
    def emit(message):
        with output_lock:
            output.write(json.dumps(message, separators=(',', ':')) + '\n')
            output.flush()
    bridge = ReconnectBridge(registration_path, client=client, emit=emit, background=True)
    try:
        while raw := stream.readline(MAX_MESSAGE + 1):
            if len(raw) > MAX_MESSAGE:
                return 1
            try:
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    raise ValueError
            except ValueError:
                emit({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid MCP request'}})
                continue
            response = bridge.handle(message)
            if response is not None:
                emit(response)
    finally:
        bridge.close()
    return 0


def main():
    parser = argparse.ArgumentParser(description='Prepared Qorqut MCP connection with release recovery')
    parser.add_argument('--registration', required=True, type=Path)
    return run(parser.parse_args().registration)


if __name__ == '__main__':
    raise SystemExit(main())
