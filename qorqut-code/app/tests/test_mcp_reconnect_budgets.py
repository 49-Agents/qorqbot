"""Real loopback execution/read budgets and autonomous complete discovery."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

import httpx
import pytest

from bazarlyk.bootstrap import mcp_reconnect
from bazarlyk.bootstrap.mcp_reconnect import ReconnectBridge
from test_mcp_reconnect import Peer, registration, call

INIT={'jsonrpc':'2.0','id':'native-init','method':'initialize','params':{'protocolVersion':'2025-03-26'}}


@contextmanager
def loopback(registration, handler):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            status,headers,payload,delay=handler(body,dict(self.headers))
            if delay:time.sleep(delay)
            self.send_response(status)
            for name,value in headers.items():
                if name.lower()!='content-length':self.send_header(name,value)
            self.end_headers()
            try:self.wfile.write(json.dumps(payload).encode())
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    data=json.loads(registration.read_text());data['service_url']=f'http://127.0.0.1:{server.server_port}'
    registration.write_text(json.dumps(data))
    try:yield
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def peer_handler(peer, delay=0):
    def handler(body,headers):
        response=peer(httpx.Request('POST','http://loopback/mcp',content=json.dumps(body),headers=headers))
        payload=json.loads(response.content) if response.content else {}
        return response.status_code,dict(response.headers),payload,delay if body['method']=='tools/call' and response.status_code==200 else 0
    return handler


def initialize(bridge):
    assert 'result' in bridge.handle(INIT)
    bridge.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
    assert 'result' in bridge.handle({'jsonrpc':'2.0','id':'native-list','method':'tools/list'})
    assert bridge.discovered


@pytest.mark.parametrize('name',['read','write'])
def test_healthy_slow_execution_and_execution_after_reconnect_use_normal_read_budget(registration,name,record_property):
    peer=Peer();notices=[]
    with loopback(registration,peer_handler(peer,1.1)):
        bridge=ReconnectBridge(registration,emit=notices.append)
        try:
            initialize(bridge)
            began=time.monotonic();result=bridge.handle(call(name,'slow-normal'))
            elapsed=time.monotonic()-began;record_property('normal_execution_seconds',elapsed)
            assert elapsed>=1.1
            assert result['id']=='slow-normal' and not result['result'].get('isError')
            peer.generation+=1
            began=time.monotonic();result=bridge.handle(call(name,'slow-reconnected'))
            record_property('reconnected_execution_seconds',time.monotonic()-began)
            assert result['id']=='slow-reconnected' and not result['result'].get('isError')
            assert peer.calls==[name,name] and bridge.session=='2' and bridge.discovered
            assert notices==[{'jsonrpc':'2.0','method':'notifications/tools/list_changed'}]
        finally:bridge.close()


def test_connection_refusal_stays_bounded_and_never_dispatches(registration,record_property,monkeypatch):
    monkeypatch.setattr(mcp_reconnect,'RETRY_WINDOW',.8)
    peer=Peer()
    with loopback(registration,peer_handler(peer)):
        bridge=ReconnectBridge(registration)
        try:
            initialize(bridge)
            with socket.socket() as closed:
                closed.bind(('127.0.0.1',0));port=closed.getsockname()[1]
            value=json.loads(registration.read_text());value['service_url']=f'http://127.0.0.1:{port}'
            registration.write_text(json.dumps(value))
            began=time.monotonic();result=bridge.handle(call())
            elapsed=time.monotonic()-began
            record_property('refused_connection_seconds',elapsed)
            assert result['result']['isError'] and elapsed<1.05 and not peer.calls
            assert 'outcome is unknown' not in result['result']['content'][0]['text']
        finally:bridge.close()


def test_genuine_read_timeout_keeps_dispatched_write_uncertain_and_never_replays(registration,monkeypatch):
    peer=Peer()
    with loopback(registration,peer_handler(peer,.3)):
        bridge=ReconnectBridge(registration)
        try:
            initialize(bridge)
            monkeypatch.setattr(mcp_reconnect,'HTTP_REQUEST_TIMEOUT',.15)
            result=bridge.handle(call('write','timed-out-original'))
            assert result['result']['isError'] and peer.calls==['write']
            assert 'outcome is unknown' in result['result']['content'][0]['text']
            assert 'original idempotency key' in result['result']['content'][0]['text']
            assert bridge.discovered  # Infrastructure repair is safe; mutation is not resent.
        finally:bridge.close()


@pytest.mark.parametrize('failed_part',['notifications/initialized','tools/list','pagination'])
def test_background_repairs_complete_discovery_without_another_native_request(registration,failed_part):
    peer=Peer();failed=[False];notices=[];observed=[];holder={}
    def handler(body,headers):
        method=body['method'];cursor=body.get('params',{}).get('cursor')
        match=method==failed_part or failed_part=='pagination' and method=='tools/list' and cursor=='second'
        if match and not failed[0]:
            failed[0]=True
            observed.append((holder['bridge'].discovered,set(holder['bridge'].read_tools),list(notices)))
            peer.requests.append((body,headers.get('Mcp-Session-Id')))
            return 503,{}, {},0
        if failed_part=='pagination' and method=='tools/list':
            peer.requests.append((body,headers.get('Mcp-Session-Id')))
            tools=[{'name':'read-'+('second' if cursor else 'first'),'annotations':{'readOnlyHint':True}}]
            result={'tools':tools}
            if not cursor:result['nextCursor']='second'
            return 200,{}, {'jsonrpc':'2.0','id':body['id'],'result':result},0
        return peer_handler(peer)(body,headers)
    with loopback(registration,handler):
        bridge=ReconnectBridge(registration,background=True,emit=notices.append);holder['bridge']=bridge
        try:
            bridge.initialize=INIT
            bridge.schedule_repair();bridge.repair.join(timeout=4)
            assert not bridge.repair.is_alive() and failed[0]
            assert bridge.discovered and bridge.session=='1'
            assert observed==[(False,set(),[])]
            assert bridge.read_tools==({'read-first','read-second'} if failed_part=='pagination' else {'read'})
            assert len([body for body,_ in peer.requests if body['method']=='initialize'])==2
            assert notices==[{'jsonrpc':'2.0','method':'notifications/tools/list_changed'}]
            assert not peer.calls
        finally:bridge.close()


def test_background_discovery_can_take_longer_than_old_repair_window(registration):
    peer=Peer()
    def handler(body,headers):
        status,reply_headers,payload,_=peer_handler(peer)(body,headers)
        return status,reply_headers,payload,1.1 if body['method']=='tools/list' else 0
    with loopback(registration,handler):
        bridge=ReconnectBridge(registration,background=True)
        try:
            bridge.initialize=INIT
            bridge.schedule_repair();bridge.repair.join(timeout=4)
            assert not bridge.repair.is_alive()
            assert bridge.discovered and bridge.read_tools=={'read'}
            assert len([body for body,_ in peer.requests if body['method']=='initialize'])==1
        finally:bridge.close()
