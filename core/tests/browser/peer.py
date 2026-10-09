"""A deterministic protocol peer for core tests; no Chrome or private profiles."""

import asyncio
import json
from pathlib import Path
import socket
import threading
import time
from uuid import UUID, uuid4

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from qorqut_core.browser.client import payload_hash
from qorqut_core.browser.models import Binding, ControlStatus, Identity, Observation, Receipt, Reconcile


class Peer:
    def __init__(self, agent_id, epoch):
        self.identity = Identity(agent_id=agent_id, recovery_epoch=epoch, environment_generation=1,
            boot_id=UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()), browser_instance=uuid4(), protocol=1)
        self.control = ControlStatus(binding=Binding(identity=self.identity, control_epoch=0), mode='automation', ready=True)
        self.receipts, self.posts, self.handshakes = {}, [], []
        self.pending, self.lose_response, self.online, self.bad_ready = False, False, True, False
        self.viewers = 0
        self.closed = threading.Event()
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        self.port = listener.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'

        async def identity(request):
            if not self.online:
                return JSONResponse({'error': {'code': 'unavailable'}}, status_code=503)
            return JSONResponse(Observation(protocol=1, identity=self.identity, control=self.control,
                actions=['snapshot'], handoff_enabled=True).model_dump(mode='json'))

        async def receipt(request):
            saved = self.receipts.get(request.path_params['command_id'])
            return JSONResponse(saved.model_dump(mode='json') if saved else {'error': {'code': 'not_found'}},
                status_code=200 if saved else 404)

        async def control(request):
            command = Reconcile.model_validate_json(await request.body())
            actor_id = request.headers['x-qorqut-actor']
            self.posts.append((command, actor_id))
            saved = self.receipts.get(str(command.command_id))
            if saved and saved.outcome != 'pending':
                return JSONResponse(saved.model_dump(mode='json'))
            if not saved and command.expected != self.control.binding:
                return JSONResponse({'error': {'code': 'target_changed'}}, status_code=409)
            self.control = ControlStatus(binding=command.desired,
                mode=('yielding' if command.mode == 'human' else 'reconciling') if self.pending else command.mode,
                ready=not self.pending)
            saved = Receipt(command_id=command.command_id, kind='control', binding=command.expected,
                actor_id=actor_id, payload_hash=payload_hash(command), outcome='pending' if self.pending else 'completed',
                result={'text': '' if self.pending else command.mode})
            self.receipts[str(command.command_id)] = saved
            if self.lose_response:
                self.lose_response = False
                return JSONResponse({'error': {'code': 'unavailable'}}, status_code=503)
            return JSONResponse(saved.model_dump(mode='json'))

        async def stream(ws):
            await ws.accept(subprotocol='binary' if 'binary' in ws.scope.get('subprotocols', []) else None)
            children = []
            try:
                hello = await ws.receive_json()
                self.handshakes.append((hello, ws.headers['x-qorqut-actor']))
                binding = Binding.model_validate(hello['binding'])
                if binding != self.control.binding or not self.control.ready or self.control.mode != 'human':
                    return
                self.viewers += 1
                ready = {'type': 'ready', 'protocol': 1, 'binding': binding.model_dump(mode='json')}
                if self.bad_ready:
                    ready['binding']['control_epoch'] += 1
                await ws.send_json(ready)
                await ws.send_bytes(b'RFB 003.008\n')
                async def echo():
                    while True:
                        await ws.send_bytes(await ws.receive_bytes())
                async def watch():
                    while binding == self.control.binding and self.control.ready and self.control.mode == 'human':
                        await asyncio.sleep(0.03)
                children = [asyncio.create_task(fn()) for fn in (echo, watch)]
                await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for child in children:
                    child.cancel()
                await asyncio.gather(*children, return_exceptions=True)
                self.viewers = max(0, self.viewers - 1)
                self.closed.set()
                await ws.close()

        app = Starlette(routes=[Route('/v1/identity', identity), Route('/v1/browser/control', control, methods=['POST']),
            Route('/v1/browser/commands/{command_id}', receipt), WebSocketRoute('/v1/browser/view', stream)])
        self.server = uvicorn.Server(uvicorn.Config(app, log_level='critical', lifespan='off', ws='websockets-sansio'))
        self.thread = threading.Thread(target=lambda: self.server.run(sockets=[listener]), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 5
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError('Isolated peer failed to start')
            time.sleep(.01)

    def restart(self):
        self.control = self.control.model_copy(update={'ready': False,
            'mode': 'human' if self.control.mode == 'human' else 'reconciling'})
        for key, receipt in self.receipts.items():
            if receipt.outcome == 'pending':
                self.receipts[key] = receipt.model_copy(update={'outcome': 'uncertain', 'error': 'interrupted_control'})

    def close(self):
        self.server.should_exit = True
        self.thread.join(6)
        if self.thread.is_alive():
            raise RuntimeError('Isolated peer failed to close')
