"""Fresh sessions share committed state, never a provider's chat history."""
import hashlib
import json
from dataclasses import replace

import pytest

from qorqut_operations.contracts import InvocationOptions, Principal, new_id, plain
from bazarlyk.domains.working_memory import memory_id
from test_environment_discovery import service


def invoke(s, name, args=None, *, actor=None, business=None, options=None):
    actor = actor or s.agent
    if actor.kind == 'human':
        ctx = s.fixture.ctx
    else:
        ctx = s.runtime.resolver.invocation_context(actor, **({'business_id': business} if business else {}))
    return s.runtime.application.invoke(ctx, name, args or {}, options)


def ok(result):
    assert result.status == 'ok', result.to_dict()
    return plain(result.data)


def save(s, body, previous=None, *, actor=None, sources=None, **extra):
    args = {'body': body, 'sources': sources or [], **extra}
    opts = {'idempotency_key': new_id()}
    if previous:
        args['expected_content_hash'] = previous['content_hash']
        opts['expected_revision'] = previous['revision']
    else:
        args['create_if_missing'] = True
        legacy = ok(invoke(s, 'plans_get', actor=actor))['document']
        if legacy:
            args.setdefault('legacy_plan_content_hash', legacy['content_hash'])
    return invoke(s, 'working_memory_update', args, actor=actor, options=opts)


def test_committed_state_noop_conflicts_and_history(service):
    s = service
    assert ok(invoke(s, 'working_memory_get'))['document'] is None
    first = ok(save(s, 'Checked pagination. Next: compare the UTC window.'))
    assert 'body' not in first  # replay receipts do not hold readable memory
    doc = ok(invoke(s, 'working_memory_get'))['document']
    assert doc['body'].startswith('Checked pagination')
    assert ok(save(s, doc['body'], doc))['revision'] == 1
    second = ok(save(s, 'UTC comparison disproved the timezone hypothesis.', doc))
    assert second['revision'] == 2
    assert save(s, 'Stale session overwrite', doc).error['code'] == 'revision_conflict'
    old = ok(invoke(s, 'working_memory_get', {'version': 1}))['document']
    assert old['body'] == doc['body'] and old['content_hash'] == doc['content_hash']
    history = ok(invoke(s, 'working_memory_get', {'history': True}))['history']
    assert [v['version'] for v in history] == [2, 1]
    current = ok(invoke(s, 'agent_context_get'))
    assert current['memory']['body'].startswith('UTC comparison')
    assert 'Stale session' not in json.dumps(current)


def test_ownership_drift_and_generic_readers(service):
    s = service
    ok(save(s, 'Private working interpretation.'))
    doc = ok(invoke(s, 'working_memory_get'))['document']
    business = s.fixture.business_id
    denied = invoke(s, 'working_memory_get', {'agent_id': s.agent.id}, actor=s.manager, business=business)
    assert denied.status == 'error'
    assert invoke(s, 'notes_get', {'document_id': doc['id']}, actor=s.manager, business=business).status == 'error'
    assert invoke(s, 'context_items_get', {'item': {'type': 'material', 'id': doc['id']}}, actor=s.manager, business=business).status == 'error'
    path = s.fixture.hq / f'context/agent-memory/{s.agent.id}.md'
    path.write_text(path.read_text() + '\nUNCOMMITTED CONTENT')
    assert invoke(s, 'working_memory_get').error['code'] == 'memory_drift'
    assert 'UNCOMMITTED' not in json.dumps(ok(invoke(s, 'agent_context_get')))
    assert save(s, 'Silently reconcile drift', doc).error['code'] == 'memory_drift'
    observed = hashlib.sha256(path.read_bytes()).hexdigest()
    assert save(s, 'Agent cannot repair', doc, repair_observed_hash=observed).status == 'error'
    ok(save(s, 'Owner repaired', doc, actor=s.fixture.owner, agent_id=s.agent.id, repair_observed_hash=observed))
    assert ok(invoke(s, 'working_memory_get'))['document']['body'] == 'Owner repaired'


def test_pending_memory_exposes_only_committed_authorized_handoff(service):
    s = service
    story = ok(invoke(s, 'stories_create', {'title': 'Source', 'purpose': 'Verified evidence'},
        actor=s.fixture.owner, options={'idempotency_key': new_id()}))['story']
    source = {'type': 'story', 'id': story['id']}
    observation = ok(invoke(s, 'working_memory_get', {'observe': [source]}))['observations'][0]
    refs = [{**source, 'token': observation['token']}]
    ok(save(s, 'Last committed handoff.', sources=refs))
    original = ok(invoke(s, 'working_memory_get'))['document']
    def interrupted(_resource):
        raise RuntimeError('File application interrupted')
    s.runtime.file_applier.after_replace = interrupted
    result = save(s, 'Uncommitted replacement.', original, sources=refs)
    assert result.status == 'error'
    s.runtime.file_applier.after_replace = None
    current = ok(invoke(s, 'agent_context_get'))
    assert current['memory']['body'] == original['body']
    assert current['memory']['pending_application'] is True
    assert invoke(s, 'working_memory_get').error['code'] == 'busy'
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'stories_get': 'deny'}}})
    withheld = ok(invoke(s, 'agent_context_get'))
    assert withheld['memory'] is None
    assert 'Last committed handoff' not in json.dumps(withheld)


def test_observed_story_sequence_and_revocation(service):
    s = service
    created = ok(invoke(s, 'stories_create', {'title': 'Reporting', 'purpose': 'Explain missing data'},
                        actor=s.fixture.owner, options={'idempotency_key': new_id()}))['story']
    source = {'type': 'story', 'id': created['id']}
    observation = ok(invoke(s, 'working_memory_get', {'observe': [source]}))['observations'][0]
    refs = [{**source, 'token': observation['token']}]
    ok(save(s, 'Investigating this Story.', sources=refs))
    # A timeline-only change intentionally leaves the Story detail revision intact.
    # The agent writes the timeline note; the owner's own notes belong to the private channel.
    ok(invoke(s, 'stories_note_add', {'story_id': created['id'], 'body': 'New results'},
              options={'idempotency_key': new_id()}))
    with s.runtime.database.read() as uow:
        row = uow.repository('stories', business_id=s.fixture.business_id).get(created['id'])
        assert row['revision'] == created['revision']
    fresh = ok(invoke(s, 'working_memory_get'))['document']
    assert fresh['observations'][0]['state'] == 'changed'
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'stories_get': 'deny'}}})
    assert invoke(s, 'working_memory_get').error['code'] == 'memory_sources_unavailable'
    assert 'Investigating this Story' not in json.dumps(ok(invoke(s, 'agent_context_get')))
    assert invoke(s, 'working_memory_get', {'version': 1}).status == 'error'
    metadata = ok(invoke(s, 'working_memory_get', {'metadata_only': True}))['document']
    ok(save(s, 'Explicit fresh handoff without inaccessible evidence.', metadata))
    assert ok(invoke(s, 'working_memory_get'))['document']['body'].startswith('Explicit fresh handoff')
    assert invoke(s, 'working_memory_get', {'version': 1}).status == 'error'


def test_global_and_business_memory_do_not_mix(service):
    s = service
    root = s.fixture.create('memory-root', 'ordubashy')
    ok(save(s, 'Global priorities', actor=root))
    # Explicit business selection, same identity, different deterministic document.
    args = {'body': 'Business investigation', 'sources': [], 'create_if_missing': True}
    ok(invoke(s, 'working_memory_update', args, actor=root, business=s.fixture.business_id,
              options={'idempotency_key': new_id()}))
    assert ok(invoke(s, 'working_memory_get', actor=root))['document']['body'] == 'Global priorities'
    selected = ok(invoke(s, 'working_memory_get', actor=root, business=s.fixture.business_id))['document']
    assert selected['body'] == 'Business investigation'
    assert selected['id'] != memory_id(root.id, None)
    # Global home notes are not accepted as business-scoped evidence.
    note = ok(invoke(s, 'notes_create', {'title': 'Global only', 'body': 'Do not spill into business'},
        actor=root, options={'idempotency_key': new_id()}))['document']
    denied = invoke(s, 'working_memory_get', {'observe': [{'type': 'document', 'id': note['id']}]},
        actor=root, business=s.fixture.business_id)
    assert denied.status == 'error'
    with s.runtime.database.transaction() as uow:
        repo = uow.repository('business_memberships', business_id=s.fixture.business_id)
        member = repo.get(root.id)
        repo.update(root.id, {'active': False}, expected_revision=member['revision'])
    assert invoke(s, 'working_memory_get', actor=root, business=s.fixture.business_id).status == 'error'


def test_import_preserves_legacy_bytes_and_freezes_old_writes(service):
    s = service
    body = 'Original plan: ' + 'important evidence\n' * 300
    original_plan = ok(invoke(s, 'plans_get'))['document']
    plan = ok(invoke(s, 'plans_set', {'body': body, 'expected_content_hash': original_plan['content_hash']},
                    options={'idempotency_key': new_id(), 'expected_revision': original_plan['revision']}))['document']
    path = s.fixture.hq / plan['path']
    original = path.read_bytes()
    assert invoke(s, 'working_memory_update', {'body': 'Read original plan before proceeding.', 'sources': [],
        'create_if_missing': True}, options={'idempotency_key': new_id()}).error['code'] == 'legacy_plan_conflict'
    observed = ok(invoke(s, 'working_memory_get', {'observe': [{'type': 'document', 'id': plan['id']}]}))['observations'][0]
    assert save(s, 'Do not make a retired plan a current reference.', sources=[{
        'type': 'document', 'id': plan['id'], 'token': observed['token']}],
        legacy_plan_content_hash=plan['content_hash']).error['code'] == 'validation_error'
    ok(save(s, 'Original plan needs review; full legacy evidence is preserved.', legacy_plan_content_hash=plan['content_hash']))
    assert path.read_bytes() == original
    assert ok(invoke(s, 'plans_get'))['migrated'] is True
    assert invoke(s, 'plans_set', {'body': 'Competing plan'}, options={'idempotency_key': new_id()}).error['code'] == 'plan_migrated'
    saved = ok(invoke(s, 'working_memory_get', {'legacy_plan': True}))
    assert saved['legacy_plan']['body'] == body
    assert saved['legacy_plan']['content_hash'] == hashlib.sha256(original).hexdigest()
    assert invoke(s, 'plans_get', {'agent_id': s.agent.id}, actor=s.manager, business=s.fixture.business_id).status == 'error'


def test_task_dependency_changes_and_bounds(service):
    s = service
    def mutate(name, args, **opts):
        return ok(invoke(s, name, args, actor=s.fixture.owner, options={'idempotency_key': new_id(), **opts}))
    blocker = mutate('tasks_create', {'title': 'Prepare export'})['task']
    task = mutate('tasks_create', {'title': 'Compare exports', 'blocked_by': [blocker['id']]})['task']
    source = {'type': 'task', 'id': task['id']}
    token = ok(invoke(s, 'working_memory_get', {'observe': [source]}))['observations'][0]['token']
    ok(save(s, 'Waiting for the source export.', sources=[{**source, 'token': token}]))
    mutate('tasks_complete', {'task_id': blocker['id']}, expected_revision=blocker['revision'])
    current = ok(invoke(s, 'working_memory_get'))['document']
    assert current['observations'][0]['state'] == 'changed'
    assert ok(invoke(s, 'tasks_get', {'task_id': task['id']}))['task']['revision'] == task['revision']
    dependency_token = current['observations'][0]['token']
    configuration = ok(invoke(s, 'tasks_config_get', actor=s.fixture.owner))['config']
    configuration['fields']['memory_probe'] = {'type': 'int'}
    mutate('tasks_config_update', {'config': configuration})
    assert ok(invoke(s, 'working_memory_get'))['document']['observations'][0]['token'] != dependency_token
    assert save(s, 'x' * 4001, current).status == 'error'
    assert save(s, 'duplicate evidence', current, sources=[{**source, 'token': token}] * 2).status == 'error'
    with s.runtime.database.transaction() as uow:
        repo = uow.repository('relationships', business_id=s.fixture.business_id)
        for i in range(201):
            repo.insert({'id': new_id(), 'from_type': 'task', 'from_id': task['id'],
                         'to_type': 'entity', 'to_id': str(i), 'role': 'concerns'})
    assert invoke(s, 'working_memory_get').error['code'] == 'memory_sources_unavailable'


def test_fresh_bootstrap_replay_and_private_exports(service, tmp_path):
    from bazarlyk.storage.exports import build_exports
    from bazarlyk.storage.versioning import public_path
    from bazarlyk.agents.context import MAX_CONTEXT_CHARACTERS
    s = service
    args = {'body': 'UNIQUE HANDOFF latest UTC comparison', 'sources': [], 'create_if_missing': True}
    args['legacy_plan_content_hash'] = ok(invoke(s, 'plans_get'))['document']['content_hash']
    options = {'idempotency_key': new_id()}
    first = ok(invoke(s, 'working_memory_update', args, options=options))
    assert ok(invoke(s, 'working_memory_update', args, options=options)) == first
    before = s.runtime.resolver.resolve_agent_config(s.agent).effective_config_id
    old = ok(invoke(s, 'working_memory_get'))['document']
    ok(save(s, 'UNIQUE HANDOFF second session evidence', old))
    assert s.runtime.resolver.resolve_agent_config(s.agent).effective_config_id == before
    boot = s.runtime.resolver.bootstrap(s.agent, registration_id=s.registrations[s.agent.id],
        session_id='fresh-other-session', event='startup', last_effective_config_id=before)
    assert boot['ready'], boot
    assert boot['effective_config_id'] == before
    rendered = boot['mandatory_context']['rendered'] + boot['work_briefing']['rendered']
    assert rendered.count('UNIQUE HANDOFF second session evidence') == 1
    assert len(rendered) <= MAX_CONTEXT_CHARACTERS
    assert 'latest UTC comparison' not in rendered
    with s.runtime.database.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM working_memory_provenance').fetchone()[0] == 2
        assert not uow.connection.execute("SELECT 1 FROM inbox_events WHERE source_id=?", (first['document_id'],)).fetchone()
    snapshot = tmp_path / 'snapshot.sqlite3'
    s.runtime.database.backup(snapshot)
    exported = build_exports(snapshot, s.runtime.root)
    assert first['document_id'] not in b''.join(exported.files.values()).decode()
    assert not public_path(f'widget/hq/context/agent-memory/{s.agent.id}.md', [{'hq': 'widget/hq'}])
    from bazarlyk.storage.recovery import BackupStore
    from bazarlyk.storage import Database
    backup, identity = BackupStore(s.runtime.database, s.runtime.root), new_id()
    backup.create(identity)
    restored = backup.restore(identity, tmp_path / 'restored')
    with Database(restored['database']).initialize().read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM working_memory_provenance').fetchone()[0] == 2
    relative = f'widget/hq/context/agent-memory/{s.agent.id}.md'
    assert (tmp_path / 'restored' / relative).read_bytes() == (s.runtime.root / relative).read_bytes()


def test_explicit_denials_and_parallel_initial_saves(service):
    from concurrent.futures import ThreadPoolExecutor
    from bazarlyk.policy import resolve_operation_policy
    s = service
    for op in ('working_memory_get', 'working_memory_update', 'agent_context_get'):
        assert resolve_operation_policy(op, {}, {}, s.runtime.registry).mode == 'allow'
        assert resolve_operation_policy(op, {op: 'deny'}, {}, s.runtime.registry).mode == 'deny'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda body: save(s, body), ['Session one', 'Session two']))
    assert sum(r.status == 'ok' for r in results) == 1, [r.to_dict() for r in results]
    assert sum(r.status == 'error' for r in results) == 1
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'working_memory_get': 'deny'}}})
    assert invoke(s, 'working_memory_get').status == 'error'
    assert ok(invoke(s, 'agent_context_get'))['memory'] is None


def test_metadata_recovery_and_owner_notebook_boundary(service):
    s = service
    root = s.fixture.create('notebook-boundary', 'ordubashy')
    from dataclasses import replace
    owner = replace(s.fixture.ctx, business_id=None, review_business_id=None)
    note = ok(s.runtime.application.invoke(owner, 'notes_create', {'title': 'PRIVATE OWNER TITLE', 'body': 'Owner notebook'},
        {'idempotency_key': new_id()}))['document']
    assert invoke(s, 'notes_get', {'document_id': note['id']}, actor=root).status == 'error'
    probe = invoke(s, 'working_memory_get', {'observe': [{'type': 'document', 'id': note['id']}]}, actor=root)
    assert probe.status == 'error'
    assert 'PRIVATE OWNER TITLE' not in json.dumps(probe.to_dict())
    from starlette.testclient import TestClient
    with TestClient(s.web) as client:
        response = client.post('/api/root/operations/working_memory_get', json={'agent_id': root.id},
                               headers={'Origin': 'http://testserver'})
        assert response.status_code == 200, response.text
        assert response.json()['data']['document'] is None
    ok(save(s, 'Committed original'))
    path = s.fixture.hq / f'context/agent-memory/{s.agent.id}.md'
    path.write_text(path.read_text() + '\nDRIFTED PRIVATE BODY')
    metadata = ok(invoke(s, 'working_memory_get', {'metadata_only': True}))['document']
    assert 'body' not in metadata and 'sources' not in metadata
    assert metadata['revision'] == 1
    assert 'DRIFTED' not in json.dumps(metadata)
    historical = ok(invoke(s, 'working_memory_get', {'version': 1}))['document']
    ok(save(s, historical['body'], metadata, actor=s.fixture.owner, agent_id=s.agent.id,
            repair_observed_hash=hashlib.sha256(path.read_bytes()).hexdigest()))
    assert ok(invoke(s, 'working_memory_get'))['document']['revision'] == 2


def test_interrupted_file_replace_recovers_one_version(service, monkeypatch):
    s = service
    ok(save(s, 'Before interrupted save'))
    previous = ok(invoke(s, 'working_memory_get'))['document']
    args = {'body': 'After recovered save', 'sources': [], 'expected_content_hash': previous['content_hash']}
    options = {'idempotency_key': new_id(), 'expected_revision': previous['revision']}
    class Interrupted(BaseException):
        pass
    def crash(resource):
        raise Interrupted()
    monkeypatch.setattr(s.runtime.file_applier, 'after_replace', crash)
    with pytest.raises(Interrupted):
        invoke(s, 'working_memory_update', args, options=options)
    assert invoke(s, 'working_memory_get').error['code'] == 'busy'
    assert 'After recovered save' not in json.dumps(ok(invoke(s, 'agent_context_get')))
    with s.runtime.database.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM working_memory_provenance').fetchone()[0] == 1
    monkeypatch.setattr(s.runtime.file_applier, 'after_replace', None)
    assert ok(invoke(s, 'working_memory_update', args, options=options))['revision'] == 2
    assert ok(invoke(s, 'working_memory_update', args, options=options))['revision'] == 2
    assert ok(invoke(s, 'working_memory_get'))['document']['body'] == 'After recovered save'
    with s.runtime.database.read() as uow:
        assert uow.connection.execute('SELECT count(*) FROM working_memory_provenance').fetchone()[0] == 2


def test_attention_reserves_outcomes_and_reports_omitted_work(service):
    s = service
    for i in range(10):
        ok(invoke(s, 'tasks_create', {'title': f'Active task {i}'}, actor=s.fixture.owner,
                  options={'idempotency_key': new_id()}))
    s.fixture.update(s.agent, {'tool_policy': {'overrides': {'tasks_create': 'signoff'}}})
    proposal = invoke(s, 'tasks_create', {'title': 'Owner decision needed'}, actor=s.agent,
                      options={'idempotency_key': new_id()})
    assert proposal.status == 'signoff_required', proposal.to_dict()
    context = ok(invoke(s, 'agent_context_get', actor=s.agent))
    assert len(context['attention']) == 10
    assert any(item['type'] == 'signoff' for item in context['attention'])
    assert any('additional attention items omitted' in notice for notice in context['notices'])
