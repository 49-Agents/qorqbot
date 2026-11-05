"""Actual current-state inspection for owner-confirmed satisfied work."""
from __future__ import annotations

import json
import re
from uuid import uuid4

from ..errors import AgenticError
from ..files import split_path
from ..history import GitRunner
from ..util import sha256_hex
from . import validation as v
from .appstate import _application
from .records import canonical, mutate
from .runs import admitted_run


def inspect_current(ctx, app, evidence, observations=None):
    """Read only the recorded subjects, without trusting caller-supplied hashes."""
    git = GitRunner(ctx.git_executable, ctx.root, ctx.store.scratch_dir / 'git-home')
    commit = git.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip()
    if git.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout:
        raise AgenticError('active tree is not clean', code='active_tree_changed')
    hashes = {}
    for path in evidence['resource_paths']:
        split_path(path)
        if ctx.tree.kind(path) != 'file':
            raise AgenticError('inspected resource is unavailable', code='evidence_unavailable', details={'path': path})
        entry = git.run(['ls-tree', '-z', commit, '--', path]).stdout
        if not entry:
            raise AgenticError('inspected resource is not in the active commit', code='evidence_unavailable', details={'path': path})
        meta, _, name = entry.rstrip(b'\0').partition(b'\t')
        mode, kind, oid = meta.decode().split(' ')
        if name.decode() != path or kind != 'blob' or mode not in ('100644', '100755'):
            raise AgenticError('inspected resource is not a regular committed file', code='evidence_unavailable', details={'path': path})
        digest = ctx.tree.digest(path).sha256
        if digest != sha256_hex(git.run(['cat-file', 'blob', oid]).stdout):
            raise AgenticError('inspected resource differs from active commit', code='stale_revision', details={'path': path})
        hashes[path] = digest
    gateway = getattr(ctx, 'satisfaction_gateway', None)
    resolved = []
    for kind, refs in (('source', evidence['source_refs']), ('check_receipt', evidence['check_receipt_refs'])):
        if refs and gateway is None:
            raise AgenticError('trusted satisfaction gateway required', code='evidence_unavailable')
        for ref in refs:
            try:
                item = gateway.resolve_satisfaction_reference(ref, kind=kind)
            except Exception as exc:
                raise AgenticError('satisfaction reference is unavailable', code='evidence_unavailable') from exc
            if (not isinstance(item, dict) or set(item) != {'reference', 'content_hash', 'outcome', 'checked_commit'}
                or item['reference'] != ref or not isinstance(item['content_hash'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', item['content_hash'])):
                raise AgenticError('satisfaction reference did not resolve exactly', code='evidence_unavailable')
            if kind == 'check_receipt' and (item['outcome'] != 'passed' or item['checked_commit'] != commit):
                raise AgenticError('check receipt is failed, uncertain or stale', code='evidence_unavailable')
            resolved.append({'kind': kind, **item})
    if observations is not None and (commit != observations['evidence']['active_commit']
        or hashes != observations['evidence']['resource_hashes'] or resolved != observations['resolved']):
        raise AgenticError('inspected subjects changed', code='stale_revision')
    # Detect changes during external reference resolution.
    if (git.text(['rev-parse', '--verify', 'HEAD^{commit}']).strip() != commit
        or git.run(['status', '--porcelain=v1', '-z', '--untracked-files=all']).stdout):
        raise AgenticError('active tree changed during inspection', code='stale_revision')
    for path, digest in hashes.items():
        if ctx.tree.kind(path) != 'file' or ctx.tree.digest(path).sha256 != digest:
            raise AgenticError('inspected resource changed', code='stale_revision')
    return commit, hashes, resolved


def load_inspection(conn, inspection_id):
    v.uuid(inspection_id, 'satisfaction_inspection_id')
    row = conn.execute('SELECT record FROM overseer_application_events WHERE id=?', (inspection_id,)).fetchone()
    if not row:
        raise AgenticError('satisfaction inspection not found', code='evidence_unavailable')
    record = json.loads(row['record'])
    if record.get('type') != 'satisfaction_inspection':
        raise AgenticError('invalid satisfaction inspection', code='evidence_unavailable')
    return record


class SatisfactionExecutor:
    def inspect_satisfaction(self, application_id, *, resource_paths=None, source_refs=None, check_receipt_refs=None,
                             explanation, expected_revision, operation_id):
        self._configured()
        v.uuid(application_id, 'application_id')
        resource_paths = [] if resource_paths is None else resource_paths
        source_refs = [] if source_refs is None else source_refs
        check_receipt_refs = [] if check_receipt_refs is None else check_receipt_refs
        for name, values in (('resource_paths', resource_paths), ('source_refs', source_refs), ('check_receipt_refs', check_receipt_refs)):
            if not isinstance(values, list) or len(values) > 100:
                v.invalid(name)
        for path in resource_paths:
            split_path(path)
        if len(set(resource_paths)) != len(resource_paths) or not (resource_paths or source_refs or check_receipt_refs):
            v.invalid('satisfaction evidence')
        for ref in source_refs + check_receipt_refs:
            if not isinstance(ref, dict) or not ref:
                v.invalid('satisfaction reference')
        if not isinstance(explanation, str) or not explanation.strip() or len(explanation) > 8000:
            v.invalid('explanation')
        payload = {'application_id': application_id, 'resource_paths': resource_paths, 'source_refs': source_refs,
                   'check_receipt_refs': check_receipt_refs, 'explanation': explanation, 'expected_revision': expected_revision}
        fingerprint = self._fingerprint('applications.inspect_satisfaction', payload, operation_id)
        with self.ctx.store.lock(exclusive=True):
            with self.ctx.store.reader() as conn:
                saved = self._saved(conn, operation_id, fingerprint)
                if saved:
                    return saved
                self.ctx.require_no_check_mutation()
                app = self._inspection_app(conn, application_id, expected_revision)
            self.ctx.require_no_recovery()
            subjects = {'resource_paths': resource_paths, 'source_refs': source_refs, 'check_receipt_refs': check_receipt_refs}
            commit, hashes, resolved = inspect_current(self.ctx, app, subjects)
            evidence = {'observed_at': self.ctx.now(), 'active_commit': commit, 'resource_hashes': hashes,
                        'source_refs': source_refs, 'check_receipt_refs': check_receipt_refs, 'explanation': explanation}
            inspection = {'id': str(uuid4()), 'application_id': application_id, 'application_revision': expected_revision,
                          'delta_id': app['delta_id'], 'accepted_revision': app['accepted_revision'], 'evidence': evidence}
            def write(conn):
                current = self._inspection_app(conn, application_id, expected_revision)
                record = {'type': 'satisfaction_inspection', 'inspection': inspection, 'resolved': resolved}
                conn.execute('INSERT INTO overseer_application_events VALUES (?,?,?,?)',
                             (inspection['id'], application_id, canonical(record), self.ctx.now()))
                return {'application': _application(current), 'inspection': inspection}
            return mutate(self.ctx, operation_id, 'applications.inspect_satisfaction', payload, write)

    def _inspection_app(self, conn, application_id, expected_revision):
        from .support import blocking_inputs
        app = conn.execute('SELECT * FROM delta_applications WHERE id=?', (application_id,)).fetchone()
        if app is None:
            from ..errors import NotFoundError
            raise NotFoundError('application not found')
        if type(expected_revision) is not int or app['revision'] != expected_revision:
            raise AgenticError('application revision changed', code='stale_revision')
        delta = conn.execute('SELECT * FROM deltas WHERE id=?', (app['delta_id'],)).fetchone()
        run = conn.execute('SELECT control,completed_at,cancelled_at FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()
        if (app['phase'] != 'planning' or app['merge'] is not None or delta['decision'] != 'accepted'
            or delta['accepted_revision'] != app['accepted_revision'] or delta['current_revision'] != app['accepted_revision']
            or not admitted_run(conn, app['run_id'], 'application') or not run or run['control'] != 'running'
            or run['completed_at'] or run['cancelled_at']):
            raise AgenticError('current admitted planning application required', code='invalid_transition')
        if app['waiting_input_ref'] or self._unresolved_need(conn, application_id) or blocking_inputs(conn, app['delta_id']) or self._uncertain_precheck(conn, application_id):
            raise AgenticError('application has an unresolved hold', code='owner_input_required')
        self.ctx.guard_overseer_admission(conn, application_id=application_id)
        return app
