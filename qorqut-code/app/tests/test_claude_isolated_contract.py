"""Installed Claude print/resume/hooks/MCP against isolated loopback providers.

Print clients cannot attest an interactive inbox owner. This does not demonstrate
in-place interactive configuration reload or authorize a production model turn.
"""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import uuid
import pytest


@pytest.mark.skipif(os.environ.get('QORQUT_TEST_CLAUDE_CONTRACT')!='1',reason='explicit isolated installed-Claude contract only')
def test_native_claude_scoped_mcp_hook_and_same_session_resume(tmp_path):
    requests=[];mcp=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.send_response(405);self.end_headers()
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/mcp':
                mcp.append(body)
                if 'id' not in body:self.send_response(202);self.end_headers();return
                if body['method']=='initialize':result={'protocolVersion':body['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'isolated','version':'1'}}
                else:result={'tools':[]}
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps({'jsonrpc':'2.0','id':body['id'],'result':result}).encode());return
            if 'count_tokens' in self.path:
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"input_tokens":1}');return
            requests.append(body)
            message={'id':'msg_'+uuid.uuid4().hex,'type':'message','role':'assistant','model':body['model'],
                'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'output_tokens':0}}
            events=[('message_start',{'type':'message_start','message':message}),
                ('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),
                ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'Isolated Claude contract complete.'}}),
                ('content_block_stop',{'type':'content_block_stop','index':0}),
                ('message_delta',{'type':'message_delta','delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}}),
                ('message_stop',{'type':'message_stop'})]
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            for name,event in events:self.wfile.write(('event: '+name+'\ndata: '+json.dumps(event)+'\n\n').encode());self.wfile.flush()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    home=tmp_path/'home';home.mkdir();workspace=tmp_path/'workspace';workspace.mkdir()
    events=tmp_path/'hooks.jsonl';script=tmp_path/'capture.py'
    script.write_text('import json,sys\nv=json.load(sys.stdin)\nwith open(sys.argv[1],"a") as f:f.write(json.dumps(v)+"\\n")\nprint("{}")\n')
    settings=tmp_path/'settings.json';settings.write_text(json.dumps({'hooks':{name:[{'hooks':[{'type':'command',
        'command':shlex.join([sys.executable,'-I',str(script),str(events)]),'timeout':3}]}] for name in ('SessionStart','UserPromptSubmit','Stop')}}))
    base=['claude','-p','--verbose','--output-format','stream-json','--model','claude-sonnet-4-6','--tools','',
        '--setting-sources','','--settings',str(settings),'--strict-mcp-config','--mcp-config',
        json.dumps({'mcpServers':{'fixture':{'type':'http','url':f'http://127.0.0.1:{server.server_port}/mcp'}}})]
    environment={'PATH':os.environ['PATH'],'HOME':str(home),'CLAUDE_CONFIG_DIR':str(home/'.claude'),
        'ANTHROPIC_API_KEY':'isolated-no-authority','ANTHROPIC_BASE_URL':f'http://127.0.0.1:{server.server_port}',
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','DISABLE_AUTOUPDATER':'1','TERM':'xterm-256color'}
    try:
        first=subprocess.run([*base,'--','Synthetic isolated first turn'],cwd=workspace,env=environment,
            capture_output=True,text=True,timeout=30)
        assert first.returncode==0,first.stderr+'\n'+first.stdout
        output=[json.loads(line) for line in first.stdout.splitlines()]
        session=next(item['session_id'] for item in output if item['type']=='result')
        second=subprocess.run([*base,'--resume',session,'--','Synthetic isolated resumed turn'],cwd=workspace,env=environment,
            capture_output=True,text=True,timeout=30)
        assert second.returncode==0,second.stderr+'\n'+second.stdout
        resumed=[json.loads(line) for line in second.stdout.splitlines()]
        assert next(item['session_id'] for item in resumed if item['type']=='result')==session
        hooks=[json.loads(line) for line in events.read_text().splitlines()]
        assert len([h for h in hooks if h['hook_event_name']=='UserPromptSubmit'])==2
        assert all(h['session_id']==session and not h.get('agent_id') for h in hooks)
        assert any(r['method']=='tools/list' for r in mcp)
        assert len(requests)>=2
        (tmp_path/'contract.json').write_text(json.dumps({'session':session,'hooks':hooks,'requests':len(requests),'mcp_methods':[r['method'] for r in mcp]}))
    finally:server.shutdown();server.server_close()
