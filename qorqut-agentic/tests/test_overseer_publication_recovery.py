"""Publication guard tests use synthetic records in disposable state."""
import pytest

from qorqut_agentic import AgenticError
from .test_overseer_application import agreed_application


def test_incomplete_publication_blocks_every_catalog_tree_read(layer, tmp_path):
    app_id, _ = agreed_application(layer, tmp_path)
    calls = [lambda: layer.catalog.list(), lambda: layer.catalog.read_entry('overseer/apply'),
             lambda: layer.catalog.read_resource('overseer', 'apply.yaml'), lambda: layer.catalog.validate()]
    with layer._ctx.store.transaction() as conn:
        conn.execute('INSERT INTO overseer_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                     (app_id, 'synthetic-publication', 'chg_synthetic', 'b' * 40, None, None, None, None, None, '[]', 'publishing', '[]'))
    for call in calls:
        with pytest.raises(AgenticError) as error:
            call()
        assert error.value.code == 'publication_recovery_required'
    with pytest.raises(AgenticError) as error:
        layer.tools.invoke('example/missing', {}, 'synthetic-invocation', {'actor_id': 'test', 'actor_kind': 'agent'})
    assert error.value.code == 'publication_recovery_required'
    assert layer.recovery_required
    with layer._ctx.store.transaction() as conn:
        conn.execute("UPDATE overseer_publications SET stage='merged' WHERE operation_id='synthetic-publication'")
    for call in calls:
        call()
    assert not layer.recovery_required


def _ready_to_publish(layer, tmp_path, *, failing_check=False, pre_check=False):
    import sys
    from pathlib import Path
    from .conftest import git, sha
    from .test_overseer_contracts import plan
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'synthetic base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md',
                              'description': 'Update synthetic rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(),
                          'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        if pre_check:
            record['checks'].append({'id': 'pre', 'phase': 'pre_merge', 'kind': 'command', 'arguments': {},
                                     'argv': [sys.executable, '-c', 'raise SystemExit(0)']})
        if failing_check:
            command = "open('user-preferences/rule.md', 'w').write('check changed file')" if failing_check == 'writes' else 'raise SystemExit(3)'
            record['checks'] = [{'id': 'fails', 'phase': 'post_merge', 'kind': 'command', 'arguments': {},
                                 'argv': [sys.executable, '-c', command]}]
        return record
    app_id, delta = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(draft['execution']['worktree_path'])
    (root / 'user-preferences/rule.md').write_text('after\n'); git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'draft')
    candidate = apps.prepare_merge(app_id, expected_revision=draft['application']['revision'], operation_id='candidate')
    return apps, app_id, delta, candidate


def test_recover_after_git_moves_finalizes_once_and_blocks_catalog(layer, tmp_path):
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path)
    def crash(**_kwargs):
        raise RuntimeError('synthetic crash after git')
    layer._ctx.hooks['overseer_after_publication_git'] = crash
    rev = candidate['application']['revision']
    with pytest.raises(RuntimeError):
        apps.publish(app_id, expected_revision=rev, operation_id='publish')
    layer._ctx.hooks.clear()
    assert apps.recovery_status(app_id)['recovery_required']
    with pytest.raises(AgenticError) as error:
        layer.catalog.validate()
    assert error.value.code == 'publication_recovery_required'
    from qorqut_agentic import AgenticLayer
    draft = AgenticLayer.open(candidate['execution']['worktree_path'], candidate['execution']['state_path'])
    assert draft.catalog.validate()['valid']
    recovered = apps.recover(app_id, expected_revision=rev, operation_id='recover')
    assert recovered['application']['phase'] == 'merged'
    assert apps.publish(app_id, expected_revision=rev, operation_id='publish') == recovered
    assert apps.recover(app_id, expected_revision=rev, operation_id='recover') == recovered
    assert not apps.recovery_status(app_id)['recovery_required']
    assert len([c for c in layer.changes.list()['changes'] if c['kind'] == 'overseer_publication']) == 1
    assert layer.catalog.validate()['valid']


def test_failed_checks_need_owner_retry_and_preserve_merge(layer, tmp_path):
    from .test_overseer_decisions import binding, Gateway
    apps, app_id, delta, candidate = _ready_to_publish(layer, tmp_path, failing_check=True)
    published = apps.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='publish')
    failed = apps.record_checks(app_id, phase='post_merge', expected_revision=published['application']['revision'], operation_id='post')
    assert failed['check_run']['state'] == 'failed'
    assert failed['check_run']['receipts'][0]['exit_code'] == 3
    ref = {'provider': 'qorqut_agentic', 'kind': 'check_receipt', 'application_id': app_id,
           'check_run_id': failed['check_run']['id'], 'check_id': 'fails'}
    saved_receipt = apps.get_check_receipt(ref)
    assert saved_receipt['record']['run_state'] == 'failed'
    assert failed['application']['phase'] == 'merged'
    assert failed['application']['refresh'] == {'required': True, 'state': 'unknown', 'detail': 'Native consumers have not been verified against this merged revision.'}
    from qorqut_agentic import AgenticLayer
    assert AgenticLayer.open(layer.root, layer.state).overseer.applications.get(app_id)['refresh'] == failed['application']['refresh']
    assert failed['application']['merge'] == published['application']['merge']
    with pytest.raises(AgenticError) as error:
        apps.record_checks(app_id, phase='post_merge', expected_revision=failed['application']['revision'], operation_id='no-owner')
    assert error.value.code == 'owner_input_required'
    b = binding(delta, 'failure', {'retry': 'retry_checks'})
    b['request_key'] = failed['required_input']['request_key']
    b['failure_evidence'] = failed['required_input']['failure_evidence']
    b['targets'].update(application_id=app_id, application_revision=failed['application']['revision'], plan_revision=1)
    layer.overseer.inputs.bind(b, 'failure-binding')
    attached = apps.attach_input(app_id, b['id'], expected_revision=failed['application']['revision'], operation_id='attach')
    assert attached['application']['revision'] == failed['application']['revision']
    g = Gateway(b, 'retry')
    layer.overseer.apply_owner_response(b['id'], g.call(), operation_id='retry-answer', gateway=g)
    answer_revision = apps.get(app_id)['revision']
    retry = apps.record_checks(app_id, phase='post_merge', expected_revision=answer_revision, operation_id='retry')
    assert retry['check_run']['id'] != failed['check_run']['id']
    assert apps.get_check_receipt(ref) == saved_receipt
    assert retry['application']['phase'] == 'merged'
    assert retry['application']['merge'] == published['application']['merge']


def test_recovery_retains_later_commit_and_unrelated_dirty_work(layer, tmp_path):
    from .conftest import git
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path)
    def crash(**_kwargs):
        raise RuntimeError('crash after git')
    layer._ctx.hooks['overseer_after_publication_git'] = crash
    revision = candidate['application']['revision']
    with pytest.raises(RuntimeError):
        apps.publish(app_id, expected_revision=revision, operation_id='publish')
    layer._ctx.hooks.clear()
    (layer.root / 'newer.md').write_text('newer commit\n')
    git(layer, 'add', 'newer.md'); git(layer, 'commit', '-m', 'newer work')
    newer = git(layer, 'rev-parse', 'HEAD').stdout.strip()
    (layer.root / 'pending.md').write_text('uncommitted external edit\n')
    recovered = apps.recover(app_id, expected_revision=revision, operation_id='recover')
    assert git(layer, 'rev-parse', 'HEAD').stdout.strip() == newer
    assert (layer.root / 'pending.md').read_text() == 'uncommitted external edit\n'
    assert recovered['application']['merge']['candidate'] == candidate['execution']['candidate_commit']
    events = layer.changes.list()['changes']
    assert len([e for e in events if e['kind'] == 'overseer_publication']) == 1
    assert any(e['kind'] == 'external_scan' and 'pending.md' in e['paths'] for e in events)


def test_recovery_refuses_to_overwrite_affected_third_party_edit(layer, tmp_path):
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path)
    def crash(**_kwargs):
        raise RuntimeError('crash after git')
    layer._ctx.hooks['overseer_after_publication_git'] = crash
    revision = candidate['application']['revision']
    with pytest.raises(RuntimeError):
        apps.publish(app_id, expected_revision=revision, operation_id='publish')
    layer._ctx.hooks.clear()
    (layer.root / 'user-preferences/rule.md').write_text('third-party edit\n')
    with pytest.raises(AgenticError) as error:
        apps.recover(app_id, expected_revision=revision, operation_id='recover')
    assert error.value.code == 'publication_recovery_required'
    assert (layer.root / 'user-preferences/rule.md').read_text() == 'third-party edit\n'
    assert apps.recovery_status(app_id)['recovery_required']


@pytest.mark.parametrize('hook', ['overseer_after_publication_intent', 'overseer_after_publication_finalize'])
def test_publication_crash_boundaries_replay_once(layer, tmp_path, hook):
    from .conftest import git
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path)
    base = git(layer, 'rev-parse', 'HEAD').stdout.strip()
    def crash(**_kwargs):
        raise RuntimeError('synthetic crash')
    layer._ctx.hooks[hook] = crash
    revision = candidate['application']['revision']
    with pytest.raises(RuntimeError):
        apps.publish(app_id, expected_revision=revision, operation_id='publish')
    layer._ctx.hooks.clear()
    if hook == 'overseer_after_publication_intent':
        assert git(layer, 'rev-parse', 'HEAD').stdout.strip() == base
        completed = apps.recover(app_id, expected_revision=revision, operation_id='recover')
    else:
        assert not apps.recovery_status(app_id)['recovery_required']
        completed = apps.publish(app_id, expected_revision=revision, operation_id='publish')
    assert completed['application']['phase'] == 'merged'
    assert len([e for e in layer.changes.list()['changes'] if e['kind'] == 'overseer_publication']) == 1


def test_checks_that_change_the_authored_tree_never_pass(layer, tmp_path):
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path, failing_check='writes')
    published = apps.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='publish')
    check = apps.record_checks(app_id, phase='post_merge', expected_revision=published['application']['revision'], operation_id='post')
    assert check['check_run']['state'] == 'failed'
    assert check['application']['phase'] == 'merged'
    assert check['application']['checks']['state'] == 'failed'
    assert check['required_input']['purpose'] == 'failure'
    assert (layer.root / 'user-preferences/rule.md').read_text() == 'check changed file'


def test_dirty_active_index_blocks_publication_without_touching_it(layer, tmp_path):
    from .conftest import git
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path)
    (layer.root / 'pending.md').write_text('staged\n')
    git(layer, 'add', 'pending.md')
    before_head = git(layer, 'rev-parse', 'HEAD').stdout.strip()
    before_status = git(layer, 'status', '--porcelain').stdout
    with pytest.raises(AgenticError) as error:
        apps.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='publish')
    assert error.value.code == 'active_tree_changed'
    assert git(layer, 'rev-parse', 'HEAD').stdout.strip() == before_head
    assert git(layer, 'status', '--porcelain').stdout == before_status
    assert not apps.recovery_status(app_id)['recovery_required']


def test_interrupted_check_is_uncertain_and_never_reexecuted(layer, tmp_path, monkeypatch):
    from .conftest import SimulatedCrash
    import qorqut_agentic.overseer.checks as checks_module
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path, failing_check='writes')
    published = apps.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='publish')
    def crash(**_kwargs):
        raise SimulatedCrash('check started')
    layer._ctx.hooks['overseer_before_check'] = crash
    revision = published['application']['revision']
    with pytest.raises(SimulatedCrash):
        apps.record_checks(app_id, phase='post_merge', expected_revision=revision, operation_id='post')
    layer._ctx.hooks.clear()
    import json
    with layer._ctx.store.reader() as conn:
        intent = json.loads(conn.execute('SELECT result FROM overseer_operations WHERE operation_id=?', ('post',)).fetchone()['result'])
    reference = {'provider': 'qorqut_agentic', 'kind': 'check_receipt', 'application_id': app_id,
                 'check_run_id': intent['check_run']['id'], 'check_id': 'fails'}
    with pytest.raises(AgenticError):
        apps.get_check_receipt(reference)
    monkeypatch.setattr(checks_module, '_process_token', lambda: 'synthetic:new:runner')
    result = apps.record_checks(app_id, phase='post_merge', expected_revision=revision, operation_id='post')
    assert result['check_run']['state'] == 'uncertain'
    assert result['check_run']['receipts'][0]['check_id'] == 'fails'
    assert result['check_run']['receipts'][0]['outcome'] == 'uncertain'
    assert result['required_input']['purpose'] == 'failure'
    assert (layer.root / 'user-preferences/rule.md').read_text() == 'after\n'


def test_failure_clarification_replacement_resolves_original_hold(layer, tmp_path):
    from .test_overseer_decisions import binding, Gateway
    apps, app_id, delta, candidate = _ready_to_publish(layer, tmp_path, failing_check=True)
    merged = apps.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='publish')
    failed = apps.record_checks(app_id, phase='post_merge', expected_revision=merged['application']['revision'], operation_id='post')
    original = binding(delta, 'failure', {'retry': 'retry_checks'})
    original['request_key'] = failed['required_input']['request_key']
    original['failure_evidence'] = failed['required_input']['failure_evidence']
    original['targets'].update(application_id=app_id, application_revision=failed['application']['revision'], plan_revision=1)
    layer.overseer.inputs.bind(original, 'failure-binding')
    apps.attach_input(app_id, original['id'], expected_revision=failed['application']['revision'], operation_id='attach')
    ambiguous = Gateway(original, 'retry', notes='Maybe retry later')
    interpretation = {'clarity': 'ambiguous', 'action': 'retry_checks', 'rationale': 'Uncertain timing.'}
    assert layer.overseer.apply_owner_response(original['id'], ambiguous.call(interpretation), operation_id='ambiguous', gateway=ambiguous)['status'] == 'needs_clarification'
    replacement = binding(delta, 'failure', {'retry': 'retry_checks'})
    replacement['resolves_input_ids'] = [original['id']]
    replacement['failure_evidence'] = failed['required_input']['failure_evidence']
    replacement['targets'].update(application_id=app_id, application_revision=failed['application']['revision'], plan_revision=1)
    layer.overseer.inputs.bind(replacement, 'replacement-binding')
    answer = Gateway(replacement, 'retry')
    assert layer.overseer.apply_owner_response(replacement['id'], answer.call(), operation_id='answer', gateway=answer)['action'] == 'retry_checks'
    assert apps.get(app_id)['waiting_input_ref'] is None
    retried = apps.record_checks(app_id, phase='post_merge', expected_revision=apps.get(app_id)['revision'], operation_id='retry')
    assert retried['check_run']['state'] == 'failed'


def test_uncertain_precheck_owner_repair_allows_only_replanning(layer, tmp_path, monkeypatch):
    from .conftest import SimulatedCrash
    from .test_overseer_decisions import binding, Gateway
    import qorqut_agentic.overseer.checks as checks_module
    apps, app_id, delta, candidate = _ready_to_publish(layer, tmp_path, pre_check=True)
    def crash(**_kwargs): raise SimulatedCrash('check started')
    layer._ctx.hooks['overseer_before_check'] = crash
    revision = candidate['application']['revision']
    with pytest.raises(SimulatedCrash):
        apps.record_checks(app_id, phase='pre_merge', expected_revision=revision, operation_id='pre')
    layer._ctx.hooks.clear()
    with monkeypatch.context() as patch:
        patch.setattr(checks_module, '_process_token', lambda: 'synthetic:new:runner')
        uncertain = apps.record_checks(app_id, phase='pre_merge', expected_revision=revision, operation_id='pre')
    need = uncertain['required_input']
    question = binding(delta, 'failure', {'repair': 'repair'})
    question['request_key'] = need['request_key']; question['failure_evidence'] = need['failure_evidence']
    question['targets'].update(application_id=app_id, application_revision=uncertain['application']['revision'], plan_revision=1)
    layer.overseer.inputs.bind(question, 'failure-binding')
    apps.attach_input(app_id, question['id'], expected_revision=uncertain['application']['revision'], operation_id='attach')
    answer = Gateway(question, 'repair')
    layer.overseer.apply_owner_response(question['id'], answer.call(), operation_id='answer', gateway=answer)
    with pytest.raises(AgenticError):
        apps.record_checks(app_id, phase='pre_merge', expected_revision=apps.get(app_id)['revision'], operation_id='unauthorized-retry')
    replanned = apps.replan(app_id, reason='Owner chose repair', expected_revision=apps.get(app_id)['revision'], operation_id='replan')
    assert replanned['application']['phase'] == 'planning' and replanned['application']['agreement_ref'] is None
    assert uncertain['check_run']['state'] == 'uncertain'
    assert not (layer.root / 'user-preferences/rule.md').read_text() == 'after\n'


def test_new_active_commit_requires_new_candidate_and_new_checks(layer, tmp_path):
    from .conftest import git
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path, pre_check=True)
    checked = apps.record_checks(app_id, phase='pre_merge', expected_revision=candidate['application']['revision'], operation_id='pre')
    assert checked['check_run']['state'] == 'passed'
    (layer.root / 'unrelated.md').write_text('newer\n'); git(layer, 'add', 'unrelated.md'); git(layer, 'commit', '-m', 'newer active')
    with pytest.raises(AgenticError) as error:
        apps.publish(app_id, expected_revision=checked['application']['revision'], operation_id='stale-publish')
    assert error.value.code == 'active_tree_changed'
    updated = apps.prepare_merge(app_id, expected_revision=checked['application']['revision'], operation_id='new-candidate')
    assert updated['execution']['candidate_commit'] != candidate['execution']['candidate_commit']
    retained = git(layer, 'for-each-ref', '--format=%(objectname)', f'refs/overseer/candidates/{app_id}').stdout.splitlines()
    assert candidate['execution']['candidate_commit'] in retained and updated['execution']['candidate_commit'] in retained
    with pytest.raises(AgenticError):
        apps.publish(app_id, expected_revision=updated['application']['revision'], operation_id='without-new-check')
    new_check = apps.record_checks(app_id, phase='pre_merge', expected_revision=updated['application']['revision'], operation_id='new-check')
    assert new_check['check_run']['state'] == 'passed'
    published = apps.publish(app_id, expected_revision=new_check['application']['revision'], operation_id='publish')
    assert published['application']['phase'] == 'merged'


def test_failure_later_does_not_authorize_retry(layer, tmp_path):
    from .test_overseer_decisions import binding, Gateway
    apps, app_id, delta, candidate = _ready_to_publish(layer, tmp_path, failing_check=True)
    merged = apps.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='publish')
    failed = apps.record_checks(app_id, phase='post_merge', expected_revision=merged['application']['revision'], operation_id='post')
    need = failed['required_input']
    question = binding(delta, 'failure', {'later': 'later'})
    question['request_key'] = need['request_key']; question['failure_evidence'] = need['failure_evidence']
    question['targets'].update(application_id=app_id, application_revision=failed['application']['revision'], plan_revision=1)
    layer.overseer.inputs.bind(question, 'failure-binding')
    apps.attach_input(app_id, question['id'], expected_revision=failed['application']['revision'], operation_id='attach')
    answer = Gateway(question, 'later')
    layer.overseer.apply_owner_response(question['id'], answer.call(), operation_id='answer', gateway=answer)
    with pytest.raises(AgenticError) as error:
        apps.record_checks(app_id, phase='post_merge', expected_revision=apps.get(app_id)['revision'], operation_id='unapproved-retry')
    assert error.value.code == 'owner_input_required'
    assert apps.get(app_id)['checks']['state'] == 'failed'


def test_check_receipt_lookup_refuses_duplicate_saved_ids(layer, tmp_path):
    import json
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path, pre_check=True)
    checked = apps.record_checks(app_id, phase='pre_merge', expected_revision=candidate['application']['revision'], operation_id='pre')
    run_id = checked['check_run']['id']
    reference = {'provider': 'qorqut_agentic', 'kind': 'check_receipt', 'application_id': app_id,
                 'check_run_id': run_id, 'check_id': 'pre'}
    with layer._ctx.store.transaction() as conn:
        row = conn.execute('SELECT record FROM overseer_application_events WHERE id=?', (run_id,)).fetchone()
        event = json.loads(row['record']); event['check_run']['receipts'].append(event['check_run']['receipts'][0])
        conn.execute('UPDATE overseer_application_events SET record=? WHERE id=?', (json.dumps(event), run_id))
    with pytest.raises(AgenticError) as error:
        apps.get_check_receipt(reference)
    assert error.value.code == 'evidence_unavailable'


def test_protected_interrupted_publication_refuses_without_rewriting_receipt(layer, tmp_path):
    import json
    from .conftest import git
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path)
    base = git(layer, 'rev-parse', 'HEAD').stdout.strip()
    def crash(**_kwargs):
        raise RuntimeError('synthetic crash')
    layer._ctx.hooks['overseer_after_publication_intent'] = crash
    revision = candidate['application']['revision']
    with pytest.raises(RuntimeError):
        apps.publish(app_id, expected_revision=revision, operation_id='publish')
    layer._ctx.hooks.clear()
    with layer._ctx.store.transaction() as conn:
        row = conn.execute("SELECT file_hashes FROM overseer_publications WHERE operation_id='publish'").fetchone()
        files = json.loads(row['file_hashes'])
        files[0]['path'] = 'roles/locked/rule.md'
        receipt = json.dumps(files)
        conn.execute("UPDATE overseer_publications SET file_hashes=? WHERE operation_id='publish'", (receipt,))
    with pytest.raises(AgenticError) as error:
        apps.recover(app_id, expected_revision=revision, operation_id='recover')
    assert error.value.code == 'protected_agentic_area'
    assert git(layer, 'rev-parse', 'HEAD').stdout.strip() == base
    with layer._ctx.store.reader() as conn:
        assert conn.execute("SELECT file_hashes FROM overseer_publications WHERE operation_id='publish'").fetchone()['file_hashes'] == receipt
    assert apps.recovery_status(app_id)['recovery_required']
