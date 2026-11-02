"""Run authority survives conversation changes; invocation identity remains provenance."""
import json

from qorqut_operations.contracts import DomainError
from qorqut_agentic.overseer.views import Views
from bazarlyk.management.identity import installation_owner
from bazarlyk.native_chat import store

WORKFLOWS = {'extraction': 'overseer/extract', 'review': 'overseer/review', 'application': 'overseer/apply'}


def owns_run(uow, admission, agent_id, *, kind=None):
    if not admission or admission.get('state') != 'linked' or admission.get('kind') not in WORKFLOWS:
        return False
    invocation = admission.get('invocation', {})
    return (kind in (None, admission['kind'])
            and invocation.get('executor') == {'actor_id': agent_id, 'actor_kind': 'agent'}
            and invocation.get('owner_principal_id') == installation_owner(uow))


def selected_session(runtime, ctx, uow):
    """Check the caller's actual native session and generation, never infer them from latest."""
    session = store.live_session(uow.connection, ctx.principal_id)
    if session is None or ctx.inbox_session_id != session['conversation_id']:
        raise DomainError('inbox_generation_stale', 'The current Overseer conversation is required to continue this run')
    owner = runtime.inbox.session_owner(ctx, ctx.inbox_session_id, uow)
    binding = json.loads(owner['binding_json'])
    registration = uow.connection.execute('SELECT workspace FROM machine_registrations WHERE id=?',
                                           (ctx.session_registration_id,)).fetchone()
    if (owner['desired_state'] != 'running' or binding.get('agent_id') != ctx.principal_id
            or binding.get('registration_id') != ctx.session_registration_id
            or binding.get('session_id') != session['conversation_id']
            or binding.get('runtime') != {'codex': 'codex', 'claude': 'claude-code'}.get(session['provider'])
            or registration is None or binding.get('workspace') != registration['workspace']):
        raise DomainError('inbox_generation_stale', 'The selected Overseer native binding is unavailable')
    return session


def require_run(runtime, ctx, uow, run_id, *, kind=None, active=False):
    """Authenticate continuation of the same admitted run, without re-admission or mutation."""
    if not isinstance(run_id, str) or not run_id:
        raise DomainError('owner_input_required', 'An admitted Overseer run is required')
    admission = Views(runtime.agentic_layer).get_admitted_run(run_id)
    if not owns_run(uow, admission, ctx.principal_id, kind=kind):
        raise DomainError('owner_input_required', 'The run is not admitted for this Overseer')
    session = selected_session(runtime, ctx, uow)
    workflow = runtime.agentic_layer.workflows.get(run_id)['run']
    if (workflow['workflow_ref'] != WORKFLOWS[admission['kind']]
            or active and workflow['state'] not in ('running', 'waiting_arguments')):
        raise DomainError('invalid_transition', 'The admitted Overseer workflow is unavailable or not running')
    return admission, session
