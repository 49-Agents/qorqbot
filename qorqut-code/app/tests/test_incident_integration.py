from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pytest

from qorqut_operations.contracts import DomainError, new_id
from test_service_transports import ServiceFixture
from test_agent_activity import ok


def test_incident_task_links_authorized_closure_and_scope_revocation(tmp_path, monkeypatch):
    s = ServiceFixture(tmp_path,full=True)
    app, reporter = s.runtime.application, s.runtime.incidents.reporter
    ctx = s.runtime.resolver.invocation_context(s.agent)
    at = datetime.now(timezone.utc)
    reporter.clock = lambda: at.isoformat()
    def fail():
        raise DomainError('internal_error','PRIVATE source data')
    try:
        reporter.boundary(ctx,'agent.bootstrap','prepare',fail)
    except DomainError:
        pass
    result = s.runtime.incidents.drain_once()
    assert result['created'] == 1, result
    assert reporter.health()['state'] == 'ready'
    def call(name,args,revision=None,actor=None):
        return ok(app.invoke(actor or s.fixture.ctx,name,args,
            {'idempotency_key':new_id(),'expected_revision':revision}))
    incident = call('system_incidents_agent_list',{},actor=ctx)['items'][0]
    ticket_id = incident['ticket_id']
    with s.fixture.db.read() as u:
        ticket = u.repository('dev_tickets').get(ticket_id)
        assert u.repository('principals').get(ticket['reporter_id'])['kind'] == 'system'
    task = call('tasks_create',{'title':'Work blocked by this exact incident','assignee_ids':[s.agent.id,s.manager.id]})['task']
    # The business agent writes the shared comment; the owner's own comments are private.
    task = call('tasks_comment',{'task_id':task['id'],'text':'Blocked by the recorded preparation fault.',
        'note_refs':[{'type':'dev_ticket','id':ticket_id}]},task['revision'],actor=ctx)['task']
    assert task['note_count'] == 1 and task['notes'][0]['refs'][0]['id'] == ticket_id
    owner = replace(s.fixture.ctx,business_id=None,review_business_id=None)
    denied = app.invoke(owner,'dev_tickets_status_set',{'ticket_id':ticket_id,'status':'closed'},
        {'idempotency_key':new_id(),'expected_revision':ticket['revision']})
    assert denied.status == 'error' and denied.error['code'] == 'validation_error'
    closed = call('dev_tickets_status_set',{'ticket_id':ticket_id,'status':'closed',
        'resolution':'Fixed the generated configuration reader.','verification':'Reproduced and verified the same preparation path.'},
        ticket['revision'],owner)
    assert closed['status'] == 'closed'
    with s.fixture.db.read() as u:
        assert u.connection.execute('SELECT status FROM system_incidents WHERE id=?',(incident['id'],)).fetchone()[0] == 'closed'
        deliveries = u.connection.execute("SELECT d.agent_id FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id WHERE e.event_type='system_incident_resolved'").fetchall()
        assert {r[0] for r in deliveries} == {s.manager.id,s.agent.id}
        assert u.connection.execute('PRAGMA foreign_key_check').fetchone() is None
    assert call('tasks_get',{'task_id':task['id']})['task']['note_count'] == 1
    # An ordinary availability probe cannot reopen a verified repair.
    reporter.clock = lambda: (at+timedelta(minutes=1)).isoformat()
    reporter.boundary(ctx,'agent.bootstrap','prepare',lambda:{'ready':True})
    s.runtime.incidents.drain_once()
    assert call('system_incidents_agent_get',{'incident_id':incident['id']},actor=ctx)['incident']['status'] == 'closed'
    coordinator = s.fixture.create('coordinator', 'ordubashy', runtime='codex')
    call('management_coordinator_set', {'coordinator_id':coordinator.id}, actor=owner)
    reporter.clock = lambda: (at+timedelta(minutes=2)).isoformat()
    for _ in range(3):
        with pytest.raises(DomainError):
            reporter.boundary(ctx, 'agent.bootstrap', 'prepare', fail)
    s.runtime.incidents.drain_once()
    with s.fixture.db.read() as u:
        assert u.repository('dev_tickets').get(ticket_id)['status'] == 'open'
        assert u.connection.execute('SELECT count(*) FROM dev_tickets').fetchone()[0] == 1
        notices = u.connection.execute("SELECT d.agent_id FROM inbox_deliveries d JOIN inbox_events e ON e.id=d.event_id "
            "WHERE e.source_type='system_incidents' AND e.source_id=? AND e.event_type='system_incident_opened'",
            (incident['id'],)).fetchall()
        assert [row[0] for row in notices] == [coordinator.id]
    s.fixture.update(s.agent,{'tool_policy':{'overrides':{'dev_tickets_get':'deny'}}})
    updated = s.runtime.resolver.invocation_context(s.agent)
    assert call('tasks_get',{'task_id':task['id']},actor=updated)['task']['notes'][0]['refs'] == [{'unavailable':True}]
    s.fixture.update(s.agent,{'tool_policy':{'overrides':{'tasks_get':'deny'}}})
    updated = s.runtime.resolver.invocation_context(s.agent)
    impacts = call('system_incidents_agent_get',{'incident_id':incident['id']},actor=updated)['impacts']
    assert {'unavailable':True} in impacts and all(not item.get('task_id') for item in impacts)
    with s.fixture.db.transaction() as u:
        u.connection.execute('UPDATE business_memberships SET active=0 WHERE principal_id=?',(s.agent.id,))
    assert app.invoke(updated,'system_incidents_agent_get',{'incident_id':incident['id']}).status == 'error'
    # Context preparation can fail before the dispatcher has a trusted scope.
    # Record the authenticated agent without attributing an unvalidated selector.
    def broken_context(*args, **kwargs):
        raise RuntimeError('PRIVATE configuration contents')
    monkeypatch.setattr(s.runtime.resolver, 'invocation_context', broken_context)
    with pytest.raises(RuntimeError):
        s.runtime.agent_context(s.agent, business_id=s.fixture.business_id)
    assert s.runtime.incidents.drain_once()['created'] == 1
    with s.fixture.db.read() as u:
        row = u.connection.execute("SELECT i.business_id,o.payload_json FROM system_incidents i "
            "JOIN system_incident_observations o ON o.id=i.representative_observation_id "
            "WHERE json_extract(o.payload_json,'$.component')='agent.context'").fetchone()
        assert row and row['business_id'] is None and 'PRIVATE' not in row['payload_json']
    # The observer's own failure never substitutes a new business error.
    monkeypatch.setattr(reporter, 'observe', broken_context)
    expected = DomainError('internal_error', 'Original failure')
    def original_failure():
        raise expected
    with pytest.raises(DomainError) as caught:
        s.runtime.observed_agent_call(ctx, 'fixture', 'failure', original_failure)
    assert caught.value is expected
