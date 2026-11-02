"""Portable Overseer state."""

from .records import Proposals
from .deltas import Deltas
from .support import ImmutableRecords, Inputs, Review
from .decisions import apply
from .applications import Applications
from .runs import start
from .coverage import Coverage


class Overseer:
    def __init__(self, ctx, workflows_factory):
        self._ctx = ctx
        self._workflows_factory = workflows_factory
        self.proposals = Proposals(ctx)
        self.deltas = Deltas(ctx)
        self.coverage = Coverage(ctx)
        self.materials = ImmutableRecords(ctx, 'overseer_materials', 'materials')
        self.inputs = Inputs(ctx)
        self.review = Review(ctx)
        self.applications = Applications(ctx)

    def apply_owner_response(self, binding_id, response, *, operation_id, gateway=None):
        return apply(self._ctx, binding_id, response, operation_id=operation_id, gateway=gateway)

    def start_run(self, kind, invocation, *, operation_id, gateway=None, arguments=None):
        return start(self._ctx, self._workflows_factory(), kind, invocation, operation_id=operation_id,
                     gateway=gateway, arguments=arguments)
