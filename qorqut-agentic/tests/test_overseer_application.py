"""Focused executor tests with synthetic owner and task gateways."""
import copy

import pytest

from qorqut_agentic import AgenticError
from .test_overseer_contracts import InvocationGateway, install_test_workflows, plan
from .test_overseer_decisions import Gateway, binding, setup, uid


def agreed_application(layer, tmp_path, *, external=False, local_plan=None):
    install_test_workflows(layer, tmp_path)
    _, delta = setup(layer)
    review = binding(delta)
    layer.overseer.inputs.bind(review, 'review-binding')
    answer = Gateway(review)
    app_id = layer.overseer.apply_owner_response(review['id'], answer.call(), operation_id='accept', gateway=answer)['application_id']
    invocation = InvocationGateway('application', {'application_id': app_id})
    layer.overseer.start_run('application', {'reference': invocation.ref}, operation_id='start', gateway=invocation)
    if local_plan:
        import json
        prefs = layer.root / "user-preferences"
        (prefs / "agentic.yaml").write_text(json.dumps({"schema_version": 1, "id": "user-preferences", "name": "Preferences", "description": "Preferences", "entries": [{"id": "preferences", "kind": "skill", "path": "SKILL.md", "description": "Preferences", "when_to_use": "Always"}]}))
        (prefs / "SKILL.md").write_text("Preferences\n")
    saved_plan = local_plan(delta) if local_plan else plan(delta)
    if external:
        saved_plan['changes'] = []
        saved_plan['checks'] = []
        saved_plan['external_requirements'] = [{'id': 'dev', 'summary': 'Synthetic change', 'required_for_completion': True}]
    saved = layer.overseer.applications.save_plan(app_id, saved_plan, expected_revision=2, operation_id='save')
    approval = binding(delta, 'plan', {'go': 'proceed'})
    approval['targets'].update(application_id=app_id, application_revision=saved['application']['revision'], plan_revision=1)
    layer.overseer.inputs.bind(approval, 'plan-binding')
    answer = Gateway(approval, 'go')
    layer.overseer.apply_owner_response(approval['id'], answer.call(), operation_id='approve', gateway=answer)
    return app_id, delta


def test_external_task_observations_are_canonical_and_replay_safe(layer, tmp_path):
    app_id, _ = agreed_application(layer, tmp_path, external=True)
    class TaskGateway:
        def __init__(self): self.value = {'reference': {'id': 'task'}, 'state': 'done', 'result_refs': [], 'completion_summary': None, 'observed_at': 'now'}
        def resolve_task(self, ref): return copy.deepcopy(self.value)
    gateway = TaskGateway()
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=gateway)
    initial = apps.get(app_id)
    prepared = apps.prepare(app_id, expected_revision=initial['revision'], operation_id='prepare-external')
    assert prepared['application']['phase'] == 'working' and prepared['execution'] is None
    assert apps.prepare(app_id, expected_revision=initial['revision'], operation_id='prepare-external') == prepared
    linked = apps.link_external_task(app_id, 'dev', {'id': 'task'}, expected_revision=prepared['application']['revision'], operation_id='link')
    with pytest.raises(AgenticError):
        apps.link_external_task(app_id, 'dev', {'id': 'other'}, expected_revision=linked['application']['revision'], operation_id='different')
    observed = apps.reconcile_external_tasks(app_id, expected_revision=linked['application']['revision'], operation_id='observe')
    assert observed['application']['phase'] != 'complete'
    assert observed['application']['external_tasks'][0]['observation']['state'] == 'done'
    gateway.value['result_refs'] = [{'id': 'outcome'}]
    gateway.value['completion_summary'] = 'Synthetic evidence'
    assert apps.reconcile_external_tasks(app_id, expected_revision=linked['application']['revision'], operation_id='observe') == observed
    done = apps.reconcile_external_tasks(app_id, expected_revision=observed['application']['revision'], operation_id='observe-result')
    assert done['application']['phase'] == 'complete'
    assert done['application']['outcome'] == 'external_completed' and done['application']['merge'] is None
    assert done['application']['checks']['state'] == 'not_run'


def test_prepare_isolated_draft_and_exact_replay(layer, tmp_path):
    from .conftest import git, sha
    from qorqut_agentic import AgenticLayer
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A')
        git(layer, 'commit', '-m', 'synthetic base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md',
                              'description': 'Update synthetic rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(),
                          'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        return record
    app_id, _ = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    app = apps.get(app_id)
    result = apps.prepare(app_id, expected_revision=app['revision'], operation_id='prepare')
    assert result['application']['phase'] == 'working'
    assert apps.prepare(app_id, expected_revision=app['revision'], operation_id='prepare') == result
    execution = result['execution']
    draft = AgenticLayer.open(execution['worktree_path'], execution['state_path'])
    assert draft.instance_id != layer.instance_id
    assert draft.root != layer.root
    from .conftest import ATTR
    before_feed = layer.changes.list()['changes']
    draft.authoring.apply(ATTR, [{'op': 'edit', 'path': 'user-preferences/rule.md', 'expected_sha256': sha('before\n'), 'content': 'draft only\n'}])
    assert draft.changes.list()['changes']
    assert (layer.root / 'user-preferences/rule.md').read_text() == 'before\n'
    assert layer.changes.list()['changes'] == before_feed


def test_prepare_crash_after_worktree_creation_recovers_same_draft(layer, tmp_path):
    from .conftest import git, sha
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A')
        git(layer, 'commit', '-m', 'synthetic base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md',
                              'description': 'Update synthetic rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(),
                          'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        return record
    app_id, _ = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    revision = apps.get(app_id)['revision']
    def crash(**_kwargs):
        raise RuntimeError('synthetic crash')
    layer._ctx.hooks['overseer_after_worktree_add'] = crash
    with pytest.raises(RuntimeError):
        apps.prepare(app_id, expected_revision=revision, operation_id='prepare-crash')
    layer._ctx.hooks.clear()
    prepared = apps.prepare(app_id, expected_revision=revision, operation_id='prepare-crash')
    assert prepared['application']['phase'] == 'working'
    assert prepared['execution']['worktree_path'].endswith('/plan-1/worktree')
    assert apps.prepare(app_id, expected_revision=revision, operation_id='prepare-crash') == prepared


def test_prepare_refuses_descriptive_approved_plan(layer, tmp_path):
    def descriptive(delta):
        result = plan(delta)
        result['changes'][0].pop('path')
        return result
    app_id, _ = agreed_application(layer, tmp_path, local_plan=descriptive)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    with pytest.raises(AgenticError) as error:
        apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    assert error.value.code == 'owner_input_required'
    assert error.value.details['reason'] == 'plan_not_executable'
    assert not (tmp_path / 'worktrees').exists()


def test_replan_keeps_draft_and_requires_new_plan_agreement(layer, tmp_path):
    from .conftest import git, sha
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A')
        git(layer, 'commit', '-m', 'synthetic base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md',
                              'description': 'Update synthetic rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(),
                          'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        return record
    app_id, delta = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    prepared = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    revision = prepared['application']['revision']
    replanned = apps.replan(app_id, reason='Need a revised plan', expected_revision=revision, operation_id='replan')
    assert replanned['application']['phase'] == 'planning' and replanned['application']['agreement_ref'] is None
    assert apps.replan(app_id, reason='Need a revised plan', expected_revision=revision, operation_id='replan') == replanned
    assert (tmp_path / 'worktrees' / app_id / 'plan-1' / 'worktree' / 'user-preferences/rule.md').read_text() == 'before\n'
    with pytest.raises(AgenticError):
        apps.prepare(app_id, expected_revision=replanned['application']['revision'], operation_id='prepare-old')
    old_approval = binding(delta, 'plan', {'go': 'proceed'})
    old_approval['targets'].update(application_id=app_id, application_revision=replanned['application']['revision'], plan_revision=1)
    layer.overseer.inputs.bind(old_approval, 'old-plan-binding')
    old_answer = Gateway(old_approval, 'go')
    with pytest.raises(AgenticError) as error:
        layer.overseer.apply_owner_response(old_approval['id'], old_answer.call(), operation_id='old-plan-answer', gateway=old_answer)
    assert error.value.code == 'invalid_transition'
    previous = apps.get(app_id)['plan']
    new_plan = {key: value for key, value in previous.items() if key not in ('application_id', 'revision', 'created_at')}
    saved = apps.save_plan(app_id, new_plan, expected_revision=replanned['application']['revision'], operation_id='save-new')
    approval = binding(delta, 'plan', {'go': 'proceed'})
    approval['targets'].update(application_id=app_id, application_revision=saved['application']['revision'], plan_revision=2)
    layer.overseer.inputs.bind(approval, 'new-plan-binding')
    answer = Gateway(approval, 'go')
    layer.overseer.apply_owner_response(approval['id'], answer.call(), operation_id='approve-new', gateway=answer)
    next_draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare-new')
    assert next_draft['execution']['plan_revision'] == 2
    assert next_draft['execution']['worktree_path'] != prepared['execution']['worktree_path']


def test_prepare_merge_squashes_draft_on_current_active_head(layer, tmp_path):
    from .conftest import git, sha
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'synthetic base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md',
                              'description': 'Update synthetic rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(),
                          'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        return record
    app_id, delta = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = draft['execution']['worktree_path']
    from pathlib import Path
    (Path(root) / 'user-preferences/rule.md').write_text('intermediate\n')
    git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'intermediate')
    (Path(root) / 'user-preferences/rule.md').write_text('final\n')
    git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'final')
    (layer.root / 'unrelated.md').write_text('concurrent\n')
    git(layer, 'add', 'unrelated.md'); git(layer, 'commit', '-m', 'concurrent')
    active = git(layer, 'rev-parse', 'HEAD').stdout.strip()
    rev = draft['application']['revision']
    result = apps.prepare_merge(app_id, expected_revision=rev, operation_id='candidate')
    assert apps.prepare_merge(app_id, expected_revision=rev, operation_id='candidate') == result
    candidate = result['execution']['candidate_commit']
    assert git(layer, 'rev-list', '--parents', '-1', candidate).stdout.strip().split() == [candidate, active]
    assert f'Delta-ID: {delta["delta_id"]}' in git(layer, 'show', '-s', '--format=%B', candidate).stdout
    assert git(layer, 'show', f'{candidate}:user-preferences/rule.md').stdout == 'final\n'
    assert git(layer, 'show', f'{candidate}:unrelated.md').stdout == 'concurrent\n'
    assert git(layer, 'rev-parse', 'HEAD').stdout.strip() == active


def test_publish_and_actual_check_receipts(layer, tmp_path):
    from pathlib import Path
    from .conftest import git, sha
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'synthetic base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md',
                              'description': 'Update synthetic rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(),
                          'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        record['checks'].append({'id': 'before', 'phase': 'pre_merge', 'kind': 'catalog', 'arguments': {}})
        return record
    app_id, _ = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(draft['execution']['worktree_path'])
    (root / 'user-preferences/rule.md').write_text('final\n'); git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'draft')
    candidate = apps.prepare_merge(app_id, expected_revision=draft['application']['revision'], operation_id='candidate')
    with pytest.raises(AgenticError):
        apps.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='early')
    checked = apps.record_checks(app_id, phase='pre_merge', expected_revision=candidate['application']['revision'], operation_id='pre')
    assert checked['check_run']['state'] == 'passed'
    import hashlib, json
    ref = {'provider': 'qorqut_agentic', 'kind': 'check_receipt', 'application_id': app_id,
           'check_run_id': checked['check_run']['id'], 'check_id': 'before'}
    receipt = apps.get_check_receipt(ref)
    expected_hash = hashlib.sha256(json.dumps(receipt['record'], sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    assert receipt['content_hash'] == expected_hash and receipt['record']['receipt']['outcome'] == 'passed'
    assert apps.get_check_receipt(ref) == receipt
    with pytest.raises(AgenticError): apps.get_check_receipt({**ref, 'outcome': 'passed'})
    with pytest.raises(AgenticError): apps.get_check_receipt({**ref, 'provider': 'forged'})
    with pytest.raises(AgenticError): apps.get_check_receipt({**ref, 'application_id': uid()})
    with pytest.raises(AgenticError): apps.get_check_receipt({**ref, 'check_run_id': uid()})
    with pytest.raises(AgenticError): apps.get_check_receipt({**ref, 'check_id': 'missing'})
    published = apps.publish(app_id, expected_revision=checked['application']['revision'], operation_id='publish')
    assert published['application']['phase'] == 'merged'
    assert (layer.root / 'user-preferences/rule.md').read_text() == 'final\n'
    checked_after = apps.record_checks(app_id, phase='post_merge', expected_revision=published['application']['revision'], operation_id='post')
    assert checked_after['check_run']['state'] == 'passed'
    assert checked_after['application']['phase'] == 'complete'
    assert len([e for e in layer.changes.list()['changes'] if e['kind'] == 'overseer_publication']) == 1
    from .test_overseer_state import proposal
    from .test_overseer_decisions import revision
    other = proposal(); other['id'] = uid()
    layer.overseer.proposals.record(other, 'other-proposal')
    other_delta = revision(other); layer.overseer.deltas.create(other_delta, 'other-delta')
    review = binding(other_delta); layer.overseer.inputs.bind(review, 'other-review')
    owner = Gateway(review)
    other_app = layer.overseer.apply_owner_response(review['id'], owner.call(), operation_id='other-accept', gateway=owner)['application_id']
    invocation = InvocationGateway('application', {'application_id': other_app})
    layer.overseer.start_run('application', {'reference': invocation.ref}, operation_id='other-start', gateway=invocation)
    class ReceiptGateway:
        def resolve_satisfaction_reference(self, reference, *, kind):
            assert kind == 'check_receipt'
            saved = apps.get_check_receipt(reference)
            assert saved['record']['run_state'] == 'passed' and saved['record']['receipt']['outcome'] == 'passed'
            return {'reference': saved['reference'], 'content_hash': saved['content_hash'], 'outcome': 'passed',
                    'checked_commit': saved['record']['checked_commit']}
    apps.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None, satisfaction_gateway=ReceiptGateway())
    inspected = apps.inspect_satisfaction(other_app, check_receipt_refs=[ref], explanation='Synthetic passed receipt',
                                         expected_revision=apps.get(other_app)['revision'], operation_id='receipt-inspection')
    assert inspected['inspection']['evidence']['check_receipt_refs'] == [ref]


def test_unplanned_draft_files_are_refused(layer, tmp_path):
    from pathlib import Path
    from .conftest import git, sha
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'synthetic base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md',
                              'description': 'Update synthetic rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(),
                          'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        return record
    app_id, _ = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(draft['execution']['worktree_path'])
    (root / 'user-preferences/rule.md').write_text('after\n'); (root / 'unplanned.md').write_text('extra\n')
    git(root, 'add', '-A'); git(root, 'commit', '-m', 'contains unexpected change')
    with pytest.raises(AgenticError) as error:
        apps.prepare_merge(app_id, expected_revision=draft['application']['revision'], operation_id='candidate')
    assert error.value.code == 'protected_agentic_area'
    assert error.value.details['paths'] == ['unplanned.md']
    assert not (layer.root / 'unplanned.md').exists()


def test_agreed_tool_check_records_real_isolated_receipt(layer, tmp_path):
    import sys
    from pathlib import Path
    from .conftest import git, sha, import_example
    from .test_tools import CountingRuntime
    runtime = CountingRuntime({'python': sys.executable})
    layer._ctx.runtimes['python'] = runtime
    def local_plan(delta):
        import_example(layer, 'example-project-workflow')
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'synthetic base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md',
                              'description': 'Update synthetic rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(),
                          'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        record['checks'].append({'id': 'tool', 'phase': 'pre_merge', 'kind': 'tool',
                                 'tool_ref': 'example-project-workflow/normalize-results', 'arguments': {'rows': []}})
        return record
    app_id, _ = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    prepared = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(prepared['execution']['worktree_path'])
    (root / 'user-preferences/rule.md').write_text('after\n'); git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'draft')
    candidate = apps.prepare_merge(app_id, expected_revision=prepared['application']['revision'], operation_id='candidate')
    revision = candidate['application']['revision']
    checked = apps.record_checks(app_id, phase='pre_merge', expected_revision=revision, operation_id='tool-check')
    receipt = checked['check_run']['receipts'][0]['tool_receipt_ref']['receipt']
    assert receipt['status'] == 'succeeded' and receipt['definition_revision'] and receipt['code_revision']
    assert checked['check_run']['state'] == 'passed' and runtime.calls == 1
    assert apps.record_checks(app_id, phase='pre_merge', expected_revision=revision, operation_id='tool-check') == checked
    assert runtime.calls == 1


def test_replan_refuses_paused_or_cancelled_admitted_run(layer, tmp_path):
    import json
    app_id, _ = agreed_application(layer, tmp_path, external=True)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    prepared = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    with layer._ctx.store.reader() as conn:
        actor = json.loads(conn.execute('SELECT actor FROM runs WHERE run_id=?', (prepared['application']['run_id'],)).fetchone()['actor'])
    run_id = prepared['application']['run_id']
    layer.workflows.pause(run_id, actor, 'pause')
    with pytest.raises(AgenticError) as error:
        apps.replan(app_id, reason='Need revision', expected_revision=prepared['application']['revision'], operation_id='paused-replan')
    assert error.value.code == 'invalid_transition'
    layer.workflows.cancel(run_id, actor, 'cancel')
    with pytest.raises(AgenticError) as error:
        apps.replan(app_id, reason='Need revision', expected_revision=prepared['application']['revision'], operation_id='cancelled-replan')
    assert error.value.code == 'invalid_transition'


def test_passed_merge_waits_for_real_external_outcome(layer, tmp_path):
    from pathlib import Path
    from .conftest import git, sha
    class TaskGateway:
        def resolve_task(self, ref):
            return {'reference': ref, 'state': 'done', 'result_refs': [{'id': 'delivered'}],
                    'completion_summary': 'Synthetic development result delivered', 'observed_at': 'now'}
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'synthetic base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md',
                              'description': 'Update synthetic rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(),
                          'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        record['external_requirements'] = [{'id': 'dev', 'summary': 'Synthetic development', 'required_for_completion': True}]
        return record
    app_id, _ = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=TaskGateway())
    draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(draft['execution']['worktree_path'])
    (root / 'user-preferences/rule.md').write_text('after\n'); git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'draft')
    candidate = apps.prepare_merge(app_id, expected_revision=draft['application']['revision'], operation_id='candidate')
    published = apps.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='publish')
    checked = apps.record_checks(app_id, phase='post_merge', expected_revision=published['application']['revision'], operation_id='post')
    assert checked['application']['checks']['state'] == 'passed' and checked['application']['phase'] == 'merged'
    linked = apps.link_external_task(app_id, 'dev', {'id': 'task'}, expected_revision=checked['application']['revision'], operation_id='link')
    completed = apps.reconcile_external_tasks(app_id, expected_revision=linked['application']['revision'], operation_id='observe')
    assert completed['application']['phase'] == 'complete'
    assert completed['application']['merge'] == published['application']['merge']


def test_conflicting_integration_requests_owner_input_without_touching_active(layer, tmp_path):
    from pathlib import Path
    from .conftest import git, sha
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'synthetic base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md',
                              'description': 'Update synthetic rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(),
                          'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        return record
    app_id, delta = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(draft['execution']['worktree_path'])
    (root / 'user-preferences/rule.md').write_text('draft\n'); git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'draft')
    (layer.root / 'user-preferences/rule.md').write_text('other change\n'); git(layer, 'add', 'user-preferences/rule.md'); git(layer, 'commit', '-m', 'other change')
    head = git(layer, 'rev-parse', 'HEAD').stdout.strip()
    result = apps.prepare_merge(app_id, expected_revision=draft['application']['revision'], operation_id='conflict')
    assert result['required_input']['purpose'] == 'clarify'
    assert result['required_input']['reason'] == 'integration_conflict'
    assert git(layer, 'rev-parse', 'HEAD').stdout.strip() == head
    with pytest.raises(AgenticError):
        apps.replan(app_id, reason='Need revision', expected_revision=result['application']['revision'], operation_id='too-early')
    question = binding(delta, 'clarify', {'ok': 'clarify'})
    question['request_key'] = result['required_input']['request_key']
    question['targets'].update(application_id=app_id, application_revision=result['application']['revision'], plan_revision=1)
    layer.overseer.inputs.bind(question, 'clarify-binding')
    attached = apps.attach_input(app_id, question['id'], expected_revision=result['application']['revision'], operation_id='attach')
    answer = Gateway(question, 'ok')
    layer.overseer.apply_owner_response(question['id'], answer.call(), operation_id='answer', gateway=answer)
    assert apps.get(app_id)['waiting_input_ref'] is None
    replanned = apps.replan(app_id, reason='Revise intent after clarification', expected_revision=apps.get(app_id)['revision'], operation_id='replan')
    assert replanned['application']['phase'] == 'planning'


def test_satisfaction_inspection_binds_real_resources_and_revalidates(layer, tmp_path):
    from .conftest import git
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('already true\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'observed base')
        return plan(delta)
    app_id, delta = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    revision = apps.get(app_id)['revision']
    inspection = apps.inspect_satisfaction(app_id, resource_paths=['user-preferences/rule.md'], explanation='Synthetic rule is present', expected_revision=revision, operation_id='inspect')
    assert inspection['application']['revision'] == revision
    assert apps.inspect_satisfaction(app_id, resource_paths=['user-preferences/rule.md'], explanation='Synthetic rule is present', expected_revision=revision, operation_id='inspect') == inspection
    b = binding(delta, 'close_satisfied', {'done': 'close_satisfied'})
    b['targets'].update(application_id=app_id, application_revision=revision, plan_revision=1)
    b['satisfaction_inspection_id'] = inspection['inspection']['id']
    b['satisfaction_evidence'] = copy.deepcopy(inspection['inspection']['evidence'])
    forged = copy.deepcopy(b); forged['satisfaction_evidence']['resource_hashes']['user-preferences/rule.md'] = 'f' * 64
    with pytest.raises(AgenticError):
        layer.overseer.inputs.bind(forged, 'forged')
    layer.overseer.inputs.bind(b, 'satisfied-binding')
    answer = Gateway(b, 'done')
    applied = layer.overseer.apply_owner_response(b['id'], answer.call(), operation_id='satisfied-answer', gateway=answer)
    assert applied['action'] == 'close_satisfied'
    app = apps.get(app_id)
    assert app['outcome'] == 'already_satisfied' and app['merge'] is None
    assert app['refresh']['state'] == 'not_required' and app['checks']['state'] == 'not_run'
    assert layer.overseer.apply_owner_response(b['id'], answer.call(), operation_id='satisfied-answer', gateway=answer) == applied


def test_satisfaction_old_owner_answer_cannot_close_changed_head(layer, tmp_path):
    from .conftest import git
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('already true\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'observed base')
        return plan(delta)
    app_id, delta = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    revision = apps.get(app_id)['revision']
    inspection = apps.inspect_satisfaction(app_id, resource_paths=['user-preferences/rule.md'], explanation='Synthetic evidence', expected_revision=revision, operation_id='inspect')
    b = binding(delta, 'close_satisfied', {'done': 'close_satisfied'})
    b['targets'].update(application_id=app_id, application_revision=revision, plan_revision=1)
    b['satisfaction_inspection_id'] = inspection['inspection']['id']; b['satisfaction_evidence'] = inspection['inspection']['evidence']
    layer.overseer.inputs.bind(b, 'satisfied-binding')
    (layer.root / 'newer.md').write_text('intervening\n'); git(layer, 'add', 'newer.md'); git(layer, 'commit', '-m', 'later')
    answer = Gateway(b, 'done')
    with pytest.raises(AgenticError) as error:
        layer.overseer.apply_owner_response(b['id'], answer.call(), operation_id='answer', gateway=answer)
    assert error.value.code == 'stale_revision'
    assert apps.get(app_id)['outcome'] is None
    assert layer.overseer.inputs.get(b['id'], include_response=True)['result']['status'] == 'stale_revision'


def test_satisfaction_source_only_requires_current_gateway(layer, tmp_path):
    from .conftest import git
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('tracked\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'observed base')
        return plan(delta)
    class SourceGateway:
        allowed = True
        def resolve_satisfaction_reference(self, ref, *, kind):
            if not self.allowed:
                raise ValueError('revoked')
            return {'reference': ref, 'content_hash': 'a' * 64, 'outcome': None, 'checked_commit': None}
    app_id, delta = agreed_application(layer, tmp_path, local_plan=local_plan)
    gateway = SourceGateway()
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None, satisfaction_gateway=gateway)
    revision = apps.get(app_id)['revision']
    inspection = apps.inspect_satisfaction(app_id, source_refs=[{'id': 'synthetic-source'}], explanation='Synthetic source', expected_revision=revision, operation_id='inspect')
    b = binding(delta, 'close_satisfied', {'done': 'close_satisfied'})
    b['targets'].update(application_id=app_id, application_revision=revision, plan_revision=1)
    b['satisfaction_inspection_id'] = inspection['inspection']['id']; b['satisfaction_evidence'] = inspection['inspection']['evidence']
    layer.overseer.inputs.bind(b, 'binding')
    gateway.allowed = False
    answer = Gateway(b, 'done')
    with pytest.raises(AgenticError) as error:
        layer.overseer.apply_owner_response(b['id'], answer.call(), operation_id='answer', gateway=answer)
    assert error.value.code == 'evidence_unavailable'
    assert layer.overseer.inputs.get(b['id'], include_response=True)['result']['status'] == 'evidence_unavailable'
    assert apps.get(app_id)['phase'] == 'planning'


def test_uncertain_premerge_attempt_requires_exact_one_use_owner_retry(layer, tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from .conftest import git, sha, SimulatedCrash
    import qorqut_agentic.overseer.checks as checks_module
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md',
                              'description': 'Update synthetic rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(), 'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        record['checks'].append({'id': 'pre', 'phase': 'pre_merge', 'kind': 'command', 'arguments': {},
                                 'argv': [sys.executable, '-c', 'raise SystemExit(0)']})
        return record
    app_id, delta = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(draft['execution']['worktree_path'])
    (root / 'user-preferences/rule.md').write_text('after\n'); git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'draft')
    candidate = apps.prepare_merge(app_id, expected_revision=draft['application']['revision'], operation_id='candidate')
    revision = candidate['application']['revision']
    def crash(**_kwargs): raise SimulatedCrash('check started')
    layer._ctx.hooks['overseer_before_check'] = crash
    with pytest.raises(SimulatedCrash):
        apps.record_checks(app_id, phase='pre_merge', expected_revision=revision, operation_id='pre')
    layer._ctx.hooks.clear()
    with monkeypatch.context() as patch:
        patch.setattr(checks_module, '_process_token', lambda: 'synthetic:new:runner')
        uncertain = apps.record_checks(app_id, phase='pre_merge', expected_revision=revision, operation_id='pre')
    assert uncertain['application']['phase'] == 'prepared' and uncertain['check_run']['state'] == 'uncertain'
    need = uncertain['required_input']
    assert need['failure_evidence'] == {'check_run_id': uncertain['check_run']['id'], 'phase': 'pre_merge', 'uncertain': True}
    with pytest.raises(AgenticError):
        apps.publish(app_id, expected_revision=uncertain['application']['revision'], operation_id='no-publish')
    with pytest.raises(AgenticError):
        apps.record_checks(app_id, phase='pre_merge', expected_revision=uncertain['application']['revision'], operation_id='no-retry')
    question = binding(delta, 'failure', {'retry': 'retry_checks'})
    question['request_key'] = need['request_key']; question['failure_evidence'] = need['failure_evidence']
    question['targets'].update(application_id=app_id, application_revision=uncertain['application']['revision'], plan_revision=1)
    layer.overseer.inputs.bind(question, 'failure-binding')
    apps.attach_input(app_id, question['id'], expected_revision=uncertain['application']['revision'], operation_id='attach')
    answer = Gateway(question, 'retry')
    first = layer.overseer.apply_owner_response(question['id'], answer.call(), operation_id='answer', gateway=answer)
    assert first['action'] == 'retry_checks'
    assert layer.overseer.apply_owner_response(question['id'], answer.call(), operation_id='answer-again', gateway=answer) == first
    retry = apps.record_checks(app_id, phase='pre_merge', expected_revision=apps.get(app_id)['revision'], operation_id='retry')
    assert retry['check_run']['state'] == 'passed'
    with pytest.raises(AgenticError):
        apps.record_checks(app_id, phase='pre_merge', expected_revision=retry['application']['revision'], operation_id='second-retry')
    merged = apps.publish(app_id, expected_revision=retry['application']['revision'], operation_id='publish')
    assert merged['application']['phase'] == 'merged'
    assert uncertain['check_run']['receipts'][0]['outcome'] == 'uncertain'


def test_uncertain_tool_attempt_reconciles_real_receipt_without_rerun(layer, tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from .conftest import git, sha, import_example, SimulatedCrash
    from .test_tools import CountingRuntime
    import qorqut_agentic.overseer.checks as checks_module
    runtime = CountingRuntime({'python': sys.executable}); layer._ctx.runtimes['python'] = runtime
    def local_plan(delta):
        import_example(layer, 'example-project-workflow')
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md', 'description': 'Update rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(), 'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        record['checks'].append({'id': 'tool', 'phase': 'pre_merge', 'kind': 'tool', 'tool_ref': 'example-project-workflow/normalize-results', 'arguments': {'rows': []}})
        return record
    app_id, _ = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(draft['execution']['worktree_path']); (root / 'user-preferences/rule.md').write_text('after\n'); git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'draft')
    candidate = apps.prepare_merge(app_id, expected_revision=draft['application']['revision'], operation_id='candidate')
    def crash(**_kwargs): raise SimulatedCrash('receipt persisted')
    layer._ctx.hooks['overseer_after_check_effect'] = crash
    revision = candidate['application']['revision']
    with pytest.raises(SimulatedCrash):
        apps.record_checks(app_id, phase='pre_merge', expected_revision=revision, operation_id='check')
    layer._ctx.hooks.clear()
    with monkeypatch.context() as patch:
        patch.setattr(checks_module, '_process_token', lambda: 'synthetic:new:runner')
        recovered = apps.record_checks(app_id, phase='pre_merge', expected_revision=revision, operation_id='check')
    assert runtime.calls == 1
    assert recovered['check_run']['state'] == 'uncertain'
    assert recovered['required_input'] is None and recovered['application']['checks']['state'] == 'passed'
    with layer._ctx.store.reader() as conn:
        import json
        events = [json.loads(row['record']) for row in conn.execute('SELECT record FROM overseer_application_events WHERE application_id=?', (app_id,))]
    assert any(event.get('type') == 'check_receipts_reconciled' and event['effective_passed'] for event in events)
    old_ref = {'provider': 'qorqut_agentic', 'kind': 'check_receipt', 'application_id': app_id,
               'check_run_id': recovered['check_run']['id'], 'check_id': 'tool'}
    assert apps.get_check_receipt(old_ref)['record']['receipt']['outcome'] == 'uncertain'
    factual = next(event for event in events if event.get('type') == 'check_run_reconciliation')
    new_ref = {**old_ref, 'check_run_id': factual['check_run']['id']}
    assert apps.get_check_receipt(new_ref)['record']['receipt']['outcome'] == 'passed'
    assert factual['reconciles_check_run_id'] == old_ref['check_run_id']
    merged = apps.publish(app_id, expected_revision=recovered['application']['revision'], operation_id='publish')
    assert merged['application']['phase'] == 'merged'
    assert runtime.calls == 1


def test_satisfaction_source_only_can_close_with_resolved_evidence(layer, tmp_path):
    from .conftest import git
    class GatewaySource:
        def resolve_satisfaction_reference(self, reference, *, kind):
            assert kind == 'source'
            return {'reference': reference, 'content_hash': 'b' * 64, 'outcome': None, 'checked_commit': None}
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('tracked\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'base')
        return plan(delta)
    app_id, delta = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    revision = apps.get(app_id)['revision']
    with pytest.raises(AgenticError) as error:
        apps.inspect_satisfaction(app_id, source_refs=[{'id': 'source'}], explanation='Real source', expected_revision=revision, operation_id='without-gateway')
    assert error.value.code == 'evidence_unavailable'
    apps.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None, satisfaction_gateway=GatewaySource())
    found = apps.inspect_satisfaction(app_id, source_refs=[{'id': 'source'}], explanation='Real source', expected_revision=revision, operation_id='inspect')
    question = binding(delta, 'close_satisfied', {'done': 'close_satisfied'})
    question['targets'].update(application_id=app_id, application_revision=revision, plan_revision=1)
    question['satisfaction_inspection_id'] = found['inspection']['id']; question['satisfaction_evidence'] = found['inspection']['evidence']
    layer.overseer.inputs.bind(question, 'binding')
    answer = Gateway(question, 'done')
    layer.overseer.apply_owner_response(question['id'], answer.call(), operation_id='answer', gateway=answer)
    assert apps.get(app_id)['outcome'] == 'already_satisfied'


def test_draft_edit_during_candidate_creation_is_not_silently_dropped(layer, tmp_path):
    from pathlib import Path
    from .conftest import git, sha
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md', 'description': 'Update rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(), 'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        return record
    app_id, _ = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    prepared = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(prepared['execution']['worktree_path']); (root / 'user-preferences/rule.md').write_text('committed\n'); git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'draft')
    def edit(**_kwargs): (root / 'user-preferences/rule.md').write_text('new draft edit\n')
    layer._ctx.hooks['overseer_after_candidate_ref'] = edit
    with pytest.raises(AgenticError) as error:
        apps.prepare_merge(app_id, expected_revision=prepared['application']['revision'], operation_id='candidate')
    layer._ctx.hooks.clear()
    assert error.value.code == 'active_tree_changed'
    assert (root / 'user-preferences/rule.md').read_text() == 'new draft edit\n'
    assert apps.get(app_id)['phase'] == 'working'


def test_external_outcome_before_post_merge_checks_does_not_complete(layer, tmp_path):
    from pathlib import Path
    from .conftest import git, sha
    class TaskGateway:
        def resolve_task(self, reference):
            return {'reference': reference, 'state': 'done', 'result_refs': [{'id': 'result'}],
                    'completion_summary': 'Synthetic outcome', 'observed_at': 'now'}
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'synthetic base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md', 'description': 'Rule',
                              'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(),
                          'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        record['external_requirements'] = [{'id': 'dev', 'summary': 'Development', 'required_for_completion': True}]
        record['checks'].append({'id': 'pre', 'phase': 'pre_merge', 'kind': 'catalog', 'arguments': {}})
        return record
    app_id, _ = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=TaskGateway())
    draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(draft['execution']['worktree_path']); (root / 'user-preferences/rule.md').write_text('after\n')
    git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'draft')
    candidate = apps.prepare_merge(app_id, expected_revision=draft['application']['revision'], operation_id='candidate')
    pre = apps.record_checks(app_id, phase='pre_merge', expected_revision=candidate['application']['revision'], operation_id='pre')
    merged = apps.publish(app_id, expected_revision=pre['application']['revision'], operation_id='publish')
    linked = apps.link_external_task(app_id, 'dev', {'id': 'task'}, expected_revision=merged['application']['revision'], operation_id='link')
    observed = apps.reconcile_external_tasks(app_id, expected_revision=linked['application']['revision'], operation_id='observe')
    assert observed['application']['phase'] == 'merged'
    assert observed['application']['outcome'] is None
    assert observed['application']['checks']['phase'] == 'pre_merge'
    import json
    with layer._ctx.store.transaction() as conn:
        conn.execute('UPDATE delta_applications SET checks=? WHERE id=?',
                     (json.dumps({'state': 'passed', 'phase': 'post_merge', 'receipts': []}), app_id))
    forged = apps.reconcile_external_tasks(app_id, expected_revision=observed['application']['revision'], operation_id='observe-forged')
    assert forged['application']['phase'] == 'merged' and forged['application']['outcome'] is None
    post = apps.record_checks(app_id, phase='post_merge', expected_revision=forged['application']['revision'], operation_id='post')
    assert post['application']['phase'] == 'complete'
    assert post['application']['checks']['phase'] == 'post_merge'
    assert post['check_run']['checked_commit'] == merged['application']['merge']['merged_commit']


def test_external_completion_accepts_immutable_reconciled_post_check(layer, tmp_path):
    import sys
    from pathlib import Path
    from .conftest import git, sha, import_example, SimulatedCrash
    from .test_tools import CountingRuntime
    runtime = CountingRuntime({'python': sys.executable}); layer._ctx.runtimes['python'] = runtime
    class TaskGateway:
        def resolve_task(self, reference):
            return {'reference': reference, 'state': 'done', 'result_refs': [{'id': 'result'}],
                    'completion_summary': 'Synthetic outcome', 'observed_at': 'now'}
    def local_plan(delta):
        import_example(layer, 'example-project-workflow')
        (layer.root / 'user-preferences/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'user-preferences/rule.md', 'description': 'Rule',
                              'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(), 'resource_hashes': {'user-preferences/rule.md': sha('before\n')}}
        record['external_requirements'] = [{'id': 'dev', 'summary': 'Development', 'required_for_completion': True}]
        record['checks'] = [{'id': 'tool', 'phase': 'post_merge', 'kind': 'tool',
                             'tool_ref': 'example-project-workflow/normalize-results', 'arguments': {'rows': []}}]
        return record
    app_id, _ = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=TaskGateway())
    draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(draft['execution']['worktree_path']); (root / 'user-preferences/rule.md').write_text('after\n')
    git(root, 'add', 'user-preferences/rule.md'); git(root, 'commit', '-m', 'draft')
    candidate = apps.prepare_merge(app_id, expected_revision=draft['application']['revision'], operation_id='candidate')
    merged = apps.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='publish')
    def crash(**_kwargs): raise SimulatedCrash('receipt saved')
    layer._ctx.hooks['overseer_after_check_effect'] = crash
    with pytest.raises(SimulatedCrash):
        apps.record_checks(app_id, phase='post_merge', expected_revision=merged['application']['revision'], operation_id='post')
    layer._ctx.hooks.clear()
    checked = apps.record_checks(app_id, phase='post_merge', expected_revision=merged['application']['revision'], operation_id='post')
    assert checked['check_run']['state'] == 'uncertain' and checked['application']['checks']['state'] == 'passed'
    assert checked['application']['phase'] == 'merged' and runtime.calls == 1
    linked = apps.link_external_task(app_id, 'dev', {'id': 'task'}, expected_revision=checked['application']['revision'], operation_id='link')
    completed = apps.reconcile_external_tasks(app_id, expected_revision=linked['application']['revision'], operation_id='observe')
    assert completed['application']['phase'] == 'complete' and completed['application']['outcome'] == 'implemented'
    assert runtime.calls == 1


def _satisfaction_review_fixture(layer, tmp_path):
    from .conftest import git
    def local_plan(delta):
        (layer.root / 'user-preferences/rule.md').write_text('already satisfied\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'synthetic base')
        return plan(delta)
    app_id, delta = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    revision = apps.get(app_id)['revision']
    inspection = apps.inspect_satisfaction(app_id, resource_paths=['user-preferences/rule.md'], explanation='Synthetic evidence', expected_revision=revision, operation_id='inspection')
    question = binding(delta, 'close_satisfied', {'done': 'close_satisfied'})
    question['targets'].update(application_id=app_id, application_revision=revision, plan_revision=1)
    question['satisfaction_inspection_id'] = inspection['inspection']['id']
    question['satisfaction_evidence'] = inspection['inspection']['evidence']
    layer.overseer.inputs.bind(question, 'close-binding')
    return apps, app_id, delta, question


@pytest.mark.parametrize('control', ['pause', 'cancel', 'complete'])
def test_satisfaction_answer_after_run_control_change_is_historical_not_closure(layer, tmp_path, control):
    import json
    apps, app_id, delta, question = _satisfaction_review_fixture(layer, tmp_path)
    app = apps.get(app_id)
    with layer._ctx.store.reader() as conn:
        actor = json.loads(conn.execute('SELECT actor FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()['actor'])
    if control == 'complete':
        layer.workflows.complete_step(app['run_id'], 'record', actor, 'control')
    else:
        getattr(layer.workflows, control)(app['run_id'], actor, 'control')
    answer = Gateway(question, 'done')
    with pytest.raises(AgenticError) as error:
        layer.overseer.apply_owner_response(question['id'], answer.call(), operation_id='old-answer', gateway=answer)
    assert error.value.code == 'invalid_transition'
    history = layer.overseer.inputs.get(question['id'], include_response=True)
    assert history['response']['canonical'] == answer.answer and history['result']['status'] == 'invalid_transition'
    assert apps.get(app_id)['phase'] == 'planning' and apps.get(app_id)['merge'] is None
    if control == 'pause':
        layer.workflows.resume(app['run_id'], actor, 'resume')
        with pytest.raises(AgenticError) as error:
            layer.overseer.apply_owner_response(question['id'], answer.call(), operation_id='replay-old', gateway=answer)
        assert error.value.code == 'invalid_transition'
        fresh = apps.inspect_satisfaction(app_id, resource_paths=['user-preferences/rule.md'], explanation='Fresh synthetic evidence',
                                          expected_revision=apps.get(app_id)['revision'], operation_id='fresh-inspection')
        replacement = binding(delta, 'close_satisfied', {'done': 'close_satisfied'})
        replacement['targets'].update(application_id=app_id, application_revision=apps.get(app_id)['revision'], plan_revision=1)
        replacement['satisfaction_inspection_id'] = fresh['inspection']['id']; replacement['satisfaction_evidence'] = fresh['inspection']['evidence']
        layer.overseer.inputs.bind(replacement, 'fresh-binding')
        new_answer = Gateway(replacement, 'done')
        assert layer.overseer.apply_owner_response(replacement['id'], new_answer.call(), operation_id='fresh-answer', gateway=new_answer)['action'] == 'close_satisfied'
        assert apps.get(app_id)['outcome'] == 'already_satisfied'


def test_satisfaction_answer_rejects_new_unresolved_hold(layer, tmp_path):
    apps, app_id, delta, question = _satisfaction_review_fixture(layer, tmp_path)
    clarify = binding(delta, 'clarify', {'answer': 'clarify'})
    layer.overseer.inputs.bind(clarify, 'new-clarification')
    answer = Gateway(question, 'done')
    with pytest.raises(AgenticError) as error:
        layer.overseer.apply_owner_response(question['id'], answer.call(), operation_id='held-answer', gateway=answer)
    assert error.value.code == 'invalid_transition'
    assert apps.get(app_id)['phase'] == 'planning'
    assert layer.overseer.inputs.get(question['id'], include_response=True)['result']['status'] == 'invalid_transition'


def test_successful_satisfaction_answer_replays_after_later_run_pause(layer, tmp_path):
    import json
    apps, app_id, _, question = _satisfaction_review_fixture(layer, tmp_path)
    answer = Gateway(question, 'done')
    original = layer.overseer.apply_owner_response(question['id'], answer.call(), operation_id='answer', gateway=answer)
    app = apps.get(app_id)
    with layer._ctx.store.reader() as conn:
        actor = json.loads(conn.execute('SELECT actor FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()['actor'])
    layer.workflows.pause(app['run_id'], actor, 'pause-after-closure')
    assert layer.overseer.apply_owner_response(question['id'], answer.call(), operation_id='answer-replay', gateway=answer) == original
    assert apps.get(app_id)['outcome'] == 'already_satisfied'
