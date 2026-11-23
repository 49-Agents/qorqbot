"""What is left of the direct-instruction submissions.

The retired ``agentic_change_propose`` put one explicit owner Chat instruction to the owner as its own Signoff. The tool and its
companions were removed on 23 November 2026: an agent now files such a point for the Learning Applier's review with
``experience_observation_file``. Submissions made before that stay in the store, so what the Overseer's retention and
evidence access need to read them is kept here: the binding of a saved submission and the check of its owner source.
Nothing here creates or answers a submission.
"""

from uuid import NAMESPACE_URL, uuid5

from qorqut_operations.contracts import DomainError

from qorqut.agents.global_scope import active_role_holder
from qorqut.agents.roles import LEARNING_APPLIER_ROLE
from qorqut.management.identity import installation_owner
from .authority import current_manager


def _id(value):
    return str(uuid5(NAMESPACE_URL, 'qorqut:direct-instruction:' + value))


def _source_row(runtime, context, identity, uow):
    ctx = runtime.application.current_context(context, uow)
    owner = installation_owner(uow)
    if ctx.principal_kind == 'human':
        if ctx.principal_id != owner or ctx.source not in ('dashboard', 'worker'):
            raise DomainError('forbidden', 'The current owner is required')
        manager = None
    else:
        ctx, _ = current_manager(runtime, context, uow)
        manager = ctx.principal_id
    row = uow.connection.execute('''SELECT i.*, s.provider, s.conversation_id FROM native_inputs i
        JOIN native_sessions s ON s.id=i.session_id WHERE i.id=?''', (identity,)).fetchone()
    if (row is None or row['origin'] != 'owner' or row['input_type'] != 'chat'
            or row['principal_id'] != owner or row['sensitive'] or row['state'] != 'submitted'):
        raise DomainError('evidence_unavailable', 'A submitted, non-sensitive owner Chat instruction is required')
    overseer = active_role_holder(uow.connection, LEARNING_APPLIER_ROLE)
    if manager is not None and manager not in (row['agent_id'], overseer):
        raise DomainError('forbidden', 'The instruction belongs to another agent')
    return row


def authorize_source(runtime, context, ref):
    with runtime.database.read() as uow:
        row = _source_row(runtime, context, ref.get('record_id'), uow)
        expected = {'input_id': row['id'], 'agent_id': row['agent_id'], 'request_hash': row['request_hash'],
                    'provider': row['provider'], 'conversation_id': row['conversation_id']}
        if (ref.get('provider') != 'operations' or ref.get('kind') != 'owner_instruction'
                or ref.get('locator') != expected or ref.get('revision') != row['request_hash']
                or ref.get('actor_id') != row['principal_id'] or ref.get('actor_kind') != 'human'
                or ref.get('namespace') != {'kind': 'global', 'business_id': None}):
            raise DomainError('forbidden', 'The instruction reference does not match its canonical owner event')


def binding_for(saved):
    return {'schema_version': 1, 'id': _id(saved['id'] + ':review'), 'purpose': 'review',
        'run_id': 'direct-instruction:' + saved['id'],
        'targets': {'delta_id': _id(saved['id'] + ':delta'), 'delta_revision': 1,
                    'application_id': None, 'plan_revision': None},
        'owner_scope': {'kind': 'global', 'business_id': None},
        'request_key': 'direct-instruction:' + saved['id'], 'request_ref': None,
        'choice_actions': {'accept': 'accept', 'reject': 'reject', 'revise': 'revise', 'later': 'later'},
        'direct_submission_id': saved['id']}
