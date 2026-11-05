"""Explicit input to the already-running, process-bound Codex app server.

Only read metadata and submit to the existing thread. Never start a server,
resume a thread, or replay a request whose response was lost.
"""
from contextlib import contextmanager
import json
import socket
import struct
import time

from bazarlyk.bootstrap import codex_session
from bazarlyk.transport.agent_terminal_control import OwnerInputOutcome


class RPCError(Exception):
    def __init__(self, error):
        self.code = error.get('code')
        super().__init__('Codex rejected the request')


def control_socket(owner):
    """Find the listening Unix socket actually held by the verified owner PID."""
    if not codex_session.owner_alive(owner):
        raise OSError('Native owner is gone')
    pid = owner.split(':')[0]
    inodes = set()
    for fd in (codex_session.PROC_ROOT / pid / 'fd').iterdir():
        try:
            link = str(fd.readlink())
        except OSError:
            continue
        if link.startswith('socket:[') and link.endswith(']'):
            inodes.add(link[8:-1])
    paths = set()
    for line in (codex_session.PROC_ROOT / 'net/unix').read_text().splitlines()[1:]:
        fields = line.split(maxsplit=7)
        if (len(fields) == 8 and fields[6] in inodes and fields[3] == '00010000'
                and fields[4] == '0001' and fields[5] == '01' and fields[7].startswith('/')):
            paths.add(fields[7])
    if len(paths) != 1:
        raise OSError('Native control socket is ambiguous or unavailable')
    return paths.pop()


class RPC:
    def __init__(self, connection):
        self.connection = connection
        self.sequence = 0
        self.deadline = time.monotonic() + 10

    def request(self, method, params):
        self.sequence += 1
        identity = self.sequence
        self.connection.send(json.dumps({'id': identity, 'method': method, 'params': params}))
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Codex response deadline elapsed')
            message = json.loads(self.connection.recv(timeout=remaining))
            if message.get('id') != identity:
                continue
            if 'error' in message:
                raise RPCError(message['error'])
            result = message.get('result')
            if not isinstance(result, dict):
                raise ValueError('Malformed Codex response')
            return result


@contextmanager
def connect(owner):
    from websockets.sync.client import unix_connect
    with unix_connect(control_socket(owner), open_timeout=3, close_timeout=1, max_size=2**21) as connection:
        peer, _, _ = struct.unpack('3i', connection.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer != int(owner.split(':')[0]) or not codex_session.owner_alive(owner):
            raise OSError('Native control socket belongs to another process')
        rpc = RPC(connection)
        rpc.request('initialize', {'clientInfo': {'name': 'qorqut_native_input', 'version': '1.2'}})
        connection.send(json.dumps({'method': 'initialized'}))
        yield rpc


def submit(session, text, checkpoint, *, alive=codex_session.owner_alive, connection=connect):
    owner, thread = session['owner'], session['conversation_id']
    attempted = False
    receipt = {'client': 'codex', 'transport': 'codex_app_server'}
    try:
        with connection(owner) as rpc:
            current = rpc.request('thread/read', {'threadId': thread})['thread']
            if current.get('id') != thread:
                return OwnerInputOutcome('deferred', 'codex_thread_mismatch', receipt)
            status = current.get('status', {}).get('type')
            if status == 'active':
                turns = rpc.request('thread/turns/list', {'threadId': thread, 'limit': 1,
                                    'sortDirection': 'desc', 'itemsView': 'notLoaded'})['data']
                active = [turn for turn in turns if turn.get('status') == 'inProgress' and turn.get('id')]
                if len(active) != 1:
                    return OwnerInputOutcome('deferred', 'codex_turn_changed', receipt)
                method = 'turn/steer'
                params = {'threadId': thread, 'expectedTurnId': active[0]['id']}
            elif status == 'idle':
                method, params = 'turn/start', {'threadId': thread}
            else:
                return OwnerInputOutcome('deferred', 'codex_thread_not_ready', receipt)
            params['input'] = [{'type': 'text', 'text': text}]
            if not alive(owner):
                return OwnerInputOutcome('deferred', 'codex_session_closed', receipt)
            checkpoint()
            if not alive(owner):
                return OwnerInputOutcome('deferred', 'codex_session_closed', receipt)
            receipt['method'] = method
            attempted = True
            result = rpc.request(method, params)
            turn_id = result.get('turnId') if method == 'turn/steer' else result.get('turn', {}).get('id')
            if not turn_id or (method == 'turn/steer' and turn_id != params['expectedTurnId']):
                return OwnerInputOutcome('uncertain', 'codex_response_unconfirmed', receipt)
            receipt['turn_id'] = turn_id
            return OwnerInputOutcome('submitted', 'processing_not_confirmed', receipt)
    except RPCError as error:
        receipt['rpc_code'] = error.code
        # An explicit rejection delivered no input. A stale active turn is safe
        # to retry through the arbiter after reading the current state again.
        transient = error.code in {-32600, -32602, -32001}
        return OwnerInputOutcome('deferred' if not attempted or transient else 'failed', 'codex_input_rejected', receipt)
    except Exception:
        # No provider payload or credentials in receipts. Never queue or resend
        # after a send may have crossed the socket, even when its reply is lost.
        return OwnerInputOutcome('uncertain' if attempted else 'deferred',
                                 'codex_response_lost' if attempted else 'codex_control_unavailable', receipt)
