import copy

import pytest

from qorqut_agentic import AgenticError
from qorqut_agentic.cli import dispatch
from .test_overseer_decisions import revision, uid
from .test_overseer_state import proposal


def test_mirrored_source_does_not_inflate_distinct_events(layer):
    first = proposal()
    second = copy.deepcopy(first)
    second['id'] = uid()
    second['sources'][0]['ref']['record_id'] = 'copied-message'
    second['sources'][0]['ref']['provenance']['original_refs'] = [{'record_id': 'msg1', 'revision': '1'}]
    contrary = copy.deepcopy(first['sources'][0])
    contrary['role'] = 'contrary'
    contrary['ref']['record_id'] = 'contrary-message'
    second['sources'].append(contrary)
    layer.overseer.proposals.record(first, 'first')
    layer.overseer.proposals.record(second, 'second')
    d = revision(first)
    d['proposal_ids'].append(second['id'])
    d['support_summary']['proposal_count'] = 2
    layer.overseer.deltas.create(d, 'delta')
    assert layer.overseer.deltas.get(d['delta_id'])['revision']['support_summary']['event_count'] == 1
    assert layer.overseer.proposals.get(second['id'])['sources'][1]['role'] == 'contrary'


def test_private_material_and_incomplete_coverage_are_immutable(layer, tmp_path):
    material = {'schema_version': 1, 'id': uid(), 'source_refs': [{'record_id': 'source', 'revision': '1'}]}
    install_test_workflows(layer, tmp_path)
    gateway = InvocationGateway('extraction')
    run = layer.overseer.start_run('extraction', {'reference': gateway.ref}, operation_id='extract', gateway=gateway)
    source = proposal()['sources'][0]['ref']
    pending = copy.deepcopy(source); pending['revision'] = '2'; pending['content_hash'] = 'other-hash'
    coverage = {'schema_version': 1, 'run_id': run['run_id'], 'frontier': {'snapshot_id': uid(), 'captured_at': 'now', 'providers': [{'provider': 'fixture', 'anchor': 'captured'}]}, 'complete': False,
                'analyzed_sources': [source], 'pending_sources': [pending], 'next_cursor': None,
                'gaps': [{'source': 'unavailable-history'}]}
    layer.overseer.materials.record(material, 'material')
    receipt = layer.overseer.coverage.record(coverage, 'coverage')
    assert layer.overseer.coverage.record(coverage, 'coverage') == receipt
    assert layer.overseer.materials.get(material['id']) == material
    assert layer.overseer.coverage.get(receipt['id']) == receipt
    found = layer.overseer.coverage.get(source_refs=[source, pending])
    assert found['analyzed_sources'] == [source]
    assert found['receipts'] == [{'type': 'coverage', 'id': receipt['id']}]
    wrong_hash = copy.deepcopy(source); wrong_hash['content_hash'] = 'different'
    wrong_namespace = copy.deepcopy(source); wrong_namespace['namespace'] = {'kind': 'business', 'business_id': uid()}
    assert layer.overseer.coverage.get(source_refs=[wrong_hash, wrong_namespace])['analyzed_sources'] == []
    with pytest.raises(AgenticError):
        layer.overseer.coverage.get(source_refs=[{'record_id': 'missing-identity'}])
    with pytest.raises(AgenticError):
        layer.overseer.coverage.get(source_refs=[source] * 201)
    fully_analyzed = {**coverage, 'analyzed_sources': [pending], 'pending_sources': [], 'complete': True, 'gaps': []}
    completed = layer.overseer.coverage.record(fully_analyzed, 'coverage-no-proposal')
    assert layer.overseer.coverage.get(source_refs=[pending])['receipts'][0]['id'] == completed['id']
    assert layer.overseer.proposals.list()['items'] == []
    changed = copy.deepcopy(coverage); changed['complete'] = True; changed['gaps'] = []
    with pytest.raises(AgenticError) as exc:
        layer.overseer.coverage.record(changed, 'different-operation')
    assert exc.value.code == 'validation_failed'
    assert layer.overseer.coverage.get(receipt['id']) == receipt


def test_owner_decision_has_no_cli_operation(layer):
    with pytest.raises(AgenticError) as exc:
        dispatch(layer, {'operation': 'overseer.apply_owner_response', 'arguments': {}})
    assert exc.value.code == 'invalid_request'


def install_test_workflows(layer, tmp_path):
    import json
    from .conftest import ATTR
    source = tmp_path / 'synthetic-workflows'
    source.mkdir()
    entries = []
    for name in ('extract', 'review', 'apply'):
        workflow = {'schema_version': 1, 'id': name, 'description': 'Synthetic workflow',
                    'steps': [{'id': 'record', 'instructions': 'Record progress.'}]}
        if name == 'apply':
            workflow['arguments'] = [{'name': 'application_id', 'schema': {'type': 'string'}, 'prompt': 'Select?', 'acquisition': 'ask_at_step', 'step': 'record'}]
        if name == 'extract':
            workflow['arguments'] = [{'name': 'selection', 'schema': {'type': 'object'}, 'prompt': 'Selection?', 'acquisition': 'ask_at_step', 'step': 'record'}]
        (source / f'{name}.yaml').write_text(json.dumps(workflow))
        entries.append({'id': name, 'kind': 'workflow', 'description': 'Synthetic', 'when_to_use': 'Synthetic', 'path': f'{name}.yaml'})
    manifest = {'schema_version': 1, 'id': 'overseer', 'name': 'Synthetic Overseer', 'description': 'Synthetic', 'entries': entries}
    (source / 'agentic.yaml').write_text(json.dumps(manifest))
    layer.authoring.import_package(ATTR, 'overseer', {'directory': str(source)})


class InvocationGateway:
    def __init__(self, kind, args=None, origin='owner'):
        self.ref = {'provider': 'fixture', 'kind': 'extraction_due' if origin == 'routine' else 'owner_message', 'id': uid(), 'revision': 'abc'}
        self.value = {'invocation_key': uid(), 'reference': self.ref, 'allowed_kinds': [kind], 'origin': origin,
                      'owner_principal_id': uid() if origin == 'owner' else None, 'conversation_id': uid(),
                      'executor': {'actor_id': uid(), 'actor_kind': 'agent'}, 'arguments': args or {},
                      'source_text': 'Synthetic request', 'source_hash': 'abc'}

    def resolve_invocation(self, kind, ref):
        return copy.deepcopy(self.value)


def plan(d):
    return {'schema_version': 1, 'delta_id': d['delta_id'], 'delta_revision': d['revision'],
            'replaces_accepted_revision': None, 'intended_outcome': 'Cite sources in research.',
            'changes': [{'kind': 'instruction', 'action': 'edit', 'target_ref': 'example/research',
                         'path': 'packages/example/research.md', 'description': 'Cite data.', 'expected_sha256': 'a' * 64}],
            'base': {'commit': 'b' * 40, 'resource_hashes': {'packages/example/research.md': 'a' * 64}},
            'external_requirements': [], 'checks': [{'id': 'catalog', 'phase': 'post_merge', 'kind': 'catalog', 'arguments': {}}]}


def test_admitted_run_crash_retry_and_combined_plan_response(layer, tmp_path):
    from .test_overseer_decisions import setup, binding, Gateway
    install_test_workflows(layer, tmp_path)
    _, d = setup(layer)
    b = binding(d)
    layer.overseer.inputs.bind(b, 'review-binding')
    gateway = Gateway(b)
    accepted = layer.overseer.apply_owner_response(b['id'], gateway.call(), operation_id='accept', gateway=gateway)
    app_id = accepted['application_id']
    invocation = InvocationGateway('application', {'application_id': app_id})
    call = {'reference': invocation.ref}
    def crash(**kwargs):
        raise RuntimeError('synthetic crash after runner start')
    layer._ctx.hooks['overseer_after_run_start'] = crash
    with pytest.raises(RuntimeError):
        layer.overseer.start_run('application', call, operation_id='invoke', gateway=invocation)
    layer._ctx.hooks.clear()
    admitted = layer.overseer.start_run('application', call, operation_id='invoke', gateway=invocation)
    assert layer.overseer.start_run('application', call, operation_id='invoke-again', gateway=invocation) == admitted
    with layer._ctx.store.reader() as conn:
        assert conn.execute("SELECT count(*) FROM runs WHERE workflow_ref='overseer/apply'").fetchone()[0] == 1
    app = layer.overseer.applications.get(app_id)
    assert app['phase'] == 'planning' and app['revision'] == 2
    d2 = copy.deepcopy(d); d2['revision'] = 2; d2['summary'] = 'Cite evidence clearly'
    layer.overseer.deltas.revise(d2, 1, 'revise')
    draft = plan(d2); draft['replaces_accepted_revision'] = 1
    saved = layer.overseer.applications.save_plan(app_id, draft, expected_revision=2, operation_id='save-plan')
    assert saved['application']['agreement_ref'] is None
    assert layer.overseer.deltas.get(d['delta_id'])['accepted_revision'] == 1
    pbind = binding(d2, 'plan', {'go': 'proceed', 'later': 'later'})
    pbind['targets'].update(application_id=app_id, application_revision=3, plan_revision=1)
    layer.overseer.inputs.bind(pbind, 'plan-binding')
    p_gateway = Gateway(pbind, 'go')
    layer.overseer.apply_owner_response(pbind['id'], p_gateway.call(), operation_id='proceed', gateway=p_gateway)
    after = layer.overseer.applications.get(app_id)
    assert after['agreement_ref']['response_key'] == p_gateway.answer['response_key']
    assert after['accepted_revision'] == 2 and after['revision'] == 4
    assert after['plan']['delta_revision'] == 2
    assert layer.overseer.deltas.get(d['delta_id'])['accepted_revision'] == 2


def test_untrusted_or_routine_review_invocation_is_refused(layer):
    g = InvocationGateway('review', origin='routine')
    call = {'reference': g.ref}
    with pytest.raises(AgenticError) as exc:
        layer.overseer.start_run('review', call, operation_id='run', gateway=g)
    assert exc.value.code == 'owner_input_required'
    with pytest.raises(AgenticError) as exc:
        layer.overseer.start_run('review', call, operation_id='run')
    assert exc.value.code == 'owner_input_required'


def test_selection_signoff_links_previously_unselected_application(layer, tmp_path):
    from .test_overseer_decisions import setup, binding, Gateway
    install_test_workflows(layer, tmp_path)
    _, d = setup(layer)
    b = binding(d); layer.overseer.inputs.bind(b, 'review-binding')
    response = Gateway(b)
    accepted = layer.overseer.apply_owner_response(b['id'], response.call(), operation_id='accept', gateway=response)
    app_id = accepted['application_id']
    g = InvocationGateway('application')
    run = layer.overseer.start_run('application', {'reference': g.ref}, operation_id='start', gateway=g)
    selection = binding(d, 'select', {'choose': 'select', 'later': 'later'})
    selection['run_id'] = run['run_id']
    selection['targets'] = {'delta_id': None, 'delta_revision': None, 'application_id': None, 'plan_revision': None}
    selection['choice_targets'] = {'choose': {'application_id': app_id}}
    layer.overseer.inputs.bind(selection, 'selection-binding')
    answer = Gateway(selection, 'choose')
    layer.overseer.apply_owner_response(selection['id'], answer.call(), operation_id='select', gateway=answer)
    application = layer.overseer.applications.get(app_id)
    assert application['run_id'] == run['run_id'] and application['phase'] == 'planning'
    saved = layer.overseer.applications.save_plan(app_id, plan(d), expected_revision=application['revision'], operation_id='plan')
    assert saved['application']['agreement_ref'] is None


def test_clarification_replacement_clears_hold_and_applies_exact_new_answer(layer):
    from .test_overseer_decisions import setup, binding, Gateway
    _, d = setup(layer)
    first = binding(d)
    layer.overseer.inputs.bind(first, 'first')
    g = Gateway(first, notes='Only if narrowed')
    interpretation = {'clarity': 'conditional', 'action': 'accept', 'rationale': 'Conditioned on narrowing.'}
    layer.overseer.apply_owner_response(first['id'], g.call(interpretation), operation_id='conditional', gateway=g)
    batch = {'schema_version': 1, 'id': uid(), 'run_id': 'review', 'created_at': 'now', 'presentation_ref': {},
             'items': [{'delta_id': d['delta_id'], 'revision': 1, 'ranking_reason': 'Supported.'}]}
    with pytest.raises(AgenticError) as exc:
        layer.overseer.review.save_batch(batch, 'held-batch')
    assert exc.value.code == 'owner_input_required'
    replacement = binding(d)
    replacement['resolves_input_ids'] = [first['id']]
    layer.overseer.inputs.bind(replacement, 'replacement')
    answer = Gateway(replacement, 'accept')
    result = layer.overseer.apply_owner_response(replacement['id'], answer.call(), operation_id='new-answer', gateway=answer)
    assert result['action'] == 'accept'
    with layer._ctx.store.reader() as conn:
        assert conn.execute('SELECT resolving_input_id FROM overseer_input_resolutions WHERE input_id=?', (first['id'],)).fetchone()[0] == replacement['id']
        assert conn.execute('SELECT count(*) FROM overseer_owner_responses').fetchone()[0] == 2


def _satisfaction_fixture(layer, tmp_path):
    from .test_overseer_decisions import setup, binding, Gateway
    from .conftest import git
    install_test_workflows(layer, tmp_path)
    (layer.root / 'already-satisfied.md').write_text('Synthetic established rule.\n')
    git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'synthetic inspected base')
    _, d = setup(layer)
    review = binding(d); layer.overseer.inputs.bind(review, 'review-binding')
    owner = Gateway(review)
    app_id = layer.overseer.apply_owner_response(review['id'], owner.call(), operation_id='accept', gateway=owner)['application_id']
    invocation = InvocationGateway('application', {'application_id': app_id})
    layer.overseer.start_run('application', {'reference': invocation.ref}, operation_id='start', gateway=invocation)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    revision = apps.get(app_id)['revision']
    inspection = apps.inspect_satisfaction(app_id, resource_paths=['already-satisfied.md'], explanation='Synthetic existing instruction already expresses this intent.', expected_revision=revision, operation_id='inspect')['inspection']
    close = binding(d, 'close_satisfied', {'done': 'close_satisfied', 'later': 'later'})
    close['targets'].update(application_id=app_id, application_revision=revision)
    close['satisfaction_inspection_id'] = inspection['id']; close['satisfaction_evidence'] = inspection['evidence']
    return d, app_id, close


def test_already_satisfied_requires_evidence_and_never_fabricates_merge_or_checks(layer, tmp_path):
    from .test_overseer_decisions import Gateway
    _, app_id, close = _satisfaction_fixture(layer, tmp_path)
    forged = copy.deepcopy(close); forged['satisfaction_inspection_id'] = uid()
    with pytest.raises(AgenticError):
        layer.overseer.inputs.bind(forged, 'forged-binding')
    layer.overseer.inputs.bind(close, 'close-binding')
    answer = Gateway(close, 'done')
    result = layer.overseer.apply_owner_response(close['id'], answer.call(), operation_id='close', gateway=answer)
    assert layer.overseer.apply_owner_response(close['id'], answer.call(), operation_id='close-replay', gateway=answer) == result
    app = layer.overseer.applications.get(app_id)
    assert app['phase'] == 'complete' and app['outcome'] == 'already_satisfied' and app['merge'] is None
    assert app['checks']['state'] == 'not_run' and app['satisfaction_evidence']['evidence'] == close['satisfaction_evidence']


def test_satisfied_close_rejects_merged_failed_application(layer, tmp_path):
    import json
    from .test_overseer_decisions import Gateway
    _, app_id, close = _satisfaction_fixture(layer, tmp_path)
    with layer._ctx.store.transaction() as conn:
        conn.execute("UPDATE delta_applications SET phase='merged', merge=?, checks=? WHERE id=?",
                     (json.dumps({'merged_commit': 'e' * 40}), json.dumps({'state': 'failed', 'receipts': []}), app_id))
    layer.overseer.inputs.bind(close, 'close-binding')
    answer = Gateway(close, 'done')
    with pytest.raises(AgenticError) as exc:
        layer.overseer.apply_owner_response(close['id'], answer.call(), operation_id='close', gateway=answer)
    assert exc.value.code == 'invalid_transition'
    app = layer.overseer.applications.get(app_id)
    assert app['phase'] == 'merged' and app['checks']['state'] == 'failed' and app['merge']['merged_commit'] == 'e' * 40


def test_plan_save_requires_admitted_application_run(layer):
    from .test_overseer_decisions import setup, binding, Gateway
    _, d = setup(layer)
    b = binding(d); layer.overseer.inputs.bind(b, 'review-binding')
    g = Gateway(b)
    app_id = layer.overseer.apply_owner_response(b['id'], g.call(), operation_id='accept', gateway=g)['application_id']
    with pytest.raises(AgenticError) as exc:
        layer.overseer.applications.save_plan(app_id, plan(d), expected_revision=1, operation_id='plan')
    assert exc.value.code == 'owner_input_required'
    assert layer.overseer.applications.get(app_id)['phase'] == 'queued'


def test_record_operations_cannot_forge_admitted_run_reservations(layer):
    forged = {'schema_version': 1, 'id': uid(), 'state': 'linked', 'kind': 'application', 'run_id': 'run_forged'}
    with pytest.raises(AgenticError) as exc:
        layer.overseer.materials.record(forged, 'run:forged')
    assert exc.value.code == 'invalid_request'
    with layer._ctx.store.reader() as conn:
        assert conn.execute("SELECT count(*) FROM overseer_operations WHERE operation_id='run:forged'").fetchone()[0] == 0


def test_generic_extraction_run_cannot_claim_positive_coverage(layer, tmp_path):
    install_test_workflows(layer, tmp_path)
    actor = {'actor_id': uid(), 'actor_kind': 'agent'}
    raw_run = layer.workflows.start('overseer/extract', actor, operation_id='unadmitted')['run']
    coverage = {'schema_version': 1, 'run_id': raw_run['run_id'],
                'frontier': {'snapshot_id': uid(), 'captured_at': 'now', 'providers': []},
                'analyzed_sources': [proposal()['sources'][0]['ref']], 'pending_sources': [],
                'gaps': [], 'complete': True, 'next_cursor': None}
    with pytest.raises(AgenticError) as exc:
        layer.overseer.coverage.record(coverage, 'unadmitted-coverage')
    assert exc.value.code == 'owner_input_required'
    assert layer.overseer.coverage.get(source_refs=coverage['analyzed_sources'])['analyzed_sources'] == []


def test_invalid_preselected_application_does_not_start_runner(layer, tmp_path):
    install_test_workflows(layer, tmp_path)
    g = InvocationGateway('application', {'application_id': uid()})
    with pytest.raises(AgenticError) as exc:
        layer.overseer.start_run('application', {'reference': g.ref}, operation_id='invalid-app', gateway=g)
    assert exc.value.code == 'invalid_transition'
    with layer._ctx.store.reader() as conn:
        assert conn.execute('SELECT count(*) FROM runs').fetchone()[0] == 0


def test_unknown_provenance_is_retained_without_invented_independence(layer):
    p = proposal()
    ref = p['sources'][0]['ref']
    ref.update(origin_event_id=None, episode_id=None, actor_id=None, actor_kind=None, occurred_at=None)
    ref['provenance'].update(producer_id=None, overseer_origin=None)
    layer.overseer.proposals.record(p, 'unknown-source')
    d = revision(p)
    d['support_summary'] = {'proposal_count': 1, 'event_count': 0, 'episode_count': 0, 'event_refs': [], 'episode_refs': []}
    layer.overseer.deltas.create(d, 'uncertain-delta')
    assert layer.overseer.deltas.get(d['delta_id'])['revision']['support_summary']['event_count'] == 0
    assert layer.overseer.proposals.get(p['id'])['sources'][0]['ref']['origin_event_id'] is None


@pytest.mark.parametrize('control', ['pause', 'cancel'])
def test_selection_answer_cannot_start_work_in_inactive_run(layer, tmp_path, control):
    from .test_overseer_decisions import setup, binding, Gateway
    install_test_workflows(layer, tmp_path)
    _, d = setup(layer)
    review = binding(d)
    layer.overseer.inputs.bind(review, 'review-binding')
    owner = Gateway(review)
    app_id = layer.overseer.apply_owner_response(review['id'], owner.call(), operation_id='accept', gateway=owner)['application_id']
    gateway = InvocationGateway('application')
    run = layer.overseer.start_run('application', {'reference': gateway.ref}, operation_id='start', gateway=gateway)
    selection = binding(d, 'select', {'choose': 'select', 'later': 'later'})
    selection['run_id'] = run['run_id']
    selection['targets'] = {'delta_id': None, 'delta_revision': None, 'application_id': None, 'plan_revision': None}
    selection['choice_targets'] = {'choose': {'application_id': app_id}}
    layer.overseer.inputs.bind(selection, 'selection-binding')
    getattr(layer.workflows, control)(run['run_id'], gateway.value['executor'], 'control')
    answer = Gateway(selection, 'choose')
    with pytest.raises(AgenticError) as exc:
        layer.overseer.apply_owner_response(selection['id'], answer.call(), operation_id='select', gateway=answer)
    assert exc.value.code == 'invalid_transition'
    app = layer.overseer.applications.get(app_id)
    assert app['phase'] == 'queued' and app['run_id'] is None and app['revision'] == 1
    saved = layer.overseer.inputs.get(selection['id'], include_response=True)
    assert saved['response']['canonical'] == answer.answer
    assert saved['result']['status'] == 'invalid_transition'
    assert layer.workflows.get(run['run_id'])['run']['state'] == ('paused' if control == 'pause' else 'cancelled')


def test_completed_delta_keeps_meaning_and_later_change_uses_new_delta(layer, tmp_path):
    from .test_overseer_decisions import Gateway
    d, app_id, close = _satisfaction_fixture(layer, tmp_path)
    layer.overseer.inputs.bind(close, 'close-binding')
    answer = Gateway(close, 'done')
    layer.overseer.apply_owner_response(close['id'], answer.call(), operation_id='close', gateway=answer)
    for field, replacement in (
        ('summary', 'Different summary'),
        ('change_direction', 'Change the completed rule'),
        ('scope', {**d['scope'], 'description': 'Different scope'}),
        ('target_refs', [{'description': 'Different target'}]),
    ):
        changed = copy.deepcopy(d); changed['revision'] = 2; changed[field] = replacement
        with pytest.raises(AgenticError) as exc:
            layer.overseer.deltas.revise(changed, 1, 'repurpose-' + field)
        assert exc.value.code == 'invalid_transition' and 'create a new delta' in exc.value.message
    assert layer.overseer.deltas.get(d['delta_id'])['revision'] == d
    additional = proposal(); additional['id'] = uid()
    layer.overseer.proposals.record(additional, 'additional-evidence')
    evidence = copy.deepcopy(d); evidence['revision'] = 2; evidence['proposal_ids'].append(additional['id'])
    evidence['support_summary']['proposal_count'] = 2
    updated = layer.overseer.deltas.revise(evidence, 1, 'attach-evidence')
    assert updated['decision'] == 'accepted' and updated['accepted_revision'] == 1
    later = proposal(); later['id'] = uid(); later['summary'] = 'A later distinct research change'
    later['change_direction'] = 'Apply a later distinct research change'
    layer.overseer.proposals.record(later, 'later-proposal')
    new_delta = revision(later)
    new_delta['summary'] = later['summary']; new_delta['change_direction'] = later['change_direction']
    fresh = layer.overseer.deltas.create(new_delta, 'later-delta')
    assert fresh['id'] != d['delta_id'] and fresh['decision'] == 'undecided'
    assert layer.overseer.applications.get(app_id)['phase'] == 'complete'
