import copy

import pytest

from qorqut_agentic import AgenticError
from qorqut_agentic.cli import dispatch


def proposal():
    scope = {'description': 'Research', 'project_ids': [], 'work_types': ['research'],
             'workflow_refs': [], 'conditions': [], 'exceptions': []}
    ref = {'provider': 'fixture', 'kind': 'message', 'record_id': 'msg1', 'revision': '1',
           'content_hash': 'synthetic-hash', 'origin_event_id': 'event1', 'episode_id': 'episode1',
           'actor_id': 'owner1', 'actor_kind': 'human', 'occurred_at': '2026-10-27T10:00:00Z',
           'locator': {'message': '1'}, 'scope': scope,
           'provenance': {'producer_id': 'fixture', 'original_refs': [], 'overseer_origin': False},
           'availability': 'available', 'namespace': {'kind': 'global', 'business_id': None}}
    return {'schema_version': 1, 'id': '84e2edf6-4754-539f-8e54-b394bf4b805c',
            'extraction_run_id': 'synthetic-run', 'summary': 'Cite data', 'change_direction': 'Cite data',
            'observed_fact': 'A source was not cited', 'interpretation': 'Citation could help', 'scope': scope,
            'target_refs': [{'description': 'research'}], 'origin_type': 'inferred_improvement',
            'sources': [{'ref': ref, 'role': 'supporting'}],
            'created_at': '2026-10-27T10:05:00Z', 'created_by': 'agent1'}


def test_proposal_is_immutable_and_retries_are_idempotent(layer):
    record = proposal()
    first = layer.overseer.proposals.record(record, 'op1')
    assert layer.overseer.proposals.record(record, 'op1') == first
    assert layer.overseer.proposals.get(record['id']) == record
    assert layer.overseer.proposals.list()['items'] == [record]
    changed = copy.deepcopy(record)
    changed['summary'] = 'Changed'
    with pytest.raises(AgenticError) as exc:
        layer.overseer.proposals.record(changed, 'op1')
    assert exc.value.code == 'operation_conflict'
    with pytest.raises(AgenticError):
        layer.overseer.proposals.record(changed, 'op2')
    assert layer.overseer.proposals.get(record['id']) == record


def test_record_requires_support_and_cli_uses_same_validation(layer):
    record = proposal()
    record['sources'][0]['role'] = 'contrary'
    with pytest.raises(AgenticError) as exc:
        dispatch(layer, {'operation': 'overseer.proposals.record', 'arguments': {'record': record, 'operation_id': 'bad'}})
    assert exc.value.code == 'validation_failed'
    assert layer.overseer.proposals.list()['items'] == []
