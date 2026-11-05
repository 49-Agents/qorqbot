import json
from dataclasses import replace
from qorqut_operations.contracts import InvocationOptions, new_id
from test_environment_discovery import service, invoke as call

def invoke(*args, **kwargs):
    kwargs.setdefault("idempotency_key", new_id())
    return call(*args, **kwargs)


def grant(s, mode="allow"):
    s.fixture.update(s.agent, {"tool_policy": {"overrides": {"environment_set": mode, "environment_get": "allow"}}})


def test_business_settings_write_preserves_scope_and_retries(service):
    s=service
    (s.runtime.root/'.env').write_text('RAILWAY_TOKEN=global\n')
    (s.fixture.hq/'.env').write_text('# keep comment\nOTHER=keep\n')
    key=new_id();args={"key":"RAILWAY_TOKEN","value":"business-token"}
    assert invoke(s,s.agent,'environment_set',args).status=='error'
    grant(s)
    first=invoke(s,s.agent,'environment_set',args,idempotency_key=key)
    assert first.status=='ok',first.to_dict()
    assert invoke(s,s.agent,'environment_set',args,idempotency_key=key).status=='ok'
    assert (s.runtime.root/'.env').read_text()=='RAILWAY_TOKEN=global\n'
    assert '# keep comment\nOTHER=keep\n' in (s.fixture.hq/'.env').read_text()
    assert invoke(s,s.agent,'environment_get',{'key':'RAILWAY_TOKEN'}).data['value']=='business-token'
    assert invoke(s,s.agent,'environment_set',{'key':'RAILWAY_TOKEN','value':''}).status=='ok'
    assert invoke(s,s.agent,'environment_get',{'key':'RAILWAY_TOKEN'}).data['value']==''
    assert invoke(s,s.agent,'environment_set',{'key':'BIZ_DIR','value':'/tmp'}).status=='error'
    assert invoke(s,s.agent,'environment_set',{'key':'TOKEN','value':'bad\nOTHER=x'}).status=='error'
    ctx=s.runtime.resolver.invocation_context(s.agent)
    assert s.runtime.application.invoke(replace(ctx,business_id=None),'environment_set',args).status=='error'


def test_private_binding_preserved_and_signoff_is_exact(service):
    s=service;grant(s)
    path=s.fixture.hq/'.env.private.json'
    path.write_text(json.dumps({'TOKEN':{'value':'old','allowed_roles':['business-agent']},'OTHER':{'value':'keep','allowed_roles':['founder']}}))
    assert invoke(s,s.agent,'environment_set',{'key':'TOKEN','value':'new'}).status=='ok'
    data=json.loads(path.read_text());assert data['TOKEN']=={'value':'new','allowed_roles':['business-agent']}
    assert data['OTHER']['value']=='keep'
    assert invoke(s,s.agent,'environment_set',{'key':'OTHER','value':'no'}).status=='error'
    grant(s,'signoff')
    result=invoke(s,s.agent,'environment_set',{'key':'TOKEN','value':'proposal'})
    assert result.status=='signoff_required',result.to_dict()
    assert json.loads(path.read_text())['TOKEN']['value']=='new'

    from test_person_details import approve
    approve(s, result)
    assert json.loads(path.read_text())["TOKEN"]["value"] == "proposal"
    pending = invoke(s, s.agent, "environment_set", {"key": "TOKEN", "value": "stale"})
    data = json.loads(path.read_text()); data["TOKEN"]["value"] = "owner-newer"
    path.write_text(json.dumps(data))
    approve(s, pending, stale=True)
    assert json.loads(path.read_text())["TOKEN"]["value"] == "owner-newer"
