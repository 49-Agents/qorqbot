"""Cross-day supersession uses the existing comparison and decision history."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from bazarlyk.domains.learnings import analysis
from qorqut_operations.contracts import DomainError, new_id
from foundation_support import HUMAN
from test_background_learning_analysis import brief
from test_background_programs import runtime, raw_learning
from test_learning_reconciliation import candidate
from test_learning_sources import call, question


def resolution(old, new):
    return {'earlier': old['id'], 'later': new['id'], 'kind': 'answered',
        'reason': 'Full payment of the same contract resolves the earlier uncertainty.',
        'same_subject': True, 'complete': True, 'explicit': True, 'earlier_uncertain': True,
        'replacement_state': 'resolved',
        'earlier_quote': old.get('owner_quote') or old['evidence']['owner_quote'],
        'later_quote': new.get('owner_quote') or new['evidence']['owner_quote']}


def pair():
    return (candidate('old', 'We do not know whether Karthik paid contract C42.', 'fact',
                      '2026-09-10T09:00:00Z', revision=1),
            candidate('new', 'Karthik has paid contract C42 in full.', 'fact', '2026-09-12T09:00:00Z'))


@pytest.mark.parametrize('mode,automatic', [('preview', False), ('apply', True)])
def test_cross_day_uncertainty_preview_or_reject(mode, automatic):
    old, new = pair()
    before = deepcopy(old)
    items, lines, _, _ = analysis.dedup([new], [old], brief(), 'run', supersession_mode=mode,
        chat=lambda *a: ({'resolutions': [resolution(old, new)]}, {}))
    assert old == before and len(items) == 1
    assert items[0]['reconciliations'][0]['automatic'] is automatic
    assert bool(lines) is automatic
    if automatic:
        assert lines[0]['kind'] == 'superseded'


@pytest.mark.parametrize('change', ['partial', 'different_subject', 'inferred', 'quote', 'reverse',
    'no_date', 'agent_claim', 'reviewed', 'rejected', 'accepted', 'scope', 'role', 'conflict', 'partial_state'])
def test_unsafe_or_protected_relationships_never_reject(change):
    old, new = pair()
    p = resolution(old, new)
    answer = {'resolutions': [p]}
    if change == 'partial': p['complete'] = False
    if change == 'partial_state': p['replacement_state'] = 'partial'
    if change == 'different_subject': p['same_subject'] = False
    if change == 'inferred': p['explicit'] = False
    if change == 'quote': p['later_quote'] = 'Invented evidence'
    if change == 'reverse': new['evidence']['source_at'] = '2026-09-09T00:00:00Z'
    if change == 'no_date': new['evidence']['source_at'] = None
    if change == 'agent_claim': new['said_by'] = 'agent_confirmed_by_owner'
    if change == 'reviewed': old['reviewed'] = True
    if change in {'accepted', 'rejected'}: old['state'] = change
    if change == 'scope': new['scope'] = 'global'
    if change == 'role':
        old.update(scope='agent_kind', observed={'agent_kind': 'founder'})
        new.update(scope='agent_kind', observed={'agent_kind': 'ordubashy'})
    if change == 'conflict': answer['contradicts'] = [{'fresh': 'new', 'existing': 'old'}]
    _, lines, _, _ = analysis.dedup([new], [old], brief(), 'run', supersession_mode='apply',
        chat=lambda *a: (answer, {}))
    assert not any(line['kind'] == 'superseded' for line in lines)


def test_pending_queue_can_be_compared_without_extracting_new_sources():
    old, new = pair()
    new['revision'] = 1
    _, lines, _, _ = analysis.dedup([], [old, new], brief(), 'run', supersession_mode='apply',
        chat=lambda *a: ({'resolutions': [resolution(old, new)]}, {}))
    assert {line['kind'] for line in lines} == {'related', 'superseded'}


def test_repeating_preview_does_not_accumulate_identical_relations():
    old, new = pair()
    answer = {'resolutions': [resolution(old, new)]}
    _, lines, _, _ = analysis.dedup([], [old, new], brief(), 'run', supersession_mode='preview',
        chat=lambda *a: (answer, {}))
    new['reconciliations'] = lines[0]['reconciliations']
    _, lines, _, _ = analysis.dedup([], [old, new], brief(), 'run', supersession_mode='preview',
        chat=lambda *a: (answer, {}))
    assert not lines


@pytest.mark.parametrize('reviewed_endpoint', [0, 1])
def test_owner_review_of_either_endpoint_during_pending_comparison_wins(runtime, reviewed_endpoint):
    runtime.policy.human_global_capabilities = {HUMAN: {'read_global_context'}}
    old, new = pair()
    for item in (old, new):
        item.update(id=new_id(), scope='global')
    with runtime.database.transaction() as uow:
        for item in (old, new):
            uow.repository('learning_candidates', business_id=None).insert({'id': item['id'], 'candidate_json': item})
    def compare(messages):
        reviewed = (old, new)[reviewed_endpoint]
        call(runtime, 'learnings_review', {'learning_id': reviewed['id'], 'state': 'rejected',
             'feedback': 'Owner decision during comparison'}, global_scope=True, revision=1)
        return {'resolutions': [resolution(old, new)]}
    runtime.model.answers.append(compare)
    job = call(runtime, 'learnings_extract', {'supersession_mode': 'apply'}, global_scope=True)
    result = runtime.background.run_one(job['job_id'])
    assert result.get('state') != 'succeeded', result
    for index, item in enumerate((old, new)):
        current = call(runtime, 'learnings_get', {'learning_id': item['id']}, global_scope=True)['item']
        assert current['state'] == ('rejected' if index == reviewed_endpoint else 'open')
        assert not current.get('superseded_by')


def test_agents_cannot_enable_apply_directly_or_through_routine_configuration():
    from bazarlyk.domains.learnings.operations import LearningOperations
    from qorqut_operations.routines.operations import RoutineOperations
    ctx = SimpleNamespace(principal_kind='agent')
    with pytest.raises(DomainError, match='Only the owner'):
        LearningOperations.prepare_extract(None, ctx, {'supersession_mode': 'apply'}, None, None)
    with pytest.raises(DomainError, match='Only the owner'):
        RoutineOperations.definition_values(None, ctx, {'definition': {'program': 'learnings.extract',
            'arguments': {'supersession_mode': 'apply'}}})


@pytest.mark.parametrize('mode', ['preview', 'apply', 'pending'])
@pytest.mark.parametrize('global_scope', [False, True])
def test_persist_rejection_and_restore_without_approving_replacement(runtime, mode, global_scope):
    def extract(index, text):
        runtime.signoffs.clock = lambda: f'2026-09-{10 + index * 2}T09:00:00Z'
        question(runtime, global_scope=global_scope, notes=text)
        raw = raw_learning(text)
        raw.update(text=text, tag='fact', bucket='context', scope='global' if global_scope else 'biz')
        runtime.model.answers.append({'items': [raw]})
        if index:
            def compare(messages):
                payload = json.loads(messages[1]['content'])
                return {'resolutions': [resolution(payload['existing'][0], payload['fresh'][0])]}
            runtime.model.answers.append(compare)
        job = call(runtime, 'learnings_extract', {'passes': 1, 'supersession_mode': 'preview' if mode == 'pending' else mode}, global_scope=global_scope)
        result = runtime.background.run_one(job['job_id'])
        assert result['state'] == 'succeeded', result
    old, new = pair()
    extract(0, old['text'])
    first = call(runtime, 'learnings_list', {}, global_scope=global_scope)['items'][0]
    extract(1, new['text'])
    if mode == 'pending':
        def compare_pending(messages):
            payload = json.loads(messages[1]['content'])
            assert not payload['fresh']
            ordered = sorted(payload['existing'], key=lambda i: i['source_at'])
            return {'resolutions': [resolution(*ordered)]}
        runtime.model.answers.append(compare_pending)
        job = call(runtime, 'learnings_extract', {'passes': 1, 'supersession_mode': 'apply'}, global_scope=global_scope)
        result = runtime.background.run_one(job['job_id'])
        assert result['state'] == 'succeeded', result
    items = call(runtime, 'learnings_list', {}, global_scope=global_scope)['items']
    earlier = next(i for i in items if i['id'] == first['id'])
    later = next(i for i in items if i['id'] != first['id'])
    assert later['state'] == 'open' and not later['filing']
    if mode == 'preview':
        assert earlier['state'] == 'open' and not earlier.get('superseded_by')
        assert not later['reconciliations'][0]['automatic']
        return
    assert earlier['state'] == 'rejected' and earlier['superseded_by'] == later['id']
    assert earlier['decision']['note'].startswith('Superseded by ')
    assert earlier['evidence'] == first['evidence']
    assert not earlier.get('reconciled_into')
    # The normal rejected queue retains the older finding and its explanation.
    rejected = call(runtime, 'learnings_list', {'state': 'rejected'}, global_scope=global_scope)['items']
    assert any(i['id'] == first['id'] for i in rejected)
    if global_scope:
        call(runtime, 'learnings_keep_both', {'learning_id': later['id']},
             global_scope=True, revision=later['revision'])
    else:
        call(runtime, 'learnings_review', {'learning_id': earlier['id'], 'state': 'open'},
             revision=earlier['revision'])
    restored = call(runtime, 'learnings_get', {'learning_id': earlier['id']}, global_scope=global_scope)['item']
    assert restored['state'] == 'open' and not restored.get('superseded_by')
    assert restored['reconciliation_reviewed'] is True
