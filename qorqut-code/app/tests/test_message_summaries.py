import sqlite3

import pytest
from jsonschema import Draft202012Validator

from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.conversations import SUMMARY, normalize_summary
from foundation_support import context
from test_domain_tasks import make_tasks, invoke
from test_domain_documents import setup_documents
from test_domain_stories import create, history
from test_agent_inbox import inbox
from test_agent_messages import call, sent
from test_signoffs import scenario


@pytest.mark.parametrize('value', ['', '   ', 'a' * 501, 3])
def test_invalid_summary(value):
    assert not Draft202012Validator(SUMMARY).is_valid(value)
    with pytest.raises(DomainError):
        normalize_summary(value)


def test_boundary_and_unicode_whitespace():
    value='  ' + '\u2003\n'.join(str(n) for n in range(20)) + '  '
    assert Draft202012Validator(SUMMARY).is_valid(value)
    assert normalize_summary(value)==' '.join(str(n) for n in range(20))
    assert normalize_summary(None) is None


def test_task_full_body_summary_replay_and_invalid_no_mutation(tmp_path):
    db, app = make_tasks(tmp_path)
    assert invoke(app,'tasks_create',{'id':'a','title':'A'}).status=='ok'
    args={'task_id':'a','text':'Full details including the evidence and uncertainty.','summary':'Evidence still needs owner review'}
    for _ in range(2):
        result=invoke(app,'tasks_comment',args,revision=1,key='summary')
        assert result.status=='ok',result.to_dict()
    notes=app.invoke(context(),'tasks_thread_get',{'task_id':'a'}).data['notes']
    assert len(notes)==1 and notes[0]['body']==args['text'] and notes[0]['summary']==args['summary']
    assert invoke(app,'tasks_comment',{**args,'summary':'a'*501},revision=2).status=='error'
    assert invoke(app,'tasks_update',{'task_id':'a','changes':{'title':'B'},'summary':'No full note'},revision=2).status=='error'
    assert len(app.invoke(context(),'tasks_thread_get',{'task_id':'a'}).data['notes'])==1
    longer='This summary has more words than suggested but still preserves the full message and is accepted'
    result=invoke(app,'tasks_comment',{**args,'summary':longer},revision=2)
    assert result.status=='ok',result.to_dict()
    notes=app.invoke(context(),'tasks_thread_get',{'task_id':'a'}).data['notes']
    assert len(notes)==2
    assert any(note['body']==args['text'] and note['summary']==longer for note in notes)


def test_story_draft_summary_preserves_clears_and_overrides(tmp_path):
    _,app,_=setup_documents(tmp_path)
    story=create(app);draft=new_id()
    args={'story_id':story['id'],'draft_id':draft,'body':'The complete draft with evidence.','summary':'Draft evidence needs review'}
    assert invoke(app,'stories_draft_save',args).status=='ok'
    assert invoke(app,'stories_draft_save',{k:v for k,v in args.items() if k!='summary'},revision=1).status=='ok'
    result=invoke(app,'stories_draft_commit',{'draft_id':draft},revision=1)
    assert result.status=='ok',result.to_dict()
    assert result.data['event']['body']==args['body']
    assert result.data['event']['summary']==args['summary']
    draft=new_id();args['draft_id']=draft
    assert invoke(app,'stories_draft_save',args).status=='ok'
    assert invoke(app,'stories_draft_save',{'story_id':story['id'],'draft_id':draft,'body':'Changed full body'},revision=1).status=='ok'
    assert app.invoke(context(),'stories_drafts_list',{'story_id':story['id']}).data['drafts'][0]['summary'] is None
    result=invoke(app,'stories_draft_commit',{'draft_id':draft,'summary':'Changed evidence needs review'},revision=2)
    assert result.data['event']['summary']=='Changed evidence needs review'
    result=invoke(app,'stories_note_add',{'story_id':story['id'],'body':'Complete direct note','summary':'Direct note explains evidence'})
    assert result.data['event']['summary']=='Direct note explains evidence'
    assert history(app,story)['events'][0]['body']=='Complete direct note'


def test_direct_and_management_preserve_full_delivery(inbox):
    s=inbox
    full='All details stay in the message delivered to the recipient, including this important final qualification.'
    summary='Recipient gets the complete details'
    first=sent(call(s,s.manager,'agents_message_send',{'to_agent_id':s.agent.id,'message':full,'summary':summary},business=s.fixture.business_id))
    assert first['body']==full and first['summary']==summary
    delivery=s.call('inbox_list',{'consumer_id':s.consumer}).data['items'][0]['message']
    assert delivery['body']==full and delivery['summary']==summary
    with s.fixture.db.transaction() as u:
        u.connection.execute("UPDATE installation SET management_coordinator_id=? WHERE id='service'",(s.manager.id,))
    result=call(s,s.agent,'management_message_send',{'message':full,'summary':summary})
    assert result.status=='ok',result.to_dict()
    assert result.data['routing']['message_summary']==summary
    with s.fixture.db.read() as u:
        row=u.connection.execute('SELECT * FROM agent_messages WHERE id=?',(result.data['message_id'],)).fetchone()
        assert row['body']==full and row['summary']==summary
    assert call(s,s.agent,'management_message_send',{'message':full,'summary':'word '*16}).status=='ok'


def test_signoff_summary_does_not_replace_full_message(scenario):
    s=scenario;proposal=s.propose()
    result=s.app.invoke(s.owner_context,'signoffs_comment',{'signoff_id':proposal['id'],'text':'Please revise the proposal with the required evidence.','summary':'Proposal needs additional supporting evidence'},{'idempotency_key':'summary'})
    assert result.status=='ok',result.to_dict()
    with s.db.transaction() as u:
        row=s.service._row(s.owner_context,proposal['id'],u)
        with pytest.raises(DomainError):
            s.service.notes.decision_message(s.owner_context,row,'',u,rejecting=True,summary='A summary alone cannot reject')
        with pytest.raises(sqlite3.IntegrityError):
            u.connection.execute("UPDATE signoff_notes SET summary='edited'")
    notes=s.app.invoke(s.owner_context,'signoffs_notes_list',{'signoff_id':proposal['id']}).data['notes']
    assert len(notes)==1 and notes[0]['summary']=='Proposal needs additional supporting evidence'


def test_migration_keeps_immutable_history_without_notifications(tmp_path,monkeypatch):
    from bazarlyk.storage import Database
    from foundation_support import BUSINESS,HUMAN,database_at
    migrations=Database.migrations()
    with monkeypatch.context() as patch:
        patch.setattr(Database,'migrations',staticmethod(lambda:[m for m in migrations if m[0]<67]))
        db=database_at(tmp_path)
        with db.transaction() as u:
            u.repository('tasks',business_id=BUSINESS).insert({'id':'old','title':'Old task','status':'open','created_by':HUMAN})
            u.repository('task_notes',business_id=BUSINESS).insert({'id':'old-note','task_id':'old','body':'Historical full message','created_by':HUMAN,'created_at':'2026-09-15T00:00:00Z'})
        with db.read() as u:
            counts=[u.connection.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('inbox_changes','inbox_events','task_notes')]
    db.initialize();db.initialize()
    with db.read() as u:
        row=u.repository('task_notes',business_id=BUSINESS).get('old-note')
        assert row['body']=='Historical full message' and row['summary'] is None
        assert counts==[u.connection.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('inbox_changes','inbox_events','task_notes')]
        assert not u.connection.execute('PRAGMA foreign_key_check').fetchall()
