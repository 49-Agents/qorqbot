import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from bazarlyk.domains.learnings import analysis, verification
from test_background_programs import raw_learning, runtime
from test_global_chat_learning import global_agent, keep_verified, run_global
from test_learning_sources import call


def proposals():
    core = analysis.build_core({'source_id': 'registered', 'session_id': 'session', 'content': json.dumps({
        'type': 'user', 'message': {'content': 'Always keep source quotes.'}})},
        SimpleNamespace(id=None, slug='Global', root=''))
    biz = SimpleNamespace(id=None, header=lambda: {}, agents=lambda: [])
    items, _, _ = analysis.extract(core, biz, 'run', passes=2,
        chat=lambda *args: ({'items': [{**raw_learning(), 'scope': 'global'}]}, {}))
    return core, items


def test_verifier_consolidates_exact_passes_and_preserves_original_evidence():
    core, items = proposals()
    before = copy.deepcopy(items)
    def chat(system, payload, model):
        supplied = json.loads(payload)['proposals']
        assert len(supplied) == 1
        return {'assessments': [{'id': supplied[0]['id'], 'decision': 'revise', 'reason': 'Narrow to observed role.',
            'text': 'Keep source quotes.', 'scope': 'agent_kind', 'tag': 'rule', 'category': 'delta',
            'support': 'owner', 'durability': 'durable'}]}, {}
    verified, audit = verification.verify(items, core, 'test', chat=chat)
    assert len(verified) == 1 and verified[0]['votes'] == 2
    assert verified[0]['evidence'] == before[0]['evidence']
    assert verified[0]['scope'] == 'agent_kind' and items == before
    assert audit[0]['original']['text'] == before[0]['text']


@pytest.mark.parametrize('answer', [{}, {'assessments': []}, {'assessments': [
    {'id': 'invented', 'decision': 'keep', 'reason': 'no'}]}])
def test_verifier_incomplete_or_unknown_assessments_fail_closed(answer):
    core, items = proposals()
    with pytest.raises(analysis.LearnError):
        verification.verify(items, core, 'test', chat=lambda *args: (answer, {}))


@pytest.mark.parametrize('support,durability', [('unsupported', 'durable'), ('owner', 'task_only')])
def test_verifier_cannot_keep_claims_it_marks_unsupported_or_temporary(support, durability):
    core, items = proposals()
    def chat(system, payload, model):
        item = json.loads(payload)['proposals'][0]
        return {'assessments': [{'id': item['id'], 'decision': 'keep', 'reason': 'An unconfirmed report.',
            'support': support, 'durability': durability, 'category': 'context', 'tag': 'fact', 'scope': 'global'}]}, {}
    verified, audit = verification.verify(items, core, 'test', chat=chat)
    assert not verified and audit[0]['decision'] == 'drop'


@pytest.mark.parametrize('field', ['decision', 'support', 'durability', 'category'])
def test_malformed_assessment_values_are_retryable_verification_errors(field):
    core, items = proposals()
    def chat(system, payload, model):
        item = json.loads(payload)['proposals'][0]
        assessment = {'id': item['id'], 'decision': 'keep', 'reason': 'Claim review.',
                      'support': 'owner', 'durability': 'durable', 'category': 'delta'}
        assessment[field] = {'unexpected': 'object'}
        return {'assessments': [assessment]}, {}
    with pytest.raises(analysis.LearnError):
        verification.verify(items, core, 'test', chat=chat)


def test_invalid_proposal_is_audited_without_source_retry(runtime):
    _, sources = global_agent(runtime, 'founder')
    runtime.sources.extend(sources)
    runtime.model.answers.append({'items': [{**raw_learning('Fabricated quotation'), 'scope': 'global'}]})
    run_global(runtime)
    with runtime.database.read() as uow:
        assert not uow.repository('learning_candidates', business_id=None).list()
        result = uow.repository('learning_runs', business_id=None).list()[0]['result_json']
        assert result['processed'] == 1 and result['diagnostics'][0]['stage'] == 'evidence'
    count = len(runtime.model.calls)
    run_global(runtime)
    assert len(runtime.model.calls) == count


@pytest.mark.parametrize('resume', ['explicit', 'changed'])
def test_incomplete_sources_publish_nothing_and_pause_then_resume(runtime, resume):
    _, sources = global_agent(runtime, 'founder')
    runtime.sources.extend(sources)
    for attempt in range(1, 4):
        runtime.model.answers.extend([{'items': [{**raw_learning(), 'scope': 'global'}]}, {}])
        queued = call(runtime, 'learnings_extract', {'passes': 2}, global_scope=True)
        runtime.background.run_one(queued['job_id'])
        with runtime.database.read() as uow:
            assert not uow.repository('learning_candidates', business_id=None).list()
            state = uow.repository('learning_scan_state', business_id=None).list()[0]['state_json']
            assert state['attempts'] == attempt
            assert state['status'] == ('needs_attention' if attempt == 3 else 'retry')
    count = len(runtime.model.calls)
    run_global(runtime)
    assert len(runtime.model.calls) == count
    if resume == 'changed':
        sources[0]['content'] += '\n'
        sources[0]['transcript_hash'] = hashlib.sha256(sources[0]['content'].encode()).hexdigest()
    runtime.model.answers.extend([{'items': [{**raw_learning(), 'scope': 'global'}]}, keep_verified])
    run_global(runtime, retry_failed=resume == 'explicit')
    with runtime.database.read() as uow:
        assert len(uow.repository('learning_candidates', business_id=None).list()) == 1
        assert uow.repository('learning_scan_state', business_id=None).list()[0]['state_json']['status'] == 'complete'
        assert uow.repository('learning_scan_state', business_id=None).list()[0]['state_json']['attempts'] == 1
    count = len(runtime.model.calls)
    run_global(runtime)
    assert len(runtime.model.calls) == count


def test_concurrent_scan_cannot_publish_late_candidates(runtime):
    _, sources = global_agent(runtime, 'founder')
    runtime.sources.extend(sources)
    def competing_scan(messages):
        with runtime.database.transaction() as uow:
            uow.repository('learning_scan_state', business_id=None).insert({
                'session_id': sources[0]['session_id'], 'transcript_hash': sources[0]['transcript_hash'],
                'state_json': {'status': 'complete', 'run_id': 'competing-run'}})
        return keep_verified(messages)
    runtime.model.answers.extend([{'items': [{**raw_learning(), 'scope': 'global'}]}, competing_scan])
    queued = call(runtime, 'learnings_extract', {'passes': 1}, global_scope=True)
    result = runtime.background.run_one(queued['job_id'])
    assert result.get('state') != 'succeeded'
    with runtime.database.read() as uow:
        assert not uow.repository('learning_candidates', business_id=None).list()
        assert uow.repository('learning_scan_state', business_id=None).list()[0]['state_json']['run_id'] == 'competing-run'
