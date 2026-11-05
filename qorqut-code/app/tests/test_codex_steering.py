"""Explicit steering, exact thread binding, and no replay after uncertain input."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from bazarlyk.native_chat.codex_control import RPCError, submit, control_socket
from bazarlyk.native_chat.adapters import CodexQueueAdapter

SESSION = {'owner': '42:99:3:1:2', 'conversation_id': 'thread-1'}


class Server:
    def __init__(self, state='active', outcome=None):
        self.state, self.outcome, self.calls = state, outcome, []
        self.thread = 'thread-1'
        self.turn_state = 'inProgress'

    @contextmanager
    def connection(self, owner):
        assert owner == SESSION['owner']
        yield self

    def request(self, method, params):
        self.calls.append((method, params))
        if method == 'thread/read':
            return {'thread': {'id': self.thread, 'status': {'type': self.state}}}
        if method == 'thread/turns/list':
            return {'data': [{'id': 'turn-1', 'status': self.turn_state}]}
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome or ({'turnId': 'turn-1'} if method == 'turn/steer' else {'turn': {'id': 'turn-2'}})


def send(server, checkpoint=lambda: None, alive=lambda owner: True):
    return submit(SESSION, 'Change direction', checkpoint, alive=alive, connection=server.connection)


def test_busy_input_steers_exact_active_turn_after_checkpoint():
    server = Server()
    def checkpoint():
        assert [c[0] for c in server.calls] == ['thread/read', 'thread/turns/list']
    result = send(server, checkpoint)
    assert result.status == 'submitted'
    assert server.calls[-1] == ('turn/steer', {'threadId': 'thread-1', 'expectedTurnId': 'turn-1',
                                            'input': [{'type': 'text', 'text': 'Change direction'}]})
    assert result.receipt['method'] == 'turn/steer'


def test_idle_input_starts_turn_without_loading_or_resuming_thread():
    server = Server('idle')
    assert send(server).status == 'submitted'
    assert [c[0] for c in server.calls] == ['thread/read', 'turn/start']
    assert server.calls[-1][1]['threadId'] == 'thread-1'


@pytest.mark.parametrize('state', ['notLoaded', 'systemError'])
def test_closed_or_unhealthy_thread_never_started(state):
    server = Server(state)
    assert send(server).status == 'deferred'
    assert len(server.calls) == 1


def test_mismatched_thread_and_completed_turn_do_not_receive_input():
    server = Server(); server.thread = 'other'
    assert send(server).status == 'deferred'
    assert len(server.calls) == 1
    server = Server(); server.turn_state = 'completed'
    assert send(server).status == 'deferred'
    assert len(server.calls) == 2


def test_owner_loss_or_checkpoint_failure_prevents_input():
    for alive, checkpoint in ((lambda _: False, lambda: None),
                              (lambda _: True, lambda: (_ for _ in ()).throw(RuntimeError()))):
        server = Server()
        assert send(server, checkpoint, alive).status == 'deferred'
        assert all(not m.startswith('turn/') for m, _ in server.calls)
    states = iter([True, False]); server = Server()
    assert send(server, alive=lambda _: next(states)).status == 'deferred'
    assert len(server.calls) == 2


@pytest.mark.parametrize('outcome', [TimeoutError(), OSError(), {'turnId': 'wrong-turn'}])
def test_lost_or_invalid_ack_is_uncertain_and_never_replayed(outcome):
    server = Server(outcome=outcome)
    assert send(server).status == 'uncertain'
    assert [m for m, _ in server.calls].count('turn/steer') == 1
    assert not any(m == 'turn/start' for m, _ in server.calls)


def test_explicit_stale_turn_rejection_can_be_retried_after_a_new_state_read():
    server = Server(outcome=RPCError({'code': -32602}))
    assert send(server).status == 'deferred'
    assert [m for m, _ in server.calls].count('turn/steer') == 1


def test_adapter_uses_rpc_for_steering_and_keeps_queue_for_nonsteering():
    queued = []; server = Server()
    adapter = CodexQueueAdapter(alive=lambda _: True,
        run=lambda argv, **kw: queued.append(argv) or SimpleNamespace(returncode=0),
        steering=lambda session, text, checkpoint, **kw: send(server, checkpoint))
    profile = {'workspace': '/tmp'}
    assert adapter.submit(profile, SESSION, 'Change direction', steer=True, echo=False, checkpoint=lambda: None).status == 'submitted'
    assert queued == []
    adapter.submit(profile, SESSION, 'Later', steer=False, echo=False, checkpoint=lambda: None)
    assert queued == [['codex', 'queue', '--thread', 'thread-1', '--message', 'Later']]


def test_socket_selection_uses_only_the_verified_process_listener(tmp_path, monkeypatch):
    from bazarlyk.bootstrap import codex_session
    monkeypatch.setattr(codex_session, 'PROC_ROOT', tmp_path)
    monkeypatch.setattr(codex_session, 'owner_alive', lambda _: True)
    fds = tmp_path/'42/fd'; fds.mkdir(parents=True)
    (fds/'4').symlink_to('socket:[123]')
    (tmp_path/'net').mkdir()
    listing = tmp_path/'net/unix'
    listing.write_text('header\n0: 2 0 00010000 0001 01 123 /right\n0: 2 0 00010000 0001 01 456 /wrong\n')
    assert control_socket(SESSION['owner']) == '/right'
    listing.write_text(listing.read_text()+'0: 2 0 00010000 0001 01 123 /ambiguous\n')
    with pytest.raises(OSError): control_socket(SESSION['owner'])


def test_unix_websocket_delivery_initializes_and_steers_without_a_native_model(tmp_path, monkeypatch):
    import os
    import json
    import threading
    from websockets.sync.server import unix_serve
    from bazarlyk.native_chat import codex_control
    path = str(tmp_path/'control.sock')
    observed = []
    def handler(ws):
        for raw in ws:
            message = json.loads(raw); observed.append(message)
            if 'id' not in message:
                continue
            method = message['method']
            result = ({'thread': {'id': 'thread-1', 'status': {'type': 'active'}}} if method == 'thread/read'
                      else {'data': [{'id': 'turn-1', 'status': 'inProgress'}]} if method == 'thread/turns/list'
                      else {'turnId': 'turn-1'} if method == 'turn/steer' else {})
            ws.send(json.dumps({'id': message['id'], 'result': result}))
    monkeypatch.setattr(codex_control, 'control_socket', lambda _: path)
    monkeypatch.setattr(codex_control.codex_session, 'owner_alive', lambda _: True)
    with unix_serve(handler, path) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            result = submit({**SESSION, 'owner': f'{os.getpid()}:99:3:1:2'}, 'Change direction', lambda: None,
                            alive=lambda _: True)
            assert result.status == 'submitted'
            assert [m['method'] for m in observed] == ['initialize', 'initialized', 'thread/read', 'thread/turns/list', 'turn/steer']
            assert observed[-1]['params']['expectedTurnId'] == 'turn-1'
        finally:
            server.shutdown(); worker.join(timeout=3)
