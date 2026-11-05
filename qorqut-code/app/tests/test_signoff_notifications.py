from datetime import timedelta
from dataclasses import replace
from test_signoff_inputs import scenario, create, binding
from bazarlyk.service.changes import ChangeFeed
from qorqut_operations.signoffs.notifications import instant, stamp
from qorqut_operations.contracts import new_id


def clock(s, value):
    s.service.clock = lambda: value


def items(s):
    return s.service.notifications.deliveries(s.owner_context)['items']


def test_hourly_coalesced_reminders_snooze_and_answer(scenario):
    s = scenario
    clock(s, '2026-09-09T02:00:00.000000Z')
    row = create(s, attention={'notify': True, 'urgent': True, 'reason': 'Deadline'})
    n=s.service.notifications
    n.tick()
    assert len(items(s)) == 1
    first=items(s)[0]['id']
    n.tick()
    assert items(s)[0]['id'] == first
    clock(s, '2026-09-09T02:59:59.000000Z'); n.tick()
    assert items(s)[0]['id'] == first
    clock(s, '2026-09-09T03:00:00.000000Z'); n.tick()
    assert len(items(s)) == 1 and items(s)[0]['id'] != first
    n.snooze(s.owner_context,row['id'],'2026-09-09T06:00:00Z')
    assert not items(s)
    assert n.overview(s.owner_context)['urgent'][0]['id'] == row['id']
    clock(s, '2026-09-09T06:00:00.000000Z'); n.tick()
    assert len(items(s)) == 1
    s.service.respond(s.owner_context,row['id'],notes='yes',selected_choices=['a'],**binding(row))
    assert not items(s)
    assert not n.overview(s.owner_context)['urgent']


def test_daily_eastern_summary_and_no_empty_or_duplicate_summary(scenario):
    s=scenario; n=s.service.notifications
    clock(s,'2026-09-09T11:59:59.000000Z')
    create(s,attention={'notify':True})
    n.tick(); assert not items(s)
    clock(s,'2026-09-09T12:00:00.000000Z')
    n.tick(); assert len(items(s)) == 1 and items(s)[0]['kind']=='summary'
    first=items(s)[0]['id']
    n.tick(); assert items(s)[0]['id']==first
    n.dismiss(s.owner_context,first); assert not items(s)
    clock(s,'2026-09-10T12:00:00.000000Z')
    n.tick(); assert len(items(s)) == 1 and items(s)[0]['id']!=first
    # Winter Eastern time is UTC-5; never treat 08:00 as a fixed UTC offset.
    n.dismiss(s.owner_context,items(s)[0]['id'])
    clock(s,'2026-12-01T12:59:59.000000Z')
    n.tick(); assert not items(s)
    clock(s,'2026-12-01T13:00:00.000000Z')
    n.tick(); assert len(items(s))==1


def test_notification_writes_publish_the_coarse_signoffs_kind(scenario):
    """signoff_notifications/signoff_notification_schedule have no capture
    trigger and tick()/dismiss()/snooze() write outside Application.invoke
    (bazarlyk/signoffs/notifications.py), so without their own explicit
    operation the change feed (bazarlyk/service/changes.py) would never
    learn a delivery was created or cleared; PR3's /me/attention route
    depends on exactly this.
    """
    s = scenario
    changes = ChangeFeed()
    s.db.change_feed = changes
    n = s.service.notifications
    clock(s, '2026-09-09T12:00:00.000000Z')
    create(s, attention={'notify': True})  # signoffs_input_create: trigger-covered, bumps "signoffs" on its own.
    before_tick = changes.version("signoffs", "global")
    n.tick()
    assert len(items(s)) == 1
    after_tick = changes.version("signoffs", "global")
    assert after_tick > before_tick
    n.dismiss(s.owner_context, items(s)[0]['id'])
    assert changes.version("signoffs", "global") > after_tick


def test_dismiss_is_not_resolution_and_cancellation_suppresses(scenario):
    s=scenario; n=s.service.notifications
    row=create(s,attention={'notify':True,'blocking':True,'reason':'Need access'})
    n.tick(); n.dismiss(s.owner_context,items(s)[0]['id'])
    assert not items(s)
    assert n.overview(s.owner_context)['urgent']
    clock(s,stamp(instant(s.service.clock())+timedelta(hours=6)))
    n.tick(); assert len(items(s)) == 1
    s.app.invoke(s.agent_context(),'signoffs_input_cancel',{'signoff_id':row['id']},{'idempotency_key':new_id()})
    assert not items(s)


def test_counts_use_owner_assignment_and_configured_closed_states(scenario):
    s=scenario; n=s.service.notifications
    row=create(s)
    with s.db.transaction() as uow:
        from copy import deepcopy
        from bazarlyk.domains.tasks.models import DEFAULT_CONFIG
        config=deepcopy(DEFAULT_CONFIG); config['states'].append({'name':'finished','kind':'closed'})
        uow.repository('business_configs',business_id=s.business).insert({'domain':'tasks','config_json':config})
        for status,assigned,archived in [('open',True,False),('open',False,False),('finished',True,False),('open',True,True)]:
            identity=new_id()
            uow.repository('tasks',business_id=s.business).insert({'id':identity,'title':'Test task','status':status,'created_by':s.owner.id,'archived':archived})
            if assigned:
                uow.repository('relationships',business_id=s.business).insert({'id':new_id(),'from_type':'task','from_id':identity,
                    'to_type':'person','to_id':s.owner.id,'role':'assigned_to','context':''})
    business=next(b for b in n.overview(s.owner_context)['businesses'] if b['id']==s.business)
    assert business['tasks']==1 and business['signoffs']==1 and business['total']==2 and business['urgent']==0
    with s.db.transaction() as uow:
        uow.connection.execute('UPDATE business_memberships SET active=0 WHERE business_id=? AND principal_id=?',(s.business,s.owner.id))
    assert not any(b['id']==s.business for b in n.overview(replace(s.owner_context,business_id=None))['businesses'])


def test_notifications_are_owner_only_and_invalid_snooze_is_rejected(scenario):
    import pytest
    from qorqut_operations.contracts import DomainError
    s=scenario; n=s.service.notifications
    row=create(s,attention={'notify':True,'urgent':True,'reason':'Decision needed'})
    n.tick(); delivery=items(s)[0]
    assert not n.deliveries(replace(s.owner_context,principal=s.viewer))['items']
    with pytest.raises(DomainError): n.overview(s.agent_context())
    with pytest.raises(DomainError): n.dismiss(replace(s.owner_context,principal=s.viewer),delivery['id'])
    for until in ['', None, '2026-09-08T13:00:00', '2000-01-01T00:00:00Z', '2099-01-01T00:00:00Z']:
        with pytest.raises(DomainError): n.snooze(s.owner_context,row['id'],until)
    with s.db.transaction() as uow:
        uow.connection.execute('UPDATE business_memberships SET is_owner=0 WHERE business_id=? AND principal_id=?',(s.business,s.owner.id))
    assert not n.deliveries(s.owner_context)['items']


def test_global_input_is_separate_and_not_visible_to_business_agent(scenario):
    s=scenario; n=s.service.notifications
    with s.db.transaction() as uow:
        uow.repository('installation').insert({'id':'service','installation_id':new_id(),'owner_principal_id':s.owner.id})
    s.policy.human_global_capabilities={s.owner.id: {'read_global_context','manage_shared_instructions'}}
    root=s.add_agent('ordubashy',{'signoffs_input_create':'allow'})
    from test_signoff_inputs import FORM
    result=s.app.invoke(s.agent_context(root),'signoffs_input_create',{'scope':'global','request_key':'global-input','form':FORM,
        'attention':{'notify':True,'blocking':True,'reason':'Need global decision'}},{'idempotency_key':new_id()})
    assert result.status=='ok',result.to_dict()
    row=result.data['signoff']; assert row['review_business_id'] is None
    assert s.service.get(s.agent_context(),row['id']).status=='error'
    n.tick(); assert any(i['items'][0]['id']==row['id'] for i in items(s))
    overview=n.overview(s.owner_context)
    assert next(b for b in overview['businesses'] if b['id'] is None)['urgent']==1
    business_row=create(s,attention={'notify':True,'urgent':True,'reason':'Business decision'})
    overview=n.overview(s.owner_context)
    assert sorted(r['id'] for r in overview['urgent'])==sorted([row['id'],business_row['id']])
    assert next(b for b in overview['businesses'] if b['id']==s.business)['signoffs']==1
    assert next(r for r in overview['urgent'] if r['id']==business_row['id'])['business_slug'] is not None


def test_restart_and_concurrent_ticks_do_not_duplicate(scenario):
    from concurrent.futures import ThreadPoolExecutor
    from qorqut_operations.signoffs.notifications import SignoffNotifications
    s=scenario; create(s,attention={'notify':True,'urgent':True,'reason':'Need decision'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:SignoffNotifications(s.service).tick(), range(2)))
    assert len(items(s))==1
    saved=items(s)[0]['id']
    SignoffNotifications(s.service).tick()
    assert items(s)[0]['id']==saved


def test_empty_summary_and_revoked_attention_never_deliver(scenario):
    s=scenario; n=s.service.notifications
    clock(s,'2026-09-09T12:00:00.000000Z'); n.tick()
    assert not items(s)
    with s.db.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM signoff_notifications').fetchone()[0]==0
    row=create(s,attention={'notify':True,'blocking':True,'reason':'Access missing'})
    n.tick(); assert items(s)
    result=s.app.invoke(s.agent_context(),'signoffs_attention_set',{'signoff_id':row['id'],'attention':{'notify':False}}, {'idempotency_key':new_id()})
    assert result.status=='ok',result.to_dict()
    assert not items(s)
    assert not n.overview(s.owner_context)['urgent']


def test_existing_prepared_approval_can_notify_without_changing_payload(scenario):
    s=scenario; n=s.service.notifications
    from test_management_backend import install_coordinator
    install_coordinator(s)
    row=s.propose()
    result=s.app.invoke(s.agent_context(),'signoffs_attention_set',{'signoff_id':row['id'],
        'attention':{'notify':True,'urgent':True,'reason':'Deadline today'}},{'idempotency_key':new_id()})
    assert result.status=='ok',result.to_dict()
    updated=result.data['signoff']
    assert updated['payload_hash']==row['payload_hash']
    n.tick(); assert items(s)[0]['items'][0]['id']==row['id']
    assert s.decide(updated,decision='reject',feedback='Keep current settings').status=='ok'
    assert not items(s)
