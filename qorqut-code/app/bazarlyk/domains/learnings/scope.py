"""Explicit workspace ownership for learning operations."""
from qorqut_operations.application.plans import PlanBuilder as BaseBuilder


def namespace(ctx, args=None):
    return 'business:' + ctx.business_id if ctx.business_id else 'global'


SCOPE = dict(scope_kinds={'business', 'global'}, global_capability='read_global_context', resolve_namespace=namespace)


class PlanBuilder(BaseBuilder):
    def __init__(self, ctx, *args, **kwargs):
        super().__init__(ctx, *args, namespace=namespace(ctx),
                         affected_scope={'kind': 'business', 'business_id': ctx.business_id} if ctx.business_id else {'kind': 'global'}, **kwargs)
