"""Actual CLI model precedence with isolated, credential-free loopback inference."""
import json
import os
import pty
import select
import signal
import fcntl
import termios
import struct
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import threading
import uuid

import pytest
import tomli_w

from bazarlyk.bootstrap.codex_config import configuration, role_model_default


@pytest.mark.skipif(os.environ.get('QORQUT_TEST_CODEX_MODEL_DEFAULTS') != '1',
                   reason='explicit isolated Codex CLI contract only')
def test_role_default_and_explicit_conversation_choice(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path != '/v1/responses':
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            item = {'id': 'msg_' + str(uuid.uuid4()), 'type': 'message',
                    'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': 'Synthetic complete', 'annotations': []}]}
            response = {'id': 'resp_' + str(uuid.uuid4()), 'object': 'response', 'created_at': 1,
                        'model': body['model'], 'status': 'completed', 'output': [item],
                        'usage': {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7}}
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for event in [
                {'type': 'response.created', 'response': {**response, 'status': 'in_progress', 'output': []}},
                {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
                {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
                {'type': 'response.completed', 'response': response},
            ]:
                self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode())
                self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f'http://127.0.0.1:{server.server_port}'
    codex_home, workspace = tmp_path / 'codex-home', tmp_path / 'workspace'
    codex_home.mkdir()
    (workspace / '.codex').mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
    (codex_home / 'config.toml').write_text(tomli_w.dumps({
        'model': 'gpt-5.6-terra', 'model_reasoning_effort': 'medium',
        'check_for_update_on_startup': False,
        'projects': {str(workspace): {'trust_level': 'trusted'}},
        'model_provider': 'probe', 'features': {'apps': False, 'plugins': False},
        'model_providers': {'probe': {'name': 'Loopback', 'base_url': endpoint + '/v1',
            'wire_api': 'responses', 'requires_openai_auth': False}},
    }))
    raw, _ = configuration(workspace=workspace, endpoint=endpoint, header_command='unused',
        command='unused', snapshot_command='unused', protected=[], read_denied=[], model_default=role_model_default('ecosystem', 'ordubashy'))
    import tomllib
    config = tomllib.loads(raw.decode())
    # Only model selection is under test: no real agent, MCP, hooks or credentials.
    config.pop('mcp_servers')
    (workspace / '.codex/config.toml').write_text(tomli_w.dumps(config))
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'CODEX_HOME': str(codex_home),
           'TERM': 'dumb', 'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}
    for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']:
        env[key] = 'http://127.0.0.1:1'

    def run(args):
        result = subprocess.run(['codex', *args], cwd=workspace, env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stderr[-3000:]
        return [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]

    try:
        run(['exec', '--json', 'Synthetic default check'])
        assert calls[-1]['model'] == 'gpt-6-luna'
        assert calls[-1].get('reasoning', {}).get('effort') == 'high'
        chosen = run(['exec', '--json', '--model', 'gpt-6-astra', '-c',
                      'model_reasoning_effort="xhigh"', 'Synthetic owner choice'])
        assert calls[-1]['model'] == 'gpt-6-astra'
        assert calls[-1].get('reasoning', {}).get('effort') == 'xhigh'
        thread = next(row['thread_id'] for row in chosen if row.get('type') == 'thread.started')
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        previous = len(calls)
        process = subprocess.Popen(['codex', 'resume', '--no-alt-screen', thread, 'Synthetic resume'],
            cwd=workspace, env={**env, 'TERM': 'xterm-256color'}, stdin=slave, stdout=slave,
            stderr=slave, start_new_session=True)
        os.close(slave)
        output = b''
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and process.poll() is None and len(calls) == previous:
                if select.select([master], [], [], .2)[0]:
                    chunk = os.read(master, 65536)
                    output += chunk
                    if b'\x1b[6n' in chunk:
                        os.write(master, b'\x1b[1;1R')
            assert len(calls) > previous, output[-4000:].decode(errors='replace')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
            os.close(master)
        assert calls[-1]['model'] == 'gpt-6-astra'
        assert calls[-1].get('reasoning', {}).get('effort') == 'xhigh'
    finally:
        server.shutdown()
        server.server_close()
