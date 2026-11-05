"""One extraction schedule, isolated targets, and durable owner review history."""
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from qorqut_operations.contracts import new_id
from bazarlyk.domains.learnings import sources
from foundation_support import BUSINESS, HUMAN, OTHER, context
from test_background_programs import runtime, invoke, raw_learning
from test_learning_sources import call, question, note


def routine(runtime):
    runtime.policy.human_global_capabilities = {HUMAN: {'manage_global_routines', 'read_global_context'}}
    return call(runtime, 'routines_create', {'scope':'global', 'definition':{
        'name':'Learning', 'program':'learnings.extract', 'arguments':{'passes':1},
        'schedule':'0 9 * * *', 'timezone':'UTC', 'status':'active', 'target':'each-business'}})['records'][0]


@pytest.mark.parametrize('scheduled', [False, True])
def test_one_routine_runs_global_once_and_each_business(runtime, scheduled):
    row = routine(runtime)
    question(runtime, global_scope=True)
    note(runtime)
    raw = raw_learning(); raw['scope'] = 'global'
    runtime.model.answers.extend([{'items':[raw]}, {'items':[raw_learning()]}])
    if scheduled:
        assert runtime.background.scheduler.tick(datetime(2026,9,10,9,tzinfo=timezone.utc)) == []
        ids = runtime.background.scheduler.tick(datetime(2026,9,11,9,tzinfo=timezone.utc))
        assert runtime.background.scheduler.tick(datetime(2026,9,11,9,tzinfo=timezone.utc)) == []
    else:
        ids = call(runtime, 'routines_run', {'scope':'global','routine_id':row['id']}, revision=row['revision'])['job_ids']
    expected = {None, BUSINESS, OTHER} if scheduled else {None, BUSINESS}
    assert len(ids) == len(expected)
    with runtime.database.read() as uow:
        jobs = [dict(uow.connection.execute('SELECT * FROM jobs WHERE id=?',(identity,)).fetchone()) for identity in ids]
        assert {j['business_id'] for j in jobs} == expected
        assert {j['kind'] for j in jobs} == {'learnings.extract'}
        assert len({json.loads(j['definition_ref_json'])['run_group_id'] for j in jobs}) == 1
    for identity in ids:
        assert runtime.background.run_one(identity)['state'] == 'succeeded'
    with runtime.database.read() as uow:
        for scope in (None, BUSINESS):
            rows = uow.repository('learning_candidates',business_id=scope).list()
            assert len(rows) == 1
            assert rows[0]['candidate_json']['evidence']['source']['business_id'] == scope
        assert not uow.repository('learning_candidates',business_id=OTHER).list()


@pytest.mark.parametrize('global_scope', [False, True])
def test_reviews_edits_and_notes_survive_backup_and_never_become_sources(runtime, tmp_path, global_scope):
    scope = None if global_scope else BUSINESS
    question(runtime, global_scope=global_scope)
    raw = raw_learning(); raw['scope'] = 'global' if global_scope else 'biz'
    runtime.model.answers.append({'items':[raw]})
    queued = call(runtime,'learnings_extract',{'passes':1},global_scope=global_scope)
    assert runtime.background.run_one(queued['job_id'])['state']=='succeeded'
    item = call(runtime,'learnings_list',{},global_scope=global_scope)['items'][0]
    for operation, args in [
        ('learnings_review', {'state':'rejected','feedback':'Too broad; preserve attribution.'}),
        ('learnings_review', {'state':'open'}),
        ('learnings_edit', {'changes':{'text':'Keep attributed source quotes.'}}),
        ('learnings_review', {'state':'accepted','feedback':'This version is right.'})]:
        item = call(runtime,operation,{'learning_id':item['id'],**args},global_scope=global_scope,revision=item['revision'])['item']
    with runtime.database.read() as uow:
        decisions = uow.repository('learning_decisions',business_id=scope).list()
        assert len(decisions)==1 and decisions[0]['state']=='accepted'
        history = uow.repository('learning_history',business_id=scope).list()
        assert len(history)==4
        assert all(h['actor_id']==HUMAN and h['created_at'] for h in history)
        serialized = json.dumps(history)
        assert all(text in serialized for text in ('Too broad; preserve attribution.','This version is right.','Keep attributed source quotes.'))
        seen={r['session_id']:r['transcript_hash'] for r in uow.repository('learning_scan_state',business_id=scope).list()}
        assert sources.snapshots(uow,scope,seen)==[]
    backup=tmp_path/'review-backup.sqlite3';runtime.database.backup(backup)
    from bazarlyk.storage.exports import build_exports
    exports=build_exports(backup,tmp_path)
    archived=[row for path,body in exports.files.items() if path.endswith('/learning_history.json')
              for row in json.loads(body)['records'] if row['candidate_id']==item['id']]
    assert len(archived)==4
    assert {r['id'] for r in archived}=={r['id'] for r in history}


def test_migration_retires_generated_routine_and_preserves_schedule_history(tmp_path, monkeypatch):
    from bazarlyk.storage import Database
    from foundation_support import database_at
    migrations = Database.migrations()
    with monkeypatch.context() as old:
        old.setattr(Database,'migrations',staticmethod(lambda:migrations[:32]))
        db=database_at(tmp_path)
        with db.transaction() as uow:
            repo=uow.repository('routines',business_id=None)
            for identity, target, program in [('evolution-extraction','each-business','learnings.extract'),
                ('global-learning-extraction','root','learnings.extract_global')]:
                repo.insert({'namespace':'global','id':identity,'name':identity,'scope':'global','target':target,
                    'schedule':'0 9 * * *','timezone':'UTC','status':'paused','job_kind':program,
                    'arguments_json':{},'timeout_seconds':7200})
            uow.repository('routine_slots').insert({'namespace':'global','routine_id':'global-learning-extraction',
                'target_namespace':'global','schedule_revision':1,'slot':'2026-09-10T09:00:00Z',
                'wall_slot':'2026-09-10T09:00','state_json':{'fired':True}})
            uow.repository('routine_runs',business_id=None).insert({'id':new_id(),'namespace':'global',
                'routine_id':'global-learning-extraction','trigger':'schedule','state':'succeeded',
                'started_at':'2026-09-10T09:00:00Z'})
    with monkeypatch.context() as target:
        target.setattr(Database,'migrations',staticmethod(lambda:migrations[:33]))
        db.initialize()
    with db.read() as uow:
        main=uow.repository('routines',business_id=None).get({'namespace':'global','id':'evolution-extraction'})
        retired=uow.repository('routines',business_id=None).get({'namespace':'global','id':'global-learning-extraction'})
        assert retired['metadata_json']['retired'] is True and retired['status']=='paused'
        assert main['status']=='paused' and main['schedule']=='0 9 * * *' and main['revision']==1
        slots=uow.repository('routine_slots').list()
        assert len(slots)==2 and all(s['slot']=='2026-09-10T09:00:00Z' for s in slots)
        assert len(uow.repository('routine_runs',business_id=None).list())==1
        assert not uow.connection.execute('PRAGMA foreign_key_check').fetchall()

    db.initialize()
    with db.read() as uow:
        assert not uow.repository('routines',business_id=None).get({'namespace':'global','id':'global-learning-extraction'})
        slots=uow.repository('routine_slots').list()
        assert len(slots)==1 and slots[0]['routine_id']=='evolution-extraction'
        assert not uow.repository('routine_runs',business_id=None).list()


@pytest.mark.parametrize('in_flight', [False, True])
def test_obsolete_routines_and_history_removed_but_active_shared_sync_kept(tmp_path, monkeypatch, in_flight):
    from bazarlyk.storage import Database
    from foundation_support import database_at
    migrations=Database.migrations()
    with monkeypatch.context() as old:
        old.setattr(Database,'migrations',staticmethod(lambda:migrations[:33]))
        db=database_at(tmp_path)
        with db.transaction() as uow:
            definitions=[(None,'learn-apply','learnings.apply_accepted','paused','root',{}),
                (BUSINESS,'repository-status','repository.status','paused','business',{'legacy':{'definition':{'id':'repository-status'}}}),
                (BUSINESS,'old-sync','email.sync','paused','business',{}),
                (OTHER,'unreplaced-sync','email.sync','paused','business',{}),
                (None,'shared-sync','email.sync','active','each-business',{})]
            for scope, identity, program, status, target, metadata in definitions:
                ns=scope or 'global'
                uow.repository('routines',business_id=scope).insert({'namespace':ns,'id':identity,'name':identity,
                    'scope':'business' if scope else 'global','target':target,'schedule':'*/10 * * * *','timezone':'UTC',
                    'status':status,'job_kind':program,'arguments_json':{},'timeout_seconds':900,'metadata_json':metadata})
                uow.repository('routine_runs',business_id=BUSINESS if identity=='shared-sync' else scope).insert({
                    'id':new_id(),'namespace':ns,'routine_id':identity,'trigger':'schedule','state':'succeeded',
                    'started_at':'2026-09-10T09:00:00Z'})
                uow.repository('routine_slots').insert({'namespace':ns,'routine_id':identity,'target_namespace':scope or 'global',
                    'schedule_revision':1,'slot':'2026-09-10T09:00:00Z','wall_slot':'2026-09-10T09:00','state_json':{}})
            if in_flight:
                uow.repository('jobs',business_id=None).insert({'id':new_id(),'target_namespace':'global',
                    'kind':'learnings.apply_accepted','principal_id':HUMAN,'arguments_json':{},
                    'definition_ref_json':{'routine_id':'learn-apply'},'dedupe_key':new_id(),'state':'running'})
    db.initialize()
    expected={'shared-sync','unreplaced-sync'} | ({'learn-apply'} if in_flight else set())
    with db.read() as uow:
        assert {r[0] for r in uow.connection.execute('SELECT id FROM routines')}==expected
        assert {r[0] for r in uow.connection.execute('SELECT routine_id FROM routine_runs')}==expected
        assert {r[0] for r in uow.connection.execute('SELECT routine_id FROM routine_slots')}==expected
        assert not uow.connection.execute('PRAGMA foreign_key_check').fetchall()
