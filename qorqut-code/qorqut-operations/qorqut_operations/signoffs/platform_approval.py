"""Narrow Ordubashy review of another agent's exact platform proposal.

This is the shared exact-decision orchestration of the deployed Reddit/Email approvals. A concrete approval is a
subclass that names its ``operation``, ``title``, ``description`` and the ``allowed`` ``(plugin_id, tool)``
pairs, and is registered by the host (the plugin IDs, payload interpretation and registrations stay host-owned).
The scope, binding, proposer and application checks below are the engine's; the host's ``PlatformActions.view``
only supplies the submission payload being compared against ``allowed``."""
from qorqut_operations.application.plans import PlanBuilder, schema
from qorqut_operations.application.registry import OperationDescriptor
from qorqut_operations.contracts import DomainError, plain
from qorqut_operations.storage.receipts import ApplyReceipts
from .rejection import Rejection
from .service import PLATFORM_UNAVAILABLE


class PlatformApproval(Rejection):
    operation: str
    title: str
    description: str
    allowed: frozenset

    def descriptors(self):
        return (OperationDescriptor(self.operation, schema({
            'signoff_id': {'type': 'string', 'format': 'uuid'},
            'proposal_revision': {'type': 'integer', 'minimum': 1},
            'payload_hash': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'expected_status_revision': {'type': 'integer', 'minimum': 1},
        }, ('signoff_id', 'proposal_revision', 'payload_hash', 'expected_status_revision')),
            supports_signoff=False,
            description=self.description,
            prepare=self.prepare, apply=self.apply),)

    def target(self, ctx, args, uow):
        row = super().target(ctx, args, uow)
        if row['review_business_id'] is None or row['category'] != 'platform_action':
            raise DomainError('forbidden', 'Delegated approval requires an eligible business platform action')
        submission = self.service.ports.require('platform', PLATFORM_UNAVAILABLE).view(row, uow)['submission']
        if ((submission['plugin_id'], submission['tool']) not in self.allowed
                or not row['proposer_agent_id'] or row['proposer_agent_id'] == ctx.principal_id):
            raise DomainError('forbidden', 'Ordubashy may approve only another agent’s eligible platform action')
        return row

    def prepare(self, ctx, args, options, uow):
        ctx = self.context(ctx, args, uow)
        row = self.target(ctx, args, uow)
        self.service._validate(row, uow)
        builder = PlanBuilder(ctx, self.operation, args, self.title,
            namespace='business:' + ctx.business_id,
            affected_scope={'kind': 'business', 'business_id': ctx.business_id})
        builder.record('signoffs', row['id'], row, {}, action='approve')
        return builder.build()

    def apply(self, ctx, plan, uow):
        args = plain(plan.arguments)
        ctx = self.context(ctx, args, uow)
        row = self.target(ctx, args, uow)
        _, proposal = self.service._validate(row, uow)
        ApplyReceipts(uow).create(proposal, receipt_id=row['id'], signoff_id=row['id'])
        row = self.service._update(row, {'state': 'approved', 'error_json': None,
            'decision_json': {'kind': 'approve', 'by': ctx.principal_id,
                              'at': self.service.clock(), 'feedback': None}}, uow)
        self.service._wake.set()
        return {'signoff': self.service._dto(row, uow)}
