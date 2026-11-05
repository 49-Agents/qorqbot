from dataclasses import replace
from qorqut_operations.contracts import new_id
from foundation_support import BUSINESS, HUMAN, OTHER, context
from test_business_progress import fixture


def test_global_daily_delivery_and_first_acceptance_counts(tmp_path):
    db, app = fixture(tmp_path)
    with db.transaction() as u:
        u.repository('installation').insert({'id':'service','installation_id':new_id(),'owner_principal_id':HUMAN})
        u.connection.execute('DELETE FROM business_memberships WHERE business_id=?',(OTHER,))
        agents=[]
        for i,biz in enumerate([None,BUSINESS,OTHER]):
            agent=new_id();agents.append(agent)
            u.repository('principals').insert({'id':agent,'kind':'agent','display_name':'Agent'})
            u.repository('agent_profiles',business_id=biz).insert({'agent_id':agent,'principal_id':agent,'home_business_id':biz,'namespace':biz or 'ecosystem','name':'agent'+str(i),'workspace':str(tmp_path/agent),'profile_json':{}})
        for biz, recipients in [(None,agents[:2]),(BUSINESS,agents[:2]),(OTHER,agents[2:])]:
            event=u.connection.execute("INSERT INTO inbox_events(business_id,source_type,source_id,event_type,summary,created_at) VALUES(?,?,?,?,?,?)",(biz,'task','x','updated','message','2026-09-13T01:00:00Z')).lastrowid
            for agent in recipients:u.connection.execute('INSERT INTO inbox_deliveries(agent_id,event_id,acknowledged_at) VALUES(?,?,?)',(agent,event,'2026-09-13T02:00:00Z'))
        for biz in [None,BUSINESS,OTHER]:
            candidate=new_id()
            u.repository('learning_candidates',business_id=biz).insert({'id':candidate,'candidate_json':{'text':'Keep it concise'}})
            for date,state in [('2026-09-12T09:00:00Z','accepted'),('2026-09-12T10:00:00Z','open'),('2026-09-13T09:00:00Z','accepted')]:
                u.repository('learning_history',business_id=biz).insert({'id':new_id(),'candidate_id':candidate,'event':'learnings_review','actor_id':HUMAN,'before_json':{},'after_json':{'state':state},'created_at':date})
    ctx=replace(context(global_capabilities=frozenset({'read_global_context'})),business_id=None,review_business_id=None)
    for days in [30,120,365]:
        result=app.invoke(ctx,'global_progress_get',{'days':days})
        assert result.status=='ok',result.to_dict()
        rows=result.data['series'];assert len(rows)==days
        # Every delivery, including acknowledgements and system events. 01:00Z on the 13th is
        # 21:00 on the 12th in San Juan (UTC-04:00), so it belongs to the previous operational day.
        assert rows[-2]['inbox_messages']==4 and rows[-1]['inbox_messages']==0
        assert rows[-1]['new_tickets'] == 0
        assert rows[-2]['learnings_accepted']==2
        assert rows[-1]['learnings_accepted']==0
    with db.transaction() as u:u.connection.execute('DELETE FROM installation')
    assert app.invoke(ctx,'global_progress_get',{'days':30}).status=='error'


def test_owner_http_route_exposes_global_progress(tmp_path):
    from starlette.testclient import TestClient
    from test_service_transports import ServiceFixture, ORIGIN
    service = ServiceFixture(tmp_path, full=True)
    with TestClient(service.web) as client:
        response = client.post('/api/root/operations/global_progress_get', json={'days':30}, headers=ORIGIN)
        assert response.status_code == 200, response.text
        rows = response.json()['data']['series']
        assert len(rows) == 30
        assert {'tasks_done', 'inbox_messages', 'learnings_accepted', 'new_tickets', 'actions'} <= rows[0].keys()
        assert not {'emails_sent', 'cold_outreach_emails_sent'} & rows[0].keys()


def test_global_daily_new_tickets_counts_rows_by_creation_day_across_kinds_and_status(tmp_path):
    db, app = fixture(tmp_path)
    with db.transaction() as u:
        u.repository('installation').insert({'id': 'service', 'installation_id': new_id(), 'owner_principal_id': HUMAN})
        u.connection.execute('DELETE FROM business_memberships WHERE business_id=?', (OTHER,))
        ticket_ids = []
        for i, (business_id, kind, status, created_at) in enumerate([
            (None, 'bug', 'open', '2026-09-12T09:00:00Z'),
            (None, 'feature', 'closed', '2026-09-12T10:00:00Z'),
            (BUSINESS, 'tooling', 'open', '2026-09-13T09:00:00Z'),
            (BUSINESS, 'workflow', 'closed', '2026-09-13T10:00:00Z'),
            (OTHER, 'bug', 'open', '2026-09-13T11:00:00Z'),
            (None, 'workflow', 'open', '2026-08-14T11:00:00Z'),
        ]):
            ticket_id = new_id()
            ticket_ids.append(ticket_id)
            u.repository('dev_tickets').insert({
                'id': ticket_id, 'title': f'Ticket {i}', 'details': '', 'kind': kind,
                'status': status, 'reporter_id': HUMAN, 'business_id': business_id,
                # A later update must not move the ticket to a new bucket.
                'created_at': created_at, 'updated_at': '2026-09-13T12:00:00Z',
            })
        # History records and a reopen do not create additional tickets or move
        # the original ticket away from its creation day.
        for i, event_kind in enumerate(('follow_up', 'status_changed')):
            u.repository('dev_ticket_events').insert({
                'id': new_id(), 'ticket_id': ticket_ids[0], 'event_kind': event_kind,
                'actor_id': HUMAN, 'created_at': f'2026-09-13T{12 + i:02d}:00:00Z',
                'details': 'Updated and reopened', 'resolution': '', 'verification': '', 'deployment_ref': '',
            })
    ctx = replace(context(global_capabilities=frozenset({'read_global_context'})), business_id=None, review_business_id=None)
    result = app.invoke(ctx, 'global_progress_get', {'days': 30})
    assert result.status == 'ok', result.to_dict()
    rows = {row['date']: row for row in result.data['series']}
    assert rows['2026-09-12']['new_tickets'] == 2
    assert rows['2026-09-13']['new_tickets'] == 2
    assert sum(row['new_tickets'] for row in result.data['series']) == 4
