"""Real migration/transaction tests for deterministic incident delivery."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json
from pathlib import Path

import pytest
from qorqut_operations.contracts import DomainError, OperationResult, new_id
from bazarlyk.incidents import IncidentReporter, IncidentOperations, drain_spool
from bazarlyk.storage import Database


@pytest.fixture
def system(tmp_path):
    db = Database.for_root(tmp_path).initialize()
    business, reporter_id = new_id(), new_id()
    actors = [new_id(),new_id()]
    now = datetime(2026,9,15,tzinfo=timezone.utc)
    with db.transaction() as u:
        u.repository('principals').insert({'id':reporter_id,'kind':'system','display_name':'Reporter'})
        u.repository('businesses').insert({'id':business,'slug':'fixture','name':'Fixture',
            'workspace':str(tmp_path/'biz'),'hq_path':str(tmp_path/'biz/hq')})
        for actor in actors:
            u.repository('principals').insert({'id':actor,'kind':'agent','display_name':actor})
            u.repository('agent_profiles',business_id=business).insert({'agent_id':actor,'principal_id':actor,
                'home_business_id':business,'namespace':business,'name':actor,'workspace':str(tmp_path/actor),'profile_json':{}})
            u.repository('business_memberships',business_id=business).insert({'principal_id':actor})
    reporter = IncidentReporter(tmp_path/'spool',clock=lambda:now.isoformat())
    contexts = [SimpleNamespace(principal_id=actor,principal_kind='agent',business_id=business,
                  allowed_business_ids=frozenset({business}),global_capabilities=frozenset()) for actor in actors]
    return SimpleNamespace(db=db,reporter=reporter,reporter_id=reporter_id,contexts=contexts,business=business,
        advance=lambda seconds:setattr(reporter,'clock',lambda:(now+timedelta(seconds=seconds)).isoformat()),
        drain=lambda:drain_spool(reporter,db,reporter_id=reporter_id))


def fail(s,ctx=None,code='internal_error',retryable=False):
    return s.reporter.observe_result(ctx or s.contexts[0],'tasks_get',
        OperationResult.failure(DomainError(code,'SECRET credential/error body',retryable=retryable)))


def scalar(s,sql,args=()):
    with s.db.read() as u:
        return u.connection.execute(sql,args).fetchone()[0]


def test_normal_workflow_never_files_and_transient_threshold_survives_restart(system):
    s=system
    for code in ('forbidden','policy_denied','validation_error','login_required','revision_conflict','configuration_missing','custom_business_hold'):
        assert fail(s,code=code)['status']=='ignored'
    assert scalar(s,'SELECT count(*) FROM dev_tickets')==0
    fail(s,code='busy',retryable=True); s.drain()
    s.advance(300); fail(s,code='busy',retryable=True); s.drain()
    assert scalar(s,'SELECT count(*) FROM dev_tickets')==0
    replacement=IncidentReporter(s.reporter.spool_dir,clock=lambda:'2026-09-15T00:10:01+00:00')
    replacement.observe_result(s.contexts[0],'tasks_get',OperationResult.failure(DomainError('busy','private',retryable=True)))
    drain_spool(replacement,s.db,reporter_id=s.reporter_id)
    assert scalar(s,'SELECT count(*) FROM dev_tickets')==1
    assert scalar(s,'SELECT classification FROM system_incidents')=='infrastructure_fault'


def test_failure_success_per_agent_recurrence_and_no_recovery_loop(system):
    s=system
    for ctx in s.contexts: fail(s,ctx)
    s.drain()
    identity=scalar(s,'SELECT id FROM system_incidents')
    assert scalar(s,'SELECT occurrence_count FROM system_incidents')==2
    assert scalar(s,'SELECT count(*) FROM system_incident_impacts')==2
    s.advance(60)
    s.reporter.observe_success(s.contexts[0],'tasks_get'); s.drain()
    assert scalar(s,'SELECT count(*) FROM inbox_deliveries')==1
    assert scalar(s,'SELECT count(*) FROM system_incident_impacts WHERE recovered_at IS NULL')==1
    for _ in range(10): s.reporter.observe_success(s.contexts[0],'tasks_get')
    s.drain()
    assert scalar(s,'SELECT count(*) FROM inbox_deliveries')==1
    assert scalar(s,'SELECT occurrence_count FROM system_incidents')==2
    s.reporter.observe_success(s.contexts[1],'tasks_get'); s.drain()
    assert scalar(s,'SELECT status FROM system_incidents')=='recovering'
    assert scalar(s,'SELECT status FROM dev_tickets')=='open'
    s.advance(120); fail(s); s.drain()
    s.advance(180); s.reporter.observe_success(s.contexts[0],'tasks_get'); s.drain()
    assert scalar(s,'SELECT id FROM system_incidents')==identity
    assert scalar(s,'SELECT count(*) FROM inbox_deliveries')==3
    assert scalar(s,'SELECT occurrence_count FROM system_incidents')==3
    assert scalar(s,'SELECT recovery_count FROM system_incidents')==3
    assert scalar(s,'SELECT count(*) FROM dev_tickets')==1
    with s.db.read() as u:
        assert 'SECRET' not in json.dumps([dict(r) for r in u.connection.execute('SELECT * FROM system_incident_observations')])


def test_background_recovery_matches_job_identity(system):
    s = system
    job_a = {'id': new_id(), 'business_id': s.business, 'kind': 'metrics.sync', 'attempts': 1}
    job_b = {'id': new_id(), 'business_id': s.business, 'kind': 'metrics.sync', 'attempts': 1}
    s.reporter.job_result(job_a, {'code': 'internal_error'})
    s.reporter.job_result(job_b, {'code': 'internal_error'})
    s.drain()
    assert scalar(s, 'SELECT count(*) FROM system_incidents') == 1
    assert scalar(s, 'SELECT count(*) FROM system_incident_impacts WHERE recovered_at IS NULL') == 2

    # Warm a replacement reporter to prove the job identity is persisted.
    replacement = IncidentReporter(s.reporter.spool_dir, clock=s.reporter.clock)
    replacement.load_active(s.db)
    assert replacement.job_result(job_b)['status'] == 'queued'
    drain_spool(replacement, s.db, reporter_id=s.reporter_id)
    assert scalar(s, 'SELECT count(*) FROM system_incident_recoveries') == 1
    assert scalar(s, 'SELECT count(*) FROM system_incident_impacts WHERE recovered_at IS NULL') == 1
    assert scalar(s, 'SELECT status FROM system_incidents') == 'open'

    # The original job still gets its normal recovery path without reopening B.
    assert replacement.job_result(job_a)['status'] == 'queued'
    drain_spool(replacement, s.db, reporter_id=s.reporter_id)
    assert scalar(s, 'SELECT count(*) FROM system_incident_recoveries') == 2
    assert scalar(s, 'SELECT count(*) FROM system_incident_impacts WHERE recovered_at IS NULL') == 0
    assert scalar(s, 'SELECT status FROM system_incidents') == 'recovering'
    assert scalar(s, 'SELECT count(*) FROM dev_tickets') == 1


def test_job_identity_upgrade_preserves_legacy_impact(tmp_path, monkeypatch):
    migrations = Database.migrations()
    with monkeypatch.context() as patch:
        patch.setattr(Database, 'migrations', staticmethod(lambda: migrations[:70]))
        database = Database.for_root(tmp_path).initialize()
    reporter_id, business = new_id(), new_id()
    with database.transaction() as uow:
        uow.repository('principals').insert({'id': reporter_id, 'kind': 'system', 'display_name': 'Reporter'})
        uow.repository('businesses').insert({'id': business, 'slug': 'legacy', 'name': 'Legacy',
            'workspace': str(tmp_path / 'biz'), 'hq_path': str(tmp_path / 'biz/hq')})
    reporter = IncidentReporter(tmp_path / 'spool', clock=lambda: '2026-09-15T00:00:00+00:00')
    failure = reporter.observation(component='background', operation='metrics.sync',
        error_code='internal_error', call_site='background.run_one', business_id=business,
        terminal=True, trusted_result=True)
    incident_id, ticket_id = new_id(), new_id()
    with database.transaction() as uow:
        now = failure.observed_at
        uow.connection.execute("INSERT INTO dev_tickets(id,title,details,kind,status,reporter_id,business_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (ticket_id, 'Legacy incident', 'legacy', 'bug', 'open', reporter_id, business, now, now))
        uow.connection.execute("INSERT INTO system_incidents(id,fingerprint,ticket_id,title,status,classification,first_seen,last_seen,occurrence_count,recovery_count,business_id,representative_observation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (incident_id, failure.fingerprint, ticket_id, 'Legacy incident', 'open', failure.classification,
             now, now, 1, 0, business, failure.observation_id))
        uow.connection.execute("INSERT INTO system_incident_observations(id,fingerprint,observed_at,payload_json) VALUES(?,?,?,?)",
            (failure.observation_id, failure.fingerprint, now, json.dumps(failure.to_dict(), separators=(',', ':'))))
        uow.connection.execute("INSERT INTO system_incident_impacts(incident_id,business_id,agent_id,task_id,operation,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)",
            (incident_id, business, None, None, 'metrics.sync', now, now))
    database.initialize()
    with database.read() as uow:
        assert uow.connection.execute('SELECT job_id FROM system_incident_impacts').fetchone()[0] is None

    replacement = IncidentReporter(reporter.spool_dir, clock=reporter.clock)
    replacement.load_active(database)
    unrelated = replacement.job_result({'id': new_id(), 'business_id': business,
        'kind': 'metrics.sync', 'attempts': 1})
    assert unrelated['status'] == 'ignored'
    with database.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM system_incident_recoveries').fetchone()[0] == 0

    legacy_success = replacement.observation(component='background', operation='metrics.sync',
        error_code='success', call_site='background.run_one', business_id=business, recovered=True)
    assert replacement.observe(legacy_success)['status'] == 'queued'
    drain_spool(replacement, database, reporter_id=reporter_id)
    with database.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM system_incident_recoveries').fetchone()[0] == 1


def test_receipt_identity_separates_recipients_in_one_job(system):
    s = system
    job_id, receipt_a, receipt_b = new_id(), new_id(), new_id()

    def report(reporter, receipt_id, recovered=False):
        observation = reporter.observation(component='email.receipt',
            operation='business_copy_reconcile',
            error_code='success' if recovered else 'reconciliation_timeout',
            classification='operational_observation' if recovered else 'infrastructure_fault',
            retryable=not recovered, attempts=3, call_site='background.fastmail_batch',
            business_id=s.business, job_id=job_id, receipt_id=receipt_id, recovered=recovered)
        return reporter.observe(observation)

    assert report(s.reporter, receipt_a)['status'] == 'queued'
    assert report(s.reporter, receipt_b)['status'] == 'queued'
    s.drain()
    assert scalar(s, 'SELECT count(*) FROM dev_tickets') == 1
    assert scalar(s, 'SELECT count(*) FROM system_incident_impacts WHERE recovered_at IS NULL') == 2
    with s.db.read() as uow:
        identities = {(row['job_id'], row['receipt_id']) for row in uow.connection.execute(
            'SELECT job_id,receipt_id FROM system_incident_impacts')}
    assert identities == {(job_id, receipt_a), (job_id, receipt_b)}

    assert report(s.reporter, receipt_a, recovered=True)['status'] == 'queued'
    s.drain()
    assert scalar(s, 'SELECT count(*) FROM system_incident_impacts WHERE recovered_at IS NULL') == 1
    assert scalar(s, 'SELECT status FROM system_incidents') == 'open'

    replacement = IncidentReporter(s.reporter.spool_dir, clock=s.reporter.clock)
    replacement.load_active(s.db)
    assert report(replacement, receipt_b, recovered=True)['status'] == 'queued'
    drain_spool(replacement, s.db, reporter_id=s.reporter_id)
    assert scalar(s, 'SELECT count(*) FROM system_incident_impacts WHERE recovered_at IS NULL') == 0
    assert scalar(s, 'SELECT status FROM system_incidents') == 'recovering'


def test_commit_failure_preserves_spool_and_acknowledges_only_exact_snapshot(system):
    s=system
    fail(s)
    original=s.reporter.spool_dir/'system-incidents.jsonl'
    content=original.read_bytes()
    class FailingCommit:
        @contextmanager
        def transaction(self,**kwargs):
            with s.db.transaction(**kwargs) as u:
                yield u
                raise OSError('simulated commit failure')
    with pytest.raises(OSError): drain_spool(s.reporter,FailingCommit(),reporter_id=s.reporter_id)
    assert original.read_bytes()==content and scalar(s,'SELECT count(*) FROM dev_tickets')==0
    # Two drainers can snapshot the same batch. A later append must survive both
    # acknowledgements, including one after the other drainer has removed it.
    with s.db.transaction() as u:
        first=s.reporter.drain(u,reporter_id=s.reporter_id)
    with s.db.transaction() as u:
        second=s.reporter.drain(u,reporter_id=s.reporter_id)
    fail(s,s.contexts[1])
    s.reporter.acknowledge(first['_drained_ids'])
    s.reporter.acknowledge(second['_drained_ids'])
    assert len(original.read_bytes().splitlines())==1
    s.drain()
    assert scalar(s,'SELECT occurrence_count FROM system_incidents')==2
    assert original.stat().st_mode & 0o077 == 0


def test_spool_overflow_is_bounded_and_unknown_success_is_silent(system,monkeypatch):
    import bazarlyk.incidents as module
    s=system
    assert s.reporter.observe_success(s.contexts[0],'tasks_get')['status']=='ignored'
    monkeypatch.setattr(module,'MAX_SPOOL_BYTES',100)
    assert fail(s)['status']=='unavailable'
    health=json.loads((s.reporter.spool_dir/'system-incidents.health').read_text())
    assert health['dropped_observations']==1
    assert not (s.reporter.spool_dir/'system-incidents.jsonl').exists()


def test_fault_storm_keeps_one_ticket_and_bounded_evidence(system):
    s = system
    for _ in range(1000):
        fail(s)
    while s.drain()['remaining']:
        pass
    assert scalar(s, 'SELECT count(*) FROM dev_tickets') == 1
    assert scalar(s, 'SELECT occurrence_count FROM system_incidents') == 1000
    assert scalar(s, 'SELECT count(*) FROM system_incident_history') <= 100
    assert scalar(s, 'SELECT count(*) FROM system_incident_observations') <= 101
    assert scalar(s, 'SELECT count(*) FROM inbox_deliveries') == 0
