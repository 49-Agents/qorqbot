"""Application executor and trusted external-task associations."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from uuid import uuid4
from functools import wraps
import inspect

from ..errors import AgenticError, NotFoundError
from ..files import split_path
from ..history import GitRunner
from ..util import check_operation_id
from . import validation as v
from .appstate import ApplicationState, _application
from .records import canonical, mutate
from .runs import admitted_run
from .publication import PublicationExecutor
from .checks import CheckExecutor
from .satisfaction import SatisfactionExecutor


_UNSET = object()


def _guarded_association(fn):
    @wraps(fn)
    def guarded(self, application_id, *args, **kwargs):
        self._configured()
        with self.ctx.store.lock(exclusive=True):
            bound = inspect.signature(fn).bind(self, application_id, *args, **kwargs)
            payload = {k: value for k, value in bound.arguments.items() if k not in ('self', 'operation_id')}
            fingerprint = self._fingerprint('applications.' + fn.__name__, payload, bound.arguments['operation_id'])
            with self.ctx.store.reader() as conn:
                saved = self._saved(conn, bound.arguments['operation_id'], fingerprint)
                if saved:
                    return saved
            self.ctx.require_no_check_mutation()
            self.ctx.require_no_recovery()
            with self.ctx.store.reader() as conn:
                for row in conn.execute('SELECT result FROM overseer_operations'):
                    item = json.loads(row['result'])
                    if item.get('_state') == 'check_running' and item.get('application_id') == application_id:
                        raise AgenticError('a check has unresolved effects', code='invalid_transition')
            return fn(self, application_id, *args, **kwargs)
    return guarded


class Applications(SatisfactionExecutor, CheckExecutor, PublicationExecutor, ApplicationState):
    def __init__(self, ctx):
        super().__init__(ctx)
        self._worktrees_root = self._states_root = None
        self._external_task_gateway = None

    def configure_runtime(self, *, worktrees_root, states_root, external_task_gateway, satisfaction_gateway=None):
        self.ctx.require_no_check_mutation()
        roots = []
        for name, supplied in (("worktrees_root", worktrees_root), ("states_root", states_root)):
            if not isinstance(supplied, (str, os.PathLike)) or not os.path.isabs(supplied):
                v.invalid(name, 'must be an absolute path')
            path = Path(os.path.abspath(supplied))
            if Path(os.path.realpath(path)) != path:
                v.invalid(name, 'must not contain symlinks')
            roots.append(path)
        all_roots = roots + [self.ctx.root, self.ctx.store.state_dir.resolve()]
        for i, first in enumerate(all_roots):
            for second in all_roots[i + 1:]:
                if first == second or first in second.parents or second in first.parents:
                    v.invalid('runtime roots', 'must be disjoint')
        self._worktrees_root, self._states_root = roots
        self._external_task_gateway = external_task_gateway
        self.ctx.satisfaction_gateway = satisfaction_gateway
        return self

    def _current(self, conn, application_id, expected_revision, *, agreement=True):
        v.uuid(application_id, 'application_id')
        if type(expected_revision) is not int or expected_revision < 1:
            v.invalid('expected_revision')
        app = conn.execute('SELECT * FROM delta_applications WHERE id=?', (application_id,)).fetchone()
        if app is None:
            raise NotFoundError('application not found')
        if app['revision'] != expected_revision:
            raise AgenticError('application revision changed', code='stale_revision')
        delta = conn.execute('SELECT * FROM deltas WHERE id=?', (app['delta_id'],)).fetchone()
        run = conn.execute('SELECT control,completed_at,cancelled_at FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()
        if (delta is None or delta['decision'] != 'accepted' or delta['current_revision'] != app['accepted_revision']
            or delta['accepted_revision'] != app['accepted_revision'] or not app['run_id']
            or admitted_run(conn, app['run_id'], 'application') is None or run is None or run['control'] != 'running'
            or run['completed_at'] or run['cancelled_at']):
            raise AgenticError('current accepted application and running admitted workflow required', code='owner_input_required')
        plan_row = conn.execute('SELECT record FROM overseer_plan_revisions WHERE application_id=? AND revision=?',
                                (application_id, app['plan_revision'])).fetchone()
        if plan_row is None or (agreement and not app['agreement_ref']):
            raise AgenticError('agreed plan required', code='owner_input_required')
        plan = json.loads(plan_row['record'])
        if plan['delta_revision'] != app['accepted_revision']:
            raise AgenticError('plan is stale', code='stale_revision')
        return app, plan

    def _unresolved_need(self, conn, application_id):
        for row in conn.execute('SELECT record FROM overseer_application_events WHERE application_id=? ORDER BY rowid DESC', (application_id,)):
            record = json.loads(row['record'])
            need = record.get('required_input')
            if not need:
                continue
            resolved = False
            for response in conn.execute('SELECT i.record AS binding, r.result FROM overseer_inputs i JOIN overseer_owner_responses r ON r.binding_id=i.id'):
                binding, result = json.loads(response['binding']), json.loads(response['result'])
                if binding['request_key'] == need['request_key']:
                    resolution = conn.execute('SELECT response_key FROM overseer_input_resolutions WHERE input_id=?', (binding['id'],)).fetchone()
                    if resolution:
                        replacement = conn.execute('SELECT result FROM overseer_owner_responses WHERE response_key=?', (resolution['response_key'],)).fetchone()
                        if replacement:
                            result = json.loads(replacement['result'])
                    if result['status'] == 'applied' and ((need['purpose'] == 'failure' and result['action'] in ('retry_checks', 'repair')) or (need['purpose'] == 'clarify' and result['action'] == 'clarify')):
                        resolved = True
                        break
            if not resolved:
                return need
        return None

    def _post_merge_checks_passed(self, conn, app, plan):
        merge = json.loads(app['merge']) if app['merge'] else None
        definitions = {check['id']: check['kind'] for check in plan['checks'] if check['phase'] == 'post_merge'}
        if not merge or not definitions:
            return False
        publication = conn.execute("SELECT candidate_tree FROM overseer_publications WHERE application_id=? AND change_id=? AND candidate_commit=? AND stage IN ('merged','complete')",
                                   (app['id'], merge.get('change_id'), merge.get('merged_commit'))).fetchone()
        if not publication:
            return False
        events = [(row['id'], json.loads(row['record'])) for row in conn.execute(
            'SELECT id,record FROM overseer_application_events WHERE application_id=? ORDER BY rowid DESC', (app['id'],))]
        run = None
        for event_id, event in events:
            candidate = event.get('check_run', {})
            if (event.get('type') == 'check_run' and candidate.get('phase') == 'post_merge'
                and candidate.get('plan_revision') == app['plan_revision']
                and candidate.get('checked_commit') == merge.get('merged_commit')):
                if candidate.get('id') != event_id:
                    return False
                run = candidate
                break
        if not run or run.get('checked_tree') != publication['candidate_tree']:
            return False
        if run.get('state') == 'uncertain':
            original_id = run.get('id')
            run = next((event['check_run'] for event_id, event in events
                        if event.get('type') == 'check_run_reconciliation'
                        and event.get('reconciles_check_run_id') == original_id
                        and event.get('check_run', {}).get('id') == event_id), None)
        if (not run or run.get('state') != 'passed' or run.get('phase') != 'post_merge'
            or run.get('plan_revision') != app['plan_revision']
            or run.get('checked_commit') != merge.get('merged_commit')
            or run.get('checked_tree') != publication['candidate_tree']):
            return False
        receipts = run.get('receipts')
        if not isinstance(receipts, list) or len(receipts) != len(definitions):
            return False
        ids = [receipt.get('check_id') for receipt in receipts if isinstance(receipt, dict)]
        return (len(ids) == len(definitions) and all(isinstance(item, str) for item in ids)
                and len(set(ids)) == len(ids) and set(ids) == set(definitions)
                and all(receipt.get('kind') == definitions[receipt['check_id']]
                        and receipt.get('outcome') == 'passed' and receipt.get('finished_at') is not None
                        for receipt in receipts))

    def _uncertain_precheck(self, conn, application_id):
        events = [json.loads(row['record']) for row in conn.execute(
            'SELECT record FROM overseer_application_events WHERE application_id=? ORDER BY rowid', (application_id,))]
        for item in events:
            check = item.get('check_run', {})
            if item.get('type') != 'check_run' or check.get('phase') != 'pre_merge' or check.get('state') != 'uncertain':
                continue
            disposition = any(e.get('check_run_id') == check['id'] and (e.get('type') == 'owner_authorized_replan' or (e.get('type') == 'check_receipts_reconciled' and e.get('effective_passed'))) for e in events)
            consumed = any(e.get('previous_check_run_id') == check['id'] and e.get('type') == 'retry_consumed' for e in events)
            if not disposition and not consumed:
                return True
        return False

    def _event(self, conn, application_id, record):
        conn.execute('INSERT INTO overseer_application_events VALUES (?,?,?,?)',
                     (str(uuid4()), application_id, canonical(record), self.ctx.now()))

    def _result(self, conn, application_id, *, execution=_UNSET, check_run=None, required_input=None):
        app = conn.execute('SELECT * FROM delta_applications WHERE id=?', (application_id,)).fetchone()
        if execution is _UNSET:
            execution = self._last_execution(conn, application_id, app['plan_revision']) if app['plan_revision'] else None
        return {'application': _application(app), 'execution': execution,
                'check_run': check_run, 'required_input': required_input}

    def _configured(self):
        if self._worktrees_root is None or self._states_root is None:
            raise AgenticError('application runtime is not configured', code='owner_input_required')
        for path in (self._worktrees_root, self._states_root):
            if Path(os.path.realpath(path)) != path:
                raise AgenticError('runtime root contains a symlink', code='invalid_state_path')

    def _validate_execution(self, execution):
        app_id, revision = execution['application_id'], execution['plan_revision']
        v.uuid(app_id, 'application_id')
        if type(revision) is not int or revision < 1:
            v.invalid('plan_revision')
        worktree = self._worktrees_root / app_id / f'plan-{revision}' / 'worktree'
        state = self._states_root / app_id / f'plan-{revision}'
        if (execution['worktree_path'] != str(worktree) or execution['state_path'] != str(state)
            or execution['branch'] != f'overseer/{app_id}/plan-{revision}'
            or Path(os.path.realpath(worktree)) != worktree or Path(os.path.realpath(state)) != state):
            raise AgenticError('draft ownership does not match runtime configuration', code='publication_recovery_required')

    def _executable_plan(self, plan):
        from ..layout import require_editable
        require_editable(change["path"] for change in plan["changes"] if change.get("path"))
        seen = set()
        for change in plan['changes']:
            path = change.get('path')
            if not path:
                raise AgenticError('plan has unresolved local targets', code='owner_input_required',
                                   details={'reason': 'plan_not_executable'})
            split_path(path)
            if path in seen:
                raise AgenticError('plan repeats a local path', code='owner_input_required',
                                   details={'reason': 'plan_not_executable', 'path': path})
            seen.add(path)
            expected = change['expected_sha256']
            if change['action'] != 'create' and plan['base']['resource_hashes'].get(path) != expected:
                raise AgenticError('plan lacks the agreed base hash', code='owner_input_required',
                                   details={'reason': 'plan_not_executable', 'path': path})
        if plan['changes'] and not any(c['phase'] == 'post_merge' for c in plan['checks']):
            raise AgenticError('local changes require a post-merge check', code='owner_input_required',
                               details={'reason': 'plan_not_executable'})

    def prepare(self, application_id, *, expected_revision, operation_id):
        check_operation_id(operation_id)
        if operation_id.lower().startswith('run:'):
            v.invalid('operation_id')
        self._configured()
        fingerprint = hashlib.sha256(canonical({'operation': 'applications.prepare',
            'payload': {'application_id': application_id, 'expected_revision': expected_revision}}).encode()).hexdigest()
        with self.ctx.store.reader() as conn:
            old = conn.execute('SELECT fingerprint,result FROM overseer_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise AgenticError('operation ID was used for different input', code='operation_conflict')
                saved = json.loads(old['result'])
                if saved.get('_state') != 'prepare_reserved':
                    execution = saved.get('execution')
                    if execution is not None:
                        self._validate_execution(execution)
                    return saved
            self.ctx.require_no_check_mutation()
            app, plan = self._current(conn, application_id, expected_revision)
        if not plan['changes']:
            return self._prepare_external(application_id, expected_revision=expected_revision, operation_id=operation_id)
        self._executable_plan(plan)
        from ..layer import AgenticLayer
        with self.ctx.store.lock(exclusive=True):
            self.ctx.require_no_recovery()
            self._configured()
            git = GitRunner(self.ctx.git_executable, self.ctx.root, self.ctx.store.scratch_dir / 'git-home')
            with self.ctx.store.reader() as conn:
                old = conn.execute('SELECT fingerprint,result FROM overseer_operations WHERE operation_id=?', (operation_id,)).fetchone()
                if old and old['fingerprint'] != fingerprint:
                    raise AgenticError('operation ID was used for different input', code='operation_conflict')
                if old:
                    reservation = json.loads(old['result'])
                    if reservation.get('_state') != 'prepare_reserved':
                        return reservation
                app, plan = self._current(conn, application_id, expected_revision)
                if app['phase'] != 'planning':
                    raise AgenticError('application is not planning', code='invalid_transition')
                from .support import blocking_inputs
                if app['waiting_input_ref'] or self._unresolved_need(conn, application_id) or blocking_inputs(conn, app['delta_id']):
                    raise AgenticError('application has an unresolved owner input', code='owner_input_required')
            if not old:
                self._executable_plan(plan)
                if git.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip() != plan['base']['commit'] or git.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout:
                    raise AgenticError('active tree is dirty or no longer at the agreed base', code='active_tree_changed')
                for change in plan['changes']:
                    path = change['path']
                    kind = self.ctx.tree.kind(path)
                    actual = self.ctx.tree.digest(path).sha256 if kind == 'file' else None
                    if kind not in ('file', 'missing') or actual != change['expected_sha256']:
                        raise AgenticError('agreed file changed', code='active_tree_changed', details={'path': path})
                n = app['plan_revision']
                worktree = self._worktrees_root / application_id / f'plan-{n}' / 'worktree'
                state = self._states_root / application_id / f'plan-{n}'
                branch = f'overseer/{application_id}/plan-{n}'
                if worktree.exists() or state.exists() or git.run(['show-ref', '--verify', '--quiet', f'refs/heads/{branch}'], check=False).returncode == 0:
                    raise AgenticError('draft paths or branch already exist', code='operation_conflict')
                execution = {'application_id': application_id, 'plan_revision': n, 'worktree_path': str(worktree),
                             'state_path': str(state), 'branch': branch, 'base_commit': plan['base']['commit'],
                             'integration_base': None, 'candidate_commit': None, 'candidate_tree': None,
                             'change_id': None, 'stage': 'creating'}
                reservation = {'_state': 'prepare_reserved', 'execution': execution}
                with self.ctx.store.transaction() as conn:
                    self._current(conn, application_id, expected_revision)
                    conn.execute('INSERT INTO overseer_operations VALUES (?,?,?,?)',
                                 (operation_id, fingerprint, canonical(reservation), self.ctx.now()))
                    self._event(conn, application_id, {'type': 'prepare_intent', 'operation_id': operation_id, 'execution': execution})
            execution = reservation['execution']
            if (git.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip() != execution['base_commit']
                or git.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout):
                raise AgenticError('active tree changed during draft preparation', code='active_tree_changed')
            worktree, state = Path(execution['worktree_path']), Path(execution['state_path'])
            if (worktree != self._worktrees_root / application_id / f"plan-{execution['plan_revision']}" / 'worktree'
                or state != self._states_root / application_id / f"plan-{execution['plan_revision']}"
                or Path(os.path.realpath(worktree)) != worktree or Path(os.path.realpath(state)) != state):
                raise AgenticError('draft ownership does not match runtime configuration', code='publication_recovery_required')
            worktree.parent.mkdir(parents=True, exist_ok=True)
            state.parent.mkdir(parents=True, exist_ok=True)
            if not worktree.exists():
                git.run(['worktree', 'add', '-b', execution['branch'], str(worktree), execution['base_commit']])
                self.ctx.hook('overseer_after_worktree_add', application_id=application_id)
            draft_git = GitRunner(self.ctx.git_executable, worktree, state / 'scratch' / 'git-home')
            if (draft_git.text(['rev-parse', '--show-toplevel']).strip() != str(worktree)
                or draft_git.text(['symbolic-ref', '--short', 'HEAD']).strip() != execution['branch']
                or draft_git.text(['rev-parse', 'HEAD^{commit}']).strip() != execution['base_commit']
                or draft_git.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout):
                raise AgenticError('draft worktree ownership changed', code='publication_recovery_required')
            from ..store import Store
            if Store(state).exists():
                AgenticLayer.open(worktree, state, git_executable=self.ctx.git_executable)
            else:
                AgenticLayer.initialize(worktree, state, git_executable=self.ctx.git_executable,
                                        runtimes=self.ctx.runtimes, clock=self.ctx.clock,
                                        attribution_source=self.ctx.attribution_source)
            self.ctx.hook('overseer_after_draft_initialize', application_id=application_id)
            if (git.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip() != execution['base_commit']
                or git.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout):
                raise AgenticError('active tree changed during draft preparation', code='active_tree_changed')
            with self.ctx.store.transaction() as conn:
                app, plan = self._current(conn, application_id, expected_revision)
                if app['phase'] != 'planning':
                    raise AgenticError('application changed while preparing', code='stale_revision')
                execution['stage'] = 'working'
                conn.execute("UPDATE delta_applications SET phase='working',revision=revision+1,updated_at=? WHERE id=?",
                             (self.ctx.now(), application_id))
                self._event(conn, application_id, {'type': 'prepared_draft', 'execution': execution})
                result = self._result(conn, application_id, execution=execution)
                conn.execute('UPDATE overseer_operations SET result=? WHERE operation_id=?', (canonical(result), operation_id))
                return result

    def replan(self, application_id, *, reason, expected_revision, operation_id):
        v.uuid(application_id, 'application_id')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            v.invalid('reason')
        def write(conn):
            from .support import blocking_inputs
            app = conn.execute('SELECT * FROM delta_applications WHERE id=?', (application_id,)).fetchone()
            if app is None:
                raise NotFoundError('application not found')
            if type(expected_revision) is not int or expected_revision != app['revision']:
                raise AgenticError('application revision changed', code='stale_revision')
            delta = conn.execute('SELECT * FROM deltas WHERE id=?', (app['delta_id'],)).fetchone()
            run = conn.execute('SELECT control,completed_at,cancelled_at FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()
            if (app['phase'] not in ('planning', 'working', 'prepared') or app['merge'] is not None
                or delta is None or delta['decision'] != 'accepted' or delta['accepted_revision'] != app['accepted_revision']
                or not app['run_id'] or admitted_run(conn, app['run_id'], 'application') is None
                or run is None or run['control'] != 'running' or run['completed_at'] or run['cancelled_at']):
                raise AgenticError('application cannot replan in current state', code='invalid_transition')
            if app['waiting_input_ref'] or self._unresolved_need(conn, application_id) or blocking_inputs(conn, app['delta_id']):
                raise AgenticError('application has an unresolved owner input', code='owner_input_required')
            if self._uncertain_precheck(conn, application_id):
                raise AgenticError('pre-merge check effects remain uncertain', code='invalid_transition')
            for row in conn.execute('SELECT result FROM overseer_operations'):
                result = json.loads(row['result'])
                if result.get('_state') == 'check_running' and result.get('application_id') == application_id:
                    raise AgenticError('a check has unresolved effects', code='invalid_transition')
            previous = [json.loads(r['record']) for r in conn.execute(
                'SELECT record FROM overseer_application_events WHERE application_id=? ORDER BY rowid', (application_id,))]
            for event in reversed(previous):
                check = event.get('check_run', {})
                if event.get('type') == 'check_run' and check.get('phase') == 'pre_merge' and check.get('plan_revision') == app['plan_revision']:
                    if check['state'] != 'passed' and not any(item.get('check_run_id') == check['id'] and (item.get('type') == 'owner_authorized_replan' or (item.get('type') == 'check_receipts_reconciled' and item.get('effective_passed'))) for item in previous):
                        raise AgenticError('owner repair response is required before replanning', code='owner_input_required')
                    break
            self._event(conn, application_id, {'type': 'replan', 'reason': reason, 'previous_phase': app['phase'],
                'plan_revision': app['plan_revision'], 'agreement_ref': json.loads(app['agreement_ref']) if app['agreement_ref'] else None,
                'prior_execution_and_checks': [r for r in previous if r.get('type') in ('prepared_draft', 'candidate', 'check_run')]})
            conn.execute("UPDATE delta_applications SET phase='planning',agreement_ref=NULL,revision=revision+1,updated_at=? WHERE id=?",
                         (self.ctx.now(), application_id))
            return self._result(conn, application_id, execution=None)
        payload = {'application_id': application_id, 'reason': reason, 'expected_revision': expected_revision}
        fingerprint = self._fingerprint('applications.replan', payload, operation_id)
        with self.ctx.store.lock(exclusive=True):
            with self.ctx.store.reader() as conn:
                saved = self._saved(conn, operation_id, fingerprint)
                if saved:
                    return saved
            self.ctx.require_no_check_mutation()
            self.ctx.require_no_recovery()
            return mutate(self.ctx, operation_id, 'applications.replan', payload, write)

    def _prepare_external(self, application_id, *, expected_revision, operation_id):
        def write(conn):
            app, plan = self._current(conn, application_id, expected_revision)
            if app['phase'] != 'planning':
                raise AgenticError('application is not planning', code='invalid_transition')
            from .support import blocking_inputs
            if app['waiting_input_ref'] or self._unresolved_need(conn, application_id) or blocking_inputs(conn, app['delta_id']):
                raise AgenticError('application has an unresolved owner input', code='owner_input_required')
            if plan['changes']:
                raise AgenticError('local changes require draft preparation', code='invalid_transition')
            if not any(r['required_for_completion'] for r in plan['external_requirements']):
                raise AgenticError('owner-confirmed satisfied closure required', code='owner_input_required',
                                   details={'reason': 'plan_not_executable'})
            conn.execute("UPDATE delta_applications SET phase='working',revision=revision+1,updated_at=? WHERE id=?",
                         (self.ctx.now(), application_id))
            self._event(conn, application_id, {'type': 'external_work_started', 'plan_revision': app['plan_revision']})
            return self._result(conn, application_id)
        return mutate(self.ctx, operation_id, 'applications.prepare',
                      {'application_id': application_id, 'expected_revision': expected_revision}, write)

    @_guarded_association
    def link_external_task(self, application_id, requirement_id, task_ref, *, expected_revision, operation_id):
        v.string(requirement_id, 'requirement_id')
        if not isinstance(task_ref, dict) or not task_ref:
            v.invalid('task_ref')
        def write(conn):
            app, plan = self._current(conn, application_id, expected_revision)
            if self._unresolved_need(conn, application_id):
                raise AgenticError('application has an unresolved owner input', code='owner_input_required')
            if app['phase'] not in ('planning', 'working', 'prepared', 'merged'):
                raise AgenticError('application cannot link a task', code='invalid_transition')
            if requirement_id not in {r['id'] for r in plan['external_requirements']}:
                raise AgenticError('requirement not in agreed plan', code='invalid_transition')
            tasks = json.loads(app['external_tasks'])
            existing = next((t for t in tasks if t['requirement_id'] == requirement_id), None)
            if existing and existing['linked_plan_revision'] == app['plan_revision']:
                if existing['task_ref'] != task_ref:
                    raise AgenticError('task association cannot be repointed', code='operation_conflict')
                return self._result(conn, application_id)
            if existing:
                tasks.remove(existing)
            task = {'requirement_id': requirement_id, 'task_ref': task_ref, 'observation': None,
                    'linked_plan_revision': app['plan_revision']}
            tasks.append(task)
            conn.execute('UPDATE delta_applications SET external_tasks=?,revision=revision+1,updated_at=? WHERE id=?',
                         (canonical(tasks), self.ctx.now(), application_id))
            self._event(conn, application_id, {'type': 'external_task_linked', 'task': task})
            return self._result(conn, application_id)
        return mutate(self.ctx, operation_id, 'applications.link_external_task',
                      {'application_id': application_id, 'requirement_id': requirement_id, 'task_ref': task_ref,
                       'expected_revision': expected_revision}, write)

    @_guarded_association
    def reconcile_external_tasks(self, application_id, *, expected_revision, operation_id):
        if self._external_task_gateway is None:
            raise AgenticError('external task gateway required', code='owner_input_required')
        payload = {'application_id': application_id, 'expected_revision': expected_revision}
        fingerprint = hashlib.sha256(canonical({'operation': 'applications.reconcile_external_tasks', 'payload': payload}).encode()).hexdigest()
        with self.ctx.store.reader() as conn:
            old = conn.execute('SELECT fingerprint,result FROM overseer_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise AgenticError('operation ID was used for different input', code='operation_conflict')
                return json.loads(old['result'])
            app, plan = self._current(conn, application_id, expected_revision)
            if self._unresolved_need(conn, application_id):
                raise AgenticError('application has an unresolved owner input', code='owner_input_required')
            tasks = json.loads(app['external_tasks'])
            original_tasks = canonical(tasks)
        for task in tasks:
            try:
                observation = self._external_task_gateway.resolve_task(task['task_ref'])
                v.obj(observation, 'task observation', ('reference', 'state', 'result_refs', 'completion_summary', 'observed_at'))
                if observation['state'] not in ('open', 'working', 'waiting', 'done', 'cancelled'):
                    v.invalid('task observation.state')
                if not isinstance(observation['reference'], dict) or not observation['reference']:
                    v.invalid('task observation.reference')
                v.array(observation['result_refs'], 'task observation.result_refs')
                if not all(isinstance(ref, dict) and ref for ref in observation['result_refs']):
                    v.invalid('task observation.result_refs')
                for key in ('provider', 'kind', 'id', 'namespace'):
                    if key in task['task_ref'] and observation['reference'].get(key) != task['task_ref'][key]:
                        v.invalid('task observation.reference', 'does not identify the linked task')
                v.string(observation['observed_at'], 'task observation.observed_at')
                if observation['completion_summary'] is not None and not isinstance(observation['completion_summary'], str):
                    v.invalid('task observation.completion_summary')
            except Exception as exc:
                observation = {'reference': task['task_ref'], 'state': 'unavailable', 'result_refs': [],
                               'completion_summary': None, 'observed_at': self.ctx.now(),
                               'diagnostic': f'{type(exc).__name__}: {exc}'[:1000]}
            task['observation'] = observation
        def write(conn):
            app, current_plan = self._current(conn, application_id, expected_revision)
            if current_plan != plan or app['external_tasks'] != original_tasks:
                raise AgenticError('application changed', code='stale_revision')
            required = [r['id'] for r in plan['external_requirements'] if r['required_for_completion']]
            ready = {t['requirement_id'] for t in tasks if t['linked_plan_revision'] == app['plan_revision']
                     and t['observation']['state'] == 'done' and t['observation']['result_refs']
                     and isinstance(t['observation']['completion_summary'], str)
                     and t['observation']['completion_summary'].strip()}
            phase, outcome = app['phase'], app['outcome']
            if required and all(r in ready for r in required):
                if not plan['changes'] and phase == 'working':
                    phase, outcome = 'complete', 'external_completed'
                elif phase == 'merged' and self._post_merge_checks_passed(conn, app, plan):
                    phase, outcome = 'complete', 'implemented'
            conn.execute('UPDATE delta_applications SET external_tasks=?,phase=?,outcome=?,revision=revision+1,updated_at=? WHERE id=?',
                         (canonical(tasks), phase, outcome, self.ctx.now(), application_id))
            self._event(conn, application_id, {'type': 'external_tasks_observed', 'tasks': tasks})
            return self._result(conn, application_id)
        return mutate(self.ctx, operation_id, 'applications.reconcile_external_tasks',
                      payload, write)

    @_guarded_association
    def attach_input(self, application_id, binding_id, *, expected_revision, operation_id):
        v.uuid(binding_id, 'binding_id')
        def write(conn):
            app, _ = self._current(conn, application_id, expected_revision)
            row = conn.execute('SELECT record FROM overseer_inputs WHERE id=?', (binding_id,)).fetchone()
            if row is None:
                raise NotFoundError('binding not found')
            binding = json.loads(row['record'])
            need = self._unresolved_need(conn, application_id)
            keys = {binding['request_key']}
            for old_id in binding.get('resolves_input_ids', []):
                old_row = conn.execute('SELECT record FROM overseer_inputs WHERE id=?', (old_id,)).fetchone()
                if old_row:
                    keys.add(json.loads(old_row['record'])['request_key'])
            if need is None or need['request_key'] not in keys or binding['purpose'] != need['purpose']:
                raise AgenticError('binding does not match the required input', code='invalid_transition')
            if need['purpose'] == 'failure' and binding.get('failure_evidence') != need['failure_evidence']:
                raise AgenticError('binding failure evidence changed', code='stale_revision')
            target = binding['targets']
            if (binding['purpose'] not in ('failure', 'clarify') or not binding['request_ref']
                or target['application_id'] != application_id or target['delta_id'] != app['delta_id']
                or target['delta_revision'] != app['accepted_revision'] or target['plan_revision'] != app['plan_revision']
                or target.get('application_revision') != expected_revision):
                raise AgenticError('binding does not match application', code='stale_revision')
            reference = {'binding_id': binding_id, 'request_ref': binding['request_ref']}
            old = json.loads(app['waiting_input_ref']) if app['waiting_input_ref'] else None
            if old and old != reference:
                resolved = conn.execute('SELECT 1 FROM overseer_input_resolutions WHERE input_id=?', (old['binding_id'],)).fetchone()
                if not resolved:
                    raise AgenticError('application already awaits input', code='owner_input_required')
            if old != reference:
                conn.execute('UPDATE delta_applications SET waiting_input_ref=?,updated_at=? WHERE id=?',
                             (canonical(reference), self.ctx.now(), application_id))
                self._event(conn, application_id, {'type': 'input_attached', 'input': reference})
            return self._result(conn, application_id)
        return mutate(self.ctx, operation_id, 'applications.attach_input',
                      {'application_id': application_id, 'binding_id': binding_id, 'expected_revision': expected_revision}, write)
