from dataclasses import replace
import pytest
from qorqut_operations.contracts import new_id
from test_signoffs import Scenario

FORM = {'title': 'Choose a supplier', 'explanation': 'Which supplier should we use?', 'kind': 'question',
    'selection_mode': 'single', 'selection_required': True, 'notes_required': True,
    'choices': [{'id': 'a', 'label': 'First supplier', 'explanation': 'Available today'},
                {'id': 'b', 'label': 'Second supplier', 'explanation': 'Available next week'}]}

@pytest.fixture
def scenario(tmp_path):
    s = Scenario(tmp_path)
    s.change_override('signoffs_input_create', 'allow')
    s.change_override('signoffs_input_cancel', 'allow')
    s.change_override('signoffs_attention_set', 'allow')
    return s


def create(s, **updates):
    args = {'request_key': 'supplier', 'form': FORM, **updates}
    result = s.app.invoke(s.agent_context(), 'signoffs_input_create', args, {'idempotency_key': new_id()})
    assert result.status == 'ok', result.to_dict()
    return result.data['signoff']


def binding(row):
    return {'proposal_revision': row['proposal_revision'], 'payload_hash': row['payload_hash'],
        'expected_status_revision': row['status_revision'], 'idempotency_key': 'response-'+row['id']}


def test_create_and_submit_persists_response_without_effect(scenario):
    s = scenario
    row = create(s)
    assert row['category'] == 'input_request' and row['execution_state'] == 'not_applicable'
    assert s.service.list(s.owner_context,category='input_request',execution_states=['not_applicable']).data['items'][0]['id']==row['id']
    for notes, choices in [('', ['a']), ('Answer', ['a','b']), ('Answer', ['unknown'])]:
        result = s.service.respond(s.owner_context, row['id'], notes=notes, selected_choices=choices, **binding(row))
        assert result.status == 'error'
    result = s.service.respond(s.owner_context, row['id'], notes='Use the available supplier', selected_choices=['a'], **binding(row))
    assert result.status == 'ok', result.to_dict()
    saved = result.data['signoff']
    assert saved['state'] == 'answered'
    assert saved['decision']['notes'] == 'Use the available supplier'
    assert tuple(saved['decision']['selected_choices']) == ('a',)
    assert s.service.respond(s.owner_context, row['id'], notes='Use the available supplier', selected_choices=['a'], **binding(row)).data['replayed']
    assert s.service.respond(s.owner_context, row['id'], notes='Changed', selected_choices=['b'], **binding(row)).status == 'error'
    assert s.service.run_pending() == 0
    assert s.service.list(s.owner_context, responded=True).data['items'][0]['id'] == row['id']
    assert not s.service.list(s.owner_context, responded=False).data['items']


def test_required_choice_can_be_answered_with_notes(scenario):
    s = scenario
    row = create(s, request_key='supplier-notes')
    result = s.service.respond(s.owner_context, row['id'], notes='Use the supplier described in the notes',
        selected_choices=[], **binding(row))
    assert result.status == 'ok', result.to_dict()
    saved = result.data['signoff']
    assert saved['state'] == 'answered'
    assert saved['decision']['notes'] == 'Use the supplier described in the notes'
    assert tuple(saved['decision']['selected_choices']) == ()


def test_input_cannot_be_approved_and_same_key_deduplicates(scenario):
    s = scenario
    row = create(s)
    again = create(s)
    assert again['id'] == row['id']
    assert again['status_revision'] == row['status_revision']
    assert s.service.review(s.owner_context, row['id'], decision='approve', **binding(again)).status == 'error'
    conflict = s.app.invoke(s.agent_context(), 'signoffs_input_create', {'request_key': 'supplier', 'form': {**FORM, 'title': 'Different'}}, {'idempotency_key': new_id()})
    assert conflict.status == 'error'
    result = s.app.invoke(s.agent_context(), 'signoffs_input_cancel', {'signoff_id': row['id']}, {'idempotency_key': new_id()})
    assert result.status == 'ok', result.to_dict()
    assert result.data['signoff']['state'] == 'cancelled'
    assert create(s)['id'] != row['id']


def test_wrong_owner_scope_and_agent_cannot_respond(scenario):
    s = scenario
    row = create(s)
    for ctx in [s.agent_context(), replace(s.owner_context, business_id=s.other_business), replace(s.owner_context, principal=s.viewer)]:
        assert s.service.respond(ctx, row['id'], notes='yes', selected_choices=['a'], **binding(row)).status == 'error'


def test_attention_requires_reason_and_stays_scoped(scenario):
    s = scenario
    row = create(s, attention={'notify': True, 'blocking': True, 'reason': 'Need your choice'})
    assert row['attention']['blocking'] == 1
    assert row['attention']['priority_updated_by'] == s.agent_context().principal_id
    assert row['attention']['priority_updated_by_name']
    changed = s.app.invoke(s.owner_context, 'signoffs_attention_set', {'signoff_id':row['id'],
        'attention':{'urgent':True,'reason':'Owner priority'}}, {'idempotency_key':new_id()})
    assert changed.status == 'ok', changed.to_dict()
    attention = s.service.get(s.owner_context,row['id']).data['signoff']['attention']
    assert attention['priority_updated_by'] == s.owner.id
    assert attention['priority_updated_by_name']
    assert attention['priority_updated_at']
    bad = s.app.invoke(s.agent_context(), 'signoffs_attention_set', {'signoff_id': row['id'], 'attention': {'urgent': True}}, {'idempotency_key': new_id()})
    assert bad.status == 'error'


def test_invalid_forms_and_denied_creation_do_not_write(scenario):
    s=scenario
    invalid=[{**FORM,'choices':[FORM['choices'][0]]}, {**FORM,'choices':[FORM['choices'][0]]*2},
        {**FORM,'selection_mode':'none'}, {**FORM,'choices':[{**FORM['choices'][0],'label':'Three word label'},FORM['choices'][1]]}]
    for form in invalid:
        result=s.app.invoke(s.agent_context(),'signoffs_input_create',{'request_key':'invalid','form':form},{'idempotency_key':new_id()})
        assert result.status=='error'
    s.change_override('signoffs_input_create','deny')
    result=s.app.invoke(s.agent_context(),'signoffs_input_create',{'request_key':'denied','form':FORM},{'idempotency_key':new_id()})
    assert result.status=='error'
    with s.db.read() as uow: assert uow.connection.execute('SELECT count(*) FROM signoffs').fetchone()[0]==0


def test_multiple_and_text_only_inputs(scenario):
    s=scenario
    row=create(s,form={**FORM,'selection_mode':'multiple','notes_required':False})
    response=s.service.respond(s.owner_context,row['id'],selected_choices=['b','a'],**binding(row))
    assert response.status=='ok',response.to_dict()
    assert tuple(response.data['signoff']['decision']['selected_choices'])==('a','b')
    row=create(s,form={'title':'Supply access','explanation':'Please provide the account name','kind':'request','selection_mode':'none','notes_required':True})
    response=s.service.respond(s.owner_context,row['id'],notes='Use the team account',**binding(row))
    assert response.status=='ok'
    from qorqut_operations.contracts import DomainError
    with pytest.raises(DomainError):
        with s.db.transaction() as uow:
            uow.repository('signoffs',business_id=s.business).update(row['id'],{'decision_json':{'notes':'rewritten'}},expected_revision=response.data['signoff']['status_revision'])


def test_concurrent_responses_preserve_one_owner_answer(scenario):
    from concurrent.futures import ThreadPoolExecutor
    s=scenario; row=create(s)
    def answer(choice):
        return s.service.respond(s.owner_context,row['id'],notes=choice,selected_choices=[choice],
            **{**binding(row),'idempotency_key':new_id()})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(answer,['a','b']))
    assert sorted(r.status for r in results)==['error','ok']
    saved=s.service.get(s.owner_context,row['id']).data['signoff']
    assert saved['decision']['notes']==saved['decision']['selected_choices'][0]
    assert s.count('apply_receipts')==0


def test_migration_preserves_existing_proposal_and_receipt(tmp_path,monkeypatch):
    from bazarlyk.storage import Database
    from qorqut_operations.contracts import ChangeSet
    from bazarlyk.storage.receipts import ApplyReceipts
    migrations=Database.migrations
    with monkeypatch.context() as patch:
        patch.setattr(Database,'migrations',staticmethod(lambda:[m for m in migrations() if m[0]<66]))
        s=Scenario(tmp_path)
        proposal=s.propose()
        with s.db.transaction() as uow:
            original=uow.repository('signoffs',business_id=s.business).get(proposal['id'])
            plan=ChangeSet.from_bytes(original['change_set'],expected_hash=original['payload_hash'])
            ApplyReceipts(uow).create(plan,receipt_id=proposal['id'],signoff_id=proposal['id'])
    s.db.initialize()
    with s.db.read() as uow:
        migrated=uow.repository('signoffs',business_id=s.business).get(proposal['id'])
        assert {key:migrated[key] for key in original}==original
        assert uow.connection.execute('PRAGMA foreign_key_check').fetchone() is None
        assert uow.connection.execute('SELECT signoff_id FROM apply_receipts').fetchone()[0]==proposal['id']
    assert s.service.get(s.owner_context,proposal['id']).status=='ok'


def test_http_and_mcp_owner_input_flow(tmp_path):
    from starlette.testclient import TestClient
    from test_service_transports import ServiceFixture, ORIGIN
    service=ServiceFixture(tmp_path)
    with TestClient(service.web) as client:
        headers=service.initialize(client,service.agent)
        result=service.tool(client,headers,'signoffs_input_create',{'request_key':'http-question','form':FORM,
            'attention':{'notify':True,'blocking':True,'reason':'Need an answer'},'idempotency_key':new_id()})
        envelope=result['decoded']
        assert envelope['status']=='ok',envelope
        row=envelope['data']['signoff']
        service.runtime.signoffs.notifications.tick()
        overview=client.get('/api/me/attention').json()['data']
        assert overview['urgent'][0]['id']==row['id']
        deliveries=client.get('/api/me/attention/deliveries').json()['data']['items']
        assert deliveries[0]['items'][0]['id']==row['id']
        assert client.post('/api/me/attention/deliveries/'+deliveries[0]['id']+'/dismiss',json={},headers=ORIGIN).status_code==200
        path='/api/biz/widget/signoffs/'+row['id']+'/respond'
        body={**binding(row),'notes':'Use first','selected_choices':['a']}
        assert client.post(path,json=body).status_code==403
        response=client.post(path,json=body,headers=ORIGIN)
        assert response.status_code==200,response.text
        assert response.json()['data']['signoff']['decision']['notes']=='Use first'
        assert client.get('/api/biz/widget/signoffs?responded=true').json()['data']['items'][0]['id']==row['id']
        result=service.tool(client,headers,'signoffs_get',{'signoff_id':row['id']})['decoded']
        assert result['data']['signoff']['decision']['selected_choices']==['a']
        assert not client.get('/api/me/attention').json()['data']['urgent']
