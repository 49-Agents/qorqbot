"""Global ownership, independent owner review, and legacy byte preservation."""
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from qorqut_operations.contracts import ChangeSet, DomainError, new_id, plain
from qorqut_operations.routines.operations import RoutineOperations
from qorqut_operations.background.programs import Program, ProgramRegistry
from bazarlyk.storage import Database
from test_signoffs import Scenario
from test_service_transports import ServiceFixture, ORIGIN


def global_scenario(tmp_path):
    s = Scenario(tmp_path)
    with s.db.transaction() as uow:
        uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(), 'owner_principal_id': s.owner.id})
    s.owner_context = replace(s.owner_context, business_id=None)
    return s


def shared_proposal(s, actor=None, **options):
    actor = actor or s.add_agent('ordubashy')
    document = s.document(global_scope=True)
    result = s.app.invoke(s.agent_context(actor), 'shared_instructions_update',
        {'document_ids': [document['id']], 'text': 'Global guidance\n'}, {'idempotency_key': new_id(), **options})
    return actor, document, result


def test_global_queue_is_separate_and_applies_offline(tmp_path):
    s = global_scenario(tmp_path)
    actor, document, result = shared_proposal(s)
    assert result.status == 'signoff_required', result.to_dict()
    assert result.signoff['review_scope'] == {'kind': 'global'}
    assert result.signoff['review_business_id'] is None
    assert s.service.list(s.owner_context).data['counts']['pending'] == 1
    business_owner = replace(s.owner_context, business_id=s.business)
    assert s.service.list(business_owner).data['counts']['pending'] == 0
    assert s.service.get(business_owner, result.signoff['id']).error['code'] == 'not_found'
    assert s.service.list(replace(s.agent_context(actor), business_id=None)).data['counts']['pending'] == 1
    assert s.decide(result.signoff).status == 'accepted'
    s.service.run_pending()
    assert s.detail(result.signoff)['state'] == 'applied'
    assert s.preparations == 1


def test_global_queue_includes_business_signoffs_and_keeps_business_identity(tmp_path):
    s = global_scenario(tmp_path)
    proposal = s.propose("Business review")
    queue = s.service.list(s.owner_context).data
    item = next(item for item in queue["items"] if item["id"] == proposal["id"])
    assert item["review_scope"] == {"kind": "business", "business_id": s.business}
    assert s.service.get(s.owner_context, proposal["id"]).data["signoff"]["review_business_id"] == s.business
    assert s.decide(proposal).status == "accepted"


@pytest.mark.parametrize('spoof', [False, True])
def test_business_agent_cannot_create_global_proposal(tmp_path, spoof):
    s = global_scenario(tmp_path)
    document = s.document(global_scope=True)
    context = s.agent_context()
    if spoof:
        context = replace(context, global_capabilities=frozenset({'manage_shared_instructions'}), home_business_id=None)
    result = s.app.invoke(context, 'shared_instructions_update', {'document_ids': [document['id']], 'text': 'no'}, {'idempotency_key': new_id()})
    assert result.status == 'error'
    assert result.error['code'] == 'forbidden'
    assert s.count('signoffs') == 0


def test_global_routing_override_and_noninstallation_owner_denied(tmp_path):
    s = global_scenario(tmp_path)
    _, _, result = shared_proposal(s, review_business_id=s.business)
    assert result.status == 'error' and s.count('signoffs') == 0
    _, _, result = shared_proposal(s)
    viewer = replace(s.owner_context, principal=s.viewer)
    assert s.decide(result.signoff, context=viewer).error['code'] == 'forbidden'


def test_global_grant_revocation_prevents_application(tmp_path):
    s = global_scenario(tmp_path)
    actor, _, result = shared_proposal(s)
    with s.db.transaction() as uow:
        profile, _ = s.resolver.profile(actor, connection=uow.connection)
        data = profile.to_dict()
        data['scope']['global_capabilities'] = ['read_global_context']
        data['profile_revision'] += 1
        uow.repository('agent_profiles', business_id=None).update(actor.id, {'profile_json': data}, expected_revision=profile.revision)
    assert s.decide(result.signoff).error['code'] == 'stale_signoff'
    assert s.detail(result.signoff)['state'] == 'conflicted'


def test_global_routine_http_without_business_selection(tmp_path, monkeypatch):
    service = ServiceFixture(tmp_path, full=True)
    monkeypatch.setattr(service.runtime.background, 'start', lambda: None)
    service.runtime.background.programs.register(Program('test.global', 1, {'type': 'object'}, lambda *a: {}, scope_kinds=('global',)))
    with TestClient(service.web) as client:
        definition = {'name': 'Global status', 'program': 'test.global', 'arguments': {},
                      'schedule': '0 0 * * *', 'timezone': 'UTC', 'status': 'paused', 'target': 'root'}
        response = client.post('/api/root/routines', json={'op': 'add', 'definition': definition}, headers=ORIGIN)
        assert response.status_code == 200, response.text
        view = client.get('/api/root/routines')
        assert view.status_code == 200, view.text
        assert len(view.json()['routines']) == 1
        assert view.json()['routines'][0]['location'] == 'global'
        queue = client.get('/api/root/signoffs')
        assert queue.status_code == 200, queue.text
        assert queue.json()['data']['review_scope'] == {'kind': 'global'}
        assert client.get('/api/biz/widget/signoffs?scope=global').status_code >= 400
        old = client.post('/api/root/routines', json={'op': 'add', 'definition': definition,
            'review_business_id': service.fixture.business_id}, headers=ORIGIN)
        assert old.status_code == 422, old.text


def test_migration_preserves_legacy_global_proposal_and_receipt(tmp_path, monkeypatch):
    from qorqut_operations.contracts import InvocationOptions
    from bazarlyk.storage.receipts import ApplyReceipts
    migrations = Database.migrations()
    with monkeypatch.context() as old_schema:
        old_schema.setattr(Database, 'migrations', staticmethod(lambda: migrations[:-1]))
        s = Scenario(tmp_path)
        actor = s.add_agent('ordubashy')
        document = s.document(global_scope=True)
        context = s.agent_context(actor)
        descriptor = s.registry.get('shared_instructions_update')
        args = {'document_ids': [document['id']], 'text': 'Legacy approved bytes\n'}
        with s.db.transaction() as uow:
            plan = descriptor.prepare(context, args, InvocationOptions(), uow)
            decisions = s.app._decisions(context, descriptor, plan.expected_targets, plan.changes, uow, namespace='global')
            plan = replace(plan, review_business_id=s.business, proposer_agent_id=actor.id,
                           policy_binding=s.app._binding(context, decisions))
            row = uow.repository('signoffs', business_id=s.business).insert({
                'id': new_id(), 'proposer_principal_id': actor.id, 'proposer_agent_id': actor.id,
                'operation_name': plan.operation_name, 'operation_version': 1, 'proposal_revision': 1,
                'payload_hash': plan.payload_hash, 'change_set': plan.canonical, 'preview_json': plain(plan.preview),
                'expected_targets_json': plain(plan.expected_targets), 'policy_binding_json': plain(plan.policy_binding),
                'affected_scope_json': plain(plan.affected_scope), 'state': 'pending', 'updated_sequence': uow.next_sequence('signoffs')})
            receipt = ApplyReceipts(uow).create(plan, receipt_id=row['id'], signoff_id=row['id'])
        before = s.db.backup(tmp_path / 'before.sqlite3')
    s.db.initialize()
    with s.db.read() as uow:
        migrated = uow.repository('signoffs', business_id=s.business).get(row['id'])
        assert migrated['review_scope_kind'] == 'business'
        assert all(migrated[key] == value for key, value in row.items())
        assert ApplyReceipts(uow).get(row['id']) == receipt
        assert uow.connection.execute('PRAGMA foreign_key_check').fetchall() == []
    assert ChangeSet.from_bytes(plan.canonical, expected_hash=plan.payload_hash).canonical == plan.canonical
    assert s.detail(row)['legacy_review_routing'] is True
    assert s.decide(row).status == 'accepted'
    s.service.run_pending()
    assert s.detail(row)['state'] == 'applied'
    with pytest.raises(DomainError):
        with s.db.transaction() as uow:
            uow.connection.execute("UPDATE signoffs SET review_scope_kind='global', review_business_id=NULL WHERE id=?", (row['id'],))
    restored = Database.restore(tmp_path / 'before.sqlite3', tmp_path / 'restored.sqlite3', expected_checksum=before['checksum'])
    with restored.read() as uow:
        assert uow.repository('signoffs', business_id=s.business).get(row['id'])['change_set'] == plan.canonical


def test_ordubashy_migration_grants_every_registered_business(tmp_path, monkeypatch):
    migrations = Database.migrations()
    with monkeypatch.context() as old_schema:
        # Start before the scope backfill, even after newer migrations are added.
        old_schema.setattr(Database, 'migrations', staticmethod(lambda: [row for row in migrations if row[0] < 41]))
        s = Scenario(tmp_path)
        actor = s.add_agent('ordubashy')
        with s.db.transaction() as uow:
            profile, _ = s.resolver.profile(actor, connection=uow.connection)
            data = profile.to_dict()
            data['scope']['allowed_business_ids'] = [s.business]
            data['profile_revision'] += 1
            uow.repository('agent_profiles', business_id=None).update(
                actor.id, {'profile_json': data}, expected_revision=profile.revision)
    s.db.initialize()
    with s.db.read() as uow:
        profile, _ = s.resolver.profile(actor, connection=uow.connection)
        assert profile.scope.businesses == {s.business, s.other_business}
        assert uow.repository('business_memberships', business_id=s.other_business).get(actor.id)['active']


def test_global_routine_agents_with_all_business_scope(tmp_path):
    s = global_scenario(tmp_path)
    programs = ProgramRegistry()
    programs.register(Program('test.global', 1, {'type': 'object'}, lambda *a: {}, scope_kinds=('global',)))
    RoutineOperations(s.app, programs, None).register()
    args = {'scope': 'global', 'definition': {'name': 'Global check', 'program': 'test.global',
        'arguments': {}, 'schedule': '0 0 * * *', 'timezone': 'UTC', 'status': 'paused', 'target': 'root'}}
    actor = s.add_agent('ordubashy')
    for context in (s.agent_context(), s.agent_context(actor)):
        result = s.app.invoke(context, 'routines_create', args, {'idempotency_key': new_id()})
        assert result.error['code'] == 'forbidden'
    with s.db.transaction() as uow:
        profile, _ = s.resolver.profile(actor, connection=uow.connection)
        data = profile.to_dict()
        data['scope']['global_capabilities'].append('manage_global_routines')
        data['profile_revision'] += 1
        uow.repository('agent_profiles', business_id=None).update(actor.id, {'profile_json': data}, expected_revision=profile.revision)
    from qorqut_operations.contracts import InvocationContext
    result = s.app.invoke(InvocationContext(actor), 'routines_create', args, {'idempotency_key': new_id()})
    assert result.status == 'signoff_required', result.to_dict()
    owner = replace(s.owner_context, global_capabilities=s.owner_context.global_capabilities | {'manage_global_routines'})
    preview = s.detail(result.signoff, context=owner)['preview']
    assert preview['title'] == 'Create Global check'
    assert preview['resources'][0]['label'] == 'Global check'
    assert 'Global workspace' in preview['effects'][0]
    assert s.decide(result.signoff, context=owner).status == 'accepted'
    s.service.run_pending()
    assert s.detail(result.signoff, context=owner)['state'] == 'applied'
    read = s.app.invoke(owner, 'routines_list', {'scope': 'global'})
    assert read.status == 'ok', read.to_dict()


def test_global_root_routine_works_without_any_business(tmp_path):
    from bazarlyk.application.catalog import builtin_registry
    from bazarlyk.application.engine import Application
    from qorqut_operations.background.authority import BackgroundAuthority
    from qorqut_operations.contracts import InvocationContext, Principal
    from bazarlyk.policy import PolicyResolver
    from bazarlyk.transport.auth import human_global_capabilities
    db = Database.for_root(tmp_path).initialize()
    owner = Principal(new_id(), 'human')
    with db.transaction() as uow:
        uow.repository('principals').insert({'id': owner.id, 'kind': 'human', 'display_name': 'Owner'})
        uow.repository('installation').insert({'id': 'service', 'installation_id': new_id(), 'owner_principal_id': owner.id})
    registry = builtin_registry()
    policy = PolicyResolver(registry, human_global_capabilities=human_global_capabilities)
    app = Application(db, registry, policy=policy, context_resolver=policy.refresh_context)
    programs = ProgramRegistry()
    programs.register(Program('test.global', 1, {'type': 'object'}, lambda *a: {}, scope_kinds=('global', 'business')))
    authority = BackgroundAuthority(db, programs)
    with db.transaction() as uow:
        authority.register_workers(uow, [])
    RoutineOperations(app, programs, authority).register()
    context = InvocationContext(owner, source='dashboard')
    for target in ('root', 'each-business'):
        created = app.invoke(context, 'routines_create', {'scope': 'global', 'definition': {
            'name': target, 'program': 'test.global', 'arguments': {}, 'schedule': '0 0 * * *',
            'timezone': 'UTC', 'status': 'paused', 'target': target}}, {'idempotency_key': new_id()})
        assert created.status == 'ok', created.to_dict()
        row = created.data['records'][0]
        run = app.invoke(context, 'routines_run', {'scope': 'global', 'routine_id': row['id']},
                         {'idempotency_key': new_id(), 'expected_revision': row['revision']})
        if target == 'root':
            assert run.status == 'ok', run.to_dict()
            assert len(run.data['job_ids']) == 1
        else:
            assert run.error['code'] == 'forbidden' and 'no authorized business targets' in run.error['message']
    with db.read() as uow:
        assert uow.connection.execute('SELECT COUNT(*) FROM businesses').fetchone()[0] == 0
        assert uow.connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1
