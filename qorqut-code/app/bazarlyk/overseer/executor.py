"""Host configuration and task observation for the portable application executor."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from qorqut_operations.contracts import DomainError, plain

from .authority import current_overseer

_current_context = ContextVar('overseer_executor_context', default=None)


@contextmanager
def acting_as(context):
    token = _current_context.set(context)
    try:
        yield
    finally:
        _current_context.reset(token)


class ExternalTaskGateway:
    def __init__(self, runtime):
        self.runtime = runtime

    def resolve_task(self, task_ref):
        context = _current_context.get()
        if context is None or not isinstance(task_ref, dict):
            raise DomainError('forbidden', 'An authenticated executor context is required')
        namespace = task_ref.get('namespace')
        if (task_ref.get('provider') != 'operations' or task_ref.get('kind') != 'task'
                or not isinstance(namespace, dict) or set(namespace) != {'kind', 'business_id'}):
            raise DomainError('validation_error', 'A canonical Task reference is required')
        business = namespace['business_id']
        if not (namespace['kind'] == 'global' and business is None or namespace['kind'] == 'business' and isinstance(business, str)):
            raise DomainError('validation_error', 'Invalid Task namespace')
        with self.runtime.database.read() as uow:
            ctx, _ = current_overseer(self.runtime, context, uow, capability='manage_agentic_layer')
            if business and business not in ctx.allowed_business_ids:
                raise DomainError('forbidden', 'Task scope is unavailable')
        args = {'task_id': task_ref.get('id'), **({'scope': 'global'} if business is None else {})}
        options = {'business_id': business} if business else {}
        result = self.runtime.application.invoke(ctx, 'tasks_get', args, options)
        if result.status != 'ok':
            error = plain(result.error) if result.error else {'code': 'operation_unavailable', 'message': 'Task is unavailable'}
            raise DomainError(error['code'], error['message'], details=error.get('details'))
        task = plain(result.data)['task']
        if task['id'] != task_ref.get('id') or task['business_id'] != business:
            raise DomainError('evidence_unavailable', 'Task identity does not match the reference')
        # The ordinary Task reader resolves each typed reference under current authority.
        # Only a real discussion entry with a summary and visible references is an
        # outcome candidate; meaning relative to the plan remains agent judgment.
        summary, refs = None, []
        for note in reversed(task.get('notes', [])):
            note_refs = note.get('refs') or []
            if note.get('summary') and note_refs and all(isinstance(ref, dict) and not ref.get('unavailable') for ref in note_refs):
                summary = note['summary']
                refs = [{'provider': 'operations', 'kind': 'task_message', 'id': note['id'], 'revision': '1',
                         'namespace': namespace, 'task_id': task['id']}]
                break
        return {'reference': {'provider': 'operations', 'kind': 'task', 'id': task['id'],
                              'revision': str(task['revision']), 'namespace': namespace},
                'state': task['status'], 'result_refs': refs, 'completion_summary': summary,
                'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}


class SatisfactionGateway:
    def __init__(self, runtime):
        self.runtime = runtime

    def resolve_satisfaction_reference(self, reference, *, kind):
        context = _current_context.get()
        if context is None:
            raise DomainError('forbidden', 'An authenticated executor context is required')
        if kind == 'source':
            from .access import EvidenceAccess
            from .coverage import AnalysisCoverage
            from .evidence import EvidenceProvider
            from .provenance import content_hash
            with self.runtime.database.read() as uow:
                ctx, _ = current_overseer(self.runtime, context, uow, capability='read_agent_experience')
            provider = EvidenceProvider(self.runtime, ctx, self.runtime.paths.overseer_archive, EvidenceAccess(self.runtime, ctx),
                                        overseer_ids={ctx.principal_id}, coverage=AnalysisCoverage(self.runtime.agentic_layer))
            source = provider.get_source(reference)
            digest = content_hash(source['content'])
            if digest != reference.get('content_hash'):
                raise DomainError('evidence_unavailable', 'The exact source failed integrity verification')
            return {'reference': reference, 'content_hash': digest, 'outcome': None, 'checked_commit': None}
        if kind == 'check_receipt':
            import hashlib
            import json
            if (not isinstance(reference, dict) or set(reference) != {'provider', 'kind', 'application_id', 'check_run_id', 'check_id'}
                    or reference['provider'] != 'qorqut_agentic' or reference['kind'] != 'check_receipt'):
                raise DomainError('validation_error', 'A canonical check receipt reference is required')
            with self.runtime.database.read() as uow:
                ctx, _ = current_overseer(self.runtime, context, uow, capability='manage_agentic_layer')
            component = self.runtime.agentic_layer.overseer.applications
            application = component.get(reference['application_id'])
            self.runtime.overseer._check_delta(ctx, self.runtime.agentic_layer.overseer.deltas.get(application['delta_id']))
            method = getattr(component, 'get_check_receipt', None)
            if not callable(method):
                raise DomainError('operation_unavailable', 'Canonical check-receipt lookup is not installed')
            result = method(reference)
            record = result.get('record') if isinstance(result, dict) else None
            if (not isinstance(record, dict) or result.get('reference') != reference
                    or set(record) != {'schema_version', 'application_id', 'check_run_id', 'phase', 'plan_revision',
                                       'checked_commit', 'checked_tree', 'run_state', 'receipt'}
                    or record.get('schema_version') != 1
                    or record.get('application_id') != reference['application_id']
                    or record.get('check_run_id') != reference['check_run_id']
                    or not isinstance(record.get('receipt'), dict)
                    or set(record['receipt']) != {'check_id', 'kind', 'outcome', 'started_at', 'finished_at', 'exit_code',
                                                  'tool_receipt_ref', 'output_ref', 'diagnostic'}
                    or record['receipt'].get('check_id') != reference['check_id']):
                raise DomainError('evidence_unavailable', 'The exact check receipt is unavailable')
            raw = json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
            digest = hashlib.sha256(raw).hexdigest()
            if (result.get('content_hash') != digest or record.get('run_state') != 'passed'
                    or record['receipt'].get('outcome') != 'passed' or not record['receipt'].get('finished_at')
                    or not isinstance(record.get('checked_commit'), str) or not record['checked_commit']):
                raise DomainError('evidence_unavailable', 'The check receipt is not a finalized passed result')
            return {'reference': reference, 'content_hash': digest, 'outcome': 'passed',
                    'checked_commit': record['checked_commit']}
        raise DomainError('validation_error', 'Unsupported satisfaction reference kind')


def configure(runtime):
    component = runtime.agentic_layer.overseer.applications
    method = getattr(component, 'configure_runtime', None)
    if not callable(method):
        return False
    method(worktrees_root=runtime.paths.operations / '.agentic-worktrees',
           states_root=runtime.paths.runtime / 'agentic-worktrees',
           external_task_gateway=ExternalTaskGateway(runtime), satisfaction_gateway=SatisfactionGateway(runtime))
    return True


def satisfy_input_need(runtime, context, result):
    """Turn a durable executor InputNeed into the ordinary bound owner Signoff."""
    from uuid import NAMESPACE_URL, uuid5
    from .inputs import create_input_request

    need = result.get('required_input') if isinstance(result, dict) else None
    if need is None:
        return result
    app = result['application']
    if (need['application_id'] != app['id'] or need['application_revision'] != app['revision']
            or need['plan_revision'] != app['plan_revision'] or need['purpose'] not in ('clarify', 'failure')):
        raise DomainError('invalid_transition', 'Executor input does not match the current application')
    saved = runtime.agentic_layer.overseer.applications.get(app['id'])
    plan = saved.get('plan') or {}
    target_revision = plan.get('delta_revision', app['accepted_revision'])
    binding_id = str(uuid5(NAMESPACE_URL, 'qorqut:overseer-input:' + need['request_key']))
    if need['purpose'] == 'failure':
        failure = need.get('failure_evidence')
        if (not isinstance(failure, dict) or set(failure) != {'check_run_id', 'phase', 'uncertain'}
                or failure['phase'] not in ('pre_merge', 'post_merge') or type(failure['uncertain']) is not bool
                or not isinstance(failure['check_run_id'], str) or not failure['check_run_id']):
            raise DomainError('invalid_transition', 'The executor did not provide canonical failure evidence')
        check_run = result.get('check_run')
        if (not isinstance(check_run, dict) or check_run.get('id') != failure['check_run_id']
                or check_run.get('phase') != failure['phase'] or check_run.get('plan_revision') != need['plan_revision']
                or not isinstance(check_run.get('receipts'), list)):
            raise DomainError('evidence_unavailable', 'The exact failed check run must be available for presentation')
        actions = {'retry_checks': 'retry_checks', 'repair': 'repair', 'leave': 'leave', 'later': 'later'}
        if failure['phase'] == 'post_merge':
            actions['revert'] = 'revert'
        labels = {'retry_checks': ('Retry checks', 'Request a new check attempt.'), 'repair': ('Repair', 'Discuss a repair.'),
                  'revert': ('Revert', 'Discuss a reversion.'), 'leave': ('Leave', 'Leave the merged change as it is.'),
                  'later': ('Later', 'Decide later.')}
    else:
        actions = {'clarify': 'clarify', 'later': 'later'}
        labels = {'clarify': ('Clarify', 'Provide the requested direction.'), 'later': ('Later', 'Decide later.')}
    binding = {'schema_version': 1, 'id': binding_id, 'purpose': need['purpose'], 'run_id': app['run_id'],
               'targets': {'delta_id': app['delta_id'], 'delta_revision': target_revision, 'application_id': app['id'],
                           'application_revision': app['revision'], 'plan_revision': app['plan_revision']},
               'owner_scope': {'kind': 'global', 'business_id': None}, 'request_key': need['request_key'],
               'request_ref': None, 'choice_actions': actions, 'evidence_refs': need.get('evidence_refs', [])}
    if need['purpose'] == 'failure':
        binding['failure_evidence'] = failure
        binding['failure_check_run'] = check_run
    warning = (' The earlier process may have run without a known result; a new attempt could repeat its effects.'
               if need['purpose'] == 'failure' and failure['uncertain'] else '')
    form = {'title': 'Application needs your input', 'explanation': 'The application needs direction: ' + need['reason'],
            'kind': 'question', 'selection_mode': 'single', 'selection_required': False,
            'choices': [{'id': key, 'label': labels[key][0], 'explanation': labels[key][1]} for key in actions]}
    form['explanation'] += warning
    create_input_request(runtime, context, binding, form, 'executor-input:' + binding_id)
    component = runtime.agentic_layer.overseer.applications
    attach = getattr(component, 'attach_input', None)
    if not callable(attach):
        raise DomainError('operation_unavailable', 'The application input attachment is not installed')
    attached = attach(app['id'], binding_id, expected_revision=app['revision'], operation_id='executor-attach:' + binding_id)
    return {**result, 'application': attached['application']}


def _approved_application(runtime, context, application_id, expected_revision, requirement_id=None):
    from bazarlyk.native_chat import store
    from qorqut_agentic.overseer.views import Views
    with runtime.database.read() as uow:
        ctx, _ = current_overseer(runtime, context, uow, capability='manage_agentic_layer')
        session = store.latest_session(uow.connection, ctx.principal_id)
    app = runtime.agentic_layer.overseer.applications.get(application_id)
    if app['revision'] != expected_revision:
        with runtime.database.read() as uow:
            prior = (uow.connection.execute('SELECT 1 FROM overseer_development_tasks WHERE application_id=? AND plan_revision=? AND requirement_id=?',
                     (application_id, app['plan_revision'], requirement_id)).fetchone() if requirement_id is not None else None)
        if prior is None:
            raise DomainError('stale_revision', 'Application revision changed')
    admission = Views(runtime.agentic_layer).get_admitted_run(app['run_id']) if app['run_id'] else None
    invocation = admission and admission.get('invocation', {})
    if (session is None or not admission or admission['kind'] != 'application'
            or invocation.get('conversation_id') != session['conversation_id']
            or invocation.get('executor') != {'actor_id': ctx.principal_id, 'actor_kind': 'agent'}
            or app['phase'] not in ('planning', 'working', 'prepared') or app['merge'] is not None
            or not app['agreement_ref'] or app['plan'] is None):
        raise DomainError('owner_input_required', 'A currently admitted agreed application is required')
    workflow = runtime.agentic_layer.workflows.get(app['run_id'])['run']
    if workflow['state'] not in ('running', 'waiting_arguments'):
        raise DomainError('invalid_transition', 'The application workflow is not running')
    delta = runtime.agentic_layer.overseer.deltas.get(app['delta_id'])
    if delta['decision'] != 'accepted' or delta['accepted_revision'] != app['accepted_revision']:
        raise DomainError('stale_revision', 'The accepted application revision changed')
    if requirement_id is not None and not any(r['id'] == requirement_id for r in app['plan']['external_requirements']):
        raise DomainError('invalid_transition', 'The agreed plan has no such external requirement')
    return ctx, app


def _association(runtime, context, app, requirement_id, task_ref):
    import hashlib
    import json
    key = hashlib.sha256(json.dumps([app['id'], app['plan_revision'], requirement_id], separators=(',', ':')).encode()).hexdigest()
    request_key = 'overseer-task:' + key
    runtime.require_authority()
    with runtime.database.transaction(operation='overseer.task-association') as uow:
        runtime.application.ensure_writes_open(uow)
        ctx, _ = current_overseer(runtime, context, uow, capability='manage_agentic_layer')
        business = task_ref['namespace']['business_id']
        if business and business not in ctx.allowed_business_ids:
            raise DomainError('forbidden', 'Task scope is unavailable')
        from .access import EvidenceAccess
        EvidenceAccess(runtime, ctx)._read(ctx, 'tasks_get', {'task_id': task_ref['id']}, business, uow)
        row = uow.connection.execute('SELECT * FROM overseer_development_tasks WHERE request_key=?', (request_key,)).fetchone()
        if row is None:
            uow.connection.execute('INSERT INTO overseer_development_tasks VALUES (?, ?, ?, ?, ?, ?)',
                (request_key, app['id'], app['plan_revision'], requirement_id, task_ref['id'],
                 json.dumps(task_ref, sort_keys=True, separators=(',', ':'))))
        elif (row['application_id'] != app['id'] or row['plan_revision'] != app['plan_revision']
              or row['requirement_id'] != requirement_id or json.loads(row['task_ref_json']) != task_ref):
            raise DomainError('operation_conflict', 'The requirement already names a different Task')
    return request_key


def development_task(runtime, context, name, args):
    import hashlib
    import json
    from uuid import NAMESPACE_URL, uuid5
    component = runtime.agentic_layer.overseer.applications
    method_name = 'reconcile_external_tasks' if name.endswith('reconcile') else 'link_external_task'
    method = getattr(component, method_name, None)
    if not callable(method):
        raise DomainError('operation_unavailable', 'The external-task executor is not installed')
    if name.endswith('reconcile'):
        with runtime.database.read() as uow:
            current_overseer(runtime, context, uow, capability='manage_agentic_layer')
        with acting_as(context):
            result = method(args['application_id'], expected_revision=args['expected_revision'], operation_id=args['operation_id'])
            return satisfy_input_need(runtime, context, result)
    ctx, app = _approved_application(runtime, context, args['application_id'], args['expected_revision'], args.get('requirement_id'))
    with acting_as(context):
        if name.endswith('create'):
            task = dict(args['task'])
            digest = hashlib.sha256(json.dumps([app['id'], app['plan_revision'], args['requirement_id']], separators=(',', ':')).encode()).hexdigest()
            task_id = str(uuid5(NAMESPACE_URL, 'qorqut:overseer-task:' + digest))
            if 'id' in task and task['id'] != task_id:
                raise DomainError('validation_error', 'Task identity is fixed by the requirement')
            task['id'] = task_id
            business = task.pop('business_id', None)
            if business and business not in ctx.allowed_business_ids:
                raise DomainError('forbidden', 'Task scope is unavailable')
            request_key = 'overseer-task:' + digest
            fingerprint = hashlib.sha256(json.dumps({'task': task, 'business_id': business}, ensure_ascii=False,
                sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
            runtime.require_authority()
            with runtime.database.transaction(operation='overseer.task-create-reserve') as uow:
                runtime.application.ensure_writes_open(uow)
                current_overseer(runtime, context, uow, capability='manage_agentic_layer')
                prior = uow.connection.execute('SELECT fingerprint FROM overseer_development_task_requests WHERE request_key=?',
                                               (request_key,)).fetchone()
                if prior is not None and prior['fingerprint'] != fingerprint:
                    raise DomainError('operation_conflict', 'The requirement already has a different Task creation request')
                association = uow.connection.execute('SELECT task_ref_json FROM overseer_development_tasks WHERE request_key=?',
                                                     (request_key,)).fetchone()
                if association is not None:
                    saved = json.loads(association['task_ref_json'])
                    if saved['id'] != task_id or saved['namespace']['business_id'] != business:
                        raise DomainError('operation_conflict', 'The requirement is already linked to a different Task')
                if prior is None:
                    uow.connection.execute('INSERT INTO overseer_development_task_requests VALUES (?, ?)', (request_key, fingerprint))
            options = {'idempotency_key': 'overseer-task:' + digest, **({'business_id': business} if business else {})}
            result = runtime.application.invoke(ctx, 'tasks_create', task, options)
            if result.status != 'ok':
                error = plain(result.error) if result.error else {'code': 'operation_unavailable', 'message': 'Task creation was not applied'}
                raise DomainError(error['code'], error['message'], details=error.get('details'))
            created = plain(result.data)['task']
            namespace = {'kind': 'business', 'business_id': created['business_id']} if created['business_id'] else {'kind': 'global', 'business_id': None}
            task_ref = {'provider': 'operations', 'kind': 'task', 'id': created['id'], 'revision': str(created['revision']), 'namespace': namespace}
        else:
            created = None
            task_ref = args['task_ref']
        gateway = ExternalTaskGateway(runtime)
        observed = gateway.resolve_task(task_ref)
        with runtime.database.read() as uow:
            previous = uow.connection.execute('SELECT task_ref_json FROM overseer_development_tasks WHERE application_id=? AND plan_revision=? AND requirement_id=?',
                                              (app['id'], app['plan_revision'], args['requirement_id'])).fetchone()
        same_saved = previous is not None and json.loads(previous['task_ref_json']) == task_ref
        if observed['reference'] != task_ref and not same_saved:
            raise DomainError('stale_revision', 'The Task reference is not current')
        request_key = _association(runtime, context, app, args['requirement_id'], task_ref)
        linked = method(app['id'], args['requirement_id'], task_ref, expected_revision=args['expected_revision'], operation_id=args['operation_id'])
        result_app = linked.get('application') if isinstance(linked, dict) else None
        links = result_app.get('external_tasks', []) if isinstance(result_app, dict) else []
        if not any(item.get('requirement_id') == args['requirement_id'] and item.get('task_ref') == task_ref
                   and item.get('linked_plan_revision') == app['plan_revision'] for item in links):
            raise DomainError('evidence_unavailable', 'The application did not confirm the exact Task link')
        from qorqut_operations.contracts import utc_now
        runtime.require_authority()
        with runtime.database.transaction(operation='overseer.task-link-observation') as uow:
            runtime.application.ensure_writes_open(uow)
            current_overseer(runtime, context, uow, capability='manage_agentic_layer')
            uow.connection.execute('INSERT OR IGNORE INTO overseer_development_task_links VALUES (?, ?, ?)',
                                   (request_key, args['operation_id'], utc_now()))
            uow.connection.execute('''INSERT OR IGNORE INTO overseer_retention_events
                (event_key,kind,source_json,cursor_json,material_ref_json,state,updated_at)
                VALUES (?, 'task', ?, NULL, NULL, 'pending', ?)''',
                ('task:' + request_key.removeprefix('overseer-task:'),
                 json.dumps({'request_key': request_key}, sort_keys=True, separators=(',', ':')), utc_now()))
        linked = satisfy_input_need(runtime, context, linked)
        return {'task': created, 'application_result': linked} if created is not None else linked
