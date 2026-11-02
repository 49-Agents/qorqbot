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
assert configured==selected,configured
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
assert not list((root/'runtime/src').rglob('__pycache__'))
print(json.dumps({'phase':args.mode,'selected':sorted(selected),'owner_session':'ok','task':'preserved','database_inode':db.stat().st_ino,'assets':'served','managed_packages':'present','source_bytecode':'absent'}))
