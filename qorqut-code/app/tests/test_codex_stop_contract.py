"""Opt-in native Stop contract using only a loopback synthetic model."""
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import shlex
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid

import pytest
import tomli_w


@pytest.mark.skipif(os.environ.get("QORQUT_TEST_CODEX_STOP_CONTRACT") != "1",
                    reason="explicit isolated native hook contract only")
def test_native_stop_continuation_preserves_lineage(tmp_path):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(405)
            self.end_headers()

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/mcp':
                if 'id' not in request:
                    self.send_response(202); self.end_headers(); return
                if request['method'] == 'initialize':
                    result = {'protocolVersion':request['params']['protocolVersion'],'capabilities':{'tools':{}},
                              'serverInfo':{'name':'synthetic','version':'1'}}
                elif request['method'] == 'tools/list':
                    result = {'tools':[{'name':'agent_work_update','description':'Synthetic bookkeeping contract.',
                        'inputSchema':{'type':'object','properties':{},'additionalProperties':False}}]}
                else:
                    result = {'content':[{'type':'text','text':'Synthetic work reviewed.'}]}
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode())
                return
            calls.append(request)
            main_call = sum(bool(item.get('tools')) for item in calls) if request.get('tools') else 0
            message = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
                       "status": "completed", "content": [{"type": "output_text",
                       "text": "Synthetic checkpoint response.", "annotations": []}]}
            response = {"id": "resp_" + uuid.uuid4().hex, "object": "response", "created_at": 1,
                        "model": "probe", "status": "completed", "output": [message],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            stream = [
                {"type": "response.created", "response": {**response, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {**message, "status": "in_progress", "content": []}},
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
                 "item_id": message["id"], "delta": "Synthetic checkpoint response."},
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": response},
            ]
            if main_call in {1,2}:
                assert any(tool.get('name') == 'exec_command' for tool in calls[0]['tools'])
                call = {'id': 'fc_probe', 'type': 'function_call', 'call_id': 'call_probe',
                        'name': 'exec_command', 'arguments': json.dumps({'cmd':'pwd','login':False}), 'status':'completed'}
                if main_call == 2:
                    call.update(id='fc_work',call_id='call_work',namespace='mcp__bazarlyk',name='agent_work_update',arguments='{}')
                response['output'] = [call]
                stream = [
                    {'type':'response.created','response':{**response,'status':'in_progress','output':[]}},
                    {'type':'response.output_item.added','output_index':0,'item':{**call,'arguments':'','status':'in_progress'}},
                    {'type':'response.function_call_arguments.delta','output_index':0,'item_id':call['id'],'delta':call['arguments']},
                    {'type':'response.output_item.done','output_index':0,'item':call},
                    {'type':'response.completed','response':response},
                ]
            for event in stream:
                self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    (workspace / ".codex").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    events = tmp_path / "events.jsonl"
    hook = workspace / ".codex/stop.py"
    hook.write_text(
        "import json,sys\n"
        "value=json.load(sys.stdin)\n"
        "with open(sys.argv[1],'a') as stream: stream.write(json.dumps(value)+'\\n')\n"
        "output={}\n"
        "if value['hook_event_name']=='PostToolUse':\n"
        "    output={'hookSpecificOutput':{'hookEventName':'PostToolUse','additionalContext':'SYNTHETIC_RECOVERY_BEGIN'+'r'*1500+'SYNTHETIC_RECOVERY_END'}}\n"
        "if value['hook_event_name']=='Stop' and not value.get('stop_hook_active'):\n"
        "    output={'decision':'block','reason':'SYNTHETIC_CHECKPOINT: confirm current work once.'}\n"
        "print(json.dumps(output))\n"
    )
    command = shlex.join([sys.executable, "-I", str(hook), str(events)])
    hooks = {name: [{"hooks": [{"type": "command", "command": command, "timeout": 3}]}]
             for name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop")}
    (workspace / ".codex/hooks.json").write_text(json.dumps({"hooks": hooks}))
    (home / "config.toml").write_text(tomli_w.dumps({
        "model": "probe", "model_provider": "probe", "check_for_update_on_startup": False,
        "approval_policy": "never", "sandbox_mode": "danger-full-access",
        "model_providers": {"probe": {"name": "Probe",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1", "wire_api": "responses",
            "requires_openai_auth": False}},
        "projects": {str(workspace): {"trust_level": "trusted"}},
        "features": {"apps": False, "plugins": False},
        'mcp_servers':{'bazarlyk':{'url':f'http://127.0.0.1:{server.server_port}/mcp','required':True}},
    }))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    process = subprocess.Popen(
        ["codex", "--strict-config", "--dangerously-bypass-hook-trust", "--no-alt-screen",
         "Synthetic original request"],
        cwd=workspace, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                           "CODEX_HOME": str(home), "TERM": "xterm-256color"},
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
    )
    os.close(slave)
    output = b""
    observed = []
    try:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and process.poll() is None:
            if select.select([master], [], [], .1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                output += chunk
                if b"\x1b[6n" in chunk:
                    os.write(master, b"\x1b[1;1R")
            if events.exists():
                observed = [json.loads(line) for line in events.read_text().splitlines()]
                if sum(e["hook_event_name"] == "Stop" for e in observed) >= 2:
                    break
        stops = [e for e in observed if e["hook_event_name"] == "Stop"]
        assert len(stops) == 2, output[-5000:].decode(errors="replace")
        assert stops[0]["stop_hook_active"] is False
        assert stops[1]["stop_hook_active"] is True
        assert all(e.get("turn_id") and e.get("session_id") for e in stops)
        assert stops[0]["session_id"] == stops[1]["session_id"]
        assert stops[0]['turn_id'] == stops[1]['turn_id']
        assert sum(bool(item.get('tools')) for item in calls) == 4
        assert 'SYNTHETIC_RECOVERY_BEGIN' + 'r'*1500 + 'SYNTHETIC_RECOVERY_END' in json.dumps(calls)
        tools = [event for event in observed if event['hook_event_name'] in {'PreToolUse','PostToolUse'}]
        assert [event['hook_event_name'] for event in tools] == ['PreToolUse','PostToolUse'] * 2, observed
        assert all(event['turn_id'] == stops[0]['turn_id'] for event in tools)
        assert all(event['tool_name'] == 'Bash' and event['tool_input'] == {'command':'pwd'} for event in tools[:2])
        assert str(workspace) in tools[1]['tool_response']
        assert tools[2]['tool_name'] == tools[3]['tool_name'] == 'mcp__bazarlyk__agent_work_update'
        prompts = [e for e in observed if e["hook_event_name"] == "UserPromptSubmit"]
        assert len(prompts) == 1
        assert prompts[0]["prompt"] == "Synthetic original request"
        # Exercise the installed queue against this still-running interactive
        # native process, without recreating/resuming its conversation.
        session=stops[0]['session_id'];owner_pid=process.pid
        queued=subprocess.run(['codex','queue','--thread',session,'--message','SYNTHETIC_SERVICE_NOTICE'],
            cwd=workspace,env={'PATH':os.environ['PATH'],'HOME':str(tmp_path),'CODEX_HOME':str(home),'TERM':'xterm-256color'},
            capture_output=True,text=True,timeout=5)
        assert queued.returncode==0,queued.stderr
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if select.select([master],[],[],.1)[0]:
                try:chunk=os.read(master,65536)
                except OSError:break
                output+=chunk
                if b'\x1b[6n' in chunk:os.write(master,b'\x1b[1;1R')
            all_hooks=[json.loads(line) for line in events.read_text().splitlines()]
            if any(h.get('prompt')=='SYNTHETIC_SERVICE_NOTICE' for h in all_hooks) and len([h for h in all_hooks if h['hook_event_name']=='Stop'])>=4:break
        assert process.pid==owner_pid and process.poll() is None
        notices=[h for h in all_hooks if h.get('prompt')=='SYNTHETIC_SERVICE_NOTICE']
        assert len(notices)==1 and notices[0]['session_id']==session
        # Preserve observed lineage for future CLI changes without assuming IDs
        # stay identical when Stop creates a fresh native user prompt.
        from bazarlyk.bootstrap.features import record_contract
        record_contract('codex',tmp_path/'native-contracts',stop_hook_correction=True,queue=True)
        (tmp_path / "contract.json").write_text(json.dumps({
            "stops": [{"turn_id": e["turn_id"], "stop_hook_active": e["stop_hook_active"]} for e in stops],
            "prompts": [{"turn_id": e.get("turn_id"), "prompt": e.get("prompt")} for e in prompts],
            'tools': tools,
        }))
    finally:
        (tmp_path / 'requests.json').write_text(json.dumps(calls))
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
        os.close(master)
        server.shutdown()
        server.server_close()
