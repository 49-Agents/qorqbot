"""Loopback-only checks of a disposable native install and its synthetic task."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import selectors
import subprocess
import urllib.error
import urllib.request

parser=argparse.ArgumentParser()
parser.add_argument('mode',choices=['write','verify'])
parser.add_argument('--root',type=Path,default=Path('/install/qorqbot'))
parser.add_argument('--selected',default='')
args=parser.parse_args()
root=args.root
base='http://127.0.0.1:1049'
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def request(path,body=None):
    payload=json.dumps(body).encode() if body is not None else None
    try:
        response=opener.open(urllib.request.Request(base+path,data=payload,headers={'Origin':base,'Content-Type':'application/json'}),timeout=60)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(exc.read().decode()) from exc
    with response:
        data=response.read()
        assert response.status==200
        return json.loads(data) if 'application/json' in response.headers.get('Content-Type','') else data
assert request('/health/live')['live']
assert request('/health/ready')['data']['ready']
session=request('/api/session')['data']
assert session['is_installation_owner']
selected=set(filter(None,args.selected.split(',')))
configuration=json.loads((root/'instance/config/extensions.json').read_text())
configured={entry['factory'].split('.')[0].removeprefix('qorqut_') for entry in configuration['extensions']}
# The launcher selects the bundled treg toolkit on every installation; the optional connectors come on top of it.
assert configured==selected|{'treg'},configured
manifest=request('/api/root/extensions')['data']['extensions']
for entry in manifest:
    for kind in ('scripts','styles'):
        for asset in entry.get('assets',{}).get(kind,[]):
            assert request(asset),asset
# The managed binding format may wrap its inventory; keep the disk-level selected
# configuration assertion separate from this exact imported-package evidence.
for name in selected:
    package=root/'instance/operations/agentic/plugins'/('imported-fastmail-metadata' if name=='fastmail' else 'imported-'+name)
    assert (package/'agentic.yaml').is_file(),package
state=root.parent/'extension-acceptance-state.json'
db=root/'instance/operations/.runtime/operations.sqlite3'
marker=root/'instance/operations/extension-acceptance.txt'
if args.mode=='write':
    result=request('/api/root/operations/tasks_create',{'scope':'global','title':'Synthetic optional connector acceptance','idempotency_key':'optional-connector-acceptance-1'})
    assert result['status']=='ok',result
    with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as connection:
        task=connection.execute('SELECT id FROM tasks WHERE title=?',('Synthetic optional connector acceptance',)).fetchone()[0]
    marker.write_text('Synthetic marker: preserve through optional connector installation.\n')
    state.write_text(json.dumps({'task_id':task,'owner':session['principal_id'],'database_inode':db.stat().st_ino,'marker_sha256':hashlib.sha256(marker.read_bytes()).hexdigest()}))
else:
    saved=json.loads(state.read_text())
    result=request('/api/root/operations/tasks_get',{'scope':'global','task_id':saved['task_id']})
    assert result['status']=='ok',result
    assert saved['owner']==session['principal_id']
    assert saved['database_inode']==db.stat().st_ino
    assert saved['marker_sha256']==hashlib.sha256(marker.read_bytes()).hexdigest()
if selected=={'whatsapp'}:
    helper=root/'runtime/venv/lib/python3.12/site-packages/qorqut_whatsapp/bin/whatsapp-personal'
    packets=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'synthetic-native-acceptance','version':'1'}}},
             {'jsonrpc':'2.0','method':'notifications/initialized'},
             {'jsonrpc':'2.0','id':2,'method':'tools/list'}]
    process=subprocess.Popen([helper,'mcp','--socket',root/'instance/operations/.runtime/absent-synthetic-whatsapp.sock'],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout,selectors.EVENT_READ)
            process.stdin.write(json.dumps(packets[0])+'\n');process.stdin.flush()
            assert ready.select(15),'Helper initialize timed out'
            assert json.loads(process.stdout.readline())['id']==1
            process.stdin.write(''.join(json.dumps(item)+'\n' for item in packets[1:]));process.stdin.flush()
            assert ready.select(15),'Helper tools listing timed out'
            listed=json.loads(process.stdout.readline())['result']['tools']
    finally:
        process.stdin.close()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill();process.wait()
        process.stdout.close();process.stderr.close()
    assert {tool['name'] for tool in listed}=={'whatsapp_read'},listed
assert not list((root/'runtime/src').rglob('__pycache__'))
print(json.dumps({'phase':args.mode,'selected':sorted(selected),'owner_session':'ok','task':'preserved','database_inode':db.stat().st_ino,'assets':'served','managed_packages':'present','source_bytecode':'absent'}))
