import copy
import uuid

import pytest

from qorqut_agentic import AgenticError
from .conftest import git
from .test_overseer_state import proposal


def uid():
    return str(uuid.uuid4())


def revision(p, delta_id=None):
    ref = p['sources'][0]['ref']
    return {'schema_version': 1, 'delta_id': delta_id or uid(), 'revision': 1,
            'summary': 'Cite data', 'change_direction': 'Cite data', 'scope': p['scope'],
            'target_refs': p['target_refs'], 'proposal_ids': [p['id']],
            'support_summary': {'proposal_count': 1, 'event_count': 1, 'episode_count': 1,
                                'event_refs': [ref['origin_event_id']], 'episode_refs': [ref['episode_id']]},
            'uncertainties': [], 'ranking_reason': 'Supported by an observed result.',
            'created_at': '2026-10-27T10:10:00Z', 'created_by': 'agent1'}


def binding(delta, purpose='review', actions=None):
    return {'schema_version': 1, 'id': uid(), 'purpose': purpose, 'run_id': 'synthetic-run',
            'targets': {'delta_id': delta['delta_id'], 'delta_revision': delta['revision'],
                        'application_id': None, 'plan_revision': None},
            'owner_scope': {'kind': 'global', 'business_id': None}, 'request_key': uid(),
            'request_ref': {'provider': 'fixture', 'signoff_id': uid(), 'proposal_revision': 1, 'payload_hash': 'hash'},
            'choice_actions': actions or {'accept': 'accept', 'reject': 'reject', 'later': 'later'}}


class Gateway:
    def __init__(self, b, choice='accept', notes=''):
        self.ref = {**b['request_ref'], 'status_revision': 8}
        self.answer = {'reference': self.ref, 'state': 'answered', 'superseded_by_id': None,
                       'owner_principal_id': uid(), 'response_key': uid(), 'message_ref': None,
                       'selected_choice_ids': [choice], 'notes': notes, 'answered_at': '2026-10-27T17:00:00Z'}

    def resolve_response(self, binding, reference):
        return copy.deepcopy(self.answer)

    def call(self, interpretation=None):
        return {'reference': self.ref, 'interpretation': interpretation}


def setup(layer):
    p = proposal()
    layer.overseer.proposals.record(p, 'p')
    d = revision(p)
    layer.overseer.deltas.create(d, 'd')
    return p, d


def test_acceptance_replay_is_dormant_and_gateway_required(layer):
    _, d = setup(layer)
    b = binding(d)
    layer.overseer.inputs.bind(b, 'b')
    g = Gateway(b)
    with pytest.raises(AgenticError) as exc:
        layer.overseer.apply_owner_response(b['id'], g.call(), operation_id='answer')
    assert exc.value.code == 'owner_input_required'
    before = set(layer.root.parent.iterdir())
    worktrees_before = git(layer, 'worktree', 'list', '--porcelain').stdout
    accepted = layer.overseer.apply_owner_response(b['id'], g.call(), operation_id='answer', gateway=g)
    assert accepted['action'] == 'accept'
    assert layer.overseer.apply_owner_response(b['id'], g.call(), operation_id='another-replay', gateway=g) == accepted
    assert set(layer.root.parent.iterdir()) == before
    assert git(layer, 'worktree', 'list', '--porcelain').stdout == worktrees_before
    with layer._ctx.store.reader() as conn:
        apps = conn.execute('SELECT * FROM delta_applications').fetchall()
        assert len(apps) == 1 and apps[0]['phase'] == 'queued' and apps[0]['run_id'] is None
    g.answer['notes'] = 'different canonical content'
    with pytest.raises(AgenticError) as exc:
        layer.overseer.apply_owner_response(b['id'], g.call(), operation_id='conflict', gateway=g)
    assert exc.value.code == 'operation_conflict'


def test_conditional_and_stale_responses_never_approve(layer):
    _, d = setup(layer)
    b = binding(d)
    layer.overseer.inputs.bind(b, 'b')
    g = Gateway(b, notes='If scope is narrowed')
    interpretation = {'clarity': 'conditional', 'action': 'accept', 'rationale': 'Depends on narrowing.'}
    assert layer.overseer.apply_owner_response(b['id'], g.call(interpretation), operation_id='answer', gateway=g)['status'] == 'needs_clarification'
    b2 = binding(d)
    layer.overseer.inputs.bind(b2, 'b2')
    d2 = copy.deepcopy(d)
    d2['revision'] = 2
    d2['summary'] = 'Different scope'
    layer.overseer.deltas.revise(d2, 1, 'revision')
    g2 = Gateway(b2)
    with pytest.raises(AgenticError) as exc:
        layer.overseer.apply_owner_response(b2['id'], g2.call(), operation_id='stale', gateway=g2)
    assert exc.value.code == 'stale_revision'
    assert layer.overseer.deltas.get(d['delta_id'])['decision'] == 'undecided'
    with layer._ctx.store.reader() as conn:
        assert conn.execute('SELECT count(*) FROM delta_applications').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM overseer_owner_responses').fetchone()[0] == 2


def test_rejection_persists_and_reopening_requires_owner(layer):
    p, d = setup(layer)
    b = binding(d)
    layer.overseer.inputs.bind(b, 'b')
    g = Gateway(b, 'reject')
    layer.overseer.apply_owner_response(b['id'], g.call(), operation_id='reject', gateway=g)
    d2 = copy.deepcopy(d); d2['revision'] = 2; d2['summary'] = 'Repackaged'
    with pytest.raises(AgenticError) as exc:
        layer.overseer.deltas.revise(d2, 1, 'bad')
    assert exc.value.code == 'invalid_transition'
    d2['summary'] = d['summary']
    layer.overseer.deltas.revise(d2, 1, 'evidence')
    assert layer.overseer.deltas.get(d['delta_id'])['decision'] == 'rejected'
    b2 = binding(d2, 'reopen', {'open': 'reopen', 'later': 'later'})
    layer.overseer.inputs.bind(b2, 'b2')
    g2 = Gateway(b2, 'open')
    layer.overseer.apply_owner_response(b2['id'], g2.call(), operation_id='reopen', gateway=g2)
    restored = layer.overseer.deltas.get(d['delta_id'], history_limit=1)
    assert restored['decision'] == 'undecided' and restored['history_complete'] is False
    assert restored['decision_history'][0]['result']['action'] == 'reject'
    next_page = layer.overseer.deltas.get(d['delta_id'], history_limit=1, history_cursor=restored['history_next_cursor'])
    assert next_page['decision_history'][0]['result']['action'] == 'reopen' and next_page['history_complete'] is True
    assert layer.overseer.deltas.get(d['delta_id'], revision=1)['revision'] == d


def test_membership_moves_are_explicit_and_decided_members_are_retained(layer):
    p, first = setup(layer)
    second = revision(p)
    with pytest.raises(AgenticError) as exc:
        layer.overseer.deltas.create(second, 'attempt-steal')
    assert exc.value.code == 'invalid_transition'
    empty = copy.deepcopy(first)
    empty['revision'] = 2
    empty['proposal_ids'] = []
    empty['support_summary'] = {'proposal_count': 0, 'event_count': 0, 'episode_count': 0, 'event_refs': [], 'episode_refs': []}
    layer.overseer.deltas.revise(empty, 1, 'detach')
    layer.overseer.deltas.create(second, 'new-delta')
    assert layer.overseer.deltas.get(first['delta_id'], 1)['revision']['proposal_ids'] == [p['id']]
    b = binding(second)
    layer.overseer.inputs.bind(b, 'b')
    g = Gateway(b, 'reject')
    layer.overseer.apply_owner_response(b['id'], g.call(), operation_id='reject', gateway=g)
    replacement = copy.deepcopy(second)
    replacement['revision'] = 2
    replacement['proposal_ids'] = []
    replacement['support_summary'] = empty['support_summary']
    with pytest.raises(AgenticError) as exc:
        layer.overseer.deltas.revise(replacement, 1, 'evade')
    assert exc.value.code == 'invalid_transition'


def test_batch_is_frozen_and_rejected_delta_is_ineligible(layer):
    _, d = setup(layer)
    batch = {'schema_version': 1, 'id': uid(), 'run_id': 'review', 'created_at': 'now',
             'presentation_ref': {'id': 'fixture'},
             'items': [{'delta_id': d['delta_id'], 'revision': 1, 'ranking_reason': 'Useful.'}]}
    assert layer.overseer.review.save_batch(batch, 'batch') == batch
    b = binding(d)
    layer.overseer.inputs.bind(b, 'b')
    g = Gateway(b, 'reject')
    layer.overseer.apply_owner_response(b['id'], g.call(), operation_id='reject', gateway=g)
    assert layer.overseer.review.get_batch(batch['id']) == batch
    second = copy.deepcopy(batch); second['id'] = uid()
    with pytest.raises(AgenticError) as exc:
        layer.overseer.review.save_batch(second, 'second')
    assert exc.value.code == 'invalid_transition'


def test_binding_initial_request_reference_is_bound_only_once(layer):
    _, d = setup(layer)
    b = binding(d)
    pending = copy.deepcopy(b); pending['request_ref'] = None
    layer.overseer.inputs.bind(pending, 'bind-pending')
    layer.overseer.inputs.bind(b, 'bind-request')
    assert layer.overseer.inputs.get(b['id']) == b
    changed = copy.deepcopy(b); changed['request_ref']['payload_hash'] = 'other'
    with pytest.raises(AgenticError) as exc:
        layer.overseer.inputs.bind(changed, 'repoint')
    assert exc.value.code == 'operation_conflict'


@pytest.mark.parametrize('mutate_answer', [
    lambda g: g.answer.update(superseded_by_id=uid()),
    lambda g: g.answer['reference'].update(payload_hash='different'),
    lambda g: g.answer.update(state='cancelled'),
])
def test_rejects_stale_canonical_response(layer, mutate_answer):
    _, d = setup(layer)
    b = binding(d)
    layer.overseer.inputs.bind(b, 'b')
    g = Gateway(b)
    request = copy.deepcopy(g.call())
    mutate_answer(g)
    with pytest.raises(AgenticError) as exc:
        layer.overseer.apply_owner_response(b['id'], request, operation_id='answer', gateway=g)
    assert exc.value.code == 'stale_revision'
    assert layer.overseer.deltas.get(d['delta_id'])['decision'] == 'undecided'


def test_contradictory_interpretation_never_overrides_choice(layer):
    _, d = setup(layer)
    b = binding(d)
    layer.overseer.inputs.bind(b, 'b')
    g = Gateway(b, choice='reject', notes='Actually accept')
    interpretation = {'clarity': 'explicit', 'action': 'accept', 'rationale': 'Contradicts the selected choice.'}
    outcome = layer.overseer.apply_owner_response(b['id'], g.call(interpretation), operation_id='answer', gateway=g)
    assert outcome['status'] == 'needs_clarification'
    assert layer.overseer.deltas.get(d['delta_id'])['decision'] == 'undecided'


def test_ambiguous_response_cannot_be_reinterpreted_or_replaced_on_same_binding(layer):
    _, d = setup(layer)
    b = binding(d); layer.overseer.inputs.bind(b, 'b')
    g = Gateway(b, notes='Maybe')
    unresolved = {'clarity': 'ambiguous', 'action': 'accept', 'rationale': 'Not an explicit decision.'}
    layer.overseer.apply_owner_response(b['id'], g.call(unresolved), operation_id='first', gateway=g)
    answer = layer.overseer.inputs.get(b['id'], include_response=True)
    assert answer['response']['canonical']['notes'] == 'Maybe'
    assert answer['result']['status'] == 'needs_clarification'
    g.answer['response_key'] = uid()
    g.answer['notes'] = ''
    with pytest.raises(AgenticError) as exc:
        layer.overseer.apply_owner_response(b['id'], g.call(), operation_id='different', gateway=g)
    assert exc.value.code == 'operation_conflict'
    assert layer.overseer.deltas.get(d['delta_id'])['decision'] == 'undecided'
