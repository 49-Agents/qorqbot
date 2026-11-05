"""Cross-thread/process coordination uses disposable runtime and Git state."""
import json
import multiprocessing
import threading

import pytest

from qorqut_agentic import AgenticError, AgenticLayer
from qorqut_agentic.store import Store
from .test_overseer_publication_recovery import _ready_to_publish


def test_reentrant_identity_thread_timeout_and_upgrade(layer):
    other = Store(layer.state)
    with layer._ctx.store.lock(exclusive=True):
        with other.lock(exclusive=False), other.lock(exclusive=True):
            pass
        result = []
        def contend():
            try:
                with other.lock(exclusive=True, timeout=.1):
                    result.append('acquired')
            except AgenticError as exc:
                result.append(exc.code)
        thread = threading.Thread(target=contend)
        thread.start(); thread.join(timeout=3)
        assert result == ['lock_timeout']
    with other.lock(exclusive=False):
        with pytest.raises(AgenticError) as error:
            with layer._ctx.store.lock(exclusive=True):
                pass
        assert error.value.code == 'state_conflict'


def test_fork_cannot_inherit_reentrant_owner(layer):
    context = multiprocessing.get_context('fork')
    parent, child = context.Pipe(duplex=False)
    def contend():
        try:
            with Store(layer.state).lock(exclusive=True, timeout=.2):
                child.send('acquired')
        except AgenticError as exc:
            child.send(exc.code)
    with layer._ctx.store.lock(exclusive=True):
        proc = context.Process(target=contend)
        proc.start(); child.close()
        assert parent.poll(3) and parent.recv() == 'lock_timeout'
        proc.join(timeout=3)
        assert proc.exitcode == 0


def test_pause_waits_for_coherent_publication(layer, tmp_path):
    apps, app_id, delta, candidate = _ready_to_publish(layer, tmp_path)
    app = apps.get(app_id)
    with layer._ctx.store.reader() as conn:
        actor = json.loads(conn.execute('SELECT actor FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()['actor'])
    context = multiprocessing.get_context('fork')
    ready_parent, ready_child = context.Pipe(duplex=False)
    release_child, release_parent = context.Pipe(duplex=False)
    result_parent, result_child = context.Pipe(duplex=False)
    pause_parent, pause_child = context.Pipe(duplex=False)
    revision_parent, revision_child = context.Pipe(duplex=False)
    def publish():
        active = AgenticLayer.open(layer.root, layer.state)
        executor = active.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
        def at_intent(**_kwargs):
            ready_child.send('journaled')
            release_child.recv()
        active._ctx.hooks['overseer_after_publication_intent'] = at_intent
        try:
            result_child.send(executor.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='concurrent-publish')['application']['phase'])
        except Exception as exc:
            result_child.send(f'error:{exc}')
    def pause():
        active = AgenticLayer.open(layer.root, layer.state)
        try:
            pause_child.send(active.workflows.pause(app['run_id'], actor, 'concurrent-pause')['run']['state'])
        except Exception as exc:
            pause_child.send(f'error:{exc}')
    def revise():
        import copy
        active = AgenticLayer.open(layer.root, layer.state)
        changed = copy.deepcopy(delta); changed['revision'] = 2
        try:
            revision_child.send(active.overseer.deltas.revise(changed, 1, 'concurrent-revision')['current_revision'])
        except Exception as exc:
            revision_child.send(f'error:{exc}')
    publisher = context.Process(target=publish)
    publisher.start(); ready_child.close(); release_child.close(); result_child.close()
    assert ready_parent.poll(5) and ready_parent.recv() == 'journaled'
    pauser = context.Process(target=pause)
    reviser = context.Process(target=revise)
    pauser.start(); pause_child.close(); reviser.start(); revision_child.close()
    assert not pause_parent.poll(.3) and not revision_parent.poll(.3)
    release_parent.send('continue')
    assert result_parent.poll(10) and result_parent.recv() == 'merged'
    assert pause_parent.poll(10) and pause_parent.recv() == 'paused'
    assert revision_parent.poll(10) and revision_parent.recv() == 2
    publisher.join(timeout=5); pauser.join(timeout=5); reviser.join(timeout=5)
    assert publisher.exitcode == 0 and pauser.exitcode == 0 and reviser.exitcode == 0
    assert layer.overseer.applications.get(app_id)['phase'] == 'merged'


def test_crash_barrier_blocks_pause_and_delta_revision_until_recovery(layer, tmp_path):
    import copy
    apps, app_id, delta, candidate = _ready_to_publish(layer, tmp_path)
    app = apps.get(app_id)
    with layer._ctx.store.reader() as conn:
        actor = json.loads(conn.execute('SELECT actor FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()['actor'])
    def crash(**_kwargs):
        raise RuntimeError('synthetic crash')
    layer._ctx.hooks['overseer_after_publication_intent'] = crash
    with pytest.raises(RuntimeError):
        apps.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='publish')
    layer._ctx.hooks.clear()
    with pytest.raises(AgenticError) as error:
        layer.workflows.pause(app['run_id'], actor, 'pause-before-recovery')
    assert error.value.code == 'publication_recovery_required'
    from .test_overseer_state import proposal
    from uuid import uuid4
    unrelated = proposal(); unrelated['id'] = str(uuid4())
    assert layer.overseer.proposals.record(unrelated, 'unrelated-proposal')['id'] == unrelated['id']
    changed = copy.deepcopy(delta); changed['revision'] = 2
    with pytest.raises(AgenticError) as error:
        layer.overseer.deltas.revise(changed, 1, 'revise-before-recovery')
    assert error.value.code == 'publication_recovery_required'
    apps.recover(app_id, expected_revision=candidate['application']['revision'], operation_id='recover')
    assert layer.workflows.pause(app['run_id'], actor, 'pause-before-recovery')['run']['state'] == 'paused'


def test_check_callback_cannot_use_reentrant_lock_to_mutate(layer, tmp_path):
    import sys
    from pathlib import Path
    from .conftest import git, sha, import_example, ATTR
    from .test_overseer_application import agreed_application
    from .test_overseer_contracts import plan
    from .test_tools import CountingRuntime
    class MutatingRuntime(CountingRuntime):
        blocked = None
        def execute(self, request):
            try:
                layer.authoring.apply(ATTR, [{'op': 'create', 'path': 'plugins/example-project-workflow/unauthorized.md', 'content': 'bad'}])
            except AgenticError as exc:
                self.blocked = exc.code
            return super().execute(request)
    runtime = MutatingRuntime({'python': sys.executable}); layer._ctx.runtimes['python'] = runtime
    def local_plan(delta):
        import_example(layer, 'example-project-workflow')
        (layer.root / 'plugins/example-project-workflow/rule.md').write_text('before\n')
        git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'base')
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': 'plugins/example-project-workflow/rule.md', 'description': 'Update rule', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(), 'resource_hashes': {'plugins/example-project-workflow/rule.md': sha('before\n')}}
        record['checks'].append({'id': 'tool', 'phase': 'pre_merge', 'kind': 'tool', 'tool_ref': 'example-project-workflow/normalize-results', 'arguments': {'rows': []}})
        return record
    app_id, _ = agreed_application(layer, tmp_path, local_plan=local_plan)
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    draft = apps.prepare(app_id, expected_revision=apps.get(app_id)['revision'], operation_id='prepare')
    root = Path(draft['execution']['worktree_path']); (root / 'plugins/example-project-workflow/rule.md').write_text('after\n'); git(root, 'add', 'plugins/example-project-workflow/rule.md'); git(root, 'commit', '-m', 'draft')
    candidate = apps.prepare_merge(app_id, expected_revision=draft['application']['revision'], operation_id='candidate')
    checked = apps.record_checks(app_id, phase='pre_merge', expected_revision=candidate['application']['revision'], operation_id='check')
    assert checked['check_run']['state'] == 'passed'
    assert runtime.blocked == 'invalid_transition'
    assert not (layer.root / 'plugins/example-project-workflow/unauthorized.md').exists()


def test_pause_before_admission_prevents_git_publication(layer, tmp_path):
    from .conftest import git
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path)
    app = apps.get(app_id)
    with layer._ctx.store.reader() as conn:
        actor = json.loads(conn.execute('SELECT actor FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()['actor'])
    before = git(layer, 'rev-parse', 'HEAD').stdout.strip()
    layer.workflows.pause(app['run_id'], actor, 'pause-before-publish')
    with pytest.raises(AgenticError) as error:
        apps.publish(app_id, expected_revision=candidate['application']['revision'], operation_id='publish')
    assert error.value.code == 'owner_input_required'
    assert git(layer, 'rev-parse', 'HEAD').stdout.strip() == before
    assert not apps.recovery_status(app_id)['recovery_required']


def test_reentrant_lock_refuses_replaced_file_identity(layer):
    import os
    lock_path = layer._ctx.store.lock_path
    original = lock_path.with_name('saved-authoring.lock')
    with layer._ctx.store.lock(exclusive=True):
        os.rename(lock_path, original)
        lock_path.write_text('replacement')
        try:
            with pytest.raises(AgenticError) as error:
                with Store(layer.state).lock(exclusive=True, timeout=.1):
                    pass
            assert error.value.code == 'state_mismatch'
        finally:
            lock_path.unlink()
            os.rename(original, lock_path)


def test_pause_waits_for_check_effect_and_receipt(layer, tmp_path):
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path, pre_check=True)
    app = apps.get(app_id)
    with layer._ctx.store.reader() as conn:
        actor = json.loads(conn.execute('SELECT actor FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()['actor'])
    context = multiprocessing.get_context('fork')
    ready_parent, ready_child = context.Pipe(duplex=False)
    release_child, release_parent = context.Pipe(duplex=False)
    result_parent, result_child = context.Pipe(duplex=False)
    pause_parent, pause_child = context.Pipe(duplex=False)
    def check():
        active = AgenticLayer.open(layer.root, layer.state)
        executor = active.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
        def at_check(**_kwargs):
            ready_child.send('check-intent')
            release_child.recv()
        active._ctx.hooks['overseer_before_check'] = at_check
        try:
            result_child.send(executor.record_checks(app_id, phase='pre_merge', expected_revision=candidate['application']['revision'], operation_id='check')['check_run']['state'])
        except Exception as exc:
            result_child.send(f'error:{exc}')
    def pause():
        active = AgenticLayer.open(layer.root, layer.state)
        try:
            pause_child.send(active.workflows.pause(app['run_id'], actor, 'pause')['run']['state'])
        except Exception as exc:
            pause_child.send(f'error:{exc}')
    checker = context.Process(target=check); checker.start(); ready_child.close(); release_child.close(); result_child.close()
    assert ready_parent.poll(5) and ready_parent.recv() == 'check-intent'
    pauser = context.Process(target=pause); pauser.start(); pause_child.close()
    assert not pause_parent.poll(.3)
    release_parent.send('continue')
    assert result_parent.poll(10) and result_parent.recv() == 'passed'
    assert pause_parent.poll(10) and pause_parent.recv() == 'paused'
    checker.join(timeout=5); pauser.join(timeout=5)
    assert checker.exitcode == 0 and pauser.exitcode == 0


def test_pausing_after_interrupted_check_is_allowed_and_does_not_retry(layer, tmp_path):
    from .conftest import SimulatedCrash
    apps, app_id, _, candidate = _ready_to_publish(layer, tmp_path, pre_check=True)
    app = apps.get(app_id)
    with layer._ctx.store.reader() as conn:
        actor = json.loads(conn.execute('SELECT actor FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()['actor'])
    def crash(**_kwargs): raise SimulatedCrash('check interrupted')
    layer._ctx.hooks['overseer_before_check'] = crash
    revision = candidate['application']['revision']
    with pytest.raises(SimulatedCrash):
        apps.record_checks(app_id, phase='pre_merge', expected_revision=revision, operation_id='check')
    layer._ctx.hooks.clear()
    assert layer.workflows.pause(app['run_id'], actor, 'pause')['run']['state'] == 'paused'
    with pytest.raises(AgenticError):
        apps.record_checks(app_id, phase='pre_merge', expected_revision=revision, operation_id='check')
    assert layer.workflows.resume(app['run_id'], actor, 'resume')['run']['state'] == 'running'
    result = apps.record_checks(app_id, phase='pre_merge', expected_revision=revision, operation_id='check')
    assert result['check_run']['state'] == 'uncertain'
    assert result['required_input']['failure_evidence']['uncertain'] is True


def test_check_callback_cannot_publish_another_application_but_replay_is_safe(layer, tmp_path):
    import copy
    import sys
    from pathlib import Path
    from .conftest import git, sha, import_example
    from .test_overseer_contracts import install_test_workflows, InvocationGateway, plan
    from .test_overseer_decisions import Gateway, binding, revision, uid
    from .test_overseer_state import proposal
    from .test_tools import CountingRuntime
    install_test_workflows(layer, tmp_path)
    import_example(layer, 'example-project-workflow')
    for name in ('plugins/example-project-workflow/a.md', 'plugins/example-project-workflow/b.md'):
        (layer.root / name).write_text('before\n')
    git(layer, 'add', '-A'); git(layer, 'commit', '-m', 'synthetic base')
    apps = layer.overseer.applications.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None)
    def prepare_application(prefix, path, with_tool=False):
        raw = proposal(); raw['id'] = uid(); layer.overseer.proposals.record(raw, prefix + '-proposal')
        delta = revision(raw); layer.overseer.deltas.create(delta, prefix + '-delta')
        review = binding(delta); layer.overseer.inputs.bind(review, prefix + '-review')
        owner = Gateway(review)
        app_id = layer.overseer.apply_owner_response(review['id'], owner.call(), operation_id=prefix + '-accept', gateway=owner)['application_id']
        invocation = InvocationGateway('application', {'application_id': app_id})
        layer.overseer.start_run('application', {'reference': invocation.ref}, operation_id=prefix + '-start', gateway=invocation)
        record = plan(delta)
        record['changes'] = [{'kind': 'file', 'action': 'edit', 'path': path, 'description': 'Synthetic change', 'expected_sha256': sha('before\n')}]
        record['base'] = {'commit': git(layer, 'rev-parse', 'HEAD').stdout.strip(), 'resource_hashes': {path: sha('before\n')}}
        if with_tool:
            record['checks'].append({'id': 'pre-tool', 'phase': 'pre_merge', 'kind': 'tool',
                                     'tool_ref': 'example-project-workflow/normalize-results', 'arguments': {'rows': []}})
        else:
            record['checks'].append({'id': 'pre-catalog', 'phase': 'pre_merge', 'kind': 'catalog', 'arguments': {}})
        saved = apps.save_plan(app_id, record, expected_revision=2, operation_id=prefix + '-plan')
        approval = binding(delta, 'plan', {'go': 'proceed'})
        approval['targets'].update(application_id=app_id, application_revision=saved['application']['revision'], plan_revision=1)
        layer.overseer.inputs.bind(approval, prefix + '-plan-binding')
        answer = Gateway(approval, 'go')
        layer.overseer.apply_owner_response(approval['id'], answer.call(), operation_id=prefix + '-approve', gateway=answer)
        revision_before_prepare = apps.get(app_id)['revision']
        draft = apps.prepare(app_id, expected_revision=revision_before_prepare, operation_id=prefix + '-prepare')
        root = Path(draft['execution']['worktree_path']); (root / path).write_text('after\n')
        git(root, 'add', path); git(root, 'commit', '-m', 'draft')
        candidate = apps.prepare_merge(app_id, expected_revision=draft['application']['revision'], operation_id=prefix + '-candidate')
        return app_id, revision_before_prepare, draft, candidate, saved
    app_a, _, _, ready_a, _ = prepare_application('a', 'plugins/example-project-workflow/a.md', with_tool=True)
    app_b, revision_b, draft_b, ready_b, saved_b = prepare_application('b', 'plugins/example-project-workflow/b.md')
    checked_b = apps.record_checks(app_b, phase='pre_merge', expected_revision=ready_b['application']['revision'], operation_id='b-pre')
    saved_plan = apps.get(app_b)['plan']
    saved_plan = {key: value for key, value in saved_plan.items() if key not in ('application_id', 'revision', 'created_at')}
    feed_before = copy.deepcopy(layer.changes.list()['changes'])
    class PublishingRuntime(CountingRuntime):
        nested_error = None
        other_errors = None
        replay_prepare = replay_candidate = replay_check = replay_plan = read_receipt = None
        def execute(self, request):
            self.replay_prepare = apps.prepare(app_b, expected_revision=revision_b, operation_id='b-prepare')
            self.replay_candidate = apps.prepare_merge(app_b, expected_revision=draft_b['application']['revision'], operation_id='b-candidate')
            self.replay_check = apps.record_checks(app_b, phase='pre_merge', expected_revision=ready_b['application']['revision'], operation_id='b-pre')
            self.replay_plan = apps.save_plan(app_b, saved_plan, expected_revision=2, operation_id='b-plan')
            self.read_receipt = apps.get_check_receipt({'provider': 'qorqut_agentic', 'kind': 'check_receipt', 'application_id': app_b,
                                                        'check_run_id': checked_b['check_run']['id'], 'check_id': 'pre-catalog'})
            revision = checked_b['application']['revision']
            calls = [
                lambda: apps.configure_runtime(worktrees_root=tmp_path / 'worktrees', states_root=tmp_path / 'states', external_task_gateway=None),
                lambda: apps.prepare(app_b, expected_revision=revision, operation_id='nested-prepare'),
                lambda: apps.prepare_merge(app_b, expected_revision=revision, operation_id='nested-candidate'),
                lambda: apps.recover(app_b, expected_revision=revision, operation_id='nested-recover'),
                lambda: apps.replan(app_b, reason='Synthetic', expected_revision=revision, operation_id='nested-replan'),
                lambda: apps.save_plan(app_b, saved_plan, expected_revision=revision, operation_id='nested-save-plan'),
                lambda: apps.attach_input(app_b, uid(), expected_revision=revision, operation_id='nested-attach'),
                lambda: apps.record_checks(app_b, phase='pre_merge', expected_revision=revision, operation_id='nested-check'),
                lambda: apps.inspect_satisfaction(app_b, resource_paths=['plugins/example-project-workflow/a.md'], explanation='Synthetic', expected_revision=revision, operation_id='nested-inspect'),
                lambda: apps.link_external_task(app_b, 'dev', {'id': 'task'}, expected_revision=revision, operation_id='nested-link'),
                lambda: apps.reconcile_external_tasks(app_b, expected_revision=revision, operation_id='nested-reconcile'),
            ]
            self.other_errors = []
            for call in calls:
                try:
                    call()
                except AgenticError as exc:
                    self.other_errors.append((exc.code, exc.message))
            try:
                apps.publish(app_b, expected_revision=revision, operation_id='b-publish')
            except AgenticError as exc:
                self.nested_error = exc.code
            return super().execute(request)
    runtime = PublishingRuntime({'python': sys.executable}); layer._ctx.runtimes['python'] = runtime
    checked_a = apps.record_checks(app_a, phase='pre_merge', expected_revision=ready_a['application']['revision'], operation_id='a-check')
    assert checked_a['check_run']['state'] == 'passed' and runtime.nested_error == 'invalid_transition'
    assert len(runtime.other_errors) == 11
    assert all(code == 'invalid_transition' and 'mutations are forbidden' in message for code, message in runtime.other_errors)
    assert runtime.replay_prepare == draft_b and runtime.replay_candidate == ready_b and runtime.replay_check == checked_b
    assert runtime.replay_plan == saved_b and runtime.read_receipt['record']['receipt']['outcome'] == 'passed'
    assert apps.get(app_b)['phase'] == 'prepared' and (layer.root / 'plugins/example-project-workflow/b.md').read_text() == 'before\n'
    assert layer.changes.list()['changes'] == feed_before
    with layer._ctx.store.reader() as conn:
        assert conn.execute('SELECT count(*) FROM overseer_publications WHERE application_id=?', (app_b,)).fetchone()[0] == 0
    published = apps.publish(app_b, expected_revision=checked_b['application']['revision'], operation_id='b-publish')
    assert published['application']['phase'] == 'merged' and (layer.root / 'plugins/example-project-workflow/b.md').read_text() == 'after\n'
