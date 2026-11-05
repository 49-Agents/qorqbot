import hashlib
import json
from dataclasses import replace

import pytest

from qorqut_operations.contracts import DomainError, new_id
from bazarlyk.service.local import LocalFiles
from foundation_support import BUSINESS, HUMAN, context
from test_background_programs import runtime, raw_learning
from test_learning_sources import call


def global_agent(runtime, role, count=1):
    identity = new_id()
    workspace = runtime.root / 'agents' / identity
    (workspace / 'sessions').mkdir(parents=True)
    with runtime.database.transaction() as uow:
        uow.repository('principals').insert({'id': identity, 'kind': 'agent', 'display_name': role})
        uow.repository('agent_profiles', business_id=None).insert({'agent_id': identity, 'principal_id': identity,
            'namespace': 'ecosystem', 'name': role, 'workspace': str(workspace), 'profile_json': {'role': {'id': role}}})
    sources = []
    for _ in range(count):
        session = new_id()
        content = json.dumps({'type': 'user', 'uuid': new_id(), 'sessionId': session,
            'timestamp': '2026-09-13T00:00:00Z', 'message': {'content': 'Always keep source quotes.'}})
        path = workspace / 'sessions' / (session + '.jsonl')
        path.write_text(content)
        sources.append({'session_id': session, 'agent_id': identity, 'agent_kind': role,
            'source_id': str(path.relative_to(runtime.root)), 'content': content,
            'transcript_hash': hashlib.sha256(content.encode()).hexdigest()})
    runtime.policy.human_global_capabilities = {HUMAN: {'read_global_context', 'manage_global_routines'}}
    return identity, sources


def run_global(runtime, **args):
    queued = call(runtime, 'learnings_extract', {'passes': 1, **args}, global_scope=True)
    result = runtime.background.run_one(queued['job_id'])
    assert result['state'] == 'succeeded', result
    return result


def keep_verified(messages):
    proposals = json.loads(messages[-1]['content'])['proposals']
    return {'assessments': [{'id': item['id'], 'decision': 'keep', 'reason': 'Fixture evidence supports this claim.',
                            'support': item['said_by'], 'durability': 'durable', 'category': item['bucket'],
                            'tag': item['tag'], 'scope': item['scope']}
                            for item in proposals]}


def test_global_transcripts_use_existing_pipeline_but_never_applier(runtime):
    founder, first = global_agent(runtime, 'founder')
    ordubashy, second = global_agent(runtime, 'ordubashy')
    applier, excluded = global_agent(runtime, 'learning-applier')
    runtime.sources.extend([*first, *second, *excluded])
    one = {**raw_learning(), 'scope': 'agent_kind'}
    two = {**raw_learning(), 'scope': 'global', 'bucket': 'context', 'tag': 'fact'}
    runtime.model.answers.extend([{'items': [one]}, keep_verified, {'items': [two]}, keep_verified, {}])
    run_global(runtime)
    with runtime.database.read() as uow:
        scanned = uow.repository('learning_scan_state', business_id=None).list()
        assert {row['state_json']['agent_id'] for row in scanned} == {founder, ordubashy}
        items = uow.repository('learning_candidates', business_id=None).list()
        assert len(items) == 2
        assert all(item['candidate_json']['observed']['biz'] is None for item in items)
        assert not uow.repository('learning_candidates', business_id=BUSINESS).list()
    assert 'Global agent conversation' in runtime.model.calls[0][0][0]['content']
    count = len(runtime.model.calls)
    run_global(runtime)
    assert len(runtime.model.calls) == count
    ctx = replace(context(), business_id=None, review_business_id=None)
    denied = runtime.application.invoke(ctx, 'learnings_extract', {'agent_id': applier}, {'idempotency_key': new_id()})
    assert denied.error['code'] == 'forbidden'


def test_global_adapter_excludes_applier_and_backfill_reaches_older_files(runtime):
    founder, sources = global_agent(runtime, 'founder', count=22)
    applier, _ = global_agent(runtime, 'learning-applier')
    local = LocalFiles(runtime)
    assert len(list(local.iter_transcripts(None))) == 22
    with pytest.raises(DomainError):
        list(local.iter_transcripts(None, applier))
    # Exercise the production iterator while the fake model returns no candidates.
    program = runtime.background.programs.require('learnings.extract').run.__self__
    program.transcripts = local.iter_transcripts
    runtime.model.answers.extend([{'items': []}] * 22)
    for _ in range(3):
        run_global(runtime, max_sessions=10)
    with runtime.database.read() as uow:
        scanned = uow.repository('learning_scan_state', business_id=None).list()
        assert len(scanned) == 22
        assert {row['state_json']['agent_id'] for row in scanned} == {founder}
    assert len(runtime.model.calls) == 22


def test_global_adapter_rejects_symlinks_and_uses_registered_role(runtime):
    founder, sources = global_agent(runtime, 'founder')
    applier, _ = global_agent(runtime, 'learning-applier')
    local = LocalFiles(runtime)
    # Display names and transcript-provided role names cannot override exclusion.
    with runtime.database.transaction() as uow:
        row = uow.repository('agent_profiles', business_id=None).get(applier)
        uow.repository('agent_profiles', business_id=None).update(applier, {'name': 'founder-renamed'}, expected_revision=row['revision'])
    assert {item['agent_id'] for item in local.iter_transcripts(None)} == {founder}
    path = runtime.root / sources[0]['source_id']
    path.unlink()
    path.symlink_to(runtime.root / 'private-history.jsonl')
    assert list(local.iter_transcripts(None, founder)) == []
    with pytest.raises(DomainError):
        local.session_bytes(None, founder, path.name)


def test_global_scope_keeps_agent_specific_deltas_and_rejects_business_scope(runtime):
    _, sources = global_agent(runtime, 'founder')
    runtime.sources.extend(sources)
    runtime.model.answers.append({'items': [{**raw_learning(), 'scope': 'agent_kind'}]})
    runtime.model.answers.append(keep_verified)
    run_global(runtime)
    item = call(runtime, 'learnings_list', {}, global_scope=True)['items'][0]
    edited = call(runtime, 'learnings_edit', {'learning_id': item['id'], 'changes': {'scope': 'agent'}},
                  global_scope=True, revision=item['revision'])['item']
    assert edited['scope'] == 'agent'
    ctx = replace(context(), business_id=None, review_business_id=None)
    result = runtime.application.invoke(ctx, 'learnings_edit', {'learning_id': item['id'], 'changes': {'scope': 'biz'}},
        {'expected_revision': edited['revision'], 'idempotency_key': new_id()})
    assert result.status == 'error'


def test_global_history_is_not_limited_to_fifty_profiles(runtime):
    for index in range(50):
        global_agent(runtime, 'historical-' + str(index), count=0)
    founder, _ = global_agent(runtime, 'founder')
    assert {item['agent_id'] for item in LocalFiles(runtime).iter_transcripts(None)} == {founder}
