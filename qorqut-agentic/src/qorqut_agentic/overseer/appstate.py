"""Read access to dormant applications and saved plans."""
import base64
import json

from ..errors import InvalidRequestError, NotFoundError
from . import validation as v


def _application(row):
    data = dict(row)
    for key in ('acceptance_ref', 'invocation_ref', 'agreement_ref', 'waiting_input_ref', 'external_tasks', 'merge', 'checks', 'satisfaction_evidence'):
        if data[key] is not None:
            data[key] = json.loads(data[key])
    data['schema_version'] = 1
    return data


class ApplicationState:
    def __init__(self, ctx):
        self.ctx = ctx

    def get(self, id: str, plan_revision: int | None = None):
        v.uuid(id, 'id')
        if plan_revision is not None and (type(plan_revision) is not int or plan_revision < 1):
            v.invalid('plan_revision')
        with self.ctx.store.reader() as conn:
            row = conn.execute('SELECT * FROM delta_applications WHERE id=?', (id,)).fetchone()
            if row is None:
                raise NotFoundError('application not found')
            revision = plan_revision if plan_revision is not None else row['plan_revision']
            plan = None
            if revision is not None:
                saved = conn.execute('SELECT record FROM overseer_plan_revisions WHERE application_id=? AND revision=?', (id, revision)).fetchone()
                if saved is None:
                    raise NotFoundError('plan revision not found')
                plan = json.loads(saved['record'])
            return {**_application(row), 'plan': plan}

    def list(self, limit: int = 50, cursor: str | None = None):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidRequestError('limit must be between 1 and 200')
        after = ''
        if cursor is not None:
            try:
                after = base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True).decode('ascii')
                v.uuid(after, 'cursor')
            except Exception as exc:
                raise InvalidRequestError('invalid cursor') from exc
        with self.ctx.store.reader() as conn:
            rows = conn.execute('SELECT * FROM delta_applications WHERE id > ? ORDER BY id LIMIT ?', (after, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = base64.urlsafe_b64encode(rows[-1]['id'].encode()).decode() if more else None
        return {'items': [_application(row) for row in rows], 'next_cursor': next_cursor, 'complete': not more, 'availability': 'available'}

    def save_plan(self, application_id: str, plan: dict, *, expected_revision: int, operation_id: str):
        import re
        from ..errors import AgenticError
        from .deltas import _delta, _revision
        from .records import canonical, mutate
        from .runs import admitted_run

        v.uuid(application_id, 'application_id')
        v.obj(plan, 'plan', ('schema_version', 'delta_id', 'delta_revision', 'replaces_accepted_revision', 'intended_outcome', 'changes', 'base', 'external_requirements', 'checks'))
        v.version(plan)
        v.uuid(plan['delta_id'], 'delta_id')
        if type(plan['delta_revision']) is not int or plan['delta_revision'] < 1:
            v.invalid('delta_revision')
        replacement = plan['replaces_accepted_revision']
        if replacement is not None and (type(replacement) is not int or replacement < 1):
            v.invalid('replaces_accepted_revision')
        v.string(plan['intended_outcome'], 'intended_outcome')
        v.array(plan['changes'], 'changes')
        for change in plan['changes']:
            v.obj(change, 'change', ('kind', 'action', 'description', 'expected_sha256'))
            if change['kind'] not in ('file', 'entry', 'tool', 'workflow', 'instruction') or change['action'] not in ('create', 'edit', 'delete'):
                v.invalid('change')
            v.string(change['description'], 'change.description')
            if not any(isinstance(change.get(k), str) and change[k].strip() for k in ('target_ref', 'path', 'target_description')):
                v.invalid('change', 'requires a target')
            digest = change['expected_sha256']
            if change['action'] == 'create':
                if digest is not None:
                    v.invalid('change.expected_sha256')
            elif not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
                v.invalid('change.expected_sha256')
        v.obj(plan['base'], 'base', ('commit', 'resource_hashes'))
        if not isinstance(plan['base']['commit'], str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', plan['base']['commit']):
            v.invalid('base.commit')
        if not isinstance(plan['base']['resource_hashes'], dict):
            v.invalid('base.resource_hashes')
        for path, digest in plan['base']['resource_hashes'].items():
            v.string(path, 'base.resource_hashes.path')
            if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
                v.invalid('base.resource_hashes')
        v.array(plan['external_requirements'], 'external_requirements')
        seen_requirements = set()
        for requirement in plan['external_requirements']:
            v.obj(requirement, 'external_requirement', ('id', 'summary', 'required_for_completion'))
            v.string(requirement['id'], 'external_requirement.id')
            v.string(requirement['summary'], 'external_requirement.summary')
            if requirement['id'] in seen_requirements or type(requirement['required_for_completion']) is not bool:
                v.invalid('external_requirement')
            seen_requirements.add(requirement['id'])
        v.array(plan['checks'], 'checks')
        seen_checks = set()
        for check in plan['checks']:
            v.obj(check, 'check', ('id', 'phase', 'kind', 'arguments'))
            v.string(check['id'], 'check.id')
            if check['id'] in seen_checks or check['phase'] not in ('pre_merge', 'post_merge') or check['kind'] not in ('catalog', 'tool', 'command'):
                v.invalid('check')
            seen_checks.add(check['id'])
            if not isinstance(check['arguments'], dict):
                v.invalid('check.arguments')
            if check['kind'] == 'tool':
                v.string(check.get('tool_ref'), 'check.tool_ref')
            if check['kind'] == 'command':
                v.array(check.get('argv'), 'check.argv')
                if not check['argv']:
                    v.invalid('check.argv')
                for arg in check['argv']:
                    v.string(arg, 'check.argv')
        if type(expected_revision) is not int or expected_revision < 1:
            v.invalid('expected_revision')
        def write(conn):
            app = conn.execute('SELECT * FROM delta_applications WHERE id=?', (application_id,)).fetchone()
            if app is None:
                raise NotFoundError('application not found')
            if app['revision'] != expected_revision:
                raise AgenticError('application revision changed', code='stale_revision')
            if app['phase'] != 'planning' or not app['run_id'] or not admitted_run(conn, app['run_id'], 'application'):
                raise AgenticError('admitted application planning is required', code='owner_input_required')
            run = conn.execute('SELECT control FROM runs WHERE run_id=?', (app['run_id'],)).fetchone()
            if run is None or run['control'] != 'running':
                raise AgenticError('application workflow is not running', code='invalid_transition')
            delta = _delta(conn, app['delta_id'])
            if delta['decision'] != 'accepted' or plan['delta_id'] != app['delta_id'] or plan['delta_revision'] != delta['current_revision']:
                raise AgenticError('delta revision changed', code='stale_revision')
            _revision(conn, app['delta_id'], plan['delta_revision'])
            if plan['delta_revision'] != app['accepted_revision']:
                if replacement != app['accepted_revision']:
                    raise AgenticError('replaced acceptance is not current', code='stale_revision')
            elif replacement is not None:
                v.invalid('replaces_accepted_revision')
            plan_revision = (app['plan_revision'] or 0) + 1
            stored = {**plan, 'application_id': application_id, 'revision': plan_revision, 'created_at': self.ctx.now()}
            conn.execute('INSERT INTO overseer_plan_revisions VALUES (?, ?, ?)', (application_id, plan_revision, canonical(stored)))
            conn.execute('UPDATE delta_applications SET plan_revision=?, agreement_ref=NULL, revision=revision+1, updated_at=? WHERE id=?',
                         (plan_revision, self.ctx.now(), application_id))
            updated = _application(conn.execute('SELECT * FROM delta_applications WHERE id=?', (application_id,)).fetchone())
            return {'application': updated, 'plan': stored}
        return mutate(self.ctx, operation_id, 'applications.save_plan', {'application_id': application_id, 'plan': plan, 'expected_revision': expected_revision}, write)
