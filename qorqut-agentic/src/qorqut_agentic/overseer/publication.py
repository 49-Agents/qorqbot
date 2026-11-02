"""Isolated Git candidate creation and durable publication machinery."""
from __future__ import annotations

import hashlib
import json

from ..errors import AgenticError
from ..history import GitRunner
from ..util import check_operation_id
from .records import canonical


class PublicationExecutor:
    def _last_execution(self, conn, application_id, plan_revision):
        for row in conn.execute('SELECT record FROM overseer_application_events WHERE application_id=? ORDER BY rowid DESC', (application_id,)):
            event = json.loads(row['record'])
            execution = event.get('execution')
            if execution and execution['plan_revision'] == plan_revision:
                self._validate_execution(execution)
                return execution
        return None

    def _fingerprint(self, name, payload, operation_id):
        check_operation_id(operation_id)
        if operation_id.lower().startswith('run:'):
            raise AgenticError('reserved operation ID', code='invalid_request')
        return hashlib.sha256(canonical({'operation': name, 'payload': payload}).encode()).hexdigest()

    def _saved(self, conn, operation_id, fingerprint):
        row = conn.execute('SELECT fingerprint,result FROM overseer_operations WHERE operation_id=?', (operation_id,)).fetchone()
        if not row:
            return None
        if row['fingerprint'] != fingerprint:
            raise AgenticError('operation ID was used for different input', code='operation_conflict')
        return json.loads(row['result'])

    def prepare_merge(self, application_id, *, expected_revision, operation_id):
        self._configured()
        payload = {'application_id': application_id, 'expected_revision': expected_revision}
        fingerprint = self._fingerprint('applications.prepare_merge', payload, operation_id)
        with self.ctx.store.lock(exclusive=True):
            with self.ctx.store.reader() as conn:
                replay = self._saved(conn, operation_id, fingerprint)
                if replay and replay.get('_state') not in ('candidate_reserved', 'check_running'):
                    if replay.get('execution'):
                        self._validate_execution(replay['execution'])
                    return replay
            self.ctx.require_no_check_mutation()
            self.ctx.require_no_recovery()
            with self.ctx.store.transaction() as conn:
                saved = self._saved(conn, operation_id, fingerprint)
                if saved and saved.get('_state') != 'candidate_reserved':
                    if saved.get('execution'):
                        self._validate_execution(saved['execution'])
                    return saved
                app, plan = self._current(conn, application_id, expected_revision)
                if app['phase'] not in ('working', 'prepared') or app['waiting_input_ref'] or self._unresolved_need(conn, application_id):
                    raise AgenticError('application is not ready to prepare a candidate', code='invalid_transition')
                self._executable_plan(plan)
                for operation_row in conn.execute('SELECT result FROM overseer_operations'):
                    attempt = json.loads(operation_row['result'])
                    if attempt.get('_state') == 'check_running' and attempt.get('application_id') == application_id:
                        raise AgenticError('a check has unresolved effects', code='invalid_transition')
                if self._uncertain_precheck(conn, application_id):
                    raise AgenticError('pre-merge check effects remain uncertain', code='invalid_transition')
                execution = self._last_execution(conn, application_id, app['plan_revision'])
                if not execution:
                    raise AgenticError('owned draft is missing', code='invalid_transition')
                if saved is None:
                    saved = {'_state': 'candidate_reserved', 'application_id': application_id, 'execution': execution}
                    conn.execute('INSERT INTO overseer_operations VALUES (?,?,?,?)', (operation_id, fingerprint, canonical(saved), self.ctx.now()))
                    self._event(conn, application_id, {'type': 'candidate_intent', 'operation_id': operation_id, 'execution': execution})
            from pathlib import Path
            active = GitRunner(self.ctx.git_executable, self.ctx.root, self.ctx.store.scratch_dir / 'git-home')
            from ..layer import AgenticLayer
            draft_layer = AgenticLayer.open(execution['worktree_path'], execution['state_path'], git_executable=self.ctx.git_executable)
            with draft_layer._ctx.store.lock(exclusive=True):
                draft_layer._ctx.require_no_recovery()
                draft = GitRunner(self.ctx.git_executable, execution['worktree_path'], Path(execution['state_path']) / 'scratch' / 'git-home')
                if active.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout:
                    raise AgenticError('active index or worktree is dirty', code='active_tree_changed')
                base = active.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip()
                if draft.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout:
                    raise AgenticError('draft has uncommitted changes', code='invalid_transition')
                if draft.text(['symbolic-ref', '--short', 'HEAD']).strip() != execution['branch']:
                    raise AgenticError('draft branch changed', code='publication_recovery_required')
                draft_head = draft.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip()
                if draft.run(['merge-base', '--is-ancestor', execution['base_commit'], draft_head], check=False).returncode != 0:
                    raise AgenticError('draft no longer descends from its agreed base', code='invalid_transition')
                if active.run(['merge-base', '--is-ancestor', execution['base_commit'], base], check=False).returncode != 0:
                    raise AgenticError('active history no longer descends from agreed base', code='active_tree_changed')
                if 'draft_head' in saved:
                    if saved['draft_head'] != draft_head or saved['integration_base'] != base:
                        raise AgenticError('candidate inputs changed after reservation', code='operation_conflict')
                else:
                    saved['draft_head'], saved['integration_base'] = draft_head, base
                    with self.ctx.store.transaction() as conn:
                        self._current(conn, application_id, expected_revision)
                        conn.execute('UPDATE overseer_operations SET result=? WHERE operation_id=?', (canonical(saved), operation_id))
                merge = active.run(['merge-tree', '--write-tree', base, draft_head], check=False)
                if merge.returncode != 0:
                    need = {'purpose': 'clarify', 'request_key': f'integration:{application_id}:{operation_id}',
                            'application_id': application_id, 'application_revision': expected_revision,
                            'plan_revision': app['plan_revision'], 'reason': 'integration_conflict',
                            'evidence_refs': []}
                    with self.ctx.store.transaction() as conn:
                        self._current(conn, application_id, expected_revision)
                        self._event(conn, application_id, {'type': 'integration_conflict', 'required_input': need,
                            'diagnostic': merge.stdout.decode('utf-8', 'replace')[:2000]})
                        result = self._result(conn, application_id, execution=execution, required_input=need)
                        conn.execute('UPDATE overseer_operations SET result=? WHERE operation_id=?', (canonical(result), operation_id))
                    return result
                tree = merge.stdout.decode().splitlines()[0].strip()
                raw = active.run(['diff-tree', '--no-commit-id', '--name-status', '-r', '-z', '--no-renames', base, tree]).stdout
                fields = raw.decode('utf-8', 'strict').split('\0')
                observed = {fields[i + 1]: fields[i] for i in range(0, len(fields) - 1, 2)}
                expected = {c['path']: {'create': 'A', 'edit': 'M', 'delete': 'D'}[c['action']] for c in plan['changes']}
                if observed != expected:
                    raise AgenticError('candidate changes do not match the agreed paths and actions', code='owner_input_required',
                                       details={'reason': 'plan_not_executable', 'expected': expected, 'observed': observed})
                n = app['plan_revision']
                ref = f'refs/heads/overseer-candidate/{application_id}/plan-{n}'
                archive = f'refs/overseer/candidates/{application_id}/plan-{n}/{hashlib.sha256(operation_id.encode()).hexdigest()}'
                old = active.text(['rev-parse', '--verify', '--quiet', ref], check=False).strip()
                archived = active.text(['rev-parse', '--verify', '--quiet', archive], check=False).strip()
                if archived:
                    candidate = archived
                    parents = active.text(['rev-list', '--parents', '-n', '1', candidate]).strip().split()
                    recorded_tree = active.text(['rev-parse', f'{candidate}^{{tree}}']).strip()
                    message = active.text(['log', '-1', '--format=%B', candidate])
                    if parents != [candidate, base] or recorded_tree != tree or f'Delta-ID: {app["delta_id"]}' not in message:
                        raise AgenticError('candidate retry no longer matches', code='publication_recovery_required')
                else:
                    if old:
                        with self.ctx.store.reader() as conn:
                            prior = self._last_execution(conn, application_id, n)
                        if not prior or prior.get('candidate_commit') != old:
                            raise AgenticError('candidate branch already exists', code='operation_conflict')
                    message = f'Apply accepted Overseer delta\n\nDelta-ID: {app["delta_id"]}\n'
                    candidate = active.text(['commit-tree', tree, '-p', base], input=message.encode()).strip()
                    active.run(['update-ref', archive, candidate, '0' * len(candidate)])
                    self.ctx.hook('overseer_after_candidate_archive', application_id=application_id)
                active.run(['update-ref', ref, candidate, old or '0' * len(candidate)])
                self.ctx.hook('overseer_after_candidate_ref', application_id=application_id)
                if active.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip() != base or active.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout:
                    raise AgenticError('active tree changed during integration', code='active_tree_changed')
                if draft.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip() != draft_head or draft.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout:
                    raise AgenticError('draft changed during integration', code='active_tree_changed')
                execution.update(integration_base=base, candidate_commit=candidate, candidate_tree=tree, stage='prepared')
                with self.ctx.store.transaction() as conn:
                    self._current(conn, application_id, expected_revision)
                    conn.execute("UPDATE delta_applications SET phase='prepared',revision=revision+1,updated_at=? WHERE id=?", (self.ctx.now(), application_id))
                    self._event(conn, application_id, {'type': 'candidate', 'execution': execution, 'draft_head': draft_head})
                    result = self._result(conn, application_id, execution=execution)
                    conn.execute('UPDATE overseer_operations SET result=? WHERE operation_id=?', (canonical(result), operation_id))
                    return result

    def _publication_files(self, git, base, candidate):
        from ..util import sha256_hex
        raw = git.run(['diff-tree', '--no-commit-id', '--name-status', '-r', '-z', '--no-renames', base, candidate]).stdout
        fields = raw.decode('utf-8', 'strict').split('\0')
        files = []
        for i in range(0, len(fields) - 1, 2):
            status, path = fields[i], fields[i + 1]
            from ..files import split_path
            split_path(path)
            def content(revision):
                record = git.run(['ls-tree', '-z', revision, '--', path]).stdout
                if not record:
                    return None
                meta, _, name = record.rstrip(b'\0').partition(b'\t')
                mode, kind, oid = meta.decode().split(' ')
                if name.decode() != path or kind != 'blob' or mode not in ('100644', '100755'):
                    raise AgenticError('publication includes a non-regular file', code='unsafe_path')
                return git.run(['cat-file', 'blob', oid]).stdout
            before, after = content(base), content(candidate)
            files.append({'path': path, 'action': {'A': 'create', 'M': 'edit', 'D': 'delete'}[status],
                          'before_sha256': sha256_hex(before) if before is not None else None,
                          'after_sha256': sha256_hex(after) if after is not None else None,
                          'size': len(after) if after is not None else 0})
        return files

    def publish(self, application_id, *, expected_revision, operation_id):
        from ..changes import Changes, EXTERNAL_ATTRIBUTION, entries_touching
        from ..util import new_id
        self._configured()
        payload = {'application_id': application_id, 'expected_revision': expected_revision}
        fingerprint = self._fingerprint('applications.publish', payload, operation_id)
        with self.ctx.store.lock(exclusive=True):
            with self.ctx.store.reader() as conn:
                saved = self._saved(conn, operation_id, fingerprint)
                if saved:
                    if saved.get('_state') == 'publication_reserved':
                        raise AgenticError('publication requires recovery', code='publication_recovery_required')
                    if saved.get('execution'):
                        self._validate_execution(saved['execution'])
                    return saved
            self.ctx.require_no_check_mutation()
            self.ctx.require_no_recovery()
            with self.ctx.store.reader() as conn:
                app, plan = self._current(conn, application_id, expected_revision)
                if app['phase'] != 'prepared' or app['waiting_input_ref'] or self._unresolved_need(conn, application_id):
                    raise AgenticError('application is not prepared for publication', code='invalid_transition')
                execution = self._last_execution(conn, application_id, app['plan_revision'])
                if not execution or not execution.get('candidate_commit'):
                    raise AgenticError('candidate missing', code='invalid_transition')
                for row in conn.execute('SELECT result FROM overseer_operations'):
                    pending = json.loads(row['result'])
                    if pending.get('_state') == 'check_running' and pending.get('application_id') == application_id:
                        raise AgenticError('a check has unresolved effects', code='invalid_transition')
                if self._uncertain_precheck(conn, application_id):
                    raise AgenticError('pre-merge check effects remain uncertain', code='invalid_transition')
                pre_checks = [c for c in plan['checks'] if c['phase'] == 'pre_merge']
                if pre_checks:
                    passed = False
                    events = [json.loads(row['record']) for row in conn.execute('SELECT record FROM overseer_application_events WHERE application_id=? ORDER BY rowid DESC', (application_id,))]
                    reconciled = {event['check_run_id'] for event in events if event.get('type') == 'check_receipts_reconciled' and event.get('effective_passed')}
                    for record in events:
                        check = record.get('check_run', {})
                        if record.get('type') == 'check_run' and check.get('phase') == 'pre_merge' and check.get('plan_revision') == app['plan_revision'] and check.get('checked_commit') == execution['candidate_commit']:
                            passed = (check.get('state') == 'passed' or check['id'] in reconciled) and {r['check_id'] for r in check['receipts']} == {c['id'] for c in pre_checks}
                            break
                    if not passed:
                        raise AgenticError('candidate has not passed its pre-merge checks', code='invalid_transition')
            git = GitRunner(self.ctx.git_executable, self.ctx.root, self.ctx.store.scratch_dir / 'git-home')
            candidate, base = execution['candidate_commit'], execution['integration_base']
            if (git.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip() != base
                or git.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout):
                raise AgenticError('active head or worktree changed', code='active_tree_changed')
            candidate_ref = f"refs/heads/overseer-candidate/{application_id}/plan-{app['plan_revision']}"
            if (git.text(['rev-parse', '--verify', '--quiet', candidate_ref], check=False).strip() != candidate
                or git.text(['rev-parse', f'{candidate}^{{tree}}']).strip() != execution['candidate_tree']
                or git.text(['rev-list', '--parents', '-n', '1', candidate]).split() != [candidate, base]
                or f"Delta-ID: {app['delta_id']}" not in git.text(['log', '-1', '--format=%B', candidate])):
                raise AgenticError('candidate changed', code='publication_recovery_required')
            changes = Changes(self.ctx)
            changes.scan_locked(dict(EXTERNAL_ATTRIBUTION))
            files = self._publication_files(git, base, candidate)
            before_entries = entries_touching(self.ctx.load_catalog_locked(), {item['path'] for item in files})
            change_id = new_id('chg')
            execution['change_id'] = change_id
            execution['stage'] = 'publishing'
            reservation = {'_state': 'publication_reserved', 'application_id': application_id, 'execution': execution}
            with self.ctx.store.transaction() as conn:
                self._current(conn, application_id, expected_revision)
                conn.execute('INSERT INTO overseer_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                             (application_id, operation_id, change_id, base, candidate, execution['candidate_tree'],
                              execution['branch'], execution['worktree_path'], execution['state_path'], canonical(files), 'publishing', '[]'))
                conn.execute('INSERT INTO overseer_operations VALUES (?,?,?,?)', (operation_id, fingerprint, canonical(reservation), self.ctx.now()))
                self._event(conn, application_id, {'type': 'publication_intent', 'execution': execution, 'before_entries': before_entries})
            self.ctx.hook('overseer_after_publication_intent', application_id=application_id)
            try:
                return self._finish_publication(application_id, expected_revision, operation_id, execution, files)
            except Exception as exc:
                self._record_publication_error(operation_id, exc)
                raise

    def _record_publication_error(self, operation_id, error):
        with self.ctx.store.transaction() as conn:
            row = conn.execute('SELECT observed_errors FROM overseer_publications WHERE operation_id=?', (operation_id,)).fetchone()
            if row:
                errors = json.loads(row['observed_errors'])
                errors.append({'at': self.ctx.now(), 'error': f'{type(error).__name__}: {error}'[:1000]})
                conn.execute('UPDATE overseer_publications SET observed_errors=? WHERE operation_id=?', (canonical(errors[-20:]), operation_id))

    def _finish_publication(self, application_id, expected_revision, publication_operation, execution, files, recovery_operation=None, recovery_fingerprint=None):
        from ..changes import Changes, EXTERNAL_ATTRIBUTION, entries_touching, insert_event
        from .runs import admitted_run
        git = GitRunner(self.ctx.git_executable, self.ctx.root, self.ctx.store.scratch_dir / 'git-home')
        base, candidate = execution['integration_base'], execution['candidate_commit']
        head = git.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip()
        dirty = git.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout
        if head == base:
            if dirty:
                raise AgenticError('active worktree changed before publication', code='publication_recovery_required')
            try:
                git.run(['merge', '--ff-only', '--no-edit', '--no-overwrite-ignore', candidate])
            except AgenticError as exc:
                raise AgenticError('fast-forward was interrupted or refused; inspect publication recovery',
                                   code='publication_recovery_required', details={'cause': str(exc)[:1000]}) from exc
            self.ctx.hook('overseer_after_publication_git', application_id=application_id)
            head = git.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip()
        if git.run(['merge-base', '--is-ancestor', candidate, head], check=False).returncode != 0:
            raise AgenticError('active history does not contain recorded candidate', code='publication_recovery_required')
        dirty = git.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout
        staged = set(git.run(['diff', '--cached', '--name-only', '-z', '--no-renames']).stdout.decode('utf-8', 'strict').split('\0'))
        from ..util import sha256_hex
        for item in files:
            path = item['path']
            if path in staged:
                raise AgenticError('affected active file is staged', code='publication_recovery_required', details={'path': path})
            entry = git.run(['ls-tree', '-z', head, '--', path]).stdout
            if entry:
                meta, _, name = entry.rstrip(b'\0').partition(b'\t')
                mode, kind, oid = meta.decode().split(' ')
                if name.decode() != path or kind != 'blob' or mode not in ('100644', '100755'):
                    raise AgenticError('affected active path is no longer a regular file', code='publication_recovery_required', details={'path': path})
                expected = sha256_hex(git.run(['cat-file', 'blob', oid]).stdout)
            else:
                expected = None
            kind = self.ctx.tree.kind(path)
            actual = self.ctx.tree.digest(path).sha256 if kind == 'file' else None
            if kind not in ('file', 'missing') or actual != expected:
                raise AgenticError('affected active file has unrecorded edits', code='publication_recovery_required', details={'path': path})
        self.ctx.hook('overseer_before_publication_finalize', application_id=application_id)
        with self.ctx.store.transaction() as conn:
            app, _ = self._current(conn, application_id, expected_revision)
            if app['phase'] != 'prepared':
                raise AgenticError('application changed during publication', code='publication_recovery_required')
            admission = admitted_run(conn, app['run_id'], 'application')
            actor = admission['invocation']['executor']
            attribution = {'actor_id': actor['actor_id'], 'actor_kind': actor['actor_kind'],
                           'reason': 'Apply accepted Overseer delta', 'source_refs': [f"delta:{app['delta_id']}"],
                           'trust': self.ctx.attribution_source}
            prior = conn.execute('SELECT 1 FROM changes WHERE change_id=?', (execution['change_id'],)).fetchone()
            if not prior:
                catalog = self.ctx.load_catalog_locked()
                before_entries = []
                for event_row in conn.execute('SELECT record FROM overseer_application_events WHERE application_id=? ORDER BY rowid DESC', (application_id,)):
                    event = json.loads(event_row['record'])
                    if event.get('type') == 'publication_intent' and event.get('execution', {}).get('change_id') == execution['change_id']:
                        before_entries = event.get('before_entries', [])
                        break
                entry_refs = sorted(set(before_entries) | set(entries_touching(catalog, {f['path'] for f in files})))
                insert_event(conn, change_id=execution['change_id'], created_at=self.ctx.now(), kind='overseer_publication',
                             attribution=attribution, files=files, entry_refs=entry_refs,
                             details={'application_id': application_id, 'delta_id': app['delta_id'], 'candidate_commit': candidate})
                conn.execute('UPDATE change_files SET commit_id=? WHERE change_id=?', (candidate, execution['change_id']))
            merge = {'base': base, 'candidate': candidate, 'merged_commit': candidate, 'change_id': execution['change_id']}
            execution['stage'] = 'merged'
            conn.execute("UPDATE delta_applications SET phase='merged',merge=?,revision=revision+1,updated_at=? WHERE id=?",
                         (canonical(merge), self.ctx.now(), application_id))
            conn.execute("UPDATE overseer_publications SET stage='merged' WHERE operation_id=?", (publication_operation,))
            self._event(conn, application_id, {'type': 'publication_merged', 'execution': execution})
            result = self._result(conn, application_id, execution=execution)
            conn.execute('UPDATE overseer_operations SET result=? WHERE operation_id=?', (canonical(result), publication_operation))
            if recovery_operation:
                conn.execute('INSERT INTO overseer_operations VALUES (?,?,?,?)',
                             (recovery_operation, recovery_fingerprint, canonical(result), self.ctx.now()))
        self.ctx.hook('overseer_after_publication_finalize', application_id=application_id)
        if head != candidate or dirty:
            Changes(self.ctx).scan_locked(dict(EXTERNAL_ATTRIBUTION))
        return result

    def recovery_status(self, application_id):
        from . import validation as v
        v.uuid(application_id, 'application_id')
        self._configured()
        with self.ctx.store.reader() as conn:
            app = conn.execute('SELECT * FROM delta_applications WHERE id=?', (application_id,)).fetchone()
            if not app:
                from ..errors import NotFoundError
                raise NotFoundError('application not found')
            row = conn.execute('SELECT * FROM overseer_publications WHERE application_id=? ORDER BY rowid DESC LIMIT 1', (application_id,)).fetchone()
            execution = self._last_execution(conn, application_id, app['plan_revision']) if app['plan_revision'] else None
            if row and row['stage'] not in ('prepared', 'merged', 'complete', 'superseded'):
                reserved = conn.execute('SELECT result FROM overseer_operations WHERE operation_id=?', (row['operation_id'],)).fetchone()
                execution = json.loads(reserved['result'])['execution'] if reserved else None
                if execution:
                    self._validate_execution(execution)
                diagnostics = json.loads(row['observed_errors'])
                return {'application_id': application_id, 'recovery_required': True, 'stage': row['stage'],
                        'execution': execution, 'diagnostics': diagnostics, 'allowed_actions': ['recover']}
            return {'application_id': application_id, 'recovery_required': False,
                    'stage': execution['stage'] if execution else app['phase'], 'execution': execution,
                    'diagnostics': [], 'allowed_actions': []}

    def recover(self, application_id, *, expected_revision, operation_id):
        self._configured()
        payload = {'application_id': application_id, 'expected_revision': expected_revision}
        fingerprint = self._fingerprint('applications.recover', payload, operation_id)
        with self.ctx.store.lock(exclusive=True):
            with self.ctx.store.reader() as conn:
                saved = self._saved(conn, operation_id, fingerprint)
                if saved:
                    if saved.get('execution'):
                        self._validate_execution(saved['execution'])
                    return saved
                self.ctx.require_no_check_mutation()
                app, _ = self._current(conn, application_id, expected_revision)
                row = conn.execute("SELECT * FROM overseer_publications WHERE application_id=? AND stage NOT IN ('prepared','merged','complete','superseded') ORDER BY rowid DESC LIMIT 1", (application_id,)).fetchone()
                if not row:
                    raise AgenticError('no interrupted publication for application', code='invalid_transition')
                reserved = conn.execute('SELECT result FROM overseer_operations WHERE operation_id=?', (row['operation_id'],)).fetchone()
                if not reserved:
                    raise AgenticError('publication intent is missing', code='publication_recovery_required')
                execution = json.loads(reserved['result'])['execution']
                self._validate_execution(execution)
                files = json.loads(row['file_hashes'])
                if (row['change_id'] != execution['change_id'] or row['expected_base'] != execution['integration_base']
                    or row['candidate_commit'] != execution['candidate_commit'] or row['candidate_tree'] != execution['candidate_tree']
                    or row['branch_ref'] != execution['branch'] or row['worktree_ref'] != execution['worktree_path']
                    or row['state_ref'] != execution['state_path']):
                    raise AgenticError('publication intent does not match execution', code='publication_recovery_required')
            git = GitRunner(self.ctx.git_executable, self.ctx.root, self.ctx.store.scratch_dir / 'git-home')
            if (git.text(['rev-parse', f"{execution['candidate_commit']}^{{tree}}"]).strip() != execution['candidate_tree']
                or git.text(['rev-list', '--parents', '-n', '1', execution['candidate_commit']]).split() != [execution['candidate_commit'], execution['integration_base']]
                or self._publication_files(git, execution['integration_base'], execution['candidate_commit']) != files):
                raise AgenticError('publication candidate or file hashes changed', code='publication_recovery_required')
            try:
                return self._finish_publication(application_id, expected_revision, row['operation_id'], execution, files,
                                                recovery_operation=operation_id, recovery_fingerprint=fingerprint)
            except Exception as exc:
                self._record_publication_error(row['operation_id'], exc)
                raise
