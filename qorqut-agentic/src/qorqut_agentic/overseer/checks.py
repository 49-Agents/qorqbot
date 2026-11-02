"""Execution of agreed, bounded application checks."""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from pathlib import Path
from uuid import uuid4
from functools import wraps

from ..errors import AgenticError
from ..history import GitRunner
from ..runtime import _kill_group
from ..tools import _process_token
from .records import canonical
from .runs import admitted_run


def _command(argv, cwd, env, timeout, maximum):
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return None, '', f'could not start command: {exc}'
    selector = selectors.DefaultSelector()
    for stream in (process.stdout, process.stderr):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + timeout
    diagnostic = None
    exited_at = None
    try:
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                diagnostic = 'command timed out'
                break
            if process.poll() is not None:
                if exited_at is None:
                    exited_at = now
                    _kill_group(process.pid)
                elif now - exited_at > 1:
                    diagnostic = 'command output streams did not close'
                    break
            for key, _ in selector.select(min(.05, max(deadline - now, .001))):
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    room = maximum * 4 - len(output)
                    output.extend(chunk[:max(room, 0)])
                    if len(chunk) > room:
                        diagnostic = 'command output exceeded bound'
                        break
            if diagnostic:
                break
    finally:
        if diagnostic or process.poll() is None:
            _kill_group(process.pid)
        selector.close()
        process.stdout.close(); process.stderr.close()
        process.wait(timeout=5)
    return process.returncode, output.decode('utf-8', 'replace')[:maximum], diagnostic


def _check_critical_section(fn):
    @wraps(fn)
    def guarded(self, *args, **kwargs):
        self.ctx.require_no_check_mutation()
        from ..core import check_effects_scope
        with self.ctx.store.lock(exclusive=True), check_effects_scope():
            return fn(self, *args, **kwargs)
    return guarded


class CheckExecutor:
    def get_check_receipt(self, reference):
        import hashlib
        from ..errors import NotFoundError
        from . import validation as v
        fields = {'provider', 'kind', 'application_id', 'check_run_id', 'check_id'}
        if not isinstance(reference, dict) or set(reference) != fields or reference.get('provider') != 'qorqut_agentic' or reference.get('kind') != 'check_receipt':
            raise AgenticError('invalid check receipt reference', code='invalid_request')
        v.uuid(reference['application_id'], 'application_id')
        v.uuid(reference['check_run_id'], 'check_run_id')
        v.string(reference['check_id'], 'check_id')
        with self.ctx.store.reader() as conn:
            row = conn.execute('SELECT application_id,record FROM overseer_application_events WHERE id=?', (reference['check_run_id'],)).fetchone()
        if not row or row['application_id'] != reference['application_id']:
            raise NotFoundError('check attempt not found')
        event = json.loads(row['record'])
        if event.get('type') not in ('check_run', 'check_run_reconciliation'):
            raise AgenticError('check attempt is incomplete', code='evidence_unavailable')
        run = event.get('check_run', {})
        if run.get('id') != reference['check_run_id'] or not isinstance(run.get('receipts'), list) or len(run['receipts']) > 101:
            raise AgenticError('invalid saved check attempt', code='evidence_unavailable')
        if (run.get('state') not in ('passed', 'failed', 'uncertain') or run.get('phase') not in ('pre_merge', 'post_merge')
            or not all(isinstance(item, dict) for item in run['receipts'])):
            raise AgenticError('invalid saved check attempt', code='evidence_unavailable')
        ids = [item.get('check_id') for item in run['receipts']]
        if any(not isinstance(item, str) or not item for item in ids) or len(ids) != len(set(ids)):
            raise AgenticError('duplicate or invalid check receipt IDs', code='evidence_unavailable')
        matches = [item for item in run['receipts'] if item.get('check_id') == reference['check_id']]
        if len(matches) != 1:
            if not matches:
                raise NotFoundError('check receipt not found')
            raise AgenticError('duplicate check receipts', code='evidence_unavailable')
        fields = ('check_id', 'kind', 'outcome', 'started_at', 'finished_at', 'exit_code', 'tool_receipt_ref', 'output_ref', 'diagnostic')
        if any(field not in matches[0] for field in fields) or any(field not in run for field in ('plan_revision', 'checked_commit', 'checked_tree')):
            raise AgenticError('saved check receipt is incomplete', code='evidence_unavailable')
        receipt = {field: matches[0][field] for field in fields}
        record = {'schema_version': 1, 'application_id': reference['application_id'], 'check_run_id': run['id'],
                  'phase': run['phase'], 'plan_revision': run['plan_revision'], 'checked_commit': run['checked_commit'],
                  'checked_tree': run['checked_tree'], 'run_state': run['state'], 'receipt': receipt}
        return {'reference': reference, 'content_hash': hashlib.sha256(canonical(record).encode('utf-8')).hexdigest(), 'record': record}

    def _check_layer(self, execution, phase):
        from ..layer import AgenticLayer
        if phase == 'post_merge':
            return AgenticLayer.open(self.ctx.root, self.ctx.store.state_dir, git_executable=self.ctx.git_executable,
                                     runtimes=self.ctx.runtimes, clock=self.ctx.clock, attribution_source=self.ctx.attribution_source)
        candidate = execution['candidate_commit']
        root = self._worktrees_root / execution['application_id'] / f"plan-{execution['plan_revision']}" / f'candidate-{candidate}'
        state = self._states_root / execution['application_id'] / f"plan-{execution['plan_revision']}-candidate-{candidate}"
        if Path(os.path.realpath(root)) != root or Path(os.path.realpath(state)) != state:
            raise AgenticError('candidate check paths contain symlinks', code='publication_recovery_required')
        git = GitRunner(self.ctx.git_executable, self.ctx.root, self.ctx.store.scratch_dir / 'git-home')
        if not root.exists():
            root.parent.mkdir(parents=True, exist_ok=True)
            git.run(['worktree', 'add', '--detach', str(root), candidate])
        candidate_git = GitRunner(self.ctx.git_executable, root, state / 'scratch' / 'git-home')
        if candidate_git.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip() != candidate:
            raise AgenticError('candidate check worktree changed', code='publication_recovery_required')
        from ..store import Store
        if Store(state).exists():
            return AgenticLayer.open(root, state, git_executable=self.ctx.git_executable, runtimes=self.ctx.runtimes,
                                     clock=self.ctx.clock, attribution_source=self.ctx.attribution_source)
        return AgenticLayer.initialize(root, state, git_executable=self.ctx.git_executable, runtimes=self.ctx.runtimes,
                                       clock=self.ctx.clock, attribution_source=self.ctx.attribution_source)

    def _check_clean(self, layer, commit, tree):
        git = GitRunner(self.ctx.git_executable, layer.root, layer._ctx.store.scratch_dir / 'git-home')
        if (git.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip() != commit
            or git.text(['rev-parse', '--verify', 'HEAD^{tree}']).strip() != tree
            or git.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout):
            raise AgenticError('checked tree changed', code='active_tree_changed')
        return git

    @_check_critical_section
    def record_checks(self, application_id, *, phase, expected_revision, operation_id):
        if phase not in ('pre_merge', 'post_merge'):
            raise AgenticError('invalid check phase', code='invalid_request')
        self._configured()
        payload = {'application_id': application_id, 'phase': phase, 'expected_revision': expected_revision}
        fingerprint = self._fingerprint('applications.record_checks', payload, operation_id)
        with self.ctx.store.lock(exclusive=True):
            with self.ctx.store.reader() as conn:
                replay = self._saved(conn, operation_id, fingerprint)
                if replay and replay.get('_state') not in ('candidate_reserved', 'check_running'):
                    if replay.get('execution'):
                        self._validate_execution(replay['execution'])
                    return replay
            self.ctx.require_no_recovery()
            with self.ctx.store.transaction() as conn:
                saved = self._saved(conn, operation_id, fingerprint)
                if saved and saved.get('_state') != 'check_running':
                    if saved.get('execution'):
                        self._validate_execution(saved['execution'])
                    return saved
                app, plan = self._current(conn, application_id, expected_revision)
                interrupted = bool(saved)
                if not saved:
                    if (phase == 'pre_merge' and app['phase'] != 'prepared') or (phase == 'post_merge' and app['phase'] != 'merged'):
                        raise AgenticError('checks are not allowed in this state', code='invalid_transition')
                    if app['waiting_input_ref'] or self._unresolved_need(conn, application_id):
                        raise AgenticError('application has an unresolved owner input', code='owner_input_required')
                    execution = self._last_execution(conn, application_id, app['plan_revision'])
                    if not execution or not execution.get('candidate_commit'):
                        raise AgenticError('candidate is missing', code='invalid_transition')
                    previous_failure = None
                    events = [json.loads(r['record']) for r in conn.execute(
                        'SELECT record FROM overseer_application_events WHERE application_id=? ORDER BY rowid', (application_id,))]
                    for item in reversed(events):
                        previous = item.get('check_run', {})
                        if (item.get('type') == 'check_run' and previous.get('phase') == phase
                            and previous.get('plan_revision') == app['plan_revision']
                            and previous.get('checked_commit') == execution['candidate_commit']):
                            if previous['state'] == 'passed':
                                raise AgenticError('these checks already passed for this candidate', code='invalid_transition')
                            previous_failure = item
                            break
                    retry = None
                    if previous_failure:
                        check_id = previous_failure['check_run']['id']
                        for item in reversed(events):
                            if item.get('type') == 'owner_authorized_retry' and item.get('check_run_id') == check_id:
                                retry = item
                                break
                        if (not retry or any(item.get('type') == 'retry_consumed' and
                                             item.get('response_key') == retry['response_key'] for item in events)):
                            raise AgenticError('an unused owner retry response is required', code='owner_input_required')
                    for row in conn.execute('SELECT result FROM overseer_operations'):
                        pending = json.loads(row['result'])
                        if pending.get('_state') == 'check_running' and pending.get('application_id') == application_id:
                            raise AgenticError('another check attempt is unresolved', code='invalid_transition')
                    check_run = {'id': str(uuid4()), 'phase': phase, 'plan_revision': app['plan_revision'],
                                 'checked_commit': execution['candidate_commit'], 'checked_tree': execution['candidate_tree'],
                                 'state': 'uncertain', 'receipts': []}
                    saved = {'_state': 'check_running', 'runner': _process_token(), 'application_id': application_id,
                             'execution': execution, 'check_run': check_run}
                    conn.execute('INSERT INTO overseer_operations VALUES (?,?,?,?)', (operation_id, fingerprint, canonical(saved), self.ctx.now()))
                    if retry:
                        self._event(conn, application_id, {'type': 'retry_consumed', 'response_key': retry['response_key'],
                                                          'previous_check_run_id': retry['check_run_id'], 'check_run_id': check_run['id']})
                    self._event(conn, application_id, {'type': 'check_intent', 'check_run': check_run})
        run, execution = saved['check_run'], saved['execution']
        checks = [check for check in plan['checks'] if check['phase'] == phase]
        reconciled = []
        if interrupted:
            completed = {item['check_id'] for item in run['receipts']}
            for item in run['receipts']:
                if item['outcome'] == 'uncertain':
                    item['diagnostic'] = 'Check execution was interrupted; effects are unknown.'
            for check in checks:
                if check['id'] not in completed:
                    run['receipts'].append({'check_id': check['id'], 'kind': check['kind'], 'outcome': 'unavailable',
                        'started_at': self.ctx.now(), 'finished_at': None, 'exit_code': None, 'tool_receipt_ref': None,
                        'output_ref': None, 'diagnostic': 'Check was not started before the attempt ended.'})
            try:
                layer = self._check_layer(execution, phase)
                self._check_clean(layer, run['checked_commit'], run['checked_tree'])
                with self.ctx.store.reader() as conn:
                    actor = admitted_run(conn, app['run_id'], 'application')['invocation']['executor']
                for index, receipt in enumerate(run['receipts'], 1):
                    if receipt['kind'] != 'tool' or receipt['outcome'] != 'uncertain':
                        continue
                    invocation = 'overseer-check-' + run['id'].replace('-', '') + '-' + str(index)
                    actual = layer.tools.receipt(invocation, actor=actor)['receipt']
                    definition = next(item for item in checks if item['id'] == receipt['check_id'])
                    entry = layer.catalog.read_entry(definition['tool_ref'])['entry']
                    if (actual['status'] == 'succeeded' and actual['effect_state'] == 'completed'
                        and actual['tool_ref'] == definition['tool_ref'] and actual['definition_revision'] == entry['revision']
                        and actual['code_revision'] == entry['code_revision']):
                        reconciled.append({'check_id': receipt['check_id'], 'invocation_id': invocation,
                                           'receipt': actual, 'outcome': 'passed'})
            except Exception:
                pass
        else:
            try:
                layer = self._check_layer(execution, phase)
                self._check_clean(layer, run['checked_commit'], run['checked_tree'])
                baseline_inventory = layer._ctx.tree.inventory()
                with self.ctx.store.reader() as conn:
                    admitted = admitted_run(conn, app['run_id'], 'application')
                actor = admitted['invocation']['executor']
                for check in checks:
                    receipt = {'check_id': check['id'], 'kind': check['kind'], 'outcome': 'uncertain',
                               'started_at': self.ctx.now(), 'finished_at': None, 'exit_code': None,
                               'tool_receipt_ref': None, 'output_ref': None, 'diagnostic': None}
                    run['receipts'].append(receipt)
                    with self.ctx.store.transaction() as conn:
                        conn.execute('UPDATE overseer_operations SET result=? WHERE operation_id=?', (canonical(saved), operation_id))
                        self._event(conn, application_id, {'type': 'check_started', 'check_run_id': run['id'], 'receipt': receipt})
                    self.ctx.hook('overseer_before_check', application_id=application_id, check_id=check['id'])
                    try:
                        self._check_clean(layer, run['checked_commit'], run['checked_tree'])
                        if layer._ctx.tree.inventory() != baseline_inventory:
                            raise AgenticError('checked files changed before check', code='active_tree_changed')
                        if check['kind'] == 'catalog':
                            from ..core import check_effects_scope
                            with check_effects_scope():
                                verdict = layer.catalog.validate(**check['arguments'])
                            receipt['outcome'] = 'passed' if verdict['valid'] else 'failed'
                            receipt['diagnostic'] = None if verdict['valid'] else str(verdict['issues'])[:2000]
                        elif check['kind'] == 'tool':
                            invocation = 'overseer-check-' + run['id'].replace('-', '') + '-' + str(len(run['receipts']))
                            from ..core import check_effects_scope
                            with check_effects_scope():
                                result = layer.tools.invoke(check['tool_ref'], check['arguments'], invocation, actor)['receipt']
                            self.ctx.hook('overseer_after_check_effect', application_id=application_id, check_id=check['id'])
                            receipt['tool_receipt_ref'] = {'invocation_id': invocation, 'receipt': result}
                            receipt['outcome'] = 'passed' if result.get('status') == 'succeeded' else 'failed'
                        else:
                            args = check['arguments']
                            timeout = args.get('timeout_seconds', 30)
                            maximum = args.get('max_output_chars', 16000)
                            if type(timeout) not in (int, float) or not 1 <= timeout <= 60 or type(maximum) is not int or not 1 <= maximum <= 64000 or set(args) - {'timeout_seconds', 'max_output_chars'}:
                                raise AgenticError('invalid command check limits', code='invalid_request')
                            from ..core import check_effects_scope
                            with check_effects_scope():
                                exit_code, output, diagnostic = _command(check['argv'], layer.root,
                                    GitRunner(self.ctx.git_executable, layer.root, layer._ctx.store.scratch_dir / 'git-home').env(), timeout, maximum)
                            receipt['exit_code'] = exit_code
                            receipt['outcome'] = 'passed' if exit_code == 0 and not diagnostic else 'failed'
                            receipt['diagnostic'] = diagnostic
                            with self.ctx.store.transaction() as conn:
                                event_id = str(uuid4())
                                conn.execute('INSERT INTO overseer_application_events VALUES (?,?,?,?)', (event_id, application_id,
                                    canonical({'type': 'check_output', 'output': output}), self.ctx.now()))
                            receipt['output_ref'] = {'provider': 'agentic', 'kind': 'application_event', 'id': event_id}
                        self._check_clean(layer, run['checked_commit'], run['checked_tree'])
                        if layer._ctx.tree.inventory() != baseline_inventory:
                            raise AgenticError('check changed authored files', code='active_tree_changed')
                    except Exception as exc:
                        receipt['outcome'] = 'unavailable'
                        receipt['diagnostic'] = f'{type(exc).__name__}: {exc}'[:2000]
                    receipt['finished_at'] = self.ctx.now()
                    with self.ctx.store.transaction() as conn:
                        conn.execute('UPDATE overseer_operations SET result=? WHERE operation_id=?', (canonical(saved), operation_id))
                    self.ctx.hook('overseer_after_check', application_id=application_id, check_id=check['id'])
                self._check_clean(layer, run['checked_commit'], run['checked_tree'])
                if layer._ctx.tree.inventory() != baseline_inventory:
                    raise AgenticError('checked files changed before receipt finalization', code='active_tree_changed')
            except Exception as exc:
                run['receipts'].append({'check_id': 'environment', 'kind': 'catalog', 'outcome': 'unavailable',
                    'started_at': self.ctx.now(), 'finished_at': self.ctx.now(), 'exit_code': None,
                    'tool_receipt_ref': None, 'output_ref': None, 'diagnostic': f'{type(exc).__name__}: {exc}'[:2000]})
        run['state'] = 'passed' if all(r['outcome'] == 'passed' for r in run['receipts']) else ('uncertain' if any(r['outcome'] == 'uncertain' for r in run['receipts']) else 'failed')
        known_passes = {r['check_id'] for r in reconciled}
        effective_passed = all(r['outcome'] == 'passed' or (r['outcome'] == 'uncertain' and r['check_id'] in known_passes) for r in run['receipts'])
        with self.ctx.store.lock(exclusive=True):
            with self.ctx.store.transaction() as conn:
                self._current(conn, application_id, expected_revision)
                need = None
                state = 'passed' if effective_passed else 'failed'
                next_phase = app['phase']
                if phase == 'post_merge' and state == 'passed':
                    tasks = json.loads(app['external_tasks'])
                    ready = {t['requirement_id'] for t in tasks if t['linked_plan_revision'] == app['plan_revision']
                             and (t.get('observation') or {}).get('state') == 'done'
                             and t['observation'].get('result_refs') and t['observation'].get('completion_summary')}
                    if all(r['id'] in ready for r in plan['external_requirements'] if r['required_for_completion']):
                        next_phase = 'complete'
                checks_record = {'state': state, 'phase': phase, 'receipts': run['receipts']}
                outcome = 'implemented' if next_phase == 'complete' else app['outcome']
                conn.execute('UPDATE delta_applications SET checks=?,phase=?,outcome=?,revision=revision+1,updated_at=? WHERE id=?',
                             (canonical(checks_record), next_phase, outcome, self.ctx.now(), application_id))
                if state == 'failed':
                    need = {'purpose': 'failure', 'request_key': f"failure:{application_id}:{run['id']}",
                            'application_id': application_id, 'application_revision': expected_revision + 1,
                            'plan_revision': app['plan_revision'], 'reason': f'{phase}_checks_failed',
                            'evidence_refs': [{'provider': 'agentic', 'kind': 'check_run', 'id': run['id']}],
                            'failure_evidence': {'check_run_id': run['id'], 'phase': phase,
                                                 'uncertain': run['state'] == 'uncertain'}}
                conn.execute('INSERT INTO overseer_application_events VALUES (?,?,?,?)',
                             (run['id'], application_id, canonical({'type': 'check_run', 'check_run': run, 'required_input': need}), self.ctx.now()))
                if reconciled:
                    self._event(conn, application_id, {'type': 'check_receipts_reconciled', 'check_run_id': run['id'],
                                'plan_revision': run['plan_revision'], 'checked_commit': run['checked_commit'],
                                'receipts': reconciled, 'effective_passed': effective_passed})
                    if effective_passed:
                        facts = {item['check_id']: item for item in reconciled}
                        factual_receipts = []
                        for original in run['receipts']:
                            item = dict(original)
                            if item['check_id'] in facts:
                                fact = facts[item['check_id']]
                                item.update(outcome='passed', finished_at=fact['receipt']['ended_at'],
                                            tool_receipt_ref={'invocation_id': fact['invocation_id'], 'receipt': fact['receipt']},
                                            diagnostic=None)
                            factual_receipts.append(item)
                        snapshot = {**run, 'id': str(uuid4()), 'state': 'passed', 'receipts': factual_receipts}
                        conn.execute('INSERT INTO overseer_application_events VALUES (?,?,?,?)',
                                     (snapshot['id'], application_id, canonical({'type': 'check_run_reconciliation',
                                      'check_run': snapshot, 'reconciles_check_run_id': run['id']}), self.ctx.now()))
                result = self._result(conn, application_id, execution=execution, check_run=run, required_input=need)
                conn.execute('UPDATE overseer_operations SET result=? WHERE operation_id=?', (canonical(result), operation_id))
                return result
