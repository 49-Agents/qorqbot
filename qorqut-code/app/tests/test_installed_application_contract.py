"""Opt-in fixed launcher and migrated SQLite, with synthetic native ownership.

Real packaged service code/dependencies; no production database/native client or
provider. Source identity and fencing are distinct from native hot reload.
"""
from dataclasses import replace
from datetime import datetime,timezone
import getpass
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
import pytest

from bazarlyk.bootstrap.codex_session import process_identity
from qorqut_operations.contracts import DomainError,new_id
from bazarlyk.release_worker.lifetime import atomic_json
from bazarlyk.storage import Database
from test_service_transports import ServiceFixture


@pytest.mark.skipif(not os.environ.get('QORQUT_TEST_APP_INSTALL_ROOT'),reason='explicit isolated installed app contract only')
def test_fixed_launcher_migrated_database_preserves_binding_events_and_fencing(tmp_path,monkeypatch):
    root=Path(os.environ['QORQUT_TEST_APP_INSTALL_ROOT'])
    config_path=root/'config/installation.json';config=json.loads(config_path.read_text())
    monkeypatch.setenv('QORQUT_INSTALLATION_CONFIG',str(config_path))
    original_migrations=Database.migrations
    with monkeypatch.context() as historical:
        historical.setattr(Database,'migrations',staticmethod(lambda:original_migrations()[:111]))
        service=ServiceFixture(tmp_path,full=True)
    consumer=new_id();now=datetime.now(timezone.utc).isoformat()
    with service.fixture.db.transaction() as uow:
        uow.connection.execute('INSERT INTO inbox_watchers(agent_id,consumer_id,last_check_at,lease_until) VALUES(?,?,?,?)',
            (service.agent.id,consumer,'1970-01-01T00:00:00.000000Z','1970-01-01T00:00:00.000000Z'))
    sent=service.runtime.application.invoke(service.fixture.ctx,'agents_message_send',
        {'to_agent_id':service.agent.id,'message':'Isolated pending event before migration'}, {'idempotency_key':new_id()})
    assert sent.status=='ok',sent.to_dict()
    with service.fixture.db.read() as uow:
        pending_before=[dict(row) for row in uow.connection.execute('SELECT * FROM inbox_deliveries WHERE agent_id=?',(service.agent.id,))]
    assert pending_before
    # Apply the real packaged migration through the dependency-only interpreter.
    manifest_path=root.joinpath('current').resolve()/'manifest.json';manifest=json.loads(manifest_path.read_text())
    subprocess.run([manifest['python'],'-I','-B','-c',
        'import sys;sys.path.insert(0,sys.argv[1]);from bazarlyk.storage import Database;Database(sys.argv[2]).initialize()',
        manifest['components']['core']['path'],str(service.fixture.db.path)],check=True)
    owner=subprocess.Popen(['/usr/bin/sleep','3600']);process=None
    try:
        workspace=Path(service.runtime.resolver.resolve_agent_config(service.agent).to_dict()['workspace'])
        binding={'agent_id':service.agent.id,'registration_id':service.registrations[service.agent.id],
            'workspace':str(workspace),'session_id':consumer,'runtime':'claude-code','pid':owner.pid,
            'started':process_identity(owner.pid).split(':')[1],'owner':process_identity(owner.pid),
            'execution_target':'local','event':'resume'}
        ctx=service.runtime.resolver.invocation_context(service.agent)
        selected=service.runtime.inbox.start_session(ctx,service.registrations[service.agent.id],consumer,'resume',binding=binding)
        assert selected['inbox_generation']==1
        renewed=service.runtime.inbox.start_session(ctx,service.registrations[service.agent.id],consumer,'prompt',binding=binding,expected_generation=1)
        assert renewed['inbox_generation']==1
        manager_config=service.runtime.resolver.resolve_agent_config(service.manager).to_dict()
        scope=manager_config['scope']
        service.fixture.ctx=replace(service.fixture.ctx,global_capabilities=service.fixture.ctx.global_capabilities|{'manage_delegated_assignments'})
        service.fixture.update(service.manager,{'scope':{**scope,'global_capabilities':sorted(set(scope['global_capabilities'])|{'manage_delegated_assignments'})}})
        from bazarlyk.bootstrap.preparation import prepare_agent
        prepared=prepare_agent(service.runtime.resolver,service.manager,endpoint='http://127.0.0.1:9988')
        assert prepared['ready']
        manager_session=new_id();manager_workspace=service.runtime.resolver.resolve_agent_config(service.manager).to_dict()['workspace']
        manager_context=service.runtime.resolver.invocation_context(service.manager)
        manager_binding={**binding,'agent_id':service.manager.id,'registration_id':service.registrations[service.manager.id],
            'workspace':manager_workspace,'session_id':manager_session,'runtime':'codex'}
        manager_selected=service.runtime.inbox.start_session(manager_context,service.registrations[service.manager.id],manager_session,'resume',binding=manager_binding)
        assert manager_selected['inbox_generation']==1
        event_ids=[row['event_id'] for row in pending_before]
        with service.fixture.db.read() as uow:
            before=[dict(row) for row in uow.connection.execute('SELECT * FROM inbox_watchers WHERE agent_id=?',(service.agent.id,))]
        with socket.socket() as listener:listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        origin=f'http://127.0.0.1:{port}'
        config.update(origin=origin,businesses_root=str(service.fixture.root),database=str(service.fixture.db.path))
        atomic_json(config_path,config)
        environment={'PATH':os.environ['PATH'],'HOME':str(tmp_path),'USER':os.environ.get('USER') or getpass.getuser(),
            'BIZ_DASH_STATE':str(root/'state/dashboard'),'BIZ_TMUX_SOCKET':'isolated-app-contract-'+str(os.getpid())}
        log=tmp_path/'application.log'
        with log.open('w') as stream:
            process=subprocess.Popen([str(root/'bin/app'),'--root',str(service.fixture.root),'--workspace',str(root),
                '--host','127.0.0.1','--port',str(port)],env=environment,stdout=stream,stderr=stream)
        def request(path,body=None,agent=False,token=None):
            headers={'Origin':origin,'Content-Type':'application/json'}
            if agent:headers['Authorization']='Bearer '+service.tokens[service.agent.id]
            if token:headers['Authorization']='Bearer '+token
            req=urllib.request.Request(origin+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
            with urllib.request.urlopen(req,timeout=1) as response:return json.load(response)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            try:
                if request('/health/ready')['data']['ready']:break
            except (OSError,KeyError):
                if process.poll() is not None:pytest.fail(log.read_text())
                time.sleep(.1)
        else:pytest.fail(log.read_text())
        status=request('/api/root/release/status')['data']
        assert status['release_id']==manifest['release_id']
        assert status['components']=={name:item['commit'] for name,item in manifest['components'].items()}
        from urllib.error import HTTPError
        # The poll route no longer exists in the installed application.
        with pytest.raises(HTTPError) as gone:request('/api/agent/inbox/watch',{'registration_id':service.registrations[service.agent.id],'consumer_id':consumer},agent=True)
        assert json.load(gone.value)['error']['code']=='not_found'
        with pytest.raises(HTTPError) as dispatch:request('/api/agent/delegation/claim',{
            'registration_id':service.registrations[service.manager.id],'consumer_id':manager_session,'binding_generation':0},token=service.tokens[service.manager.id])
        assert json.load(dispatch.value)['error']['code']=='inbox_generation_stale'
        fenced=subprocess.run([manifest['python'],'-I','-B','-c',
            'import sys,json;from dataclasses import replace;sys.path.insert(0,sys.argv[1]);'
            'from bazarlyk.service.runtime import build_runtime;from qorqut_operations.contracts import Principal,new_id;'
            'r=build_runtime(sys.argv[2],sys.argv[3]);c=r.resolver.invocation_context(Principal(sys.argv[4],"agent"));'
            'c=replace(c,inbox_session_id=sys.argv[5],inbox_generation=0);'
            'v=r.application.invoke(c,"inbox_acknowledge",{"consumer_id":sys.argv[5],"event_ids":json.loads(sys.argv[6]),"outcome":"Stale fixture must not acknowledge"},{"idempotency_key":new_id()});'
            'assert v.to_dict()["error"]["code"]=="inbox_generation_stale",v.to_dict();'
            'import bazarlyk.inbox.service;assert bazarlyk.inbox.service.__file__.startswith(sys.argv[1]);'
            'r.database.close();print("packaged stale acknowledgement refused")',
            manifest['components']['core']['path'],str(service.fixture.root),str(root),service.agent.id,consumer,json.dumps(event_ids)],
            env={**environment,'QORQUT_INSTALLATION_CONFIG':str(config_path),'QORQUT_APP_MANIFEST':str(manifest_path),
                'QORQUT_PACKAGE_SOURCES':config['package_sources']},capture_output=True,text=True)
        assert fenced.returncode==0,fenced.stderr+'\n'+fenced.stdout
        # Real root control refuses scoped credentials and invalid origins.
        with pytest.raises(HTTPError):request('/api/root/release/drain',{'timeout':1},agent=True)
        assert request('/api/root/release/drain',{'timeout':1})['data']['drained']
        assert request('/api/root/release/resume',{'timeout':1})['data']['drained'] is False
        with service.fixture.db.read() as uow:
            after=dict(uow.connection.execute('SELECT * FROM inbox_watchers WHERE agent_id=?',(service.agent.id,)).fetchone())
            assert after['consumer_id']==before[0]['consumer_id'] and after['binding_json']==before[0]['binding_json'] and after['generation']==1
            assert [dict(row) for row in uow.connection.execute('SELECT * FROM inbox_deliveries WHERE agent_id=?',(service.agent.id,))]==pending_before
        print(json.dumps({'release':manifest['release_id'],'core_commit':manifest['components']['core']['commit'],
            'pid':process.pid,'schema':112,'binding_protocol':1,'pending_events':len(pending_before)}))
    finally:
        if process and process.poll() is None:process.terminate();process.wait(timeout=15)
        owner.terminate();owner.wait(timeout=3)
        service.close()
