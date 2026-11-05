import io
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading
import time
from bazarlyk.bootstrap import claude_session
from bazarlyk.bootstrap.client import BootstrapClient
from qorqut_operations.contracts import new_id
from test_dual_cli import registration


def test_delayed_healthy_bootstrap_and_receipt_complete_with_loopback_backend(tmp_path,monkeypatch):
    path=registration(tmp_path,capture=False);selected=json.loads(path.read_text())
    credential=tmp_path/'.bazarlyk/credential';credential.write_text('x'*40);credential.chmod(0o600)
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            requests.append(self.path)
            # Both exceed the former 800ms bootstrap / 100ms receipt limits.
            time.sleep(1.1 if self.path.endswith('/bootstrap') else .2)
            body={'status':'ok','data':{'ready':True,'effective_config_id':'fixture',
                'mandatory_context':'Current mandatory context'}}
            self.send_response(200);self.end_headers()
            try:self.wfile.write(json.dumps(body).encode())
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    selected['service_url']=f'http://127.0.0.1:{server.server_port}';path.write_text(json.dumps(selected))
    # Binding is independently exercised in native-binding tests. This check
    # measures synchronous hooks without a synthetic startup or native turn.
    monkeypatch.setattr('bazarlyk.bootstrap.native_binding.attest',lambda *a:None)
    hook={'hook_event_name':'UserPromptSubmit','cwd':str(tmp_path),'session_id':new_id(),'prompt':'Owner repository work'}
    def invoke():
        stream=io.StringIO();began=time.monotonic()
        claude_session.run(hook,path,stream=stream,diagnostics=io.StringIO())
        return time.monotonic()-began,json.loads(stream.getvalue())
    try:
        elapsed,result=invoke()
        assert 'Current mandatory context' in result['hookSpecificOutput']['additionalContext']
        assert requests == ['/api/agent/bootstrap','/api/agent/bootstrap/receipt']
        assert not list((tmp_path/'.bazarlyk/preparation-recovery').glob('*.json'))
        assert not list((tmp_path/'.bazarlyk/technical-reports/active').glob('*.json'))
        assert elapsed < 5
    finally:server.shutdown();server.server_close()


def test_bootstrap_request_budget_is_bounded_and_restored():
    from bazarlyk.bootstrap.client import BOOTSTRAP_TIMEOUT
    from qorqut_operations.contracts import DomainError
    import pytest

    class Client:
        timeout = 45
        calls = 0
        def post(self,route,body):
            self.calls += 1
            assert self.timeout == BOOTSTRAP_TIMEOUT
            raise DomainError('bootstrap_unavailable','Timed out',retryable=True)

    client=Client()
    with pytest.raises(DomainError):claude_session.prepare_with_retry(client,{})
    assert client.calls == 1 and client.timeout == 45


