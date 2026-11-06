"""Delegated rejection only; never grants owner approval or response authority."""

from qorqut_agentic.behavior.tool_help import text as instruction_text
from dataclasses import replace

from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.conversations import SUMMARY
from qorqut_operations.storage.repositories import decode_row


class Rejection:
    def __init__(self, service):
        self.service = service

    def descriptors(self):
        return (OperationDescriptor('signoffs_reject', schema({
            'scope': {'enum': ['business', 'global']},
            'signoff_id': {'type': 'string', 'format': 'uuid'},
            'proposal_revision': {'type': 'integer', 'minimum': 1},
            'payload_hash': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'expected_status_revision': {'type': 'integer', 'minimum': 1},
            'feedback': {'type': 'string', 'maxLength': 8000}, 'summary': SUMMARY,
        }, ('signoff_id', 'proposal_revision', 'payload_hash', 'expected_status_revision')),
            supports_signoff=False, scope_kinds=frozenset({'business', 'global'}),
            global_capability='read_global_context',
            resolve_namespace=lambda c,a: 'global' if a.get('scope') == 'global' else 'business:' + (c.business_id or ''),
            description=instruction_text('rejection-ordubashy-only-reject-a-pending-action-signoff-with'),
            prepare=self.prepare, apply=self.apply),)

    def context(self, ctx, args, uow):
        if args.get('scope') == 'global':
            ctx = replace(ctx, business_id=None, review_business_id=None)
        ctx = self.service._context(ctx, uow)
        row = decode_row(uow.connection.execute('SELECT * FROM agent_profiles WHERE agent_id=?', (ctx.principal_id,)).fetchone())
        if (ctx.principal_kind != 'agent' or not row or row['status'] != 'active'
                or row['profile_json']['role']['id'] != 'ordubashy'
                or row['profile_json']['scope']['kind'] != 'ecosystem'):
            raise DomainError('forbidden', 'Delegated review requires active Ordubashy authority')
        return ctx

    def target(self, ctx, args, uow):
        row = self.service._row(ctx, args['signoff_id'], uow)
        if row['legacy_evidence_json'] is not None:
            raise DomainError('historical_record', 'Historical signoffs cannot receive new decisions')
        if row['category'] == 'input_request':
            raise DomainError('invalid_transition', 'Input requests require an owner answer')
        if (row['state'] != 'pending' or row['decision_json'] or row['superseded_by_id']
                or row['status_revision'] != args['expected_status_revision']
                or not self.service._binding_matches(row, args['proposal_revision'], args['payload_hash'])):
            raise DomainError('revision_conflict', 'Signoff changed; read it again before deciding')
        return row

    def prepare(self, ctx, args, options, uow):
        ctx = self.context(ctx, args, uow)
        row = self.target(ctx, args, uow)
        builder = PlanBuilder(ctx, 'signoffs_reject', args, 'Reject signoff',
            namespace='business:' + ctx.business_id if ctx.business_id else 'global',
            affected_scope={'kind': 'business', 'business_id': ctx.business_id} if ctx.business_id else {'kind': 'global'})
        builder.record('signoffs', row['id'], row, {}, action='reject')
        return builder.build()

    def apply(self, ctx, plan, uow):
        args = plain(plan.arguments)
        ctx = self.context(ctx, args, uow)
        row = self.target(ctx, args, uow)
        feedback = args.get('feedback', '').strip() or None
        message_id = self.service.notes.decision_message(ctx, row, feedback, uow, rejecting=True, summary=args.get('summary'))
        row = self.service._update(row, {'state': 'rejected', 'decision_json': {
            'kind': 'reject', 'by': ctx.principal_id, 'at': self.service.clock(), 'message_id': message_id}}, uow)
        return {'signoff': self.service._dto(row, uow)}
