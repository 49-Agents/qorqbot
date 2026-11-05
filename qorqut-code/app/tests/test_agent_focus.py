"""Real policies, references, persistence and owner-message delivery, no live agents."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qorqut_operations.contracts import DomainError, new_id, plain
from bazarlyk.inbox.focus import FocusService, valid_desktop_url
from bazarlyk.domains.relationships.items import ContextItems
from test_ordubashy_owner_inbox import inbox, task, edit


@pytest.fixture
def focus(inbox):
    s = inbox
    import json
    with s.db.transaction() as u:
        u.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'",(s.ordubashy.id,))
        profile=u.repository("agent_profiles",business_id=None).get(s.ordubashy.id)["profile_json"]
        profile["runtime"]="codex"
        u.connection.execute("UPDATE agent_profiles SET profile_json=? WHERE agent_id=?",(json.dumps(profile),s.ordubashy.id))
    from bazarlyk.transport.auth import human_global_capabilities
    s.policy.human_global_capabilities = human_global_capabilities
    s.inbox.domains.stories.relationships = SimpleNamespace(items=ContextItems(s.inbox.domains.stories, s.app))
    s.focus = FocusService(s.inbox, s.service).register()
    s.consumer = new_id()
    with s.db.transaction() as u:
        registration = new_id()
        u.repository('machine_registrations').insert({'id': registration, 'principal_id': s.ordubashy.id,
            'workspace': str(s.root / 'agent'), 'service_url': 'http://test', 'profile_revision': 1})
    s.registration = registration
    s.inbox.start_session(s.ctx(), registration, s.consumer, 'startup')
    s.get = lambda: ok(s.app.invoke(replace(s.ctx(), business_id=None), 'focus_get', {'scope': 'global'}))
    def call(op, **args):
        return s.app.invoke(replace(s.ctx(), business_id=None), op, {'scope': 'global', 'consumer_id': s.consumer,
            'expected_focus_revision': s.get()['revision'], **args}, {'idempotency_key': new_id()})
    s.focus_call = call
    s.ref = lambda business=None: {'business_id': business or s.business, 'type': 'task', 'id': 'owner-task', 'reason': 'Need your input'}
    return s


def ok(result):
    assert result.status == 'ok', result.to_dict()
    return plain(result.data)


def pending(s):
    browser={'request_id':new_id(),'state':'pending','prompt':'Enter code','app':'Chrome','tab':'Account'}
    with s.db.transaction() as u:
        ctx=s.focus.context(s.ctx(),u)
        s.focus.write(ctx,s.focus.row(ctx,u),{'browser_json':browser,'items_json':[],
            'status':'Legacy input','consumer_id':s.consumer,'updated_at':'2026-09-15T00:00:00Z'},u)
    return browser


def test_legacy_callers_get_upgrade_without_clearing_pending_guard(focus):
    s=focus;browser=pending(s)
    for name,args in [('focus_show',{'status':'New','items':[]}),('focus_clear',{}),
            ('focus_browser_input',{'prompt':'New','app':'Chrome','tab':'Tab'})]:
        s.change_override(name,'allow',s.ordubashy)
        assert s.focus_call(name,**args).error['code']=='upgrade_required'
    assert s.get()['browser']['request_id']==browser['request_id']
    with pytest.raises(DomainError,match='Browser is with the owner'):
        s.focus.browser_activity(s.ctx(),s.consumer,'click',new_id(),'running')


def test_legacy_done_is_atomic_and_replay_safe(focus,monkeypatch):
    s=focus;browser=pending(s)
    original=s.inbox.messages.apply
    def fail(*args):
        original(*args)
        raise RuntimeError('delivery failed')
    with monkeypatch.context() as patch:
        patch.setattr(s.inbox.messages,'apply',fail)
        with pytest.raises(RuntimeError):s.focus.owner_done(s.owner_context,s.ordubashy.id,browser['request_id'])
    assert s.get()['browser']['state']=='pending'
    s.focus.owner_done(s.owner_context,s.ordubashy.id,browser['request_id'],'Completed')
    s.focus.owner_done(s.owner_context,s.ordubashy.id,browser['request_id'],'Retry')
    assert s.get()['browser']['state']=='done'
    with s.db.read() as u:
        assert u.connection.execute('SELECT count(*) FROM agent_messages').fetchone()[0]==1
    with pytest.raises(DomainError):s.focus.owner_done(s.owner_context,s.ordubashy.id,new_id())


@pytest.mark.parametrize('url',[None,'','javascript:alert(1)','data:text/html,x','//host/path',
    'http://user:secret@host/','http://host:bad/','http://host/\nother'])
def test_invalid_desktop_config_is_not_exposed(url):
    assert valid_desktop_url(url) is None


def test_desktop_reference_resolves_only_configured_url(focus):
    s=focus
    ctx=replace(s.ctx(),business_id=None,review_business_id=None)
    args={'scope':'global','reference':{'key':'login','title':'Log in','target':{'type':'desktop'}}}
    assert s.app.invoke(ctx,'focus_present',args,{'idempotency_key':new_id()}).error['code']=='configuration_required'
    s.focus.desktop_url=lambda:'http://desktop:1070/path/#/'
    card=ok(s.app.invoke(ctx,'focus_present',args,{'idempotency_key':new_id()}))['item']
    assert card['opening_target']['url']=='http://desktop:1070/path/#/'
    assert card['conversation']['type']=='reference'


def test_owner_browser_wrapper_reports_activity_and_never_executes_when_pending(tmp_path, monkeypatch):
    from bazarlyk.bootstrap import browser
    registration = {'registration_id':new_id()}
    consumer, calls, commands = new_id(), [], []
    class Client:
        def __init__(self, *args, **kwargs): pass
        def post(self, route, body):
            assert route == '/api/agent/focus/browser'
            calls.append(body)
            if block[0]: raise DomainError('input_pending','Wait for owner')
            return {'recorded':True}
    block = [False]
    monkeypatch.setattr(browser, 'load_registration', lambda p:registration)
    monkeypatch.setattr(browser, 'BootstrapClient', Client)
    monkeypatch.setattr(browser, 'run', lambda p,args:commands.append(args) or 0)
    assert browser.run_owner(tmp_path,['--consumer-id',consumer,'click','e1']) == 0
    assert [call['phase'] for call in calls] == ['running','active']
    assert calls[0]['command_id'] == calls[1]['command_id']
    assert calls[0]['consumer_id'] == consumer
    block[0] = True
    with pytest.raises(DomainError): browser.run_owner(tmp_path,['--consumer-id',consumer,'click','e1'])
    assert commands == [['click','e1']]
    with pytest.raises(browser.BrowserError): browser.run_owner(tmp_path,['attach'])
